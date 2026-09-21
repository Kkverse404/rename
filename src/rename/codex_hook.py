"""Adapter for Codex lifecycle hook input."""

from __future__ import annotations

import json
import ntpath
import os
import time
import uuid
from dataclasses import dataclass
from typing import TextIO

from .models import Session

_MAX_EVENT_BYTES = 64 * 1024
_MAX_UUID_CREATION_DELTA_MS = 10_000


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


def _paths_related(first: str | None, second: str | None) -> bool:
    first = _normalized_path(first)
    second = _normalized_path(second)
    if not first or not second:
        return False
    try:
        common = ntpath.commonpath((first, second))
    except ValueError:
        return False
    return common in {first, second}


def _uuid7_timestamp_ms(value: str) -> int | None:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        return None
    if parsed.version != 7:
        return None
    return int(parsed.hex[:12], 16)


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

    event_created_at = _uuid7_timestamp_ms(event.session_id)
    if event_created_at is not None:
        event_cwd = _normalized_path(event.cwd)
        candidates: list[tuple[float, Session]] = []
        for session in sessions:
            created_at = session.meta.get("created_at_ms")
            if not isinstance(created_at, (int, float)):
                continue
            if event_cwd and session.cwd and not _paths_related(event.cwd, session.cwd):
                continue
            delta = abs(created_at - event_created_at)
            if delta <= _MAX_UUID_CREATION_DELTA_MS:
                candidates.append((delta, session))
        if candidates:
            nearest_delta = min(delta for delta, _session in candidates)
            nearest = [session for delta, session in candidates if delta == nearest_delta]
            if len(nearest) == 1:
                return nearest[0]

    cwd = _normalized_path(event.cwd)
    if not cwd:
        return None
    current_time = time.time() if now is None else now
    recent = [
        session
        for session in sessions
        if _paths_related(session.cwd, cwd)
        and session.last_active >= current_time - cwd_recency_seconds
    ]
    return max(recent, key=lambda session: session.last_active, default=None)


__all__ = [
    "CodexHookInputError",
    "CodexStopEvent",
    "read_stop_event",
    "resolve_stop_session",
]
