"""Adapter registry."""

from __future__ import annotations

from .. import util
from ..config import Config
from .aider import AiderAdapter
from .antigravity import AntigravityAdapter
from .base import Adapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .continue_dev import ContinueAdapter
from .cursor import CursorAdapter
from .windsurf import WindsurfAdapter
from .zed import ZedAdapter

_REGISTRY: dict[str, type[Adapter]] = {
    ClaudeCodeAdapter.name: ClaudeCodeAdapter,
    CodexAdapter.name: CodexAdapter,
    CursorAdapter.name: CursorAdapter,
    AntigravityAdapter.name: AntigravityAdapter,
    ContinueAdapter.name: ContinueAdapter,
    ZedAdapter.name: ZedAdapter,
    WindsurfAdapter.name: WindsurfAdapter,
    AiderAdapter.name: AiderAdapter,
}


def _new_adapter(cls: type[Adapter], cfg: Config | None = None) -> Adapter:
    if cls is CodexAdapter and cfg is not None:
        return cls(codex_home=cfg.codex_home)
    return cls()


def all_adapters(cfg: Config | None = None) -> list[Adapter]:
    return [_new_adapter(cls, cfg) for cls in _REGISTRY.values()]


def get_adapters(cfg: Config) -> list[Adapter]:
    """Instantiate the configured adapters whose data is present locally."""
    out: list[Adapter] = []
    for name in cfg.tools:
        cls = _REGISTRY.get(name)
        if cls is None:
            util.log(f"unknown tool '{name}' in config", level="warn")
            continue
        adapter = _new_adapter(cls, cfg)
        if adapter.available():
            out.append(adapter)
        else:
            util.log(f"{name}: no local data found, skipping", level="debug")
    return out


__all__ = [
    "Adapter",
    "AiderAdapter",
    "AntigravityAdapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "ContinueAdapter",
    "CursorAdapter",
    "WindsurfAdapter",
    "ZedAdapter",
    "all_adapters",
    "get_adapters",
]
