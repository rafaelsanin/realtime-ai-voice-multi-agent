"""Bella Vista voice bot entrypoint.

Commit 1: env-var validation stub only. Pipeline wiring lands in commit 2.
"""

import os
import sys

from dotenv import load_dotenv

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


def main() -> int:
    missing = [name for name in REQUIRED_VARS if not os.getenv(name)]
    if missing:
        print(f"missing: {', '.join(missing)}")
        return 1
    print("env ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
