import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from rename.session_registry import (
    InvalidTransitionError,
    RegistryCorruptError,
    SessionRegistry,
)


def test_reads_of_missing_registry_are_side_effect_free(tmp_path):
    path = tmp_path / "state" / "sessions.sqlite3"
    registry = SessionRegistry(path)

    assert registry.get("codex", "missing") is None
    assert registry.list() == ()
    assert registry.counts().total == 0
    assert not path.exists()


def test_ensure_allocates_permanent_identity_and_persists(tmp_path):
    path = tmp_path / "registry.sqlite3"
    registry = SessionRegistry(path)

    first = registry.ensure("codex", "native-1")
    repeated = registry.ensure("codex", "native-1")
    second = registry.ensure("codex", "native-2")

    assert first == repeated
    assert first.display_id == 1
    assert second.display_id == 2
    assert first.status == "pending"
    assert SessionRegistry(path).get("codex", "native-1") == first
    assert registry.list() == (first, second)
    assert registry.counts()["pending"] == 2
    assert registry.counts().total == 2


def test_records_unclear_evaluation_without_staging_a_write(tmp_path):
    registry = SessionRegistry(tmp_path / "registry.sqlite3")
    registry.ensure("codex", "native-1")

    record = registry.record_unclear(
        "codex",
        "native-1",
        decision={"ready": False, "reason_code": "unclear_goal", "evidence": ["u1"]},
        input_sig="sha256:one",
        last_evaluated_active=123.5,
    )

    assert record.status == "pending"
    assert record.input_sig == "sha256:one"
    assert record.last_evaluated_active == 123.5
    assert record.decision["reason_code"] == "unclear_goal"
    with pytest.raises(TypeError):
        record.decision["reason_code"] = "changed"


def test_stage_then_finalize_keeps_write_intent_and_logs_operations(tmp_path):
    registry = SessionRegistry(tmp_path / "registry.sqlite3")
    registry.ensure("codex", "native-1")

    staged = registry.stage_write(
        "codex",
        "native-1",
        expected="Old title",
        original="Old title",
        desired="#1-Core 修复并发分配",
        module="Core",
        summary="修复并发分配",
        decision={"ready": True, "reason_code": "explicit_goal"},
        input_sig="sha256:two",
        last_evaluated_active=456.0,
    )
    finalized = registry.finalize("codex", "native-1")

    assert staged.status == "write_pending"
    assert staged.expected_title == "Old title"
    assert staged.original_title == "Old title"
    assert staged.desired_title == "#1-Core 修复并发分配"
    assert finalized.status == "finalized"
    assert finalized.finalized_at is not None
    assert finalized.desired_title == staged.desired_title
    assert [op.operation for op in registry.operations("codex", "native-1")] == [
        "allocated",
        "write_staged",
        "finalized",
    ]


def test_resolution_write_preserves_original_title_and_summary(tmp_path):
    registry = SessionRegistry(tmp_path / "registry.sqlite3")
    registry.ensure("codex", "native-1")
    registry.stage_write(
        "codex",
        "native-1",
        expected="Old title",
        original="Old title",
        desired="#1- 修复并发分配",
        module="Core",
        summary="修复并发分配",
        decision={"ready": True, "resolved": False},
        input_sig="sha256:one",
        last_evaluated_active=123.0,
    )
    registry.finalize("codex", "native-1")

    staged = registry.stage_resolution(
        "codex",
        "native-1",
        desired="#1- 修复并发分配（已解决）",
        decision={"ready": True, "resolved": True},
        input_sig="sha256:two",
        last_evaluated_active=456.0,
    )
    finalized = registry.finalize("codex", "native-1")

    assert staged.status == "write_pending"
    assert staged.expected_title == "#1- 修复并发分配"
    assert staged.original_title == "Old title"
    assert staged.summary == "修复并发分配"
    assert finalized.desired_title == "#1- 修复并发分配（已解决）"
    assert [op.operation for op in registry.operations("codex", "native-1")] == [
        "allocated",
        "write_staged",
        "finalized",
        "resolution_write_staged",
        "finalized",
    ]


def test_invalid_transition_fails_closed(tmp_path):
    registry = SessionRegistry(tmp_path / "registry.sqlite3")
    registry.ensure("codex", "native-1")

    with pytest.raises(InvalidTransitionError):
        registry.finalize("codex", "native-1")
    assert registry.get("codex", "native-1").status == "pending"


def test_manual_override_and_explicit_reopen_preserve_display_id(tmp_path):
    registry = SessionRegistry(tmp_path / "registry.sqlite3")
    original = registry.ensure("codex", "native-1")
    registry.stage_write(
        "codex",
        "native-1",
        expected="Old",
        original="Old",
        desired="#1-Core Work",
        module="Core",
        summary="Work",
        decision={"ready": True},
    )
    registry.finalize("codex", "native-1")

    overridden = registry.manual_override("codex", "native-1", observed="My title")
    reopened = registry.reopen("codex", "native-1")

    assert overridden.status == "manual_override"
    assert overridden.observed_title == "My title"
    assert reopened.status == "pending"
    assert reopened.display_id == original.display_id
    assert reopened.desired_title is None
    assert reopened.input_sig is None


