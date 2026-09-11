"""Policy and recovery workflow for permanent Codex session names.

This is the single public seam between discovery and the structured classifier,
registry, and native writer.  Preview methods are pure reads; ``prepare`` and
``process`` are called only by an apply pass.
"""

from __future__ import annotations

import dataclasses
import math
import ntpath
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from . import util
from .adapters.codex_writer import CodexConflictError, CodexWriterError
from .config import StructuredNamingConfig
from .models import Message, RenamePlan, Session
from .namers.structured_codex import (
    NamingClassifier,
    NamingDecision,
    bounded_evidence_ids,
)
from .session_registry import RegistryError, SessionRecord, SessionRegistry

_READY_REASONS = {"explicit_goal", "context_converged"}
_UNCLEAR_REASONS = {"ambiguous", "insufficient_context"}
_PREFIX_RE = re.compile(r"^\s*#\d+-")
_MAX_TITLE_CHARS = 120


class NativeTitleWriter(Protocol):
    def read_title(self, thread_id: str) -> str | None: ...

    def set_title(
        self,
        thread_id: str,
        title: str,
        *,
        expected_title: str | None,
        expected_updated_at: int | float | None = None,
        expected_status: object | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkflowResult:
    handled: bool
    candidate: bool = False
    renamed: bool = False
    display_id: int | None = None
    status: str | None = None
    reason: str = ""
    title: str | None = None


def _module_from_cwd(cwd: str | None) -> str | None:
    if not cwd:
        return None
    value = ntpath.basename(cwd.rstrip("\\/"))
    return value.strip() or None


def _allowed_modules(configured: tuple[str, ...], cwd: str | None) -> tuple[str, ...]:
    if configured:
        return configured
    derived = _module_from_cwd(cwd)
    return (derived,) if derived else ()


def _decision_data(decision: NamingDecision) -> dict[str, object]:
    return dataclasses.asdict(decision)


def validate_naming_decision(
    decision: NamingDecision,
    *,
    messages: list[Message],
    modules: tuple[str, ...],
    confidence_threshold: float,
    max_messages: int,
    max_input_chars: int,
) -> tuple[bool, str | None]:
    """Validate authorization fields independently of the model's schema."""

    if not math.isfinite(decision.confidence) or not 0 <= decision.confidence <= 1:
        return False, "confidence is outside 0..1"
    evidence = decision.evidence_message_ids
    if len(evidence) != len(set(evidence)):
        return False, "evidence IDs are duplicated"
    available = set(
        bounded_evidence_ids(
            messages,
            max_messages=max_messages,
            max_chars=max_input_chars,
        )
    )
    if any(item not in available for item in evidence):
        return False, "evidence does not identify an available user message"

    if not decision.ready:
        if decision.module is not None or decision.summary is not None:
            return False, "unclear decisions must not supply a module or summary"
        if decision.reason_code not in _UNCLEAR_REASONS:
            return False, "unclear decision has a ready-only reason"
        return False, None

    if decision.reason_code not in _READY_REASONS:
        return False, "ready decision has an unclear-only reason"
    if decision.confidence < confidence_threshold:
        return False, "confidence is below the configured threshold"
    if not modules or decision.module not in modules:
        return False, "module is not in the effective allowlist"
    if not evidence:
        return False, "ready decision has no user evidence"
    if not isinstance(decision.summary, str):
        return False, "ready decision has no summary"
    summary = decision.summary.strip()
    if not summary:
        return False, "summary is empty"
    if "\n" in summary or "\r" in summary:
        return False, "summary is not one line"
    if _PREFIX_RE.match(summary):
        return False, "summary contains a managed title prefix"
    return True, None


def _format_title(display_id: int, module: str, summary: str) -> tuple[str, str]:
    normalized = " ".join(summary.split())
    prefix = f"#{display_id}-{module} "
    remaining = _MAX_TITLE_CHARS - len(prefix)
    if remaining < 1:
        raise ValueError("display ID and module leave no room for a summary")
    normalized = normalized[:remaining].rstrip()
    if not normalized:
        raise ValueError("summary is empty after title bounding")
    return prefix + normalized, normalized


class SessionNamingWorkflow:
    """Coordinate one-time structured names without exposing internal stages."""

    def __init__(
        self,
        config: StructuredNamingConfig,
        registry: SessionRegistry,
        classifier: NamingClassifier,
        writer: NativeTitleWriter,
        *,
        dry_run: bool = False,
    ) -> None:
        self.config = config
        self.registry = registry
        self.classifier = classifier
        self.writer = writer
        self.read_only = dry_run or config.mode == "preview"

    def _get(self, session: Session) -> SessionRecord | None:
        return self.registry.get(session.tool, session.id)

    def _rollback_is_staged(self, session: Session) -> bool:
        operations = self.registry.operations(session.tool, session.id)
        return bool(operations and operations[-1].operation == "rollback_staged")

    def handles(self, session: Session) -> bool:
        if session.tool != "codex":
            return False
        if self.config.enabled:
            return True
        try:
            return self._get(session) is not None
        except RegistryError:
            # A broken registry must fail closed instead of exposing managed
            # titles to the legacy repeat-renaming path.
            return self.registry.path.exists()

    def preview(self, session: Session, now_ts: float, *, historical: bool) -> RenamePlan:
        """Describe state without allocating, reading a transcript, or calling a model."""

        try:
            record = self._get(session)
        except RegistryError as exc:
            return RenamePlan(
                session,
                "skip",
                reason=f"structured registry unavailable: {exc}",
                naming_status="error",
            )
        if record is None:
            if not self.config.enabled:
                return RenamePlan(session, "skip", reason="unmanaged")
            if historical:
                reason = "pre-existing (use --historical to opt in)"
            elif self.read_only:
                reason = "structured preview: not registered; no ID or model call"
            elif session.idle_seconds(now_ts) < self.config.idle_seconds:
                reason = "structured apply: awaiting idle threshold"
            else:
                reason = "structured apply: pending registration and classification"
            return RenamePlan(session, "skip", reason=reason, naming_status="unregistered")

        reason = {
            "pending": "structured: awaiting a clear task",
            "write_pending": "structured: native write recovery pending",
            "recovery": "structured: native state requires recovery",
            "finalized": "structured: finalized and protected",
            "manual_override": "structured: user title protected until reopen",
            "rolled_back": "structured: rolled back and protected until reopen",
        }.get(record.status, f"structured: {record.status}")
        if record.status == "finalized" and session.native_title != record.desired_title:
            reason = "structured: external title change will be protected"
        return RenamePlan(
            session,
            "skip",
            reason=reason,
            display_id=record.display_id,
            naming_status=record.status,
        )

    def prepare(self, session: Session, now_ts: float, *, historical: bool) -> WorkflowResult:
        """Allocate an eligible identity and say whether processing is needed."""

        if not self.handles(session):
            return WorkflowResult(False)
        if self.read_only or self.config.mode != "apply":
            plan = self.preview(session, now_ts, historical=historical)
            return WorkflowResult(
                True,
                display_id=plan.display_id,
                status=plan.naming_status,
                reason=plan.reason,
            )
        record = self._get(session)
        if record is None:
            if historical:
                return WorkflowResult(True, status="unregistered", reason="pre-existing")
            record = self.registry.ensure(session.tool, session.id)

        if record.status in {"manual_override", "rolled_back"}:
            return WorkflowResult(
                True,
                display_id=record.display_id,
                status=record.status,
                reason="protected until explicit reopen",
            )
        if record.status == "finalized" and self._rollback_is_staged(session):
            return WorkflowResult(
                True,
                True,
                display_id=record.display_id,
                status=record.status,
                reason="staged rollback requires recovery",
            )
        if record.status == "finalized" and session.native_title == record.desired_title:
            return WorkflowResult(
                True,
                display_id=record.display_id,
                status=record.status,
                reason="finalized and protected",
            )
        if record.status in {"write_pending", "recovery", "finalized"}:
            return WorkflowResult(
                True, True, display_id=record.display_id, status=record.status
            )
        if session.idle_seconds(now_ts) < self.config.idle_seconds:
            return WorkflowResult(
                True,
                display_id=record.display_id,
                status=record.status,
                reason="awaiting idle threshold",
            )
        if record.last_evaluated_active == session.last_active:
            return WorkflowResult(
                True,
                display_id=record.display_id,
                status=record.status,
                reason="no activity since last classification",
            )
        return WorkflowResult(True, True, display_id=record.display_id, status=record.status)

    def _writer_args(self, session: Session) -> dict[str, object]:
        return {
            "expected_updated_at": session.meta.get("updated_at"),
            "expected_status": session.meta.get("status"),
        }

    def _recover_write(self, session: Session, record: SessionRecord) -> WorkflowResult:
        desired = record.desired_title
        if not desired:
            return WorkflowResult(
                True,
                display_id=record.display_id,
                status=record.status,
                reason="recovery record has no staged title",
            )
        try:
            observed = self.writer.read_title(session.id)
        except CodexWriterError as exc:
            if record.status == "write_pending":
                record = self.registry.recovery(
                    session.tool, session.id, observed=session.native_title, reason=str(exc)
                )
            return WorkflowResult(
                True,
                True,
                display_id=record.display_id,
                status=record.status,
                reason=f"native read failed: {exc}",
            )
        if observed == desired:
            record = self.registry.finalize(session.tool, session.id, observed=observed)
            return WorkflowResult(
                True,
                display_id=record.display_id,
                status=record.status,
                reason="recovered completed native write",
                title=desired,
            )
        if observed != record.expected_title:
            record = self.registry.manual_override(
                session.tool,
                session.id,
                observed=observed or "",
                reason="native title changed during staged write",
            )
            return WorkflowResult(
                True,
                display_id=record.display_id,
                status=record.status,
                reason="external title protected",
            )
        try:
            self.writer.set_title(
                session.id, desired, expected_title=observed, **self._writer_args(session)
            )
        except CodexConflictError as exc:
            return self._resolve_conflict(session, record, exc.actual_title)
        except CodexWriterError as exc:
            if record.status == "write_pending":
                record = self.registry.recovery(
                    session.tool, session.id, observed=observed, reason=str(exc)
                )
            return WorkflowResult(
                True,
                True,
                display_id=record.display_id,
                status=record.status,
                reason=f"native write failed: {exc}",
            )
        record = self.registry.finalize(session.tool, session.id, observed=desired)
        return WorkflowResult(
            True,
            renamed=True,
            display_id=record.display_id,
            status=record.status,
            reason="staged title written and verified",
            title=desired,
        )

    def _recover_rollback(
        self, session: Session, record: SessionRecord
    ) -> WorkflowResult:
        target = record.original_title
        managed = record.desired_title
        if target is None or managed is None:
            return WorkflowResult(
                True,
                display_id=record.display_id,
                status=record.status,
                reason="rollback record is missing a staged title",
            )
        try:
            observed = self.writer.read_title(session.id)
        except CodexWriterError as exc:
            return WorkflowResult(
                True,
                True,
                display_id=record.display_id,
                status=record.status,
                reason=f"rollback native read failed: {exc}",
            )
        if observed == target:
            completed = self.registry.rolled_back(
                session.tool, session.id, observed=observed
            )
            return WorkflowResult(
                True,
                display_id=completed.display_id,
                status=completed.status,
                reason="recovered completed rollback",
                title=target,
            )
        if observed != managed:
            overridden = self.registry.manual_override(
                session.tool,
                session.id,
                observed=observed or "",
                reason="native title changed during staged rollback",
            )
            return WorkflowResult(
                True,
                display_id=overridden.display_id,
                status=overridden.status,
                reason="external title protected",
            )
        try:
            self.writer.set_title(
                session.id,
                target,
                expected_title=observed,
                **self._writer_args(session),
            )
        except CodexConflictError as exc:
            if exc.actual_title == target:
                completed = self.registry.rolled_back(
                    session.tool, session.id, observed=target
                )
                return WorkflowResult(
                    True,
                    display_id=completed.display_id,
                    status=completed.status,
                    reason="recovered concurrent rollback",
                    title=target,
                )
            if exc.actual_title != managed:
                overridden = self.registry.manual_override(
                    session.tool,
                    session.id,
                    observed=exc.actual_title or "",
                    reason="native title changed before rollback compare-and-set",
                )
                return WorkflowResult(
                    True,
                    display_id=overridden.display_id,
                    status=overridden.status,
                    reason="external title protected",
                )
            return WorkflowResult(
                True,
                True,
                display_id=record.display_id,
                status=record.status,
                reason="native metadata conflict requires rollback retry",
            )
        except CodexWriterError as exc:
            return WorkflowResult(
                True,
                True,
                display_id=record.display_id,
                status=record.status,
                reason=f"rollback native write failed: {exc}",
            )
        completed = self.registry.rolled_back(
            session.tool, session.id, observed=target
        )
        return WorkflowResult(
            True,
            renamed=True,
            display_id=completed.display_id,
            status=completed.status,
            reason="staged rollback written and verified",
            title=target,
        )

    def _resolve_conflict(
        self, session: Session, record: SessionRecord, observed: str | None
    ) -> WorkflowResult:
        if observed == record.desired_title:
            record = self.registry.finalize(session.tool, session.id, observed=observed)
            reason = "concurrent write already completed"
        elif observed != record.expected_title:
            record = self.registry.manual_override(
                session.tool,
                session.id,
                observed=observed or "",
                reason="native title changed before compare-and-set",
            )
            reason = "external title protected"
        else:
            if record.status != "recovery":
                record = self.registry.recovery(
                    session.tool,
                    session.id,
                    observed=observed,
                    reason="native metadata changed before compare-and-set",
                )
            reason = "native metadata conflict requires recovery"
        return WorkflowResult(
            True,
            display_id=record.display_id,
            status=record.status,
            reason=reason,
            title=record.desired_title,
        )

    def process(
        self,
        session: Session,
        read_transcript: Callable[[Session], list[Message]],
    ) -> WorkflowResult:
        """Advance one prepared session through classification or recovery."""

        record = self._get(session)
        if record is None:
            return WorkflowResult(False)
        if record.status == "finalized" and self._rollback_is_staged(session):
            return self._recover_rollback(session, record)
        if record.status == "finalized":
            observed = session.native_title
            if observed != record.desired_title:
                try:
                    observed = self.writer.read_title(session.id)
                except CodexWriterError as exc:
                    return WorkflowResult(
                        True,
                        True,
                        display_id=record.display_id,
                        status=record.status,
                        reason=f"could not verify apparent external edit: {exc}",
                    )
            if observed == record.desired_title:
                return WorkflowResult(
                    True,
                    display_id=record.display_id,
                    status=record.status,
                    reason="finalized and protected",
                )
            record = self.registry.manual_override(
                session.tool,
                session.id,
                observed=observed or "",
                reason="native title changed after finalization",
            )
            return WorkflowResult(
                True,
                display_id=record.display_id,
                status=record.status,
                reason="external title protected",
            )
        if record.status in {"manual_override", "rolled_back"}:
            return WorkflowResult(
                True,
                display_id=record.display_id,
                status=record.status,
                reason="protected until explicit reopen",
            )
        if record.status in {"write_pending", "recovery"}:
            return self._recover_write(session, record)

        messages = list(read_transcript(session))
        input_sig = util.signature(messages)
        if record.input_sig == input_sig and record.decision is not None:
            self.registry.record_unclear(
                session.tool,
                session.id,
                decision=dict(record.decision),
                input_sig=input_sig,
                last_evaluated_active=session.last_active,
            )
            return WorkflowResult(
                True,
                display_id=record.display_id,
                status="pending",
                reason="conversation content is unchanged",
            )
        try:
            decision = self.classifier.classify(
                messages,
                cwd=session.cwd,
                modules=self.config.modules,
            )
        except Exception as exc:
            failure = {
                "ready": False,
                "module": None,
                "summary": None,
                "reason_code": "classifier_error",
                "evidence_message_ids": [],
                "confidence": 0.0,
                "error_type": type(exc).__name__,
            }
            self.registry.record_unclear(
                session.tool,
                session.id,
                decision=failure,
                input_sig=input_sig,
                last_evaluated_active=session.last_active,
            )
            return WorkflowResult(
                True,
                display_id=record.display_id,
                status="pending",
                reason=f"classifier failed safely: {exc}",
            )

        modules = _allowed_modules(self.config.modules, session.cwd)
        ready, validation_error = validate_naming_decision(
            decision,
            messages=messages,
            modules=modules,
            confidence_threshold=self.config.confidence_threshold,
            max_messages=self.config.max_messages,
            max_input_chars=self.config.max_input_chars,
        )
        data = _decision_data(decision)
        if validation_error:
            data["validation_error"] = validation_error
        if not ready:
            self.registry.record_unclear(
                session.tool,
                session.id,
                decision=data,
                input_sig=input_sig,
                last_evaluated_active=session.last_active,
            )
            reason = validation_error or f"task is {decision.reason_code}"
            return WorkflowResult(
                True,
                display_id=record.display_id,
                status="pending",
                reason=reason,
            )

        assert decision.module is not None and decision.summary is not None
        try:
            desired, summary = _format_title(
                record.display_id, decision.module, decision.summary
            )
        except ValueError as exc:
            data["validation_error"] = str(exc)
            self.registry.record_unclear(
                session.tool,
                session.id,
                decision=data,
                input_sig=input_sig,
                last_evaluated_active=session.last_active,
            )
            return WorkflowResult(
                True,
                display_id=record.display_id,
                status="pending",
                reason=str(exc),
            )
        record = self.registry.stage_write(
            session.tool,
            session.id,
            expected=session.native_title,
            original=session.title,
            desired=desired,
            module=decision.module,
            summary=summary,
            decision=data,
            input_sig=input_sig,
            last_evaluated_active=session.last_active,
        )
        try:
            self.writer.set_title(
                session.id,
                desired,
                expected_title=session.native_title,
                **self._writer_args(session),
            )
        except CodexConflictError as exc:
            return self._resolve_conflict(session, record, exc.actual_title)
        except CodexWriterError as exc:
            record = self.registry.recovery(
                session.tool, session.id, observed=session.native_title, reason=str(exc)
            )
            return WorkflowResult(
                True,
                True,
                display_id=record.display_id,
                status=record.status,
                reason=f"native write failed: {exc}",
                title=desired,
            )
        record = self.registry.finalize(session.tool, session.id, observed=desired)
        return WorkflowResult(
            True,
            renamed=True,
            display_id=record.display_id,
            status=record.status,
            reason="title written and verified",
            title=desired,
        )

def is_historical_session(
    session: Session, baseline: float | None, *, include_historical: bool
) -> bool:
    """Use creation time for enrollment; unknown creation time fails closed."""

    if include_historical or baseline is None:
        return False
    created_ms = session.meta.get("created_at_ms")
    if isinstance(created_ms, (int, float)) and not isinstance(created_ms, bool):
        return float(created_ms) / 1000.0 < baseline
    if session.tool == "codex":
        return True
    return session.last_active < baseline


__all__ = [
    "NativeTitleWriter",
    "SessionNamingWorkflow",
    "WorkflowResult",
    "is_historical_session",
    "validate_naming_decision",
]
