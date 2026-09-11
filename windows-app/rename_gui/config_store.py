"""Read config with stdlib TOML and update owned keys without dropping others."""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ALL_NAMERS = ("auto", "heuristic", "claude", "codex", "anthropic", "openai")
ALL_STRUCTURED_MODES = ("off", "preview", "apply")
ALL_TOOLS = (
    "claude-code",
    "codex",
    "cursor",
    "antigravity",
    "continue",
    "zed",
    "windsurf",
    "aider",
)
DEFAULT_CLAUDE_MODEL = "haiku"
DEFAULT_CODEX_MODEL = "gpt-5.3-codex-spark"
DEFAULT_STRUCTURED_MODEL = "gpt-5.6-terra"


def config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "rename/config.toml"


@dataclass
class Values:
    idle_seconds: int = 300
    poll_seconds: int = 60
    batch_size: int = 25
    max_age_days: int = 7
    min_user_messages: int = 1
    namer: str = "auto"
    dry_run: bool = False
    tools: list[str] = field(
        default_factory=lambda: ["claude-code", "codex", "cursor", "antigravity"]
    )
    claude_model: str = ""
    codex_model: str = ""
    structured_mode: str = "off"
    structured_model: str = ""
    structured_modules: list[str] = field(default_factory=list)


def _top_level_lines(text: str) -> list[str]:
    """Yield top-level lines (before the first ``[section]``)."""
    out = []
    for line in text.split("\n"):
        if line.strip().startswith("["):
            break
        out.append(line)
    return out


def _match(text: str, key: str) -> str | None:
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*)$")
    for line in _top_level_lines(text):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        m = pat.match(line)
        if m:
            return m.group(1).strip()
    return None


def _int(text: str, key: str, default: int) -> int:
    raw = _match(text, key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool(text: str, key: str, default: bool) -> bool:
    raw = _match(text, key)
    if raw is None:
        return default
    return raw.strip().lower() == "true"


def _string(text: str, key: str, default: str) -> str:
    raw = _match(text, key)
    if raw and raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        try:
            value = json.loads(raw)
            return value if isinstance(value, str) else default
        except (json.JSONDecodeError, ValueError):
            return default
    return default


def _array(text: str, key: str, default: list[str]) -> list[str]:
    raw = _match(text, key)
    if not raw or not raw.startswith("[") or not raw.endswith("]"):
        return default
    body = raw[1:-1]
    items = []
    for piece in body.split(","):
        p = piece.strip()
        if p.startswith('"') and p.endswith('"') and len(p) >= 2:
            items.append(p[1:-1])
    return items


def _section_string(text: str, section: str, key: str, default: str = "") -> str:
    lines = text.split("\n")
    header = next(
        (i for i, line in enumerate(lines) if line.strip() == f"[{section}]"),
        None,
    )
    if header is None:
        return default
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*)$")
    for line in lines[header + 1:]:
        stripped = line.strip()
        if stripped.startswith("["):
            break
        if stripped.startswith("#"):
            continue
        m = pat.match(line)
        if not m:
            continue
        raw = m.group(1).strip()
        if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
            try:
                value = json.loads(raw)
                return value if isinstance(value, str) else default
            except (json.JSONDecodeError, ValueError):
                return default
        return default
    return default


