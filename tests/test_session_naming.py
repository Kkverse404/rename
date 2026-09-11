from __future__ import annotations

import time

import pytest

from rename.adapters.codex_writer import CodexProcessError
from rename.config import StructuredNamingConfig
from rename.models import Message, Session
from rename.namers.structured_codex import NamingDecision
from rename.session_naming import SessionNamingWorkflow, is_historical_session
from rename.session_registry import InvalidTransitionError, SessionRegistry


class FakeClassifier:
    def __init__(self, decision: NamingDecision | None = None, error: Exception | None = None):
        self.decision = decision
        self.error = error
        self.calls = 0

    def classify(self, messages, *, cwd, modules):
        self.calls += 1
        if self.error:
            raise self.error
        assert self.decision is not None
        return self.decision


class FakeWriter:
    def __init__(self, title="Old", error: Exception | None = None):
        self.title = title
        self.error = error
        self.reads = 0
        self.writes: list[tuple[str, str, str | None]] = []

    def read_title(self, thread_id):
        self.reads += 1
        if self.error:
            raise self.error
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
        if self.error:
            raise self.error
        self.writes.append((thread_id, title, expected_title))
        self.title = title


def _ready(**changes):
    values = dict(
        ready=True,
        module="repo",
        summary="修复并发标题写入",
        reason_code="explicit_goal",
        evidence_message_ids=["user-1"],
        confidence=0.95,
    )
    values.update(changes)
    return NamingDecision(**values)


def _session(*, title="Old", last_active=None, created_at_ms=2_000_000):
    return Session(
        "codex",
        "thread-1",
        title,
        last_active=time.time() - 120 if last_active is None else last_active,
        cwd=r"C:\work\repo",
        meta={"created_at_ms": created_at_ms, "updated_at": 1234},
    )


def _workflow(tmp_path, *, mode="apply", decision=None, writer=None, dry_run=False):
    config = StructuredNamingConfig(
        mode=mode,
        modules=("repo",),
        confidence_threshold=0.8,
        idle_seconds=30,
    )
    registry = SessionRegistry(tmp_path / "registry.sqlite3")
    classifier = FakeClassifier(decision or _ready())
    writer = writer or FakeWriter()
    workflow = SessionNamingWorkflow(
        config, registry, classifier, writer, dry_run=dry_run
    )
    return workflow, registry, classifier, writer


def test_preview_is_pure_read_without_allocation_or_model(tmp_path):
    workflow, registry, classifier, writer = _workflow(tmp_path, mode="preview")

    prepared = workflow.prepare(_session(), time.time(), historical=False)

    assert prepared.handled and not prepared.candidate
    assert not registry.path.exists()
    assert classifier.calls == 0
    assert writer.writes == []


def test_global_dry_run_is_pure_even_when_mode_is_apply(tmp_path):
    workflow, registry, classifier, writer = _workflow(tmp_path, dry_run=True)

    workflow.prepare(_session(), time.time(), historical=False)

    assert not registry.path.exists()
    assert classifier.calls == 0
    assert writer.writes == []


def test_active_session_allocates_id_but_does_not_classify(tmp_path):
    workflow, registry, classifier, _writer = _workflow(tmp_path)
    session = _session(last_active=time.time())

    prepared = workflow.prepare(session, time.time(), historical=False)

    record = registry.get("codex", session.id)
    assert prepared.handled and not prepared.candidate
    assert record is not None and record.display_id == 1 and record.status == "pending"
    assert classifier.calls == 0


def test_ready_decision_stages_writes_and_finalizes(tmp_path):
    workflow, registry, classifier, writer = _workflow(tmp_path)
    session = _session()
    prepared = workflow.prepare(session, time.time(), historical=False)

    result = workflow.process(session, lambda _session: [Message("user", "修复并发写入")])

    record = registry.get("codex", session.id)
    assert prepared.candidate
    assert result.renamed and result.title == "#1-repo 修复并发标题写入"
    assert writer.writes == [(session.id, result.title, "Old")]
    assert record is not None and record.status == "finalized"
    assert [op.operation for op in registry.operations("codex", session.id)] == [
        "allocated",
        "write_staged",
        "finalized",
    ]
    assert classifier.calls == 1


