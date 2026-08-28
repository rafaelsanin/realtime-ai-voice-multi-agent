"""Per-call dispatch (commit 9): one `CallSession` per inbound call.

Until now every call was pinned to one fixed room the bot sat in forever, so
a second caller would have joined the first caller's conversation -- same
pipeline, same transcript, same voice. The SIP dispatch rule now gives each
call its own room (`dispatch-rule.json`), and this dispatcher notices those
rooms and starts an isolated session for each.

Discovery is a poll of `ListRooms` rather than a LiveKit webhook: a webhook
would mean exposing an HTTP endpoint (and verifying signatures) on what is
otherwise a worker with no inbound network surface. The cost is up to one
poll interval of extra answer latency.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass

from livekit import api as lk_api
from loguru import logger

from reservations import ReservationsRepository
from session import CallSession
from settings import Settings

POLL_INTERVAL_SECONDS = 1.0

# How long to wait for in-flight calls to wind down on shutdown before
# dropping them. Fly sends SIGTERM and then SIGKILL, so this can't be long.
SHUTDOWN_GRACE_SECONDS = 10.0


@dataclass
class _ActiveCall:
    """A running session and the task driving it."""

    session: CallSession
    task: asyncio.Task[None]
    started_at: float


class CallDispatcher:
    """Watches LiveKit for new call rooms and runs one session per room."""

    def __init__(
        self,
        *,
        settings: Settings,
        livekit_api: lk_api.LiveKitAPI,
        repository: ReservationsRepository,
    ) -> None:
        self._settings = settings
        self._livekit_api = livekit_api
        self._repository = repository
        self._calls: dict[str, _ActiveCall] = {}
        # Rooms we've already logged as over capacity, so a caller waiting for
        # a free slot doesn't emit one log line per poll.
        self._deferred: set[str] = set()
        self._stopping = asyncio.Event()

    @property
    def active_calls(self) -> int:
        """How many calls are in progress right now."""
        return len(self._calls)

    def stop(self) -> None:
        """Ask the dispatcher to stop accepting calls and wind down (signal-safe)."""
        self._stopping.set()

    async def run(self) -> None:
        """Poll for new call rooms until stopped."""
        logger.bind(
            event="dispatcher_started",
            room_prefix=self._settings.livekit_room_prefix,
            max_concurrent_calls=self._settings.max_concurrent_calls,
        ).info("dispatcher started")
        try:
            while not self._stopping.is_set():
                try:
                    await self._dispatch_new_rooms()
                except Exception as exc:
                    # A failed poll is transient (network, LiveKit hiccup);
                    # keep the line up and try again next tick.
                    logger.bind(event="dispatch_poll_failed", error=str(exc)).warning(
                        "room poll failed"
                    )
                await self._wait_for_next_poll()
        finally:
            await self._end_active_calls()
            logger.bind(event="dispatcher_stopped").info("dispatcher stopped")

    async def _wait_for_next_poll(self) -> None:
        """Sleep one poll interval, or wake immediately once stopping."""
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=POLL_INTERVAL_SECONDS)

    async def _dispatch_new_rooms(self) -> None:
        """Start a session for every call room that doesn't have one yet."""
        response = await self._livekit_api.room.list_rooms(lk_api.ListRoomsRequest())
        prefix = self._settings.livekit_room_prefix
        live_rooms = {room.name for room in response.rooms}
        self._deferred &= live_rooms

        for room in response.rooms:
            if not room.name.startswith(prefix) or room.name in self._calls:
                continue
            # The room exists before anyone is in it when a call is still
            # being set up; wait for the caller so a session isn't started
            # against an empty room. Our own agent isn't in it yet, so every
            # participant here is a caller.
            if room.num_participants == 0:
                continue
            if len(self._calls) >= self._settings.max_concurrent_calls:
                self._defer(room.name)
                continue
            self._start_call(room.name)

    def _defer(self, room_name: str) -> None:
        """Note (once) that a call is waiting for a free slot."""
        if room_name in self._deferred:
            return
        self._deferred.add(room_name)
        logger.bind(
            event="call_deferred",
            room=room_name,
            active_calls=len(self._calls),
            max_concurrent_calls=self._settings.max_concurrent_calls,
        ).warning("at capacity; call waiting for a free slot")

    def _start_call(self, room_name: str) -> None:
        """Spawn a session for `room_name` and track it."""
        session = CallSession(
            settings=self._settings,
            room_name=room_name,
            livekit_api=self._livekit_api,
            repository=self._repository,
        )
        task = asyncio.create_task(self._run_call(room_name), name=f"call:{room_name}")
        self._calls[room_name] = _ActiveCall(
            session=session, task=task, started_at=time.monotonic()
        )
        self._deferred.discard(room_name)
        logger.bind(
            event="call_dispatched", room=room_name, active_calls=len(self._calls)
        ).info("call dispatched")

    async def _run_call(self, room_name: str) -> None:
        """Drive one session to completion, then clean up its room."""
        call = self._calls[room_name]
        try:
            await call.session.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.opt(exception=True).bind(event="call_failed", room=room_name).error(
                "call session failed"
            )
        finally:
            self._calls.pop(room_name, None)
            # Deleting the room hangs up any leg still connected (a SIP
            # caller Twilio is still billing) and keeps the next poll from
            # seeing this room as a new call.
            await self._delete_room(room_name)
            logger.bind(
                event="session_ended",
                room=room_name,
                duration_seconds=round(time.monotonic() - call.started_at, 1),
                active_calls=len(self._calls),
            ).info("call session ended")

    async def _delete_room(self, room_name: str) -> None:
        """Delete a finished call's room, ignoring one already gone."""
        try:
            await self._livekit_api.room.delete_room(lk_api.DeleteRoomRequest(room=room_name))
        except Exception as exc:
            logger.bind(event="room_delete_failed", room=room_name, error=str(exc)).debug(
                "could not delete room"
            )

    async def _end_active_calls(self) -> None:
        """Wind down every in-flight call on shutdown."""
        active = list(self._calls.values())
        if not active:
            return
        logger.bind(event="dispatcher_draining", active_calls=len(active)).info(
            "ending active calls"
        )
        for call in active:
            await call.session.stop()
        tasks = [call.task for call in active]
        _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE_SECONDS)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