def _section_array(
    text: str, section: str, key: str, default: list[str] | None = None
) -> list[str]:
    lines = text.split("\n")
    header = next(
        (i for i, line in enumerate(lines) if line.strip() == f"[{section}]"),
        None,
    )
    if header is None:
        return list(default or [])
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*)$")
    for line in lines[header + 1 :]:
        if line.strip().startswith("["):
            break
        match = pattern.match(line)
        if not match or line.lstrip().startswith("#"):
            continue
        try:
            value = json.loads(match.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            return list(default or [])
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return value
        return list(default or [])
    return list(default or [])


def load() -> Values:
    path = config_path()
    if not path.exists():
        return Values()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return Values()
    structured = data.get("structured_naming", {})
    if not isinstance(structured, dict):
        structured = {}
    claude = data.get("claude", {})
    if not isinstance(claude, dict):
        claude = {}
    codex = data.get("codex", {})
    if not isinstance(codex, dict):
        codex = {}
    tools = data.get("tools", ["claude-code", "codex", "cursor", "antigravity"])
    if not isinstance(tools, list) or not all(isinstance(item, str) for item in tools):
        tools = ["claude-code", "codex", "cursor", "antigravity"]
    mode = structured.get("mode", "off")
    if mode not in ALL_STRUCTURED_MODES:
        mode = "off"
    modules = structured.get("modules", [])
    if not isinstance(modules, list) or not all(isinstance(item, str) for item in modules):
        modules = []
    return Values(
        idle_seconds=int(data.get("idle_seconds", 300)),
        poll_seconds=int(data.get("poll_seconds", 60)),
        batch_size=int(data.get("batch_size", 25)),
        max_age_days=int(data.get("max_age_days", 7)),
        min_user_messages=int(data.get("min_user_messages", 1)),
        namer=str(data.get("namer", "auto")),
        dry_run=bool(data.get("dry_run", False)),
        tools=list(tools),
        claude_model=str(claude.get("model", "")),
        codex_model=str(codex.get("model", "")),
        structured_mode=mode,
        structured_model=str(structured.get("model", "")),
        structured_modules=list(modules),
    )


def _replace_or_append(text: str, key: str, value: str) -> str:
    lines = text.split("\n")
    section_at: int | None = None
    for i, line in enumerate(lines):
        if line.strip().startswith("["):
            section_at = i
            break
    scan_end = section_at if section_at is not None else len(lines)
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i in range(scan_end):
        if lines[i].lstrip().startswith("#"):
            continue
        if pat.match(lines[i]):
            lines[i] = f"{key} = {value}"
            return "\n".join(lines)
    insert_at = section_at if section_at is not None else len(lines)
    lines.insert(insert_at, f"{key} = {value}")
    return "\n".join(lines)


def _set_section_string(text: str, section: str, key: str, value: str) -> str:
    is_empty = not value.strip()
    # JSON string escaping is a valid subset of TOML basic-string escaping and
    # correctly preserves Windows backslashes, quotes, Unicode, and spaces.
    quoted = json.dumps(value, ensure_ascii=False)
    lines = text.split("\n")
    header = next(
        (i for i, line in enumerate(lines) if line.strip() == f"[{section}]"),
        None,
    )

    if header is None:
        if is_empty:
            return text
        out = text
        if out and not out.endswith("\n"):
            out += "\n"
        return out + f"\n[{section}]\n{key} = {quoted}\n"

    end = len(lines)
    for i in range(header + 1, len(lines)):
        if lines[i].strip().startswith("["):
            end = i
            break

    pat = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i in range(header + 1, end):
        if lines[i].lstrip().startswith("#"):
            continue
        if pat.match(lines[i]):
            if is_empty:
                del lines[i]
            else:
                lines[i] = f"{key} = {quoted}"
            return "\n".join(lines)

    if is_empty:
        return text
    lines.insert(header + 1, f"{key} = {quoted}")
    return "\n".join(lines)


def _set_section_array(text: str, section: str, key: str, values: list[str]) -> str:
    encoded = json.dumps(values, ensure_ascii=False)
    lines = text.split("\n")
    header = next(
        (i for i, line in enumerate(lines) if line.strip() == f"[{section}]"),
        None,
    )
    if header is None:
        out = text
        if out and not out.endswith("\n"):
            out += "\n"
        return out + f"\n[{section}]\n{key} = {encoded}\n"
    end = next(
        (
            i
            for i in range(header + 1, len(lines))
            if lines[i].strip().startswith("[")
        ),
        len(lines),
    )
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i in range(header + 1, end):
        if pattern.match(lines[i]) and not lines[i].lstrip().startswith("#"):
            close_at = _toml_array_end(lines, i, end)
            lines[i : close_at + 1] = [f"{key} = {encoded}"]
            return "\n".join(lines)
    lines.insert(end, f"{key} = {encoded}")
    return "\n".join(lines)


def _toml_array_end(lines: list[str], start: int, end: int) -> int:
    """Find the real closing bracket, ignoring brackets in strings/comments."""

    depth = 0
    started = False
    quote: str | None = None
    escaped = False
    for line_index in range(start, end):
        value = lines[line_index].split("=", 1)[1] if line_index == start else lines[line_index]
        for char in value:
            if quote is not None:
                if quote == '"' and escaped:
                    escaped = False
                elif quote == '"' and char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
                continue
            if char == "#":
                break
            if char in {'"', "'"}:
                quote = char
            elif char == "[":
                depth += 1
                started = True
            elif char == "]" and started:
                depth -= 1
                if depth == 0:
                    return line_index
    return start


def save(v: Values) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    text = _replace_or_append(text, "idle_seconds", str(v.idle_seconds))
    text = _replace_or_append(text, "poll_seconds", str(v.poll_seconds))
    text = _replace_or_append(text, "batch_size", str(v.batch_size))
    text = _replace_or_append(text, "max_age_days", str(v.max_age_days))
    text = _replace_or_append(text, "min_user_messages", str(v.min_user_messages))
    text = _replace_or_append(text, "namer", f'"{v.namer}"')
    text = _replace_or_append(text, "dry_run", "true" if v.dry_run else "false")
    tools_str = "[" + ", ".join(f'"{t}"' for t in v.tools) + "]"
    text = _replace_or_append(text, "tools", tools_str)
    text = _set_section_string(text, "claude", "model", v.claude_model)
    text = _set_section_string(text, "codex", "model", v.codex_model)
    mode = v.structured_mode if v.structured_mode in ALL_STRUCTURED_MODES else "off"
    text = _set_section_string(text, "structured_naming", "mode", mode)
    text = _set_section_string(
        text, "structured_naming", "model", v.structured_model
    )
    text = _set_section_array(
        text, "structured_naming", "modules", v.structured_modules
    )
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
