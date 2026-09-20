"""Adapter for Codex lifecycle hook input."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import TextIO

from .models import Session

_MAX_EVENT_BYTES = 64 * 1024


class CodexHookInputError(ValueError):
    """Codex supplied an invalid or unsupported hook event."""


@dataclass(frozen=True)
class CodexStopEvent:
    session_id: str
    cwd: str | None = None
    transcript_path: str | None = None


def _optional_path(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 32_768:
        raise CodexHookInputError(f"Codex Stop hook event has an invalid {field}")
    value = value.strip()
    return value or None


def _normalized_path(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith("\\\\?\\"):
        value = value[4:]
    return os.path.normcase(os.path.normpath(value))


def read_stop_event(stream: TextIO) -> CodexStopEvent:
    """Parse one bounded Codex ``Stop`` event from stdin."""

    raw = stream.read(_MAX_EVENT_BYTES + 1)
    if len(raw.encode("utf-8")) > _MAX_EVENT_BYTES:
        raise CodexHookInputError("Codex hook event exceeds 64 KiB")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CodexHookInputError("Codex hook event is not valid JSON") from exc
    if not isinstance(value, dict) or value.get("hook_event_name") != "Stop":
        raise CodexHookInputError("expected a Codex Stop hook event")
    session_id = value.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 128:
        raise CodexHookInputError("Codex Stop hook event has an invalid session_id")
    return CodexStopEvent(
        session_id=session_id.strip(),
        cwd=_optional_path(value.get("cwd"), "cwd"),
        transcript_path=_optional_path(value.get("transcript_path"), "transcript_path"),
    )


def resolve_stop_session(
    event: CodexStopEvent,
    sessions: list[Session],
    *,
    now: float | None = None,
    cwd_recency_seconds: float = 600,
) -> Session | None:
    """Map a Codex Stop event to its persisted sidebar thread."""

    for session in sessions:
        if session.id == event.session_id:
            return session

    transcript_path = _normalized_path(event.transcript_path)
    if transcript_path:
        for session in sessions:
            if _normalized_path(session.meta.get("rollout_path")) == transcript_path:
                return session

    cwd = _normalized_path(event.cwd)
    if not cwd:
        return None
    current_time = time.time() if now is None else now
    recent = [
        session
        for session in sessions
        if _normalized_path(session.cwd) == cwd
        and session.last_active >= current_time - cwd_recency_seconds
    ]
    return max(recent, key=lambda session: session.last_active, default=None)


__all__ = [
    "CodexHookInputError",
    "CodexStopEvent",
    "read_stop_event",
    "resolve_stop_session",
]
