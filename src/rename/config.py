"""Configuration: typed defaults, TOML loading, and a friendly default file."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import util

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

DEFAULT_TOML = """\
# rename configuration — https://github.com/study8677/rename
# All times are in seconds. A running daemon reloads this file before its next pass.

# Rename a session once it has been idle for this long (default: 5 minutes).
idle_seconds = 300

# How often the daemon scans for idle sessions.
poll_seconds = 60

# Rename at most this many sessions per scan, so a big backlog doesn't hammer
# your claude/codex CLI all at once — the daemon works through the rest over the
# next passes (most-recent first). 0 = no limit. `rename once --all` ignores it.
batch_size = 25

# Which tools to manage. Remove any you don't use.
#
# Stable: "claude-code", "codex"
# Experimental: "cursor", "antigravity", "continue", "zed", "windsurf", "aider"
#   * "antigravity" is read-only for naming when transcripts are encrypted, but
#     is still listed/searched alongside the others (drop it to hide it)
#   * "aider" is read-only — Aider has no native title slot, so renames go to
#     a sidecar file that only rename reads
tools = ["claude-code", "codex", "cursor", "antigravity"]

# How titles are generated. The default needs NO API key.
#   "auto"      - reuse the `claude` or `codex` CLI you're already logged into
#                 for good titles (no API key!); falls back to "heuristic" if
#                 neither is installed. (default)
#   "heuristic" - instant, fully offline, no LLM, no token cost
#   "claude"    - always use the `claude` CLI (defaults to the fast Haiku model)
#   "codex"     - always use the `codex` CLI (defaults to gpt-5.3-codex-spark)
#   "anthropic" - Anthropic API directly, with your OWN key (set api_key in the
#                 [anthropic] table below, or export ANTHROPIC_API_KEY)
#   "openai"    - OpenAI API directly, with your OWN key (set api_key in the
#                 [openai] table below, or export OPENAI_API_KEY)
namer = "auto"

# Ignore sessions whose last activity is older than this many days.
max_age_days = 7

# Only rename sessions with at least this many real (non-trivial) user messages.
min_user_messages = 1

# Set true to preview renames without writing anything.
dry_run = false

# Codex-only stable IDs and one-time structured titles. This is deliberately
# off by default. "preview" is read-only; "apply" can allocate IDs, call the
# configured model, and write titles through the Codex app-server protocol.
[structured_naming]
mode = "off" # "off" | "preview" | "apply"
model = "gpt-5.6-terra"
reasoning_effort = "low"
modules = [] # allowed labels; empty derives one label from the project folder
confidence_threshold = 0.80
idle_seconds = 30
max_messages = 12
max_input_chars = 12000
max_output_bytes = 16384
timeout_seconds = 90

# Model overrides for the CLI namers (optional). These reuse your existing
# login — no API key. Defaults are the fast/cheap models, which are plenty for
# a short title. CLI namers run ephemerally (no extra Claude/Codex session).
[claude]
model = "haiku"

[codex]
model = "gpt-5.3-codex-spark"
# home = "D:/path/to/codex-home" # otherwise CODEX_HOME, then ~/.codex

# Bring-your-own-key namers. To use one, set `namer` above to "anthropic" or
# "openai" and provide a key below (or via the matching environment variable).
# The Rename desktop app can fill these in for you under Settings → Namer.
[anthropic]
model = "claude-haiku-4-5"
# api_key = "sk-ant-..."     # or export ANTHROPIC_API_KEY

