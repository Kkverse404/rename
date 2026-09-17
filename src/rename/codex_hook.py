"""Adapter for Codex lifecycle hook input."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TextIO

_MAX_EVENT_BYTES = 64 * 1024


class CodexHookInputError(ValueError):
    """Codex supplied an invalid or unsupported hook event."""


@dataclass(frozen=True)
class CodexStopEvent:
    session_id: str


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
    return CodexStopEvent(session_id=session_id.strip())


__all__ = ["CodexHookInputError", "CodexStopEvent", "read_stop_event"]