def test_codex_fallback_display_title_is_separate_from_native_name(tmp_path):
    writer = FakeWriter(title=None)
    workflow, registry, _classifier, _writer = _workflow(tmp_path, writer=writer)
    session = _session(title="Generated fallback")
    session.meta["native_name"] = None
    workflow.prepare(session, time.time(), historical=False)

    result = workflow.process(session, lambda _session: [Message("user", "do it")])

    record = registry.get("codex", session.id)
    assert result.status == "finalized"
    assert writer.writes == [(session.id, result.title, None)]
    assert record is not None
    assert record.expected_title is None
    assert record.original_title == "Generated fallback"


def test_unclear_decision_is_cached_without_write(tmp_path):
    unclear = _ready(
        ready=False,
        module=None,
        summary=None,
        reason_code="ambiguous",
        evidence_message_ids=[],
        confidence=0.4,
    )
    workflow, registry, classifier, writer = _workflow(tmp_path, decision=unclear)
    session = _session()
    workflow.prepare(session, time.time(), historical=False)

    first = workflow.process(session, lambda _session: [Message("user", "看看这个")])
    second = workflow.prepare(session, time.time(), historical=False)

    assert first.status == "pending"
    assert not second.candidate
    assert classifier.calls == 1
    assert writer.writes == []
    assert registry.get("codex", session.id).decision["reason_code"] == "ambiguous"


def test_ready_requires_real_evidence_and_allowed_module(tmp_path):
    decision = _ready(module="other", evidence_message_ids=["user-99"])
    workflow, registry, _classifier, writer = _workflow(tmp_path, decision=decision)
    session = _session()
    workflow.prepare(session, time.time(), historical=False)

    result = workflow.process(session, lambda _session: [Message("user", "do it")])

    assert result.status == "pending"
    assert "evidence" in result.reason
    assert writer.writes == []
    assert registry.get("codex", session.id).status == "pending"


def test_writer_failure_enters_recovery_then_restart_finalizes(tmp_path):
    writer = FakeWriter(error=CodexProcessError("app-server stopped"))
    workflow, registry, _classifier, _writer = _workflow(tmp_path, writer=writer)
    session = _session()
    workflow.prepare(session, time.time(), historical=False)

    failed = workflow.process(session, lambda _session: [Message("user", "do it")])
    desired = registry.get("codex", session.id).desired_title
    writer.error = None
    writer.title = desired
    recovered = workflow.process(session, lambda _session: [])

    assert failed.status == "recovery"
    assert recovered.status == "finalized"
    assert not recovered.renamed
    assert writer.writes == []


def test_external_edit_is_permanently_protected_until_reopen(tmp_path):
    workflow, registry, classifier, writer = _workflow(tmp_path)
    session = _session()
    workflow.prepare(session, time.time(), historical=False)
    result = workflow.process(session, lambda _session: [Message("user", "do it")])
    session.title = "My title"
    writer.title = "My title"

    prepared = workflow.prepare(session, time.time(), historical=False)
    override = workflow.process(session, lambda _session: [])

    assert result.status == "finalized" and prepared.candidate
    assert override.status == "manual_override"
    assert registry.get("codex", session.id).display_id == 1
    assert classifier.calls == 1
    assert len(writer.writes) == 1


def test_mode_off_still_handles_registered_session(tmp_path):
    workflow, registry, _classifier, _writer = _workflow(tmp_path)
    session = _session()
    workflow.prepare(session, time.time(), historical=False)
    off = SessionNamingWorkflow(
        StructuredNamingConfig(mode="off"), registry, FakeClassifier(_ready()), FakeWriter()
    )

    assert off.handles(session)
    assert not off.prepare(session, time.time(), historical=False).candidate
    assert not off.handles(_session(title="Other").__class__(
        "codex", "thread-2", "Other", session.last_active
    ))


def test_historical_enrollment_uses_creation_time_and_fails_closed_without_it():
    baseline = 2_000.0
    old_reactivated = _session(last_active=3_000.0, created_at_ms=1_000_000)
    unknown = _session(last_active=3_000.0, created_at_ms=None)

    assert is_historical_session(old_reactivated, baseline, include_historical=False)
    assert is_historical_session(unknown, baseline, include_historical=False)
    assert not is_historical_session(old_reactivated, baseline, include_historical=True)