def test_reopen_pending_clears_cached_decision_for_explicit_retry(tmp_path):
    registry = SessionRegistry(tmp_path / "registry.sqlite3")
    original = registry.ensure("codex", "native-1")
    registry.record_unclear(
        "codex",
        "native-1",
        decision={"ready": False, "reason_code": "classifier_error"},
        input_sig="same-input",
        last_evaluated_active=123.0,
    )

    reopened = registry.reopen("codex", "native-1")

    assert reopened.status == "pending"
    assert reopened.display_id == original.display_id
    assert reopened.decision is None
    assert reopened.input_sig is None
    assert reopened.last_evaluated_active is None


def test_recovery_can_resume_staged_write_without_reallocation(tmp_path):
    registry = SessionRegistry(tmp_path / "registry.sqlite3")
    original = registry.ensure("codex", "native-1")
    registry.stage_write(
        "codex",
        "native-1",
        expected="Old",
        original="Old",
        desired="#1-Core Work",
        module="Core",
        summary="Work",
        decision={"ready": True},
    )

    recovering = registry.recovery(
        "codex", "native-1", observed="Old", reason="native outcome unknown"
    )
    finalized = registry.finalize("codex", "native-1", observed="#1-Core Work")

    assert recovering.status == "recovery"
    assert recovering.recovery_reason == "native outcome unknown"
    assert finalized.status == "finalized"
    assert finalized.display_id == original.display_id


def test_rollback_and_reopen_preserve_identity(tmp_path):
    registry = SessionRegistry(tmp_path / "registry.sqlite3")
    original = registry.ensure("codex", "native-1")
    registry.stage_write(
        "codex",
        "native-1",
        expected="Old",
        original="Old",
        desired="#1-Core Work",
        module="Core",
        summary="Work",
        decision={"ready": True},
    )
    registry.finalize("codex", "native-1")

    rolled_back = registry.rolled_back("codex", "native-1", observed="Old")
    reopened = registry.reopen("codex", "native-1")

    assert rolled_back.status == "rolled_back"
    assert reopened.status == "pending"
    assert reopened.display_id == original.display_id


def test_rollback_intent_is_persisted_and_idempotent_before_completion(tmp_path):
    registry = SessionRegistry(tmp_path / "registry.sqlite3")
    registry.ensure("codex", "native-1")
    registry.stage_write(
        "codex",
        "native-1",
        expected="Old",
        original="Old",
        desired="#1-Core Work",
        module="Core",
        summary="Work",
        decision={"ready": True},
    )
    registry.finalize("codex", "native-1", observed="#1-Core Work")

    staged = registry.stage_rollback("codex", "native-1")
    again = registry.stage_rollback("codex", "native-1")
    completed = registry.rolled_back("codex", "native-1", observed="Old")

    assert staged.status == again.status == "finalized"
    assert staged.recovery_reason == "rollback pending"
    assert completed.status == "rolled_back"
    assert completed.recovery_reason is None
    assert [
        operation.operation
        for operation in registry.operations("codex", "native-1")
    ].count("rollback_staged") == 1


def test_registry_and_operation_rows_cannot_be_deleted_or_rewritten(tmp_path):
    path = tmp_path / "registry.sqlite3"
    registry = SessionRegistry(path)
    registry.ensure("codex", "native-1")

    connection = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM registry WHERE display_id = 1")
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE operations SET operation = 'tampered'")
    connection.rollback()
    connection.close()

    assert registry.ensure("codex", "native-2").display_id == 2


def test_corrupt_database_fails_closed_without_replacing_it(tmp_path):
    path = tmp_path / "registry.sqlite3"
    original = b"not a sqlite database"
    path.write_bytes(original)
    registry = SessionRegistry(path)

    with pytest.raises(RegistryCorruptError):
        registry.get("codex", "native-1")
    with pytest.raises(RegistryCorruptError):
        registry.ensure("codex", "native-1")
    assert path.read_bytes() == original


def test_concurrent_connections_allocate_one_id_for_same_native_session(tmp_path):
    path = tmp_path / "registry.sqlite3"

    def allocate(_):
        return SessionRegistry(path).ensure("codex", "shared").display_id

    with ThreadPoolExecutor(max_workers=12) as pool:
        ids = list(pool.map(allocate, range(48)))

    assert set(ids) == {1}
    registry = SessionRegistry(path)
    assert len(registry.list()) == 1
    assert len(registry.operations("codex", "shared")) == 1


def test_concurrent_connections_never_duplicate_ids_for_distinct_sessions(tmp_path):
    path = tmp_path / "registry.sqlite3"

    def allocate(index):
        return SessionRegistry(path).ensure("codex", f"native-{index}").display_id

    with ThreadPoolExecutor(max_workers=12) as pool:
        ids = list(pool.map(allocate, range(48)))

    assert len(ids) == len(set(ids)) == 48
    assert sorted(ids) == list(range(1, 49))
    assert SessionRegistry(path).counts().total == 48
