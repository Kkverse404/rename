from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).parents[1] / "windows-app" / "rename_gui" / "bridge.py"
_SPEC = importlib.util.spec_from_file_location("rename_gui_bridge", _MODULE_PATH)
assert _SPEC and _SPEC.loader
bridge = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = bridge
_SPEC.loader.exec_module(bridge)


def test_windows_resume_does_not_spawn_over_external_daemon(monkeypatch):
    control = bridge.DaemonControl("rename.exe")
    status = {"daemon": {"status_line": "daemon: running (Windows Startup)"}}
    monkeypatch.setattr(bridge.sys, "platform", "win32")
    monkeypatch.setattr(
        bridge.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )

    assert control.resume(status) is False
    assert control.pause(status) is False
    assert not control.can_pause(status)


def test_rename_session_requests_json_and_returns_real_change_result(monkeypatch):
    client = bridge.RenameCLI("rename.exe")
    seen = {}

    def fake_run(args, timeout=90):
        seen["args"] = args
        seen["timeout"] = timeout
        return b'{"renamed": 0, "candidates": 0, "changed": false}'

    monkeypatch.setattr(client, "_run", fake_run)

    result = client.rename_session("thread-1", "codex")

    assert result["changed"] is False
    assert seen["args"] == [
        "once",
        "--session",
        "thread-1",
        "--json",
        "--tool",
        "codex",
    ]
