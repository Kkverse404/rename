from __future__ import annotations

import os
import sys

import pytest

import rename.adapters.codex_writer as codex_writer
import rename.codex_executable as codex_executable
from rename.adapters.codex_writer import (
    CodexConflictError,
    CodexEOFError,
    CodexProcessError,
    CodexProtocolError,
    CodexRPCError,
    CodexTimeoutError,
    CodexVerificationError,
    CodexWriter,
    _app_server_command,
    _StdioJsonRpcClient,
)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.notifications = []
        self.closed = False

    def request(self, method, params):
        self.requests.append((method, params))
        expected_method, response = self.responses.pop(0)
        assert method == expected_method
        if isinstance(response, BaseException):
            raise response
        return response

    def notify(self, method, params=None):
        self.notifications.append((method, params))

    def close(self):
        self.closed = True


def _factory_for(client, captured):
    def factory(argv, env, timeout):
        captured.update(argv=argv, env=env, timeout=timeout)
        return client

    return factory


def _thread(thread_id, name, *, updated_at=10, status=None):
    return {
        "thread": {
            "id": thread_id,
            "name": name,
            "updatedAt": updated_at,
            "status": status or {"type": "idle"},
        }
    }


def test_writer_success_initializes_compares_writes_and_verifies(tmp_path):
    tid = "thread-1"
    updated_at = 1_800_000_000
    client = FakeClient(
        [
            ("initialize", {"serverInfo": {}}),
            ("thread/read", _thread(tid, "旧标题", updated_at=updated_at)),
            ("thread/name/set", {}),
            ("thread/read", _thread(tid, "修复登录 ✨", updated_at=updated_at + 1)),
        ]
    )
    captured = {}
    writer = CodexWriter(
        codex_home=tmp_path,
        executable=sys.executable,
        timeout=3,
        client_factory=_factory_for(client, captured),
    )

    writer.set_title(
        tid,
        "修复登录 ✨",
        expected_title="旧标题",
        expected_updated_at=updated_at * 1000,
        expected_status="idle",
    )

    assert client.notifications == [("initialized", None)]
    assert client.requests[1] == (
        "thread/read",
        {"threadId": tid, "includeTurns": False},
    )
    assert client.requests[2] == (
        "thread/name/set",
        {"threadId": tid, "name": "修复登录 ✨"},
    )
    assert client.closed is True
    assert captured["env"]["CODEX_HOME"] == str(tmp_path)
    assert captured["timeout"] == 3


def test_writer_conflict_reports_actual_title_and_does_not_write():
    tid = "thread-2"
    client = FakeClient(
        [
            ("initialize", {}),
            ("thread/read", _thread(tid, "用户手动标题")),
        ]
    )
    writer = CodexWriter(
        executable=sys.executable,
        client_factory=_factory_for(client, {}),
    )

    with pytest.raises(CodexConflictError) as caught:
        writer.set_title(tid, "新标题", expected_title="旧标题")

    assert caught.value.actual_title == "用户手动标题"
    assert [method for method, _ in client.requests] == ["initialize", "thread/read"]
    assert client.closed is True


def test_writer_compares_optional_updated_at_and_status():
    tid = "thread-3"
    client = FakeClient(
        [
            ("initialize", {}),
            ("thread/read", _thread(tid, "Old", updated_at=99)),
        ]
    )
    writer = CodexWriter(
        executable=sys.executable,
        client_factory=_factory_for(client, {}),
    )

    with pytest.raises(CodexConflictError) as caught:
        writer.set_title(tid, "New", expected_title="Old", expected_updated_at=98)
    assert caught.value.field == "updatedAt"
    assert caught.value.actual_title == "Old"


def test_writer_rejects_inconsistent_read_back():
    tid = "thread-4"
    client = FakeClient(
        [
            ("initialize", {}),
            ("thread/read", _thread(tid, "Old")),
            ("thread/name/set", {}),
            ("thread/read", _thread(tid, "Someone else")),
        ]
    )
    writer = CodexWriter(
        executable=sys.executable,
        client_factory=_factory_for(client, {}),
    )

    with pytest.raises(CodexVerificationError) as caught:
        writer.set_title(tid, "New", expected_title="Old")
    assert caught.value.actual_title == "Someone else"


