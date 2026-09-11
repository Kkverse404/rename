import time

from rename.adapters.base import Adapter
from rename.config import Config, StructuredNamingConfig
from rename.engine import Engine
from rename.models import Message, Session
from rename.namers.base import Namer
from rename.namers.structured_codex import NamingDecision
from rename.session_naming import SessionNamingWorkflow
from rename.session_registry import SessionRegistry
from rename.state import StateStore


class FakeAdapter(Adapter):
    name = "fake"
    label = "Fake"

    def __init__(self, sessions, transcripts):
        self._sessions = sessions
        self._transcripts = transcripts
        self.writes: list[tuple[str, str]] = []

    def available(self):
        return True

    def discover(self, since):
        return [s for s in self._sessions if s.last_active >= since]

    def read_transcript(self, session):
        return self._transcripts[session.id]

    def set_title(self, session, title):
        self.writes.append((session.id, title))
        session.title = title


class FakeNamer(Namer):
    name = "fake"

    def __init__(self, title="Generated Title"):
        self._title = title

    def generate(self, messages, *, old_title=None, cwd=None, tool=None):
        return self._title


def _engine(tmp_path, adapter, namer, **cfg_kw):
    cfg = Config(idle_seconds=300, max_age_days=30, min_user_messages=1, **cfg_kw)
    state = StateStore(tmp_path / "state.json")
    # Tests exercise the default rename path, which in production is gated by
    # the first-run baseline. Backdate it so test sessions (which sit "10
    # minutes ago") count as new activity rather than historical backlog.
    state.set_baseline(0.0)
    return Engine(cfg, [adapter], namer, state)


def _idle_session(sid="s1", title="Old"):
    return Session("fake", sid, title, last_active=time.time() - 600)


TRANSCRIPT = {"s1": [Message("user", "Build the billing export feature")]}


def test_renames_idle_changed_session(tmp_path):
    adapter = FakeAdapter([_idle_session()], TRANSCRIPT)
    eng = _engine(tmp_path, adapter, FakeNamer("Billing export"))
    renamed, total = eng.tick()
    assert renamed == 1
    assert adapter.writes == [("s1", "Billing export")]


def test_skips_namer_prompt_title_without_calling_namer(tmp_path):
    """CLI namer side-effect sessions must not be fed back into the namer."""
    s = _idle_session(
        title="You name coding-assistant sessions. Read the conversation and reply"
    )
    adapter = FakeAdapter(
        [s],
        {"s1": [Message("user", "You name coding-assistant sessions. Read the conversation")]},
    )
    namer = FakeNamer("Should not run")
    calls = {"n": 0}
    orig = namer.generate

    def wrapped(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)

    namer.generate = wrapped
    eng = _engine(tmp_path, adapter, namer)
    renamed, _ = eng.tick()
    assert renamed == 0
    assert adapter.writes == []
    assert calls["n"] == 0


def test_skips_claude_tab_title_artifact_by_transcript(tmp_path):
    s = _idle_session(title="Untitled")
    adapter = FakeAdapter(
        [s],
        {
            "s1": [
                Message(
                    "user",
                    "Generate a concise tab title for this coding chat. Rules: - 2 to 5 words.",
                )
            ]
        },
    )
    namer = FakeNamer("Should not run")
    calls = {"n": 0}
    orig = namer.generate

    def wrapped(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)

    namer.generate = wrapped
    eng = _engine(tmp_path, adapter, namer)
    renamed, _ = eng.tick()
    assert renamed == 0
    assert adapter.writes == []
    assert calls["n"] == 0


def test_skips_active_session(tmp_path):
    s = Session("fake", "s1", "Old", last_active=time.time())  # just now
    adapter = FakeAdapter([s], TRANSCRIPT)
    eng = _engine(tmp_path, adapter, FakeNamer())
    renamed, _ = eng.tick()
    assert renamed == 0
    assert adapter.writes == []


def test_idempotent_no_double_rename(tmp_path):
    adapter = FakeAdapter([_idle_session()], TRANSCRIPT)
    eng = _engine(tmp_path, adapter, FakeNamer("Billing export"))
    eng.tick()
    eng.tick()  # nothing changed -> must not rename again
    assert len(adapter.writes) == 1


def test_respects_manual_edit_until_content_changes(tmp_path):
    s = _idle_session()
    adapter = FakeAdapter([s], TRANSCRIPT)
    eng = _engine(tmp_path, adapter, FakeNamer("Billing export"))
    eng.tick()  # -> "Billing export"
    # user renames by hand; content (and last_active) unchanged
    s.title = "My custom name"
    eng.tick()
    assert s.title == "My custom name"  # not overwritten
    assert len(adapter.writes) == 1


