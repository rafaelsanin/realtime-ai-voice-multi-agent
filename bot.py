"""Bella Vista voice bot entrypoint.

Commit 6: wraps pipeline/worker construction in BellaVistaBot, taking a
Settings object (settings.py) instead of reading os.environ ad hoc -- the
class half of this commit's dependency injection (Settings is the other).

Commit 9: this process is now a host, not the call. It builds the
dependencies a call needs (config, reservations backend, LiveKit API client)
once, then hands them to a `CallDispatcher` that runs one `CallSession` per
inbound call -- see dispatcher.py / session.py.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import uuid
from typing import TYPE_CHECKING

from dotenv import load_dotenv
from loguru import logger

from settings import LogFormat, Settings, load_settings

if TYPE_CHECKING:
    from dispatcher import CallDispatcher

load_dotenv()


def configure_logging(settings: Settings) -> None:
    """Human-readable console logs locally, structured JSON on a cloud deployment.

    A container platform's log collector (for a log drain to Datadog,
    CloudWatch, etc.) just captures stdout as opaque text by default -- JSON
    lines let it parse fields like `event`/`room`/`participant` instead of
    grepping free text.
    """
    logger.remove()
    if settings.effective_log_format is LogFormat.JSON:
        logger.add(sys.stdout, serialize=True, level="INFO")
    else:
        logger.add(sys.stdout, level="INFO")


class BellaVistaBot:
    """Always-on host: builds shared dependencies and runs the call dispatcher."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def run(self) -> None:
        # Imported lazily so a missing/invalid Settings can fail fast without
        # pulling in the (heavier) pipeline dependencies first.
        from livekit import api as lk_api
        from supabase import create_async_client

        from dispatcher import CallDispatcher
        from reservations import SupabaseReservationsRepository

        settings = self._settings

        supabase_client = await create_async_client(settings.supabase_url, settings.supabase_key)
        repository = SupabaseReservationsRepository(supabase_client)

        # Shared by every call: used to hang up a caller (end_conversation)
        # and to discover/tear down each call's room.
        livekit_api = lk_api.LiveKitAPI(
            settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret
        )

        dispatcher = CallDispatcher(
            settings=settings, livekit_api=livekit_api, repository=repository
        )

        if settings.is_local:
            self._log_test_link()

        self._install_signal_handlers(dispatcher)
        try:
            await dispatcher.run()
        finally:
            await livekit_api.aclose()

    def _install_signal_handlers(self, dispatcher: "CallDispatcher") -> None:
        """Stop the dispatcher (draining calls) on SIGINT/SIGTERM.

        Signals are handled here rather than in each call's `WorkerRunner`,
        which would otherwise install one handler per concurrent call and let
        whichever registered last decide what shutdown means.
        """
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, dispatcher.stop)

    def _log_test_link(self) -> None:
        """Print a ready-to-click meet.livekit.io link for a browser test call.

        Joining that link creates the room, which the dispatcher then picks up
        exactly like an inbound phone call -- the room just isn't named by a
        SIP dispatch rule. Not useful once deployed (nobody reads container
        logs to find a link to a call already in progress), so local only.
        """
        from urllib.parse import urlencode

        from pipecat.runner.livekit import generate_token

        settings = self._settings
        room_name = f"{settings.livekit_room_prefix}_local_{uuid.uuid4().hex[:6]}"
        user_token = generate_token(
            room_name, "User", settings.livekit_api_key, settings.livekit_api_secret
        )
        query = urlencode({"liveKitUrl": settings.livekit_url, "token": user_token})
        logger.info(f"Join to test: https://meet.livekit.io/custom?{query}")


def main() -> int:
    settings = load_settings()
    if settings is None:
        return 1
    configure_logging(settings)
    asyncio.run(BellaVistaBot(settings).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
