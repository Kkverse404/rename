"""Codex discovery/transcript adapter with app-server title writes."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable

from ..models import Message, Session
from ._sqlite import connect_read
from .base import Adapter
from .codex_writer import CodexWriter

_VER_RE = re.compile(r"state_(\d+)\.sqlite$")
_DISCOVERY_COLS = (
    "id",
    "title",
    "name",
    "preview",
    "rollout_path",
    "updated_at_ms",
    "created_at_ms",
    "cwd",
    "archived",
    "first_user_message",
)


def _codex_root(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser()
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex"


def _find_state_db(root: Path | None = None) -> Path | None:
    root = root or _codex_root()
    if not root.is_dir():
        return None
    candidates = sorted(
        root.glob("state_*.sqlite"),
        key=lambda p: int(m.group(1)) if (m := _VER_RE.search(p.name)) else -1,
        reverse=True,
    )
    if candidates:
        return candidates[0]
    legacy = root / "state.sqlite"
    return legacy if legacy.exists() else None


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("text")
        ]
        return "\n".join(parts)
    return ""


class CodexAdapter(Adapter):
    name = "codex"
    label = "Codex"

    def __init__(
        self,
        codex_home: str | os.PathLike[str] | None = None,
        writer: CodexWriter | None = None,
        writer_factory: Callable[..., CodexWriter] = CodexWriter,
    ) -> None:
        self.codex_home = _codex_root(codex_home)
        self._writer = (
            writer if writer is not None else writer_factory(codex_home=self.codex_home)
        )

    def available(self) -> bool:
        return _find_state_db(self.codex_home) is not None

    @property
    def writer(self) -> CodexWriter:
        """Native writer shared with the structured workflow."""
        return self._writer

    def discover(self, since: float) -> list[Session]:
        db = _find_state_db(self.codex_home)
        if not db:
            return []
        since_ms = int(since * 1000)
        con = connect_read(db)
        try:
            available_cols = {
                row[1] for row in con.execute("PRAGMA table_info(threads)").fetchall()
            }
            required = {"id", "title", "rollout_path", "updated_at_ms"}
            if not required.issubset(available_cols):
                return []
            selected_cols = [col for col in _DISCOVERY_COLS if col in available_cols]
            cur = con.execute(
                f"SELECT {','.join(selected_cols)} FROM threads "
                "WHERE updated_at_ms >= ? ORDER BY updated_at_ms DESC",
                (since_ms,),
            )
            cols = [d[0] for d in cur.description]
            out: list[Session] = []
            for row in cur.fetchall():
                r = dict(zip(cols, row))
                if r.get("archived"):  # skip archived threads (truthy flag)
                    continue
                updated = r.get("updated_at_ms")
                if not updated:
                    continue
                native_name = r.get("name")
                fallback_title = r.get("title") or r.get("preview")
                effective_title = (
                    native_name
                    if isinstance(native_name, str) and native_name
                    else fallback_title
                )
                meta = {
                    "db": str(db),
                    "rollout_path": r.get("rollout_path"),
                    "first_user_message": r.get("first_user_message"),
                    "created_at_ms": r.get("created_at_ms"),
                    "updated_at": updated / 1000.0,
                }
                if "name" in available_cols:
                    meta["native_name"] = native_name
                out.append(
                    Session(
                        tool=self.name,
                        id=r["id"],
                        title=effective_title,
                        last_active=updated / 1000.0,
                        cwd=r.get("cwd"),
                        meta=meta,
                    )
                )
            return out
        finally:
            con.close()

    def read_transcript(self, session: Session) -> list[Message]:
        rollout = session.meta.get("rollout_path")
        msgs: list[Message] = []
        if rollout and Path(rollout).exists():
            try:
                with open(rollout, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if '"message"' not in line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("type") != "response_item":
                            continue
                        payload = obj.get("payload") or {}
                        if payload.get("type") != "message":
                            continue
                        role = payload.get("role")
                        if role not in ("user", "assistant"):
                            continue
                        text = _content_text(payload.get("content"))
                        if text.strip():
                            msgs.append(Message(role=role, text=text))
            except OSError:
                msgs = []
        if not msgs:
            fum = session.meta.get("first_user_message")
            if fum:
                return [Message(role="user", text=fum)]
        return msgs

    def set_title(self, session: Session, title: str) -> None:
        self._writer.set_title(
            session.id,
            title,
            expected_title=session.native_title,
            expected_updated_at=session.meta.get("updated_at"),
            expected_status=session.meta.get("status"),
        )
