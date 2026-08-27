"""Bella Vista voice bot entrypoint.

Commit 2: single-agent pipeline over native LiveKitTransport.
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from loguru import logger

load_dotenv()

# Required through commit 2 (LiveKitTransport + STT/LLM/TTS). Supabase vars
# join this list once tool calling lands in commit 3.
REQUIRED_VARS = [
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "LIVEKIT_ROOM_NAME",
    "OPENAI_API_KEY",
    "DEEPGRAM_API_KEY",
    "CARTESIA_API_KEY",
]

HOST_SYSTEM_PROMPT = (
    "You are the host at Bella Vista, an Italian restaurant serving "
    "wood-fired Neapolitan pizza and handmade pasta. Located at 12 Market "
    "Street, open Tuesday-Sunday 5pm-10pm (closed Mondays). Answer callers' "
    "questions about the restaurant briefly and warmly. Your responses will "
    "be spoken aloud, so avoid emojis, bullet points, or other formatting "
    "that can't be spoken."
)

# Stock Cartesia voice, used if CARTESIA_VOICE_ID is unset.
DEFAULT_CARTESIA_VOICE_ID = "86e30c1d-714b-4074-a1f2-1cb6b552fb49"


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
    llm = OpenAILLMService(
        api_key=os.environ["OPENAI_API_KEY"],
        settings=OpenAILLMService.Settings(
            model=os.getenv("OPENAI_MODEL") or "gpt-4o-mini",
            system_instruction=HOST_SYSTEM_PROMPT,
        ),
    )
    tts = CartesiaTTSService(
        api_key=os.environ["CARTESIA_API_KEY"],
        settings=CartesiaTTSService.Settings(
            voice=os.getenv("CARTESIA_VOICE_ID") or DEFAULT_CARTESIA_VOICE_ID,
        ),
    )

    # No tools yet (commit 3); barge-in is configured here via vad_analyzer,
    # not on the transport (LiveKitParams has no vad_analyzer field).
    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    worker = PipelineWorker(pipeline, params=PipelineParams(enable_metrics=True))

    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant_id):
        logger.info(f"First participant joined: {participant_id}")
        context.add_message(
            {"role": "developer", "content": "Greet the caller and introduce yourself briefly."}
        )
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_participant_disconnected")
    async def on_participant_disconnected(transport, participant_id):
        logger.info(f"Participant disconnected: {participant_id}")
        await worker.cancel()

    runner = WorkerRunner()
    await runner.add_workers(worker)
    await runner.run()


def main() -> int:
    if not check_env():
        return 1
    asyncio.run(run_bot())
    return 0


if __name__ == "__main__":
    sys.exit(main())

