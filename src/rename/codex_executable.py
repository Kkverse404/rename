"""Resolve the Codex executable across shells and desktop startup contexts."""

from __future__ import annotations

import ntpath
import os
import shutil
import sys
from pathlib import Path

_WINDOWS_DESKTOP_COMMANDS = {"codex", "codex.exe", "codex.cmd", "codex.bat"}


def _existing_path(path: str | os.PathLike[str]) -> str | None:
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        return None
    return str(candidate.resolve())


def _windows_desktop_codex() -> str | None:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    bin_root = base / "OpenAI" / "Codex" / "bin"
    candidates = [bin_root / "codex.exe", *bin_root.glob("*/codex.exe")]
    existing = [candidate for candidate in candidates if candidate.is_file()]
    if not existing:
        return None

    def modified_at(candidate: Path) -> int:
        try:
            return candidate.stat().st_mtime_ns
        except OSError:
            return -1

    newest = max(existing, key=lambda candidate: (modified_at(candidate), str(candidate)))
    return str(newest.resolve())


def resolve_codex_executable(
    executable: str | os.PathLike[str] = "codex",
) -> str | None:
    """Resolve an explicit launcher, then the Windows Codex Desktop install."""

    raw = os.fspath(executable)
    found = shutil.which(raw)
    if found is not None:
        return str(Path(found).resolve())

    explicit = _existing_path(raw)
    if explicit is not None:
        return explicit

    is_bare_codex = (
        not ntpath.dirname(raw)
        and ntpath.basename(raw).casefold() in _WINDOWS_DESKTOP_COMMANDS
    )
    if sys.platform == "win32" and is_bare_codex:
        return _windows_desktop_codex()
    return None


__all__ = ["resolve_codex_executable"]
