import argparse
import json
import time
import types

from rename import cli
from rename.config import Config
from rename.models import Message, Session
from rename.session_registry import SessionRegistry


def test_codex_stop_hook_processes_only_current_session_without_output(
    tmp_path, monkeypatch, capsys
):
    session_id = "thread-123"
    session_naming = types.SimpleNamespace(verify_native_metadata=True)
    engine = types.SimpleNamespace(session_naming=session_naming)
    calls = []
    engine.tick = lambda **kwargs: calls.append(kwargs) or (1, 1)
    structured = types.SimpleNamespace(idle_seconds=300)
    cfg = types.SimpleNamespace(
        tools=(),
        idle_seconds=300,
        structured_naming=structured,
        max_age_days=30,
    )

    class Lease:
        def __init__(self, _path):
            pass

        def acquire(self):
            return None

        def release(self):
            return None

    monkeypatch.setattr(
        cli, "read_stop_event", lambda _stream: types.SimpleNamespace(session_id=session_id)
    )
    monkeypatch.setattr(cli.config_mod, "load", lambda: cfg)
    monkeypatch.setattr(cli, "_build", lambda _cfg: ([object()], None, None, engine))
    monkeypatch.setattr(cli, "DaemonLock", Lease)
    monkeypatch.setattr(cli.util, "log_path", lambda: tmp_path / "rename.log")
    monkeypatch.setattr(cli.util, "daemon_lock_path", lambda: tmp_path / "daemon.lock")

    assert cli.cmd_codex_hook(types.SimpleNamespace()) == 0

    assert cfg.tools == ("codex",)
    assert cfg.idle_seconds == 0
    assert cfg.structured_naming.idle_seconds == 0
    assert session_naming.verify_native_metadata is False
    assert calls == [{"limit": 1, "quiet": True, "session_filter": {session_id}}]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


class FakeAdapter:
    name = "fake"
    label = "FakeTool"

    def __init__(self, sessions, transcripts=None):
        self._sessions = sessions
        self._t = transcripts or {}

    def discover(self, since):
        return [s for s in self._sessions if s.last_active >= since]

    def read_transcript(self, s):
        return self._t.get(s.id, [])