def test_renames_again_after_new_content(tmp_path):
    s = _idle_session()
    transcripts = {"s1": [Message("user", "first task")]}
    adapter = FakeAdapter([s], transcripts)
    eng = _engine(tmp_path, adapter, FakeNamer("First"))
    eng.tick()
    # new activity arrives
    transcripts["s1"] = [Message("user", "first task"), Message("user", "second task")]
    s.last_active = time.time() - 400  # advanced, still idle
    eng.namer = FakeNamer("Second")
    eng.tick()
    assert adapter.writes == [("s1", "First"), ("s1", "Second")]


def test_dry_run_writes_nothing(tmp_path):
    adapter = FakeAdapter([_idle_session()], TRANSCRIPT)
    namer = FakeNamer()
    calls = {"n": 0}
    original = namer.generate

    def counted(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    namer.generate = counted
    eng = _engine(tmp_path, adapter, namer, dry_run=True)
    renamed, _ = eng.tick()
    assert renamed == 0
    assert adapter.writes == []
    assert calls["n"] == 0


def test_plan_never_calls_legacy_namer(tmp_path):
    adapter = FakeAdapter([_idle_session()], TRANSCRIPT)
    namer = FakeNamer()
    calls = {"n": 0}
    original = namer.generate

    def counted(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    namer.generate = counted
    engine = _engine(tmp_path, adapter, namer)

    plans, _alive, _healthy = engine.plan()

    assert plans[0][1].action == "rename"
    assert plans[0][1].new_title is None
    assert calls["n"] == 0


def test_discover_failure_preserves_state(tmp_path):
    """If an adapter's discover() throws, its state must NOT be pruned —
    otherwise the next pass treats every session as new and could clobber
    titles the user edited by hand."""
    adapter = FakeAdapter([_idle_session()], TRANSCRIPT)
    eng = _engine(tmp_path, adapter, FakeNamer("Billing export"))
    eng.tick()
    assert eng.state.get("fake", "s1") is not None
    assert eng.state.get("fake", "s1").get("content_sig")

    def boom(_since):
        raise RuntimeError("database is locked")

    adapter.discover = boom
    eng.tick()  # discover fails this pass
    assert eng.state.get("fake", "s1") is not None  # state survived
    assert eng.state.get("fake", "s1").get("content_sig")


def test_skips_reread_when_no_activity(tmp_path):
    """Once evaluated, an unchanged session is skipped without re-reading."""
    s = _idle_session()
    reads = {"n": 0}
    transcripts = {"s1": [Message("user", "Build the billing export feature")]}

    class CountingAdapter(FakeAdapter):
        def read_transcript(self, session):
            reads["n"] += 1
            return transcripts[session.id]

    adapter = CountingAdapter([s], transcripts)
    eng = _engine(tmp_path, adapter, FakeNamer("Billing export"))
    eng.tick()
    eng.tick()
    eng.tick()
    assert reads["n"] == 1  # only the first pass read the transcript


def test_tick_limit_renames_most_recent_first(tmp_path):
    now = time.time()
    sessions = [
        Session("fake", f"s{i}", f"old{i}", last_active=now - 600 - i) for i in range(5)
    ]
    transcripts = {f"s{i}": [Message("user", f"build feature number {i}")] for i in range(5)}
    adapter = FakeAdapter(sessions, transcripts)
    eng = _engine(tmp_path, adapter, FakeNamer("Fresh"))
    renamed, total = eng.tick(limit=2)
    assert total == 5  # all are candidates
    assert renamed == 2  # but only 2 renamed this pass
    assert {sid for sid, _ in adapter.writes} == {"s0", "s1"}  # most recent first


def test_batch_size_caps_renames_per_pass(tmp_path):
    now = time.time()
    sessions = [
        Session("fake", f"s{i}", f"o{i}", last_active=now - 600 - i) for i in range(5)
    ]
    transcripts = {f"s{i}": [Message("user", f"task {i}")] for i in range(5)}
    adapter = FakeAdapter(sessions, transcripts)
    eng = _engine(tmp_path, adapter, FakeNamer("X"), batch_size=3)
    renamed, total = eng.tick()  # no explicit limit -> uses batch_size
    assert total == 5
    assert renamed == 3


def test_end_to_end_real_claude_adapter(tmp_path, monkeypatch):
    """Full path: real ClaudeCodeAdapter + real Engine + real file I/O."""
    import json
    import os

    from rename.adapters import claude_code
    from rename.namers.heuristic import HeuristicNamer

    projects = tmp_path / "projects"
    proj = projects / "-Users-me-proj"
    proj.mkdir(parents=True)
    sid = "22222222-2222-2222-2222-222222222222"
    f = proj / f"{sid}.jsonl"
    rows = [
        {
            "type": "last-prompt",
            "lastPrompt": "Implement CSV export for the reports page",
            "sessionId": sid,
        },
        {"type": "ai-title", "aiTitle": "Initial topic", "sessionId": sid},
    ]
    f.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )
    old = time.time() - 600  # idle 10 minutes
    os.utime(f, (old, old))

    monkeypatch.setattr(claude_code, "_projects_root", lambda: projects)
    eng = _engine(tmp_path, claude_code.ClaudeCodeAdapter(), HeuristicNamer())
    renamed, _ = eng.tick()

    assert renamed == 1
    new_title = claude_code._last_ai_title(f)
    assert new_title != "Initial topic"
    assert "csv" in new_title.lower()


# --------------------------------------------------------------------------- #
# Historical baseline: pre-install sessions must not be auto-renamed.
# --------------------------------------------------------------------------- #
def _historical_engine(tmp_path, adapter, namer):
    """Same as `_engine`, but with NO pre-set baseline — so the engine will
    record one on its first tick, and historical sessions get skipped."""
    cfg = Config(idle_seconds=300, max_age_days=30, min_user_messages=1)
    state = StateStore(tmp_path / "state.json")
    return Engine(cfg, [adapter], namer, state)


def test_first_tick_records_baseline_and_skips_old_sessions(tmp_path):
    """A fresh install must NOT retroactively rename pre-existing chats."""
    s = _idle_session()  # last_active = 10 minutes ago
    adapter = FakeAdapter([s], TRANSCRIPT)
    eng = _historical_engine(tmp_path, adapter, FakeNamer("Billing export"))
    renamed, _ = eng.tick()
    assert renamed == 0          # historical, skipped
    assert adapter.writes == []  # never renamed
    assert eng.state.baseline() is not None  # baseline recorded


def test_historical_flag_lets_user_opt_in(tmp_path):
    """The "Rename historical sessions" path renames the backlog on request."""
    s = _idle_session()
    adapter = FakeAdapter([s], TRANSCRIPT)
    eng = _historical_engine(tmp_path, adapter, FakeNamer("Billing export"))
    eng.tick()  # records baseline, skips
    renamed, _ = eng.tick(include_historical=True)
    assert renamed == 1
    assert adapter.writes == [("s1", "Billing export")]


def test_baseline_does_not_block_new_activity(tmp_path):
    """After baseline is set, a session that becomes active later still
    gets renamed — only the pre-existing backlog is held back."""
    s = _idle_session()
    adapter = FakeAdapter([s], TRANSCRIPT)
    eng = _historical_engine(tmp_path, adapter, FakeNamer("Billing export"))
    eng.tick()  # baseline set ≈ now, s is historical, skipped
    # Simulate the user touching the session right now — its last_active
    # moves well past the baseline.
    time.sleep(0.05)  # nudge clock so last_active > baseline unambiguously
    s.last_active = time.time()
    eng.cfg.idle_seconds = 0  # otherwise "active" gate keeps it out
    renamed, _ = eng.tick()
    assert renamed == 1
    assert adapter.writes == [("s1", "Billing export")]


class StructuredClassifier:
    def __init__(self):
        self.calls = 0

    def classify(self, messages, *, cwd, modules):
        self.calls += 1
        return NamingDecision(
            ready=True,
            module="repo",
            summary="实现稳定会话命名",
            reason_code="explicit_goal",
            evidence_message_ids=["user-1"],
            confidence=0.95,
        )


class StructuredWriter:
    def __init__(self, title="Old"):
        self.title = title
        self.writes = []

    def read_title(self, thread_id):
        return self.title

    def set_title(
        self,
        thread_id,
        title,
        *,
        expected_title,
        expected_updated_at=None,
        expected_status=None,
    ):
        assert self.title == expected_title
        self.writes.append((thread_id, title))
        self.title = title


class CodexFakeAdapter(FakeAdapter):
    name = "codex"
    label = "Codex"


def _structured_engine(tmp_path, *, mode="apply", dry_run=False):
    now = time.time()
    session = Session(
        "codex",
        "thread-1",
        "Old",
        last_active=now - 60,
        cwd=r"C:\work\repo",
        meta={"created_at_ms": int((now - 120) * 1000), "updated_at": now - 60},
    )
    adapter = CodexFakeAdapter(
        [session], {session.id: [Message("user", "实现稳定会话命名")]}
    )
    config = Config(
        idle_seconds=300,
        max_age_days=30,
        min_user_messages=1,
        dry_run=dry_run,
        structured_naming=StructuredNamingConfig(
            mode=mode, modules=("repo",), idle_seconds=30
        ),
    )
    state = StateStore(tmp_path / "state.json")
    state.set_baseline(0.0)
    registry = SessionRegistry(tmp_path / "registry.sqlite3")
    classifier = StructuredClassifier()
    writer = StructuredWriter()
    workflow = SessionNamingWorkflow(
        config.structured_naming,
        registry,
        classifier,
        writer,
        dry_run=dry_run,
    )
    engine = Engine(
        config,
        [adapter],
        FakeNamer("Legacy must not run"),
        state,
        session_naming=workflow,
    )
    return engine, session, registry, classifier, writer


def test_engine_routes_codex_apply_through_structured_workflow(tmp_path):
    engine, session, registry, classifier, writer = _structured_engine(tmp_path)

    renamed, total = engine.tick()

    record = registry.get("codex", session.id)
    assert (renamed, total) == (1, 1)
    assert writer.writes == [(session.id, "#1- 实现稳定会话命名")]
    assert classifier.calls == 1
    assert record is not None and record.status == "finalized"


def test_engine_quiet_apply_emits_no_success_log(tmp_path, capsys):
    engine, _session, _registry, _classifier, _writer = _structured_engine(tmp_path)

    assert engine.tick(quiet=True) == (1, 1)

    assert capsys.readouterr().out == ""


def test_engine_structured_preview_plan_and_tick_are_pure(tmp_path):
    engine, _session, registry, classifier, writer = _structured_engine(
        tmp_path, mode="preview"
    )

    plans, _alive, _healthy = engine.plan()
    renamed, total = engine.tick()

    assert plans[0][1].naming_status == "unregistered"
    assert (renamed, total) == (0, 0)
    assert not registry.path.exists()
    assert not engine.state.path.exists()
    assert classifier.calls == 0
    assert writer.writes == []


def test_engine_dry_run_structured_apply_is_pure(tmp_path):
    engine, _session, registry, classifier, writer = _structured_engine(
        tmp_path, dry_run=True
    )

    renamed, total = engine.tick()

    assert (renamed, total) == (0, 0)
    assert not registry.path.exists()
    assert classifier.calls == 0
    assert writer.writes == []


def test_structured_record_stays_protected_when_mode_is_off(tmp_path):
    engine, session, registry, classifier, writer = _structured_engine(tmp_path)
    engine.tick()
    session.title = writer.title
    engine.cfg.structured_naming.mode = "off"
    engine.session_naming = SessionNamingWorkflow(
        engine.cfg.structured_naming, registry, classifier, writer
    )

    renamed, total = engine.tick()

    assert (renamed, total) == (0, 0)
    assert len(writer.writes) == 1
    assert registry.get("codex", session.id).status == "finalized"


def test_session_filter_does_not_prune_unselected_legacy_state(tmp_path):
    now = time.time()
    sessions = [
        Session("fake", "s1", "One", last_active=now - 600),
        Session("fake", "s2", "Two", last_active=now - 601),
    ]
    transcripts = {
        "s1": [Message("user", "first")],
        "s2": [Message("user", "second")],
    }
    adapter = FakeAdapter(sessions, transcripts)
    engine = _engine(tmp_path, adapter, FakeNamer("Named"))
    engine.state.update("fake", "s1", title="One")
    engine.state.update("fake", "s2", title="Two")

    engine.tick(session_filter={"s1"})

    assert engine.state.get("fake", "s2") == {"title": "Two"}


def test_daemon_reloads_runtime_before_the_next_pass(monkeypatch):
    passes = []

    class Loop:
        def __init__(self, dry_run):
            self.cfg = Config(dry_run=dry_run, poll_seconds=1)
            self.namer = FakeNamer("unused")
            self.adapters = []

        def tick(self):
            passes.append(self.cfg.dry_run)
            return 0, 0

    first = Loop(False)
    second = Loop(True)
    monkeypatch.setattr("rename.engine.time.sleep", lambda _seconds: None)

    Engine.run_forever(
        first,
        stop=lambda: len(passes) == 2,
        reload=lambda: second,
    )

    assert passes == [False, True]
