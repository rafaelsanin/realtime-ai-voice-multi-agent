"""Host and Booking agent workers (commit 4): multi-agent handoff over the bus.

Conversation context lives once -- in the main pipeline's
LLMContextAggregatorPair (bot.py) -- not per worker. Each worker here just
wraps an LLM persona plus its own tools and exchanges frames with that shared
context over the WorkerBus (bridged=()). Only one worker is "active" at a
time; activate_worker() swaps which one processes the shared context, so
handoff carries no separate context to sync.

Commit 6: typed, and BookingWorker now takes a ReservationsRepository
constructor argument (Dependency Inversion) instead of reaching into
`params.app_resources` for a raw Supabase client.

Fix: this is an always-on line, not a one-shot script -- end_conversation
used to delete the whole room and end the WorkerRunner, which correctly hung
up the call but also killed the bot's own connection and process. It now
only removes the caller's SIP participant, tracked via CallState, so the
process (and the room) survive to answer the next call.
"""

from __future__ import annotations

from typing import Any

from livekit import api as lk_api
from loguru import logger
from pipecat.frames.frames import LLMRunFrame, TTSUpdateSettingsFrame
from pipecat.pipeline.worker import PipelineWorker
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.llm_service import FunctionCallParams, LLMService
from pipecat.workers.llm import LLMWorker, tool

from call_state import CallState
import db
from reservations import ReservationsRepository


class _HandoffWorker(LLMWorker):
    """Shared handoff/end-conversation plumbing for Host and Booking."""

    def __init__(
        self,
        name: str,
        *,
        llm: LLMService[Any],
        main_worker: PipelineWorker,
        voice_id: str,
        room_name: str,
        livekit_api: lk_api.LiveKitAPI,
        call_state: CallState,
        active: bool = False,
    ) -> None:
        super().__init__(name, llm=llm, active=active, bridged=())
        self._main_worker = main_worker
        self._voice_id = voice_id
        self._room_name = room_name
        self._livekit_api = livekit_api
        self._call_state = call_state
        # activate_worker() is deferred until the calling tool call fully
        # returns (LLMWorker runs it via _after_tool_calls), so nudging the
        # newly active worker has to happen from *its own* on_activated, not
        # right after calling activate_worker() -- doing it there would fire
        # before the swap actually takes effect and reach the wrong worker.
        # A worker that starts active is kicked off explicitly elsewhere
        # (bot.py's on_first_participant_joined); only nudge on later
        # handoffs, but do switch the voice even on that first activation.
        self._skip_next_nudge = active

    async def on_activated(self, args: dict | None) -> None:
        await super().on_activated(args)
        # There's one shared TTS instance in the main pipeline (not
        # per-worker), so give each agent a distinct voice by pushing a
        # settings update through it on activation -- this frame travels via
        # this worker's own bus edge, same path as its spoken output.
        await self.queue_frame(
            TTSUpdateSettingsFrame(delta=CartesiaTTSService.Settings(voice=self._voice_id))
        )
        if self._skip_next_nudge:
            self._skip_next_nudge = False
            return
        await self._main_worker.queue_frames([LLMRunFrame()])

    async def _handoff_to(self, target: str) -> None:
        logger.bind(event="handoff", room=self._room_name, source=self.name, target=target).info(
            "agent handoff"
        )
        await self.activate_worker(target, deactivate_self=True)
        self._call_state.active_worker = target

    @tool
    async def end_conversation(self, params: FunctionCallParams) -> None:
        """End the call. Call this once the caller is done and says goodbye."""
        logger.bind(event="call_ended", room=self._room_name, reason="caller_goodbye").info(
            "ending call"
        )
        await params.result_callback({"ended": True})
        # Stopping the pipeline alone doesn't hang up a PSTN call -- the SIP
        # participant stays in the room (and Twilio keeps billing) until it's
        # explicitly removed. Remove just the caller (not the whole room, and
        # without ending the WorkerRunner) so the bot stays up for the next
        # call -- this is a persistent line, not a one-shot script.
        identity = self._call_state.participant_identity
        if identity:
            await self._livekit_api.room.remove_participant(
                lk_api.RoomParticipantIdentity(room=self._room_name, identity=identity)
            )


class HostWorker(_HandoffWorker):
    """Greets callers and handles small talk/FAQ. Hands off to Booking for reservations."""

    @tool
    async def transfer_to_booking(self, params: FunctionCallParams) -> None:
        """Hand off the conversation to the Booking agent.

        Call this as soon as the caller wants to check availability or book a
        table.
        """
        await params.result_callback({"transferred_to": "booking"})
        await self._handoff_to("booking")


class BookingWorker(_HandoffWorker):
    """Owns check_availability/book_table. Hands back to Host for anything else."""

    def __init__(self, *args: Any, repository: ReservationsRepository, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._repository = repository

    @tool
    async def check_availability(
        self, params: FunctionCallParams, date: str, time: str, party_size: int
    ) -> None:
        """Check whether a table is available for a given date, time, and party size.

        Args:
            date: Reservation date, formatted as YYYY-MM-DD.
            time: Reservation time, formatted as HH:MM (24-hour).
            party_size: Number of guests.
        """
        result = await db.check_availability(
            self._repository, date=date, time=time, party_size=party_size
        )
        await params.result_callback(result.model_dump(mode="json"))

    @tool
    async def book_table(
        self, params: FunctionCallParams, name: str, date: str, time: str, party_size: int
    ) -> None:
        """Book a table for a caller, if the slot still has capacity.

        Args:
            name: Name to book the reservation under.
            date: Reservation date, formatted as YYYY-MM-DD.
            time: Reservation time, formatted as HH:MM (24-hour).
            party_size: Number of guests.
        """
        result = await db.book_table(
            self._repository, name=name, date=date, time=time, party_size=party_size
        )
        # mode="json" so the reservation UUID reaches the LLM as a string.
        await params.result_callback(result.model_dump(mode="json"))

    @tool
    async def transfer_to_host(self, params: FunctionCallParams) -> None:
        """Hand off back to the Host agent for anything not booking-related.

        Call this once the caller's booking-related request is fully handled,
        or if they ask about something unrelated to reservations.
        """
        await params.result_callback({"transferred_to": "host"})
        await self._handoff_to("host")