def test_ready_rejects_evidence_trimmed_from_classifier_input(tmp_path):
    workflow, registry, _classifier, writer = _workflow(
        tmp_path, decision=_ready(evidence_message_ids=["user-1"])
    )
    workflow.config.max_messages = 2
    session = _session()
    workflow.prepare(session, time.time(), historical=False)
    messages = [Message("user", "old evidence")]
    messages.extend(Message("assistant", f"reply {index}") for index in range(3))
    messages.append(Message("user", "current goal"))

    result = workflow.process(session, lambda _session: messages)

    assert result.status == "pending"
    assert "evidence" in result.reason
    assert writer.writes == []
    assert registry.get("codex", session.id).status == "pending"


def test_unclear_race_cannot_erase_an_already_staged_write(tmp_path):
    workflow, registry, _classifier, _writer = _workflow(tmp_path)
    session = _session()
    original = registry.ensure("codex", session.id)
    registry.stage_write(
        "codex",
        session.id,
        expected="Old",
        original="Old",
        desired="#1-repo Ready",
        module="repo",
        summary="Ready",
        decision={"ready": True},
    )

    with pytest.raises(InvalidTransitionError):
        registry.record_unclear(
            "codex",
            session.id,
            decision={"ready": False},
            input_sig="stale",
            last_evaluated_active=1,
        )
    current = registry.get("codex", session.id)
    assert current.display_id == original.display_id
    assert current.status == "write_pending"
    assert current.desired_title == "#1-repo Ready"


def test_stale_discovery_snapshot_does_not_create_false_manual_override(tmp_path):
    workflow, registry, _classifier, writer = _workflow(tmp_path)
    session = _session()
    workflow.prepare(session, time.time(), historical=False)
    result = workflow.process(session, lambda _session: [Message("user", "do it")])
    assert result.title == writer.title
    session.title = "Old"  # a second worker's snapshot from before the write

    prepared = workflow.prepare(session, time.time(), historical=False)
    result = workflow.process(session, lambda _session: [])

    assert prepared.candidate
    assert result.status == "finalized"
    assert registry.get("codex", session.id).status == "finalized"


def test_restart_completes_rollback_after_native_write_half_success(tmp_path):
    writer = FakeWriter(title=None)
    workflow, registry, classifier, writer = _workflow(tmp_path, writer=writer)
    session = _session(title="Old")
    session.meta["native_name"] = None
    workflow.prepare(session, time.time(), historical=False)
    finalized = workflow.process(
        session, lambda _session: [Message("user", "do it")]
    )
    assert finalized.status == "finalized"
    registry.stage_rollback("codex", session.id)
    writer.title = "Old"
    session.title = "Old"
    session.meta["native_name"] = "Old"

    prepared = workflow.prepare(session, time.time(), historical=False)
    recovered = workflow.process(session, lambda _session: [])

    assert prepared.candidate
    assert recovered.status == "rolled_back"
    assert classifier.calls == 1
    assert registry.get("codex", session.id).display_id == 1


def test_restart_retries_rollback_staged_before_native_write(tmp_path):
    workflow, registry, classifier, writer = _workflow(tmp_path)
    session = _session()
    workflow.prepare(session, time.time(), historical=False)
    finalized = workflow.process(
        session, lambda _session: [Message("user", "do it")]
    )
    registry.stage_rollback("codex", session.id)
    session.title = finalized.title
    writer.title = finalized.title

    prepared = workflow.prepare(session, time.time(), historical=False)
    recovered = workflow.process(session, lambda _session: [])

    assert prepared.candidate
    assert recovered.status == "rolled_back"
    assert recovered.renamed
    assert writer.writes[-1] == (session.id, "Old", finalized.title)
    assert classifier.calls == 1


def test_same_title_rollback_intent_is_not_hidden_by_finalized_fast_path(tmp_path):
    workflow, registry, classifier, writer = _workflow(tmp_path)
    session = _session(title="#1-repo 修复并发标题写入")
    writer.title = session.title
    workflow.prepare(session, time.time(), historical=False)
    finalized = workflow.process(
        session, lambda _session: [Message("user", "do it")]
    )
    assert finalized.title == session.title
    registry.stage_rollback("codex", session.id)

    prepared = workflow.prepare(session, time.time(), historical=False)
    recovered = workflow.process(session, lambda _session: [])

    assert prepared.candidate
    assert recovered.status == "rolled_back"
    assert classifier.calls == 1
