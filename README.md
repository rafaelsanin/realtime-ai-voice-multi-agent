# Bella Vista Voice Bot

A small Pipecat + LiveKit voice-agent demo: a restaurant phone line with two
agents (Host, Booking) that hand off to each other, call tools against
Supabase, and can be dialed over a real phone number via Twilio + LiveKit
SIP. Built commit-by-commit — see [PLAN.md](PLAN.md) for the full,
detailed build plan (exact steps, test procedures, commit messages).

## Concept map

| Commit | Concepts demonstrated |
|---|---|
| 1 — Scaffold | Project setup, env-driven config |
| 2 — Single-agent pipeline | Pipecat pipeline construction, native `LiveKitTransport`, realtime STT/LLM/TTS, VAD-driven barge-in |
| 3 — Tool calling | LLM function/tool calling against a real backend (Supabase) |
| 4 — Multi-agent handoff | Multi-agent orchestration, dynamic handoff, shared-context retention, graceful termination |
| 5 — PSTN | SIP trunking, LiveKit inbound trunk + dispatch rule, end-to-end phone calling |

## Setup

1. Install [uv](https://docs.astral.sh/uv/). `uv sync` installs everything
   from `pyproject.toml`/`uv.lock` into `.venv`.
2. Copy `.env.example` to `.env` and fill in the values — see "External
   service setup" below (or the fuller version in [PLAN.md](PLAN.md)) for
   where each one comes from.
3. `uv run bot.py` — validates env vars are present (this evolves into the
   actual bot from commit 2 onward).

## External service setup (summary — see PLAN.md for full detail)

- **LiveKit Cloud** (<https://cloud.livekit.io/>): create a project →
  Settings → API Keys → `LIVEKIT_URL` / `LIVEKIT_API_KEY` /
  `LIVEKIT_API_SECRET`. Pick any fixed room name → `LIVEKIT_ROOM_NAME`.
- **OpenAI** (<https://platform.openai.com/api-keys>): needs billing enabled
  → `OPENAI_API_KEY`.
- **Deepgram** (<https://console.deepgram.com/>): free trial credit covers
  this demo → `DEEPGRAM_API_KEY`.
- **Cartesia** (<https://play.cartesia.ai/>): Settings → API Keys →
  `CARTESIA_API_KEY`.
- **Supabase** (needed from commit 3): <https://supabase.com/dashboard> →
  new project → Project Settings → Data API (`SUPABASE_URL`) / API Keys,
  `service_role` key (`SUPABASE_KEY`).
- **Twilio + LiveKit SIP** (needed from commit 5 only): buy a Voice-capable
  number, create an Elastic SIP Trunk originating to your LiveKit SIP
  endpoint, then create a matching LiveKit inbound trunk + dispatch rule.
  Full step-by-step (including trial-account gotchas) is in PLAN.md.

## Running

```sh
uv run bot.py
```
