"""One inbound call, start to finish (commit 9).

A `CallSession` owns everything that used to be process-wide: the LiveKit
transport for its own room, the STT/LLM/TTS services, the shared
`LLMContext`, the Host/Booking workers, and the `WorkerRunner` driving them.
Two sessions share nothing but `Settings`, the reservations repository and
the LiveKit API client, which is what makes concurrent calls possible --
`dispatcher.py` runs one session per room.

Ending a session ends only that call: `runner.cancel()` tears down this
room's pipeline while other calls keep running, and the process stays up to
answer the next one.
"""

from __future__ import annotations

import asyncio
from datetime import date

from livekit import api as lk_api
from loguru import logger
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
from pipecat.runner.livekit import generate_token_with_agent
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.livekit.transport import LiveKitParams, LiveKitTransport
from pipecat.workers.runner import WorkerRunner

from call_state import CallState
from reservations import ReservationsRepository
from settings import Settings
from workers import BookingWorker, HostWorker

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

AGENT_IDENTITY = "Pipecat Agent"

# A caller who hangs up while the session is still starting leaves a room
# nobody ever joins. Give up on it rather than holding a slot until LiveKit's
# own empty-room timeout fires.
ABANDONED_CALL_TIMEOUT_SECONDS = 20.0


class CallSession:
    """Runs the Host/Booking pipeline for one caller in one room."""

    def __init__(
        self,
        *,
        settings: Settings,
        room_name: str,
        livekit_api: lk_api.LiveKitAPI,
        repository: ReservationsRepository,
    ) -> None:
        self._settings = settings
        self._room_name = room_name
        self._livekit_api = livekit_api
        self._repository = repository
        self._call_state = CallState()
        self._runner: WorkerRunner | None = None

    @property
    def room_name(self) -> str:
        """The LiveKit room this session is bound to."""
        return self._room_name

    async def stop(self) -> None:
        """End this call from the outside (process shutdown), if it is running."""
        if self._runner is not None:
            await self._runner.cancel(reason="shutting down")

    async def run(self) -> None:
        """Join the room and run the call until the caller leaves."""
        settings = self._settings
        room_name = self._room_name

        # Minted per session rather than via pipecat's configure(), which
        # reads a single fixed room from the environment (and logs a token).
        token = generate_token_with_agent(
            room_name, AGENT_IDENTITY, settings.livekit_api_key, settings.livekit_api_secret
        )
        transport = LiveKitTransport(
            settings.livekit_url,
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
        # one in the session -- Host/Booking never get their own -- which is
        # what gives context retention across the handoff below.
        context = LLMContext()
        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
        )

        # Signals are handled once, by the dispatcher's host process -- a
        # per-call runner installing its own handler would clobber that.
        runner = WorkerRunner(handle_sigint=False)
        self._runner = runner

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

        host_worker, booking_worker = self._build_workers(main_worker=main_worker)

        @transport.event_handler("on_first_participant_joined")
        async def on_first_participant_joined(
            transport: LiveKitTransport, participant_id: str
        ) -> None:
            logger.bind(event="call_started", room=room_name, participant=participant_id).info(
                "call started"
            )
            self._call_state.participant_identity = participant_id
            context.add_message(
                {"role": "developer", "content": "Greet the caller and introduce yourself briefly."}
            )
            await main_worker.queue_frames([LLMRunFrame()])

        @transport.event_handler("on_participant_disconnected")
        async def on_participant_disconnected(
            transport: LiveKitTransport, participant_id: str
        ) -> None:
            # The room is this call's alone, so the last participant leaving
            # ends it. Anyone else still on the line keeps it going.
            if transport.get_participants():
                return
            logger.bind(event="call_ended", room=room_name, participant=participant_id).info(
                "participant disconnected"
            )
            await runner.cancel(reason="caller disconnected")

        @transport.event_handler("on_disconnected")
        async def on_disconnected(transport: LiveKitTransport) -> None:
            # Reached when the room goes away under us (deleted server-side,
            # or torn down as this session ends). cancel() is idempotent.
            await runner.cancel(reason="room disconnected")

        await runner.add_workers(main_worker, host_worker, booking_worker)

        abandoned = asyncio.create_task(self._end_if_abandoned(transport, runner))
        try:
            await runner.run()
        finally:
            abandoned.cancel()
            self._runner = None

    async def _end_if_abandoned(self, transport: LiveKitTransport, runner: WorkerRunner) -> None:
        """Give up on a room nobody ever joins (caller hung up mid-startup)."""
        await asyncio.sleep(ABANDONED_CALL_TIMEOUT_SECONDS)
        if transport.get_participants():
            return
        logger.bind(event="call_abandoned", room=self._room_name).info(
            "no caller joined; ending session"
        )
        await runner.cancel(reason="no caller joined")

    def _build_workers(self, *, main_worker: PipelineWorker) -> tuple[HostWorker, BookingWorker]:
        """Construct this call's Host/Booking `LLMWorker`s and their per-persona LLM services."""
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
            room_name=self._room_name,
            livekit_api=self._livekit_api,
            call_state=self._call_state,
            active=True,
        )
        booking_worker = BookingWorker(
            "booking",
            llm=booking_llm,
            main_worker=main_worker,
            voice_id=settings.cartesia_booking_voice_id or DEFAULT_BOOKING_VOICE_ID,
            room_name=self._room_name,
            livekit_api=self._livekit_api,
            call_state=self._call_state,
            repository=self._repository,
            active=False,
        )
        return host_worker, booking_worker
