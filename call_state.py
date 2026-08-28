"""Per-call state shared between a session's event handlers and its agent workers.

One instance per `CallSession` (session.py), not per process -- concurrent
calls each get their own, which is what keeps one caller's hangup or handoff
from touching another's.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CallState:
    """Mutable state for a single call."""

    active_worker: str = "host"
    participant_identity: str | None = None