[openai]
model = "gpt-4o-mini"
# api_key = "sk-..."         # or export OPENAI_API_KEY
"""


@dataclass
class StructuredNamingConfig:
    """Effective settings for the opt-in Codex structured naming workflow."""

    mode: str = "off"
    model: str = "gpt-5.6-terra"
    reasoning_effort: str = "low"
    modules: tuple[str, ...] = ()
    confidence_threshold: float = 0.80
    idle_seconds: int = 30
    max_messages: int = 12
    max_input_chars: int = 12_000
    max_output_bytes: int = 16_384
    timeout_seconds: int = 90

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @property
    def applies(self) -> bool:
        return self.mode == "apply"


@dataclass
class Config:
    idle_seconds: int = 300
    poll_seconds: int = 60
    batch_size: int = 25
    tools: tuple[str, ...] = ALL_TOOLS
    namer: str = "auto"
    max_age_days: int = 7
    min_user_messages: int = 1
    dry_run: bool = False
    codex_home: str | None = None
    structured_naming: StructuredNamingConfig = field(default_factory=StructuredNamingConfig)
    raw: dict[str, Any] = field(default_factory=dict)

    def namer_options(self, name: str) -> dict[str, Any]:
        """Return the ``[name]`` sub-table from config (e.g. model overrides)."""
        opts = self.raw.get(name, {})
        return opts if isinstance(opts, dict) else {}


def load(path: Path | None = None) -> Config:
    """Load config from disk, falling back to defaults for anything missing."""
    path = path or util.config_path()
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            raw = tomllib.loads(path.read_text("utf-8"))
        except (tomllib.TOMLDecodeError, OSError) as exc:
            util.log(f"could not read config at {path}: {exc}", level="warn")
            raw = {}

    cfg = Config(raw=raw)
    cfg.idle_seconds = int(raw.get("idle_seconds", cfg.idle_seconds))
    cfg.poll_seconds = int(raw.get("poll_seconds", cfg.poll_seconds))
    cfg.batch_size = int(raw.get("batch_size", cfg.batch_size))
    cfg.namer = str(raw.get("namer", cfg.namer))
    cfg.max_age_days = int(raw.get("max_age_days", cfg.max_age_days))
    cfg.min_user_messages = int(raw.get("min_user_messages", cfg.min_user_messages))
    cfg.dry_run = bool(raw.get("dry_run", cfg.dry_run))

    codex = raw.get("codex")
    if isinstance(codex, dict):
        home = codex.get("home")
        if isinstance(home, str) and home.strip():
            cfg.codex_home = home.strip()

    structured = raw.get("structured_naming")
    if isinstance(structured, dict):
        mode = str(structured.get("mode", "off")).strip().lower()
        if mode not in {"off", "preview", "apply"}:
            util.log(
                "structured_naming.mode must be off, preview, or apply; "
                f"got {mode!r}. Structured naming is disabled.",
                level="warn",
            )
            mode = "off"
        modules = structured.get("modules", [])
        module_values = (
            tuple(str(value).strip() for value in modules if str(value).strip())
            if isinstance(modules, list)
            else ()
        )
        try:
            threshold = float(structured.get("confidence_threshold", 0.80))
        except (TypeError, ValueError):
            threshold = 0.80
        cfg.structured_naming = StructuredNamingConfig(
            mode=mode,
            model=str(structured.get("model", "gpt-5.6-terra")).strip()
            or "gpt-5.6-terra",
            reasoning_effort=(
                str(structured.get("reasoning_effort", "low")).strip().lower()
                if str(structured.get("reasoning_effort", "low")).strip().lower()
                in {"low", "medium", "high", "xhigh", "max", "ultra"}
                else "low"
            ),
            modules=module_values,
            confidence_threshold=min(1.0, max(0.0, threshold)),
            idle_seconds=max(0, int(structured.get("idle_seconds", 30))),
            max_messages=max(1, int(structured.get("max_messages", 12))),
            max_input_chars=max(256, int(structured.get("max_input_chars", 12_000))),
            max_output_bytes=max(256, int(structured.get("max_output_bytes", 16_384))),
            timeout_seconds=max(1, int(structured.get("timeout_seconds", 90))),
        )

    tools = raw.get("tools")
    if isinstance(tools, list):
        cfg.tools = tuple(str(t) for t in tools if t in ALL_TOOLS)
    return cfg


def ensure_default(path: Path | None = None) -> Path:
    """Create the default config file if absent. Returns its path."""
    path = path or util.config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_TOML, "utf-8")
    return path