def _args(**kw):
    base = dict(
        query="", content=False, tool=None, days=90, limit=30, verbose=False, json=False
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_search_by_title(capsys, monkeypatch):
    now = time.time()
    sessions = [
        Session("fake", "s1", "Fix the deploy script", last_active=now - 100),
        Session("fake", "s2", "Add dark mode toggle", last_active=now - 200),
    ]
    monkeypatch.setattr(cli, "get_adapters", lambda cfg: [FakeAdapter(sessions)])
    rc = cli.cmd_search(_args(query="deploy"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Fix the deploy script" in out
    assert "Add dark mode toggle" not in out
    assert "1 match" in out


def test_search_is_case_insensitive(capsys, monkeypatch):
    now = time.time()
    sessions = [Session("fake", "s1", "Deploy To Production", last_active=now)]
    monkeypatch.setattr(cli, "get_adapters", lambda cfg: [FakeAdapter(sessions)])
    cli.cmd_search(_args(query="DEPLOY"))
    assert "Deploy To Production" in capsys.readouterr().out


def test_search_no_match(capsys, monkeypatch):
    now = time.time()
    sessions = [Session("fake", "s1", "Hello world", last_active=now)]
    monkeypatch.setattr(cli, "get_adapters", lambda cfg: [FakeAdapter(sessions)])
    rc = cli.cmd_search(_args(query="zzz"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "No sessions matching" in out


def test_search_content_flag(capsys, monkeypatch):
    now = time.time()
    sessions = [Session("fake", "s1", "Generic title", last_active=now)]
    transcripts = {"s1": [Message("user", "we should migrate to postgres next sprint")]}
    monkeypatch.setattr(
        cli, "get_adapters", lambda cfg: [FakeAdapter(sessions, transcripts)]
    )

    # title alone does not match "postgres"
    cli.cmd_search(_args(query="postgres", content=False))
    assert "No sessions matching" in capsys.readouterr().out

    # --content searches the transcript and finds it
    cli.cmd_search(_args(query="postgres", content=True))
    out = capsys.readouterr().out
    assert "Generic title" in out
    assert "postgres" in out  # snippet shown


def test_search_respects_days_window(capsys, monkeypatch):
    now = time.time()
    sessions = [Session("fake", "s1", "old deploy notes", last_active=now - 100 * 86400)]
    monkeypatch.setattr(cli, "get_adapters", lambda cfg: [FakeAdapter(sessions)])
    cli.cmd_search(_args(query="deploy", days=30))  # 100 days ago > 30d window
    assert "No sessions matching" in capsys.readouterr().out


def test_search_json_output(capsys, monkeypatch):
    now = time.time()
    sessions = [
        Session("fake", "s1", "Deploy stuff", last_active=now - 50, cwd="/home/me/proj")
    ]
    monkeypatch.setattr(cli, "get_adapters", lambda cfg: [FakeAdapter(sessions)])
    cli.cmd_search(_args(query="deploy", json=True))
    data = json.loads(capsys.readouterr().out)
    assert data[0]["tool"] == "fake"
    assert data[0]["id"] == "s1"
    assert data[0]["title"] == "Deploy stuff"
    assert data[0]["cwd"] == "/home/me/proj"


def test_stats_json(capsys, monkeypatch):
    now = time.time()
    sessions = [
        Session("fake", "s1", "Has a title", last_active=now - 1000),  # stale
        Session("fake", "s2", None, last_active=now - 10),  # untitled, active
        Session("fake", "s3", "", last_active=now - 9999),  # untitled, stale
    ]
    monkeypatch.setattr(cli.config_mod, "load", lambda path=None: Config(idle_seconds=300))
    monkeypatch.setattr(cli, "get_adapters", lambda cfg: [FakeAdapter(sessions)])
    cli.cmd_stats(_args(days=0, json=True))
    data = json.loads(capsys.readouterr().out)
    assert data["total"]["sessions"] == 3
    assert data["total"]["untitled"] == 2
    assert data["total"]["stale"] == 2  # s1 + s3 (idle > 300s)
    assert data["tools"][0]["tool"] == "fake"


def test_stats_table(capsys, monkeypatch):
    now = time.time()
    sessions = [Session("fake", "s1", "Title", last_active=now - 1000)]
    monkeypatch.setattr(cli.config_mod, "load", lambda path=None: Config(idle_seconds=300))
    monkeypatch.setattr(cli, "get_adapters", lambda cfg: [FakeAdapter(sessions)])
    rc = cli.cmd_stats(_args(days=0))
    out = capsys.readouterr().out
    assert rc == 0
    assert "FakeTool" in out
    assert "Total" in out


def test_naming_status_is_pure_read_when_registry_is_absent(
    tmp_path, capsys, monkeypatch
):
    path = tmp_path / "registry.sqlite3"
    monkeypatch.setattr(cli.util, "registry_path", lambda: path)

    rc = cli.cmd_naming_status(_args(json=True))

    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["counts"]["total"] == 0
    assert data["sessions"] == []
    assert not path.exists()


def _finalized_registry(path):
    registry = SessionRegistry(path)
    registry.ensure("codex", "thread-1")
    registry.stage_write(
        "codex",
        "thread-1",
        expected="Original",
        original="Original",
        desired="#1- Stable title",
        module="repo",
        summary="Stable title",
        decision={"ready": True},
    )
    registry.finalize("codex", "thread-1", observed="#1- Stable title")
    return registry


def test_naming_reopen_preserves_display_id(tmp_path, capsys, monkeypatch):
    path = tmp_path / "registry.sqlite3"
    registry = _finalized_registry(path)
    monkeypatch.setattr(cli.util, "registry_path", lambda: path)
    monkeypatch.setattr(cli.config_mod, "load", lambda: Config())

    rc = cli.cmd_naming_reopen(_args(session="#1", json=True))

    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["display_id"] == 1
    assert data["status"] == "pending"
    assert registry.get("codex", "thread-1").display_id == 1


def test_naming_rollback_compares_restores_and_marks_registry(
    tmp_path, capsys, monkeypatch
):
    path = tmp_path / "registry.sqlite3"
    registry = _finalized_registry(path)
    monkeypatch.setattr(cli.util, "registry_path", lambda: path)
    monkeypatch.setattr(cli.config_mod, "load", lambda: Config())
    session = Session(
        "codex",
        "thread-1",
        "#1- Stable title",
        last_active=time.time(),
        meta={"updated_at": time.time()},
    )

    class Writer:
        def __init__(self):
            self.calls = []

        def set_title(self, thread_id, title, **kwargs):
            self.calls.append((thread_id, title, kwargs["expected_title"]))

    writer = Writer()

    class Codex:
        def __init__(self, codex_home=None):
            self.writer = writer

        def available(self):
            return True

        def discover(self, since):
            return [session]

    monkeypatch.setattr(cli, "CodexAdapter", Codex)

    rc = cli.cmd_naming_rollback(_args(session="thread-1", json=True))

    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert writer.calls == [("thread-1", "Original", "#1- Stable title")]
    assert data["restored_title"] == "Original"
    assert registry.get("codex", "thread-1").status == "rolled_back"


def test_parser_exposes_structured_naming_operator_commands():
    parser = cli.build_parser()

    assert parser.parse_args(["naming", "status"]).func is cli.cmd_naming_status
    assert parser.parse_args(["naming", "reopen", "#7"]).func is cli.cmd_naming_reopen
    assert parser.parse_args(["naming", "rollback", "thread-7"]).func is cli.cmd_naming_rollback


def test_global_dry_run_blocks_reopen_and_rollback_mutations(
    tmp_path, capsys, monkeypatch
):
    path = tmp_path / "registry.sqlite3"
    registry = _finalized_registry(path)
    before = registry.operations("codex", "thread-1")
    monkeypatch.setattr(cli.util, "registry_path", lambda: path)
    monkeypatch.setattr(cli.config_mod, "load", lambda: Config(dry_run=True))

    assert cli.cmd_naming_reopen(_args(session="#1", json=True)) == 0
    reopen = json.loads(capsys.readouterr().out)
    assert reopen["dry_run"] is True
    assert cli.cmd_naming_rollback(_args(session="#1", json=True)) == 0
    rollback = json.loads(capsys.readouterr().out)

    assert rollback["dry_run"] is True
    assert registry.get("codex", "thread-1").status == "finalized"
    assert registry.operations("codex", "thread-1") == before


def test_once_dry_run_does_not_create_default_config(monkeypatch):
    args = cli.build_parser().parse_args(["once", "--dry-run"])
    monkeypatch.setattr(cli.config_mod, "load", lambda: Config())

    def unexpected_config_write():
        raise AssertionError("dry-run must not create a config file")

    class FakeEngine:
        def tick(self, **kwargs):
            return 0, 0

    monkeypatch.setattr(cli.config_mod, "ensure_default", unexpected_config_write)
    monkeypatch.setattr(
        cli,
        "_build",
        lambda cfg: ([object()], type("Namer", (), {"name": "heuristic"})(), None, FakeEngine()),
    )

    assert cli.cmd_run(args) == 0


def test_once_json_reports_whether_any_title_changed(capsys, monkeypatch):
    args = cli.build_parser().parse_args(["once", "--dry-run", "--json"])
    monkeypatch.setattr(cli.config_mod, "load", lambda: Config())

    class FakeEngine:
        def tick(self, **kwargs):
            assert kwargs["progress"] is False
            assert kwargs["quiet"] is True
            return 0, 1

    monkeypatch.setattr(
        cli,
        "_build",
        lambda cfg: ([object()], type("Namer", (), {"name": "heuristic"})(), None, FakeEngine()),
    )

    assert cli.cmd_run(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "renamed": 0,
        "candidates": 1,
        "changed": False,
    }


def test_daemon_reload_preserves_limit_override(tmp_path, monkeypatch):
    args = cli.build_parser().parse_args(["run", "--limit", "3"])
    seen = []
    monkeypatch.setattr(cli.config_mod, "load", lambda: Config(batch_size=25))
    monkeypatch.setattr(cli.config_mod, "ensure_default", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(cli.util, "daemon_lock_path", lambda: tmp_path / "daemon.lock")

    class FakeEngine:
        def run_forever(self, reload):
            reload()

    def fake_build(cfg):
        seen.append(cfg.batch_size)
        return [object()], type("Namer", (), {"name": "heuristic"})(), None, FakeEngine()

    monkeypatch.setattr(cli, "_build", fake_build)

    assert cli.cmd_run(args) == 0
    assert seen == [3, 3]
