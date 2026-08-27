"""Bella Vista voice bot entrypoint.

Commit 4: multi-agent handoff (Host/Booking) with shared context retention.
"""

import asyncio
import os
import sys
from datetime import date

from dotenv import load_dotenv
from loguru import logger

load_dotenv()

REQUIRED_VARS = [
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "LIVEKIT_ROOM_NAME",
    "OPENAI_API_KEY",
    "DEEPGRAM_API_KEY",
    "CARTESIA_API_KEY",
    "SUPABASE_URL",
    "SUPABASE_KEY",
]

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
# audible, not just inferred from what's being said. Overridable via env.
DEFAULT_HOST_VOICE_ID = "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4"  # Skylar
DEFAULT_BOOKING_VOICE_ID = "62ae83ad-4f6a-430b-af41-a9bede9286ca"  # Gemma


def check_env() -> bool:
    """Print any missing required env vars. Returns True if all are present."""
    missing = [name for name in REQUIRED_VARS if not os.getenv(name)]
    if missing:
        print(f"missing: {', '.join(missing)}")
        return False
    return True


async def run_bot() -> None:
    # Imported lazily so `check_env()` can fail fast without pulling in the
    # (heavier) pipeline dependencies first.
    from urllib.parse import urlencode

    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.bus import BusBridgeProcessor
    from pipecat.frames.frames import LLMRunFrame
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
    from pipecat.services.openai.llm import OpenAILLMService
    from pipecat.transports.livekit.transport import LiveKitParams, LiveKitTransport
    from pipecat.workers.runner import WorkerRunner
    from supabase import create_async_client

    from workers import BookingWorker, HostWorker

    supabase_client = await create_async_client(
        os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"]
    )

    url, token, room_name = await configure()

    # A ready-to-click meet.livekit.io test link, so joining doesn't require
    # manually copy-pasting the URL/token into the custom-connection form.
    _, api_key, api_secret = livekit_credentials()
    user_token = generate_token(room_name, "User", api_key, api_secret)
    test_link = f"https://meet.livekit.io/custom?{urlencode({'liveKitUrl': url, 'token': user_token})}"
    logger.info(f"Join to test: {test_link}")

    transport = LiveKitTransport(
        url,
        token,
        room_name,
        params=LiveKitParams(audio_in_enabled=True, audio_out_enabled=True),
    )

    stt = DeepgramSTTService(api_key=os.environ["DEEPGRAM_API_KEY"])
    tts = CartesiaTTSService(
        api_key=os.environ["CARTESIA_API_KEY"],
        settings=CartesiaTTSService.Settings(
            voice=os.getenv("CARTESIA_VOICE_ID") or DEFAULT_HOST_VOICE_ID,
        ),
    )

    # Barge-in is configured here via vad_analyzer, not on the transport
    # (LiveKitParams has no vad_analyzer field). This context is the only one
    # in the whole session -- Host/Booking never get their own -- which is
    # what gives context retention across the handoff below.
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
    main_worker = PipelineWorker(pipeline, name="main", params=PipelineParams(enable_metrics=True))

    today = date.today().isoformat()
    host_llm = OpenAILLMService(
        api_key=os.environ["OPENAI_API_KEY"],
        settings=OpenAILLMService.Settings(
            model=os.getenv("OPENAI_MODEL") or "gpt-4o-mini",
            system_instruction=HOST_SYSTEM_PROMPT.format(today=today),
        ),
    )
    booking_llm = OpenAILLMService(
        api_key=os.environ["OPENAI_API_KEY"],
        settings=OpenAILLMService.Settings(
            model=os.getenv("OPENAI_MODEL") or "gpt-4o-mini",
            system_instruction=BOOKING_SYSTEM_PROMPT.format(today=today),
        ),
    )
    host_worker = HostWorker(
        "host",
        llm=host_llm,
        main_worker=main_worker,
        voice_id=os.getenv("CARTESIA_VOICE_ID") or DEFAULT_HOST_VOICE_ID,
        active=True,
    )
    booking_worker = BookingWorker(
        "booking",
        llm=booking_llm,
        main_worker=main_worker,
        voice_id=os.getenv("CARTESIA_BOOKING_VOICE_ID") or DEFAULT_BOOKING_VOICE_ID,
        active=False,
    )
    # LLMWorker's constructor doesn't take app_resources -- set it directly so
    # check_availability/book_table (db.py) can reach it via
    # FunctionCallParams.app_resources.
    booking_worker._app_resources = supabase_client

    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant_id):
        logger.info(f"First participant joined: {participant_id}")
        context.add_message(
            {"role": "developer", "content": "Greet the caller and introduce yourself briefly."}
        )
        await main_worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_participant_disconnected")
    async def on_participant_disconnected(transport, participant_id):
        logger.info(f"Participant disconnected: {participant_id}")
        await runner.cancel()

    await runner.add_workers(main_worker, host_worker, booking_worker)
    await runner.run()


def main() -> int:
    if not check_env():
        return 1
    asyncio.run(run_bot())
    return 0


if __name__ == "__main__":
    sys.exit(main())

