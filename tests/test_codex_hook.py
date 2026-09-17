import io
import json

import pytest

from rename.codex_hook import CodexHookInputError, CodexStopEvent, read_stop_event


def test_reads_codex_stop_event():
    stream = io.StringIO(
        json.dumps(
            {
                "session_id": "thread-123",
                "hook_event_name": "Stop",
                "cwd": r"C:\work",
                "last_assistant_message": "done",
            }
        )
    )

    assert read_stop_event(stream) == CodexStopEvent(session_id="thread-123")


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
