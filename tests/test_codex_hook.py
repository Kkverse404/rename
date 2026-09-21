import io
import json
import time

import pytest

from rename.codex_hook import (
    CodexHookInputError,
    CodexStopEvent,
    read_stop_event,
    resolve_stop_session,
)
from rename.models import Session


def test_reads_codex_stop_event():
    stream = io.StringIO(
        json.dumps(
            {
                "session_id": "thread-123",
                "hook_event_name": "Stop",
                "cwd": r"C:\work",
                "transcript_path": r"C:\Users\me\.codex\sessions\thread-123.jsonl",
                "last_assistant_message": "done",
            }
        )
    )

    assert read_stop_event(stream) == CodexStopEvent(
        session_id="thread-123",
        cwd=r"C:\work",
        transcript_path=r"C:\Users\me\.codex\sessions\thread-123.jsonl",
    )


def test_resolves_exact_native_session_id_first():
    sessions = [
        Session("codex", "thread-123", "Target", last_active=time.time()),
        Session("codex", "other", "Other", last_active=time.time() + 1),
    ]

    resolved = resolve_stop_session(CodexStopEvent("thread-123"), sessions)

    assert resolved is sessions[0]


def test_resolves_desktop_execution_id_by_rollout_path():
    sessions = [
        Session(
            "codex",
            "thread-123",
            "Target",
            last_active=time.time(),
            meta={"rollout_path": r"C:\Users\me\.codex\sessions\thread-123.jsonl"},
        )
    ]
    event = CodexStopEvent(
        "desktop-execution-id",
        transcript_path=r"\\?\C:\Users\me\.codex\sessions\thread-123.jsonl",
    )

    assert resolve_stop_session(event, sessions) is sessions[0]


def test_resolves_desktop_execution_id_to_latest_recent_session_in_same_cwd():
    now = time.time()
    sessions = [
        Session("codex", "older", "Older", last_active=now - 120, cwd=r"D:\work"),
        Session("codex", "latest", "Latest", last_active=now - 2, cwd=r"d:\WORK"),
        Session("codex", "stale", "Stale", last_active=now - 900, cwd=r"D:\work"),
    ]
    event = CodexStopEvent("desktop-execution-id", cwd=r"\\?\D:\work")

    assert resolve_stop_session(event, sessions, now=now).id == "latest"


def test_resolves_desktop_execution_id_by_nearby_uuid_creation_time():
    thread_id = "01a0c1b3-93ce-7c30-b912-82becee00ca8"
    event = CodexStopEvent("01a0c1b3-97bf-7672-aaef-9eb57bf6c0f6")
    sessions = [
        Session(
            "codex",
            thread_id,
            "Target",
            last_active=time.time(),
            meta={"created_at_ms": int(thread_id.replace("-", "")[:12], 16)},
        )
    ]

    assert resolve_stop_session(event, sessions) is sessions[0]


def test_resolves_latest_recent_session_when_hook_runs_in_project_subdirectory():
    now = time.time()
    sessions = [
        Session(
            "codex",
            "thread-123",
            "Target",
            last_active=now - 2,
            cwd=r"D:\codexpg\project",
        )
    ]
    event = CodexStopEvent(
        "desktop-execution-id",
        cwd=r"D:\codexpg\project\src",
    )

    assert resolve_stop_session(event, sessions, now=now) is sessions[0]


def test_does_not_guess_when_desktop_event_has_no_safe_match():
    now = time.time()
    sessions = [
        Session("codex", "stale", "Stale", last_active=now - 900, cwd=r"D:\work")
    ]
    event = CodexStopEvent("desktop-execution-id", cwd=r"D:\elsewhere")

    assert resolve_stop_session(event, sessions, now=now) is None


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        "[]",
        '{"hook_event_name":"SessionEnd","session_id":"thread-123"}',
        '{"hook_event_name":"Stop","session_id":""}',
    ],
)
def test_rejects_invalid_or_unsupported_events(payload):
    with pytest.raises(CodexHookInputError):
        read_stop_event(io.StringIO(payload))


def test_rejects_oversized_event():
    payload = json.dumps(
        {"hook_event_name": "Stop", "session_id": "thread-123", "extra": "x" * 65536}
    )
    with pytest.raises(CodexHookInputError, match="64 KiB"):
        read_stop_event(io.StringIO(payload))
