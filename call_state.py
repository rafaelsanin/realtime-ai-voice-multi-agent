"""Per-call state shared between bot.py's event handlers and the agent workers.

This is an always-on line handling calls one after another, not a one-shot
script -- a hangup must reset things for the next caller (context, active
worker) without restarting the whole process.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CallState:
    """Mutable, shared per-room state. One instance per bot process."""

    active_worker: str = "host"
    participant_identity: str | None = None
