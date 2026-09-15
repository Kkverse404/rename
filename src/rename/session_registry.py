"""Permanent, transactional identities for structured session naming.

The registry deliberately owns only durable state.  Transcript evaluation,
model calls, and native title writes happen before or after these short SQLite
transactions, never inside them.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

STATUSES = (
    "pending",
    "write_pending",
    "finalized",
    "manual_override",
    "rolled_back",
    "recovery",
)
_SCHEMA_VERSION = "2"


class RegistryError(RuntimeError):
    """Base class for registry failures."""


class RegistryCorruptError(RegistryError):
    """The registry cannot be trusted and was left untouched."""


class RegistryNotFoundError(RegistryError, KeyError):
    """A transition targeted an identity that has not been allocated."""


class InvalidTransitionError(RegistryError):
    """A requested state transition would violate the workflow contract."""


@dataclass(frozen=True, slots=True)
class SessionRecord:
    display_id: int
    tool: str
    native_session_id: str
    status: str
    input_sig: str | None
    last_evaluated_active: float | None
    decision: Any
    expected_title: str | None
    original_title: str | None
    desired_title: str | None
    module: str | None
    summary: str | None
    observed_title: str | None
    recovery_reason: str | None
    created_at: float
    updated_at: float
    finalized_at: float | None


@dataclass(frozen=True, slots=True)
class OperationRecord:
    operation_id: int
    display_id: int
    operation: str
    from_status: str | None
    to_status: str
    details: Any
    created_at: float


@dataclass(frozen=True, slots=True)
class RegistryCounts(Mapping[str, int]):
    pending: int = 0
    write_pending: int = 0
    finalized: int = 0
    manual_override: int = 0
    rolled_back: int = 0
    recovery: int = 0
    total: int = 0

    def __getitem__(self, key: str) -> int:
        if key not in (*STATUSES, "total"):
            raise KeyError(key)
        return int(getattr(self, key))

    def __iter__(self) -> Iterator[str]:
        return iter((*STATUSES, "total"))

    def __len__(self) -> int:
        return len(STATUSES) + 1


_SCHEMA = (
    """
    CREATE TABLE registry (
        display_id INTEGER PRIMARY KEY AUTOINCREMENT,
        tool TEXT NOT NULL CHECK (length(tool) > 0),
        native_session_id TEXT NOT NULL CHECK (length(native_session_id) > 0),
        status TEXT NOT NULL CHECK (
            status IN (
                'pending', 'write_pending', 'finalized',
                'manual_override', 'rolled_back', 'recovery'
            )
        ),
        input_sig TEXT,
        last_evaluated_active REAL,
        decision_json TEXT,
        expected_title TEXT,
        original_title TEXT,
        desired_title TEXT,
        module TEXT,
        summary TEXT,
        observed_title TEXT,
        recovery_reason TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        finalized_at REAL,
        UNIQUE (tool, native_session_id)
    )
    """,
    """
    CREATE TABLE operations (
        operation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        display_id INTEGER NOT NULL REFERENCES registry(display_id),
        operation TEXT NOT NULL,
        from_status TEXT,
        to_status TEXT NOT NULL,
        details_json TEXT,
        created_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE registry_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) WITHOUT ROWID
    """,
    """
    CREATE TRIGGER registry_no_delete
    BEFORE DELETE ON registry
    BEGIN
        SELECT RAISE(ABORT, 'registry identities are permanent');
    END
    """,
    """
    CREATE TRIGGER registry_identity_immutable
    BEFORE UPDATE OF display_id, tool, native_session_id ON registry
    BEGIN
        SELECT RAISE(ABORT, 'registry identity is immutable');
    END
    """,
    """
    CREATE TRIGGER operations_no_delete
    BEFORE DELETE ON operations
    BEGIN
        SELECT RAISE(ABORT, 'operations are append-only');
    END
    """,
    """
    CREATE TRIGGER operations_no_update
    BEFORE UPDATE ON operations
    BEGIN
        SELECT RAISE(ABORT, 'operations are append-only');
    END
    """,
)

_REGISTRY_COLUMNS = {
    "display_id",
    "tool",
    "native_session_id",
    "status",
    "input_sig",
    "last_evaluated_active",
    "decision_json",
    "expected_title",
    "original_title",
    "desired_title",
    "module",
    "summary",
    "observed_title",
    "recovery_reason",
    "created_at",
    "updated_at",
    "finalized_at",
}
_OPERATION_COLUMNS = {
    "operation_id",
    "display_id",
    "operation",
    "from_status",
    "to_status",
    "details_json",
    "created_at",
}
_TRIGGERS = {
    "registry_no_delete",
    "registry_identity_immutable",
    "operations_no_delete",
    "operations_no_update",
}


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _encode_json(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("decision/details must be JSON serializable") from exc


def _decode_json(raw: str | None, *, field: str) -> Any:
    if raw is None:
        return None
    try:
        return _freeze_json(json.loads(raw))
    except (json.JSONDecodeError, TypeError) as exc:
        raise RegistryCorruptError(f"invalid {field} JSON in registry") from exc


def _check_key(tool: str, native_session_id: str) -> None:
    for label, value in (("tool", tool), ("native_session_id", native_session_id)):
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError(f"{label} must be a non-empty string without NUL characters")


def _check_text(label: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{label} must be a non-empty string without NUL characters")


class SessionRegistry:
    """SQLite-backed permanent session registry.

    A new connection is used for every method so instances are safe to share
    between threads.  Mutations serialize only the row update and operation-log
    append under ``BEGIN IMMEDIATE``.
    """

    def __init__(self, path: str | Path, *, timeout: float = 10.0):
        self.path = Path(path)
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        try:
            if read_only:
                uri = self.path.resolve().as_uri() + "?mode=ro"
                connection = sqlite3.connect(uri, uri=True, timeout=self.timeout)
            else:
                connection = sqlite3.connect(self.path, timeout=self.timeout)
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
            connection.execute("PRAGMA foreign_keys = ON")
            return connection
        except sqlite3.DatabaseError as exc:
            self._raise_database_error(exc)

    @staticmethod
    def _raise_database_error(exc: sqlite3.DatabaseError) -> None:
        message = str(exc).lower()
        corrupt_markers = (
            "file is not a database",
            "database disk image is malformed",
            "malformed database schema",
            "database schema is corrupt",
        )
        if any(marker in message for marker in corrupt_markers):
            raise RegistryCorruptError(f"registry is corrupt: {exc}") from exc
        raise RegistryError(f"registry database error: {exc}") from exc

    def _initialize(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RegistryError(f"cannot create registry directory: {exc}") from exc

        connection = self._connect()
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if tables:
                if "registry_meta" not in tables:
                    raise RegistryCorruptError(
                        "registry schema metadata is missing; refusing to replace existing data"
                    )
                self._validate_schema(connection)
                return

            connection.execute("BEGIN IMMEDIATE")
            # Another process may have initialized the file while this
            # connection waited for the write lock, so inspect it again.
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if not tables:
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO registry_meta(key, value) VALUES ('schema_version', ?)",
                    (_SCHEMA_VERSION,),
                )
            elif "registry_meta" not in tables:
                raise RegistryCorruptError(
                    "registry schema metadata is missing; refusing to replace existing data"
                )
            connection.commit()
            # Integrity and schema checks are intentionally outside the write
            # transaction; BEGIN IMMEDIATE protects only short mutations.
            self._validate_schema(connection)
        except RegistryError:
            connection.rollback()
            raise
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            self._raise_database_error(exc)
        finally:
            connection.close()

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        try:
            check = connection.execute("PRAGMA quick_check(1)").fetchone()
            if check is None or check[0] != "ok":
                detail = "unknown" if check is None else str(check[0])
                raise RegistryCorruptError(f"registry integrity check failed: {detail}")

            version = connection.execute(
                "SELECT value FROM registry_meta WHERE key = 'schema_version'"
            ).fetchone()
            if version is None or version[0] != _SCHEMA_VERSION:
                found = None if version is None else version[0]
                raise RegistryCorruptError(f"unsupported registry schema version: {found!r}")

            registry_columns = {row[1] for row in connection.execute("PRAGMA table_info(registry)")}
            operation_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(operations)")
            }
            if registry_columns != _REGISTRY_COLUMNS:
                raise RegistryCorruptError(
                    f"registry table schema does not match version {_SCHEMA_VERSION}"
                )
            if operation_columns != _OPERATION_COLUMNS:
                raise RegistryCorruptError(
                    f"operations table schema does not match version {_SCHEMA_VERSION}"
                )

            triggers = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                )
            }
            if not _TRIGGERS.issubset(triggers):
                raise RegistryCorruptError("registry protection triggers are missing")

            unique_identity = False
            for index in connection.execute("PRAGMA index_list(registry)"):
                if not index[2]:
                    continue
                columns = [
                    row[2]
                    for row in connection.execute(
                        f"PRAGMA index_info('{str(index[1]).replace(chr(39), chr(39) * 2)}')"
                    )
                ]
                if columns == ["tool", "native_session_id"]:
                    unique_identity = True
                    break
            if not unique_identity:
                raise RegistryCorruptError("registry identity uniqueness constraint is missing")
        except RegistryError:
            raise
        except sqlite3.DatabaseError as exc:
            self._raise_database_error(exc)

    def _read_connection(self) -> sqlite3.Connection | None:
        if not self.path.exists():
            return None
        connection = self._connect(read_only=True)
        try:
            self._validate_schema(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    def _write_connection(self) -> sqlite3.Connection:
        self._initialize()
        return self._connect()

    @staticmethod
    def _record(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            display_id=int(row["display_id"]),
            tool=str(row["tool"]),
            native_session_id=str(row["native_session_id"]),
            status=str(row["status"]),
            input_sig=row["input_sig"],
            last_evaluated_active=row["last_evaluated_active"],
            decision=_decode_json(row["decision_json"], field="decision"),
            expected_title=row["expected_title"],
            original_title=row["original_title"],
            desired_title=row["desired_title"],
            module=row["module"],
            summary=row["summary"],
            observed_title=row["observed_title"],
            recovery_reason=row["recovery_reason"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            finalized_at=row["finalized_at"],
        )

    @staticmethod
    def _select_record(
        connection: sqlite3.Connection, tool: str, native_session_id: str
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM registry WHERE tool = ? AND native_session_id = ?",
            (tool, native_session_id),
        ).fetchone()

    def get(self, tool: str, native_session_id: str) -> SessionRecord | None:
        _check_key(tool, native_session_id)
        connection = self._read_connection()
        if connection is None:
            return None
        try:
            row = self._select_record(connection, tool, native_session_id)
            return None if row is None else self._record(row)
        except RegistryError:
            raise
        except sqlite3.DatabaseError as exc:
            self._raise_database_error(exc)
        finally:
            connection.close()

    def ensure(self, tool: str, native_session_id: str) -> SessionRecord:
        _check_key(tool, native_session_id)
        connection = self._write_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_record(connection, tool, native_session_id)
            if row is None:
                now = time.time()
                cursor = connection.execute(
                    """
                    INSERT INTO registry(
                        tool, native_session_id, status, created_at, updated_at
                    ) VALUES (?, ?, 'pending', ?, ?)
                    """,
                    (tool, native_session_id, now, now),
                )
                display_id = int(cursor.lastrowid)
                self._append_operation(
                    connection,
                    display_id,
                    "allocated",
                    None,
                    "pending",
                    {"tool": tool, "native_session_id": native_session_id},
                    now,
                )
                row = self._select_record(connection, tool, native_session_id)
            connection.commit()
            assert row is not None
            return self._record(row)
        except RegistryError:
            connection.rollback()
            raise
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            self._raise_database_error(exc)
        finally:
            connection.close()

    def list(
        self, *, tool: str | None = None, status: str | None = None
    ) -> tuple[SessionRecord, ...]:
        if tool is not None and (not isinstance(tool, str) or not tool):
            raise ValueError("tool must be a non-empty string")
        if status is not None and status not in STATUSES:
            raise ValueError(f"unknown registry status: {status}")
        connection = self._read_connection()
        if connection is None:
            return ()
        try:
            clauses: list[str] = []
            values: list[str] = []
            if tool is not None:
                clauses.append("tool = ?")
                values.append(tool)
            if status is not None:
                clauses.append("status = ?")
                values.append(status)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = connection.execute(
                f"SELECT * FROM registry{where} ORDER BY display_id", values
            ).fetchall()
            return tuple(self._record(row) for row in rows)
        except RegistryError:
            raise
        except sqlite3.DatabaseError as exc:
            self._raise_database_error(exc)
        finally:
            connection.close()

    def list_records(
        self, *, tool: str | None = None, status: str | None = None
    ) -> tuple[SessionRecord, ...]:
        """Named alias for callers that avoid shadowing the built-in ``list``."""
        return self.list(tool=tool, status=status)

    def counts(self, *, tool: str | None = None) -> RegistryCounts:
        if tool is not None and (not isinstance(tool, str) or not tool):
            raise ValueError("tool must be a non-empty string")
        connection = self._read_connection()
        if connection is None:
            return RegistryCounts()
        try:
            if tool is None:
                rows = connection.execute(
                    "SELECT status, COUNT(*) AS count FROM registry GROUP BY status"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT status, COUNT(*) AS count
                    FROM registry WHERE tool = ? GROUP BY status
                    """,
                    (tool,),
                ).fetchall()
            values = {status: 0 for status in STATUSES}
            for row in rows:
                status = str(row["status"])
                if status not in values:
                    raise RegistryCorruptError(f"unknown status stored in registry: {status}")
                values[status] = int(row["count"])
            return RegistryCounts(**values, total=sum(values.values()))
        except RegistryError:
            raise
        except sqlite3.DatabaseError as exc:
            self._raise_database_error(exc)
        finally:
            connection.close()

    def operations(
        self, tool: str | None = None, native_session_id: str | None = None
    ) -> tuple[OperationRecord, ...]:
        if (tool is None) != (native_session_id is None):
            raise ValueError("tool and native_session_id must be supplied together")
        if tool is not None and native_session_id is not None:
            _check_key(tool, native_session_id)
        connection = self._read_connection()
        if connection is None:
            return ()
        try:
            if tool is None:
                rows = connection.execute(
                    "SELECT * FROM operations ORDER BY operation_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT operations.* FROM operations
                    JOIN registry USING (display_id)
                    WHERE registry.tool = ? AND registry.native_session_id = ?
                    ORDER BY operations.operation_id
                    """,
                    (tool, native_session_id),
                ).fetchall()
            return tuple(
                OperationRecord(
                    operation_id=int(row["operation_id"]),
                    display_id=int(row["display_id"]),
                    operation=str(row["operation"]),
                    from_status=row["from_status"],
                    to_status=str(row["to_status"]),
                    details=_decode_json(row["details_json"], field="operation details"),
                    created_at=float(row["created_at"]),
                )
                for row in rows
            )
        except RegistryError:
            raise
        except sqlite3.DatabaseError as exc:
            self._raise_database_error(exc)
        finally:
            connection.close()

    @staticmethod
    def _append_operation(
        connection: sqlite3.Connection,
        display_id: int,
        operation: str,
        from_status: str | None,
        to_status: str,
        details: Any,
        now: float,
    ) -> None:
        connection.execute(
            """
            INSERT INTO operations(
                display_id, operation, from_status, to_status, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                display_id,
                operation,
                from_status,
                to_status,
                _encode_json(details),
                now,
            ),
        )

    def _transition(
        self,
        tool: str,
        native_session_id: str,
        *,
        allowed: set[str],
        to_status: str,
        operation: str,
        updates: Mapping[str, Any] | None = None,
        details: Any = None,
        idempotent_status: str | None = None,
    ) -> SessionRecord:
        _check_key(tool, native_session_id)
        connection = self._write_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_record(connection, tool, native_session_id)
            if row is None:
                raise RegistryNotFoundError((tool, native_session_id))
            current = self._record(row)
            if idempotent_status is not None and current.status == idempotent_status:
                connection.commit()
                return current
            if current.status not in allowed:
                raise InvalidTransitionError(
                    f"cannot {operation} session in {current.status!r} state"
                )

            fields = dict(updates or {})
            fields["status"] = to_status
            fields["updated_at"] = time.time()
            assignments = ", ".join(f"{column} = ?" for column in fields)
            connection.execute(
                f"UPDATE registry SET {assignments} WHERE display_id = ?",
                (*fields.values(), current.display_id),
            )
            self._append_operation(
                connection,
                current.display_id,
                operation,
                current.status,
                to_status,
                details,
                float(fields["updated_at"]),
            )
            updated = connection.execute(
                "SELECT * FROM registry WHERE display_id = ?", (current.display_id,)
            ).fetchone()
            connection.commit()
            assert updated is not None
            return self._record(updated)
        except RegistryError:
            connection.rollback()
            raise
        except sqlite3.DatabaseError as exc:
            connection.rollback()
            self._raise_database_error(exc)
        finally:
            connection.close()

    def record_unclear(
        self,
        tool: str,
        native_session_id: str,
        *,
        decision: Any,
        input_sig: str,
        last_evaluated_active: float,
    ) -> SessionRecord:
        _check_text("input_sig", input_sig)
        active = float(last_evaluated_active)
        decision_json = _encode_json(decision)
        return self._transition(
            tool,
            native_session_id,
            allowed={"pending"},
            to_status="pending",
            operation="unclear_recorded",
            updates={
                "decision_json": decision_json,
                "input_sig": input_sig,
                "last_evaluated_active": active,
                "expected_title": None,
                "original_title": None,
                "desired_title": None,
                "module": None,
                "summary": None,
                "observed_title": None,
                "recovery_reason": None,
                "finalized_at": None,
            },
            details={
                "decision": decision,
                "input_sig": input_sig,
                "last_evaluated_active": active,
            },
        )

    def record_unclear_decision(self, *args: Any, **kwargs: Any) -> SessionRecord:
        """Compatibility spelling for the explicit unclear-decision operation."""
        return self.record_unclear(*args, **kwargs)

    def stage_write(
        self,
        tool: str,
        native_session_id: str,
        *,
        expected: str | None,
        original: str | None,
        desired: str,
        module: str,
        summary: str,
        decision: Any,
        input_sig: str | None = None,
        last_evaluated_active: float | None = None,
    ) -> SessionRecord:
        if expected is not None and (not isinstance(expected, str) or "\x00" in expected):
            raise ValueError("expected must be None or a string without NUL characters")
        if original is not None and (not isinstance(original, str) or "\x00" in original):
            raise ValueError("original must be None or a string without NUL characters")
        _check_text("desired", desired)
        _check_text("module", module)
        _check_text("summary", summary)
        if input_sig is not None:
            _check_text("input_sig", input_sig)
        active = None if last_evaluated_active is None else float(last_evaluated_active)
        decision_json = _encode_json(decision)

        current = self.get(tool, native_session_id)
        if current is not None and current.status == "write_pending":
            same = (
                current.expected_title == expected
                and current.original_title == original
                and current.desired_title == desired
                and current.module == module
                and current.summary == summary
                and current.decision == _decode_json(decision_json, field="decision")
                and current.input_sig == input_sig
                and current.last_evaluated_active == active
            )
            if same:
                return current
            raise InvalidTransitionError("cannot replace an existing staged write")

        return self._transition(
            tool,
            native_session_id,
            allowed={"pending"},
            to_status="write_pending",
            operation="write_staged",
            updates={
                "decision_json": decision_json,
                "input_sig": input_sig,
                "last_evaluated_active": active,
                "expected_title": expected,
                "original_title": original,
                "desired_title": desired,
                "module": module,
                "summary": summary,
                "observed_title": None,
                "recovery_reason": None,
                "finalized_at": None,
            },
            details={
                "expected": expected,
                "original": original,
                "desired": desired,
                "module": module,
                "summary": summary,
                "decision": decision,
            },
        )

    def record_finalized_review(
        self,
        tool: str,
        native_session_id: str,
        *,
        decision: Any,
        input_sig: str,
        last_evaluated_active: float,
    ) -> SessionRecord:
        """Cache a completion review without changing the managed title."""

        _check_text("input_sig", input_sig)
        active = float(last_evaluated_active)
        decision_json = _encode_json(decision)
        return self._transition(
            tool,
            native_session_id,
            allowed={"finalized"},
            to_status="finalized",
            operation="resolution_checked",
            updates={
                "decision_json": decision_json,
                "input_sig": input_sig,
                "last_evaluated_active": active,
            },
            details={
                "decision": decision,
                "input_sig": input_sig,
                "last_evaluated_active": active,
            },
        )

    def stage_resolution(
        self,
        tool: str,
        native_session_id: str,
        *,
        desired: str,
        decision: Any,
        input_sig: str,
        last_evaluated_active: float,
    ) -> SessionRecord:
        """Stage a resolved-title update while preserving the original rollback target."""

        _check_text("desired", desired)
        _check_text("input_sig", input_sig)
        current = self.get(tool, native_session_id)
        if current is None:
            raise RegistryNotFoundError((tool, native_session_id))
        if not current.desired_title or not current.summary:
            raise InvalidTransitionError("finalized session has no managed title or summary")
        return self._transition(
            tool,
            native_session_id,
            allowed={"finalized"},
            to_status="write_pending",
            operation="resolution_write_staged",
            updates={
                "decision_json": _encode_json(decision),
                "input_sig": input_sig,
                "last_evaluated_active": float(last_evaluated_active),
                "expected_title": current.desired_title,
                "desired_title": desired,
                "observed_title": None,
                "recovery_reason": None,
                "finalized_at": None,
            },
            details={
                "expected": current.desired_title,
                "desired": desired,
                "decision": decision,
            },
        )

    def finalize(
        self, tool: str, native_session_id: str, *, observed: str | None = None
    ) -> SessionRecord:
        current = self.get(tool, native_session_id)
        if current is None:
            raise RegistryNotFoundError((tool, native_session_id))
        if observed is not None and observed != current.desired_title:
            raise InvalidTransitionError("observed title does not match the staged desired title")
        if current.status == "finalized":
            return current
        now = time.time()
        return self._transition(
            tool,
            native_session_id,
            allowed={"write_pending", "recovery"},
            to_status="finalized",
            operation="finalized",
            updates={
                "observed_title": observed or current.desired_title,
                "recovery_reason": None,
                "finalized_at": now,
            },
            details={"observed": observed or current.desired_title},
        )

    def manual_override(
        self, tool: str, native_session_id: str, *, observed: str, reason: str | None = None
    ) -> SessionRecord:
        if not isinstance(observed, str) or "\x00" in observed:
            raise ValueError("observed must be a string without NUL characters")
        current = self.get(tool, native_session_id)
        if current is not None and current.status == "manual_override":
            if current.observed_title == observed:
                return current
            raise InvalidTransitionError("manual override is already permanent until reopen")
        return self._transition(
            tool,
            native_session_id,
            allowed={"finalized", "write_pending", "recovery"},
            to_status="manual_override",
            operation="manual_override",
            updates={"observed_title": observed, "recovery_reason": reason},
            details={"observed": observed, "reason": reason},
        )

    def recovery(
        self,
        tool: str,
        native_session_id: str,
        *,
        observed: str | None = None,
        reason: str | None = None,
    ) -> SessionRecord:
        if observed is not None and (not isinstance(observed, str) or "\x00" in observed):
            raise ValueError("observed must be None or a string without NUL characters")
        return self._transition(
            tool,
            native_session_id,
            allowed={"pending", "write_pending"},
            to_status="recovery",
            operation="recovery_started",
            updates={"observed_title": observed, "recovery_reason": reason},
            details={"observed": observed, "reason": reason},
            idempotent_status="recovery",
        )

    def mark_recovery(self, *args: Any, **kwargs: Any) -> SessionRecord:
        return self.recovery(*args, **kwargs)

    def reopen(self, tool: str, native_session_id: str) -> SessionRecord:
        updates = {
            "input_sig": None,
            "last_evaluated_active": None,
            "decision_json": None,
            "expected_title": None,
            "original_title": None,
            "desired_title": None,
            "module": None,
            "summary": None,
            "observed_title": None,
            "recovery_reason": None,
            "finalized_at": None,
        }
        return self._transition(
            tool,
            native_session_id,
            allowed={"pending", "finalized", "manual_override", "rolled_back", "recovery"},
            to_status="pending",
            operation="reopened",
            updates=updates,
            details=None,
        )

    def stage_rollback(self, tool: str, native_session_id: str) -> SessionRecord:
        """Persist rollback intent before the native title is restored."""

        current = self.get(tool, native_session_id)
        if current is None:
            raise RegistryNotFoundError((tool, native_session_id))
        if current.status != "finalized":
            raise InvalidTransitionError(
                f"cannot stage rollback for session in {current.status!r} state"
            )
        if current.original_title is None:
            raise InvalidTransitionError("finalized session has no restorable original title")
        operations = self.operations(tool, native_session_id)
        if operations and operations[-1].operation == "rollback_staged":
            return current
        return self._transition(
            tool,
            native_session_id,
            allowed={"finalized"},
            to_status="finalized",
            operation="rollback_staged",
            updates={"recovery_reason": "rollback pending"},
            details={
                "managed_title": current.desired_title,
                "target_title": current.original_title,
            },
        )

    def rolled_back(
        self, tool: str, native_session_id: str, *, observed: str | None = None
    ) -> SessionRecord:
        if observed is not None and (not isinstance(observed, str) or "\x00" in observed):
            raise ValueError("observed must be None or a string without NUL characters")
        return self._transition(
            tool,
            native_session_id,
            allowed={"finalized"},
            to_status="rolled_back",
            operation="rolled_back",
            updates={"observed_title": observed, "recovery_reason": None},
            details={"observed": observed},
            idempotent_status="rolled_back",
        )

    def mark_rolled_back(self, *args: Any, **kwargs: Any) -> SessionRecord:
        return self.rolled_back(*args, **kwargs)


RegistryRecord = SessionRecord

__all__ = [
    "InvalidTransitionError",
    "OperationRecord",
    "RegistryCorruptError",
    "RegistryCounts",
    "RegistryError",
    "RegistryNotFoundError",
    "RegistryRecord",
    "STATUSES",
    "SessionRecord",
    "SessionRegistry",
]