def test_read_title_uses_fresh_client_and_validates_thread_id():
    client = FakeClient(
        [
            ("initialize", {}),
            ("thread/read", _thread("wrong", "Title")),
        ]
    )
    writer = CodexWriter(
        executable=sys.executable,
        client_factory=_factory_for(client, {}),
    )
    with pytest.raises(CodexProtocolError, match="thread id"):
        writer.read_title("expected")
    assert client.closed is True


def _transport(code, *, timeout=1):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return _StdioJsonRpcClient(
        [sys.executable, "-u", "-c", code], env, timeout
    )


def test_stdio_client_hides_windows_console(monkeypatch):
    seen = {}

    def fake_popen(argv, **kwargs):
        seen.update(kwargs)
        raise OSError("stop after launch options are captured")

    monkeypatch.setattr(codex_writer.subprocess, "Popen", fake_popen)
    with pytest.raises(CodexProcessError, match="could not start"):
        _StdioJsonRpcClient(["codex.exe", "app-server", "--stdio"], {}, 1)

    expected_flags = 0x08000000 if os.name == "nt" else 0
    assert seen["creationflags"] == expected_flags


def test_stdio_client_ignores_notifications_and_round_trips_unicode():
    code = (
        "import json,sys; r=json.loads(sys.stdin.readline()); "
        "print(json.dumps({'method':'thread/status/changed','params':{}}),flush=True); "
        "print(json.dumps({'id':r['id'],'result':{'text':'你好 ✨'}},"
        "ensure_ascii=False),flush=True)"
    )
    client = _transport(code)
    assert client.request("example", {"title": "中文"}) == {"text": "你好 ✨"}
    client.close()


def test_stdio_client_timeout_is_classified():
    client = _transport("import time; time.sleep(5)", timeout=0.05)
    try:
        with pytest.raises(CodexTimeoutError):
            client.request("slow", {})
    finally:
        with pytest.raises(CodexProcessError):
            client.close()


def test_stdio_client_clean_eof_is_classified():
    client = _transport("pass")
    try:
        with pytest.raises(CodexEOFError):
            client.request("missing", {})
    finally:
        client.close()


def test_stdio_client_nonzero_exit_is_classified_and_drains_stderr():
    client = _transport("import sys; print('boom',file=sys.stderr); sys.exit(7)")
    with pytest.raises(CodexProcessError, match="code 7") as caught:
        client.request("missing", {})
    assert "boom" in str(caught.value)
    with pytest.raises(CodexProcessError):
        client.close()


def test_stdio_client_rpc_error_is_classified():
    code = (
        "import json,sys; r=json.loads(sys.stdin.readline()); "
        "print(json.dumps({'id':r['id'],'error':{'code':-1,'message':'no'}}),flush=True)"
    )
    client = _transport(code)
    try:
        with pytest.raises(CodexRPCError) as caught:
            client.request("denied", {})
        assert caught.value.method == "denied"
    finally:
        client.close()


def test_stdio_client_invalid_json_is_protocol_error():
    client = _transport("print('not-json',flush=True)")
    try:
        with pytest.raises(CodexProtocolError):
            client.request("broken", {})
    finally:
        client.close()


def test_cmd_launcher_uses_comspec_and_keeps_json_out_of_argv(tmp_path):
    batch = tmp_path / "codex.cmd"
    batch.write_text("@echo off\n", encoding="utf-8")
    comspec = tmp_path / "cmd.exe"
    comspec.write_bytes(b"")

    argv = _app_server_command(batch, {"COMSPEC": str(comspec)})

    assert isinstance(argv, str)
    assert str(comspec.resolve()) in argv
    assert " /d /v:off /s /c " in argv
    assert str(batch.resolve()) in argv
    assert "app-server" in argv and "--stdio" in argv
    assert "threadId" not in argv


def test_app_server_resolves_windows_desktop_codex_without_path(monkeypatch, tmp_path):
    executable = tmp_path / "OpenAI" / "Codex" / "bin" / "build-id" / "codex.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"codex")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(codex_executable.shutil, "which", lambda name: None)
    monkeypatch.setattr(codex_executable.sys, "platform", "win32")

    argv = _app_server_command("codex", {})

    assert argv == [str(executable.resolve()), "app-server", "--stdio"]
