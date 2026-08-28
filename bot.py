"""Bella Vista voice bot entrypoint.

Commit 6: wraps pipeline/worker construction in BellaVistaBot, taking a
Settings object (settings.py) instead of reading os.environ ad hoc -- the
class half of this commit's dependency injection (Settings is the other).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from typing import TYPE_CHECKING

from dotenv import load_dotenv
from loguru import logger

from settings import LogFormat, Settings, load_settings

if TYPE_CHECKING:
    from livekit import api as lk_api
    from pipecat.pipeline.worker import PipelineWorker

    from call_state import CallState
    from reservations import ReservationsRepository
    from workers import BookingWorker, HostWorker

load_dotenv()

HOST_SYSTEM_PROMPT = (
    "You are the host at Bella Vista, an Italian restaurant serving "
    "wood-fired Neapolitan pizza and handmade pasta. Located at 12 Market "
    "Street, open Tuesday-Sunday 5pm-10pm (closed Mondays). Today's date is "
    "{today}. Answer callers' questions about the restaurant briefly and "
    "warmly. As soon as the caller wants to check availability or book a "
    "table, call transfer_to_booking -- don't try to handle that yourself. "
    "Call end_conversation once the caller is done and says goodbye. Never "
    "say you're about to do something (like transferring the call) without "
    "calling the tool in that same reply -- don't leave the caller waiting "
    "in silence. Your responses will be spoken aloud, so avoid emojis, "
    "bullet points, or other formatting that can't be spoken."
)

BOOKING_SYSTEM_PROMPT = (
    "You are the booking specialist at Bella Vista, an Italian restaurant. "
    "Today's date is {today}. As soon as you have a date, time, and party "
    "size, call check_availability -- in that same reply, not a promise to "
    "do it. Never say things like 'one moment' or 'let me check' without "
    "calling the tool right then in the same turn -- don't leave the "
    "caller waiting in silence. Call book_table once the caller confirms "
    "they want the reservation. If the caller asks about anything not "
    "related to checking availability or booking a table, call "
    "transfer_to_host. Call end_conversation once the caller is done and "
    "says goodbye. Your responses will be spoken aloud, so avoid emojis, "
    "bullet points, or other formatting that can't be spoken."
)

# Stock Cartesia voices -- distinct female voices per agent so a handoff is
# audible, not just inferred from what's being said. Both general-american
# accent (not the previous British Booking voice, which sounded odd and less
# intelligible over compressed phone audio) for clarity on a telephony call.
# Overridable via env.
DEFAULT_HOST_VOICE_ID = "f039066f-cdb7-45ed-b51d-1034ae2f04a0"  # Cindy Baker - Receptionist
DEFAULT_BOOKING_VOICE_ID = "c894559e-d529-4d70-a6fb-3330ecf7ef6b"  # Iris - Friendly Specialist


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
    """Builds and runs the Host/Booking pipeline for one call session."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def run(self) -> None:
        # Imported lazily so a missing/invalid Settings can fail fast without
        # pulling in the (heavier) pipeline dependencies first.
        from urllib.parse import urlencode

        from livekit import api as lk_api
        from pipecat.audio.vad.silero import SileroVADAnalyzer
        from pipecat.bus import BusBridgeProcessor
        from pipecat.frames.frames import LLMRunFrame
        from pipecat.observers.loggers.metrics_log_observer import MetricsLogObserver
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.worker import PipelineParams, PipelineWorker
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMContextAggregatorPair,
            LLMUserAggregatorParams,
        )
        from pipecat.runner.livekit import configure, generate_token, livekit_credentials
        from pipecat.services.cartesia.tts import CartesiaTTSService
        from pipecat.services.deepgram.stt import DeepgramSTTService
        from pipecat.transports.livekit.transport import LiveKitParams, LiveKitTransport
        from pipecat.workers.runner import WorkerRunner
        from supabase import create_async_client

        from call_state import CallState
        from reservations import SupabaseReservationsRepository

        settings = self._settings

        supabase_client = await create_async_client(settings.supabase_url, settings.supabase_key)
        repository = SupabaseReservationsRepository(supabase_client)
        call_state = CallState()

        url, token, room_name = await configure()

        # Used by end_conversation to remove just the caller on hangup -- for
        # PSTN calls, stopping the pipeline alone leaves the SIP participant
        # (and the call, and Twilio's per-minute billing) connected.
        livekit_api = lk_api.LiveKitAPI(url, settings.livekit_api_key, settings.livekit_api_secret)

        if settings.is_local:
            # A ready-to-click meet.livekit.io test link, so joining doesn't
            # require manually copy-pasting the URL/token into the
            # custom-connection form. Not useful once deployed (nobody reads
            # container logs to find a link to join a call already in
            # progress), so skip it on a cloud deployment.
            _, api_key, api_secret = livekit_credentials()
            user_token = generate_token(room_name, "User", api_key, api_secret)
            test_link = (
                f"https://meet.livekit.io/custom?{urlencode({'liveKitUrl': url, 'token': user_token})}"
            )
            logger.info(f"Join to test: {test_link}")

        transport = LiveKitTransport(
            url,
            token,
            room_name,
            params=LiveKitParams(audio_in_enabled=True, audio_out_enabled=True),
        )

        stt = DeepgramSTTService(api_key=settings.deepgram_api_key)
        tts = CartesiaTTSService(
            api_key=settings.cartesia_api_key,
            settings=CartesiaTTSService.Settings(
                voice=settings.cartesia_voice_id or DEFAULT_HOST_VOICE_ID,
            ),
        )

        # Barge-in is configured here via vad_analyzer, not on the transport
        # (LiveKitParams has no vad_analyzer field). This context is the only
        # one in the whole session -- Host/Booking never get their own --
        # which is what gives context retention across the handoff below.
        context = LLMContext()
        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
        )

        runner = WorkerRunner()

        # Sits where the LLM used to be: forwards frames to whichever agent
        # worker is currently active, and forwards that worker's output back.
        bridge = BusBridgeProcessor(bus=runner.bus, worker_name="main")

        pipeline = Pipeline(
            [
                transport.input(),
                stt,
                user_aggregator,
                bridge,
                tts,
                transport.output(),
                assistant_aggregator,
            ]
        )
        # MetricsLogObserver logs the TTFB/processing-time frames enable_metrics=True
        # already generates -- per-turn STT/LLM/TTS latency, for free.
        main_worker = PipelineWorker(
            pipeline,
            name="main",
            params=PipelineParams(enable_metrics=True),
            observers=[MetricsLogObserver()],
        )

        host_worker, booking_worker = self._build_workers(
            main_worker=main_worker,
            room_name=room_name,
            livekit_api=livekit_api,
            repository=repository,
            call_state=call_state,
        )

        @transport.event_handler("on_first_participant_joined")
        async def on_first_participant_joined(
            transport: LiveKitTransport, participant_id: str
        ) -> None:
            logger.bind(event="call_started", room=room_name, participant=participant_id).info(
                "call started"
            )
            call_state.participant_identity = participant_id
            context.add_message(
                {"role": "developer", "content": "Greet the caller and introduce yourself briefly."}
            )
            await main_worker.queue_frames([LLMRunFrame()])

        @transport.event_handler("on_participant_disconnected")
        async def on_participant_disconnected(
            transport: LiveKitTransport, participant_id: str
        ) -> None:
            logger.bind(event="call_ended", room=room_name, participant=participant_id).info(
                "participant disconnected"
            )
            # This is a persistent line, not a one-shot script -- reset for
            # the next caller instead of tearing down the runner/process.
            # (fresh conversation; next on_first_participant_joined greets
            # again once someone new dials in).
            context.set_messages([])
            if call_state.active_worker != "host":
                # Don't let Host's on_activated speak into an empty room;
                # the next caller's on_first_participant_joined greets.
                host_worker.silence_next_activation()
                await host_worker.activate_worker("host")
                call_state.active_worker = "host"
            call_state.participant_identity = None

        await runner.add_workers(main_worker, host_worker, booking_worker)
        try:
            await runner.run()
        finally:
            await livekit_api.aclose()

    def _build_workers(
        self,
        *,
        main_worker: "PipelineWorker",
        room_name: str,
        livekit_api: "lk_api.LiveKitAPI",
        repository: ReservationsRepository,
        call_state: "CallState",
    ) -> tuple["HostWorker", "BookingWorker"]:
        """Construct the Host/Booking `LLMWorker`s and their per-persona LLM services."""
        from pipecat.services.openai.llm import OpenAILLMService

        from workers import BookingWorker, HostWorker

        settings = self._settings
        today = date.today().isoformat()

        host_llm = OpenAILLMService(
            api_key=settings.openai_api_key,
            settings=OpenAILLMService.Settings(
                model=settings.openai_model,
                system_instruction=HOST_SYSTEM_PROMPT.format(today=today),
            ),
        )
        booking_llm = OpenAILLMService(
            api_key=settings.openai_api_key,
            settings=OpenAILLMService.Settings(
                model=settings.openai_model,
                system_instruction=BOOKING_SYSTEM_PROMPT.format(today=today),
            ),
        )
        host_worker = HostWorker(
            "host",
            llm=host_llm,
            main_worker=main_worker,
            voice_id=settings.cartesia_voice_id or DEFAULT_HOST_VOICE_ID,
            room_name=room_name,
            livekit_api=livekit_api,
            call_state=call_state,
            active=True,
        )
        booking_worker = BookingWorker(
            "booking",
            llm=booking_llm,
            main_worker=main_worker,
            voice_id=settings.cartesia_booking_voice_id or DEFAULT_BOOKING_VOICE_ID,
            room_name=room_name,
            livekit_api=livekit_api,
            call_state=call_state,
            repository=repository,
            active=False,
        )
        return host_worker, booking_worker


def main() -> int:
    settings = load_settings()
    if settings is None:
        return 1
    configure_logging(settings)
    asyncio.run(BellaVistaBot(settings).run())
    # runner.run() is supposed to block for the life of the process. If it
    # returns, the phone line is down -- exit non-zero on a cloud host so
    # Fly's default on-failure policy (or restart=always) brings it back.
    if not settings.is_local:
        logger.error("pipeline ended unexpectedly; exiting so the host restarts the line")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())


