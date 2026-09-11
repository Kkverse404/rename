"""Command-line interface for rename."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__, service, util
from . import config as config_mod
from .adapters import CodexAdapter, all_adapters, get_adapters
from .daemon_lock import DaemonAlreadyRunningError, DaemonLock
from .engine import Engine
from .namers import NAMER_NAMES, get_namer
from .namers.structured_codex import classifier_from_config
from .session_naming import SessionNamingWorkflow
from .session_registry import RegistryError, SessionRecord, SessionRegistry
from .state import StateStore


# --------------------------------------------------------------------------- #
# tiny tty helpers (no dependencies)
# --------------------------------------------------------------------------- #
def _tty() -> bool:
    return sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _tty() else text


def bold(s: str) -> str:
    return _c(s, "1")


def dim(s: str) -> str:
    return _c(s, "2")


def green(s: str) -> str:
    return _c(s, "32")


def trunc(text: str, width: int) -> str:
    text = text or "—"
    text = text.replace("\n", " ")
    if len(text) > width:
        return text[: width - 1] + "…"
    return text


# --------------------------------------------------------------------------- #
# wiring
# --------------------------------------------------------------------------- #
def _apply_overrides(cfg: config_mod.Config, args) -> config_mod.Config:
    if getattr(args, "idle", None) is not None:
        cfg.idle_seconds = args.idle
    if getattr(args, "interval", None) is not None:
        cfg.poll_seconds = args.interval
    if getattr(args, "namer", None):
        cfg.namer = args.namer
    if getattr(args, "tool", None):
        cfg.tools = tuple(args.tool)
    if getattr(args, "max_age_days", None) is not None:
        cfg.max_age_days = args.max_age_days
    if getattr(args, "dry_run", False):
        cfg.dry_run = True
    if getattr(args, "all", False) and getattr(args, "once", False):
        cfg.idle_seconds = 0  # include sessions of any age / idleness
        cfg.structured_naming.idle_seconds = 0
        cfg.max_age_days = 36500
    return cfg


def _build(cfg: config_mod.Config):
    adapters = get_adapters(cfg)
    namer = get_namer(cfg)
    state = StateStore()
    registry = SessionRegistry(util.registry_path())
    codex = next((a for a in adapters if isinstance(a, CodexAdapter)), None)
    writer = codex.writer if codex is not None else CodexAdapter(
        codex_home=cfg.codex_home
    ).writer
    classifier = classifier_from_config(
        cfg.structured_naming,
        codex_home=cfg.codex_home,
    )
    workflow = SessionNamingWorkflow(
        cfg.structured_naming,
        registry,
        classifier,
        writer,
        dry_run=cfg.dry_run,
    )
    return adapters, namer, state, Engine(
        cfg, adapters, namer, state, session_naming=workflow
    )


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_run(args) -> int:
    cfg = _apply_overrides(config_mod.load(), args)
    if not cfg.dry_run:
        config_mod.ensure_default()
    util.set_verbose(args.verbose)
    if getattr(args, "limit", None) is not None:
        cfg.batch_size = args.limit  # also caps the daemon's per-pass work
    session_filter: set[str] | None = None
    if getattr(args, "session", None):
        session_filter = set(args.session)
        # Per-session renames are explicit user gestures — bypass idle and
        # substance gates so they always go through (the engine still skips
        # "already current" titles, which is what we want).
        cfg.idle_seconds = 0
        cfg.structured_naming.idle_seconds = 0
        cfg.min_user_messages = 0
        cfg.max_age_days = 36500
    adapters, namer, state, engine = _build(cfg)
    if not adapters:
        util.log("no supported tools found on this machine.", level="warn")
        return 1
    include_historical = bool(getattr(args, "historical", False))
    if args.once:
        all_ = getattr(args, "all", False)
        limit = (
            0
            if (all_ or session_filter or include_historical)
            else getattr(args, "limit", None)
        )
        if cfg.structured_naming.applies and not cfg.dry_run:
            util.log(
                "structured Codex naming is enabled — eligible threads may receive "
                f"permanent IDs and use model '{cfg.structured_naming.model}'",
                level="warn",
            )
        elif (all_ or include_historical) and namer.name != "heuristic" and not cfg.dry_run:
            scope = "ALL historical sessions" if include_historical else "ALL eligible sessions"
            util.log(
                f"renaming {scope} via '{namer.name}' — this can take "
                "a while and use credits. Ctrl-C to stop; add --dry-run to preview, "
                "or --namer heuristic for instant offline titles.",
                level="warn",
            )
        json_output = bool(getattr(args, "json", False))
        renamed, total = engine.tick(
            limit=limit,
            progress=not json_output,
            quiet=json_output,
            session_filter=session_filter,
            include_historical=include_historical,
        )
        if json_output:
            print(
                json.dumps(
                    {"renamed": renamed, "candidates": total, "changed": renamed > 0},
                    ensure_ascii=False,
                )
            )
        else:
            util.log(f"done — renamed {renamed} of {total} candidate(s)")
        return 0
    def reload_engine() -> Engine:
        refreshed = _apply_overrides(config_mod.load(), args)
        if getattr(args, "limit", None) is not None:
            refreshed.batch_size = args.limit
        return _build(refreshed)[3]

    try:
        with DaemonLock(util.daemon_lock_path()):
            engine.run_forever(reload=reload_engine)
    except DaemonAlreadyRunningError:
        util.log("rename daemon is already running; exiting", level="warn")
        return 0
    except KeyboardInterrupt:
        util.log("stopped")
    return 0


def cmd_list(args) -> int:
    cfg = _apply_overrides(config_mod.load(), args)
    util.set_verbose(args.verbose)
    adapters, namer, state, engine = _build(cfg)
    if not adapters:
        print("No supported tools found (Claude Code, Codex, Cursor).")
        return 1

    plans, _, _ = engine.plan()
    if getattr(args, "json", False):
        now = util.now()
        out = [
            {
                "tool": p.session.tool,
                "id": p.session.id,
                "title": p.session.title,
                "proposed_title": p.new_title if p.action == "rename" else None,
                "action": p.action,
                "reason": p.reason,
                "idle_seconds": round(p.session.idle_seconds(now)),
                "cwd": p.session.cwd,
                "display_id": p.display_id,
                "naming_status": p.naming_status,
            }
            for _a, p in plans
        ]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    by_tool: dict[str, list] = {}
    for adapter, plan in plans:
        by_tool.setdefault(adapter.label, []).append(plan)

    now = util.now()
    would_rename = 0
    print()
    for label, group in by_tool.items():
        group.sort(key=lambda p: p.session.last_active, reverse=True)
        print(bold(label))
        for plan in group[: args.limit]:
            s = plan.session
            idle = util.fmt_dur(s.idle_seconds(now))
            cur = trunc(s.title or "—", 36)
            if plan.action == "rename":
                would_rename += 1
                right = (
                    green("→ " + plan.new_title)
                    if plan.new_title
                    else dim("· " + plan.reason)
                )
            elif plan.reason.startswith("active"):
                right = dim("· active")
            else:
                right = dim("· " + plan.reason)
            print(f"  {idle:>6}  {cur:<36}  {right}")
        if len(group) > args.limit:
            print(dim(f"  … and {len(group) - args.limit} more"))
        print()

    print(
        dim(
            f"{would_rename} session(s) would be renamed next pass "
            f"(idle ≥ {util.fmt_dur(cfg.idle_seconds)}, namer={namer.name}). "
            "Run `rename once` to apply, or `rename install` to do it continuously."
        )
    )
    return 0


def cmd_status(args) -> int:
    cfg = config_mod.load()
    cp = util.config_path()
    sp = util.state_path()
    tracked = 0
    baseline_ts: float | None = None
    if sp.exists():
        try:
            data = json.loads(sp.read_text("utf-8"))
            for k, v in data.items():
                if k == "_meta" and isinstance(v, dict):
                    raw = v.get("baseline_ts")
                    if raw is not None:
                        try:
                            baseline_ts = float(raw)
                        except (TypeError, ValueError):
                            pass
                elif isinstance(v, dict):
                    tracked += len(v)
        except (json.JSONDecodeError, OSError):
            pass
    resolved = get_namer(cfg).name
    enabled = set(cfg.tools)
    registry = SessionRegistry(util.registry_path())
    try:
        registry_counts = dict(registry.counts())
        registry_error = None
    except RegistryError as exc:
        registry_counts = None
        registry_error = str(exc)

    if getattr(args, "json", False):
        out = {
            "version": __version__,
            "config_path": str(cp),
            "config_exists": cp.exists(),
            "state_path": str(sp),
            "tracked": tracked,
            "baseline_ts": baseline_ts,
            "log_path": str(util.log_path()),
            "namer": cfg.namer,
            "namer_resolved": resolved,
            "idle_seconds": cfg.idle_seconds,
            "poll_seconds": cfg.poll_seconds,
            "max_age_days": cfg.max_age_days,
            "min_user_messages": cfg.min_user_messages,
            "batch_size": cfg.batch_size,
            "dry_run": cfg.dry_run,
            "structured_naming": {
                "mode": cfg.structured_naming.mode,
                "effective_read_only": (
                    cfg.dry_run or cfg.structured_naming.mode != "apply"
                ),
                "model": cfg.structured_naming.model,
                "idle_seconds": cfg.structured_naming.idle_seconds,
                "modules": list(cfg.structured_naming.modules),
                "registry_path": str(registry.path),
                "registry_counts": registry_counts,
                "registry_error": registry_error,
            },
            "daemon": {"status_line": service.status_line()},
            "tools": [
                {
                    "name": adapter.name,
                    "label": adapter.label,
                    "available": adapter.available(),
                    "enabled": adapter.name in enabled,
                }
                for adapter in all_adapters(cfg)
            ],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(bold(f"rename {__version__}"))
    cp_note = "" if cp.exists() else dim("  (using defaults; `rename config` to create)")
    print(f"  config : {cp}{cp_note}")
    print(f"  state  : {sp}  ({tracked} tracked)")
    print(f"  log    : {util.log_path()}")
    structured = cfg.structured_naming
    effective = "read-only" if cfg.dry_run or structured.mode != "apply" else "write-enabled"
    print(
        f"  naming : structured={structured.mode} ({effective})  "
        f"registry={registry.path}"
    )
    if registry_counts is not None:
        print(f"           registered={registry_counts['total']}  states={registry_counts}")
    elif registry_error:
        print(f"           registry error: {registry_error}")
    namer_str = cfg.namer if resolved == cfg.namer else f"{cfg.namer} → {resolved}"
    print(
        f"  config : idle={util.fmt_dur(cfg.idle_seconds)}  "
        f"poll={util.fmt_dur(cfg.poll_seconds)}  namer={namer_str}"
    )
    print(f"  {service.status_line()}")

    print(bold("  tools:"))
    for adapter in all_adapters(cfg):
        avail = green("found") if adapter.available() else dim("not found")
        suffix = "" if adapter.name in enabled else dim("  [disabled in config]")
        print(f"    {adapter.label:<13} {avail}{suffix}")
    return 0


def _find_registry_record(
    registry: SessionRegistry, identifier: str
) -> SessionRecord | None:
    value = identifier.removeprefix("#")
    records = registry.list_records(tool="codex")
    if value.isdigit():
        display_id = int(value)
        return next((record for record in records if record.display_id == display_id), None)
    return next(
        (record for record in records if record.native_session_id == identifier),
        None,
    )


def cmd_naming_status(args) -> int:
    registry = SessionRegistry(util.registry_path())
    try:
        records = registry.list_records(tool="codex")
        counts = dict(registry.counts(tool="codex"))
    except RegistryError as exc:
        util.log(f"structured registry unavailable: {exc}", level="error")
        return 1
    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "registry_path": str(registry.path),
                    "counts": counts,
                    "sessions": [
                        {
                            "display_id": record.display_id,
                            "native_session_id": record.native_session_id,
                            "status": record.status,
                            "original_title": record.original_title,
                            "desired_title": record.desired_title,
                            "observed_title": record.observed_title,
                            "updated_at": record.updated_at,
                        }
                        for record in records
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(bold("structured naming registry"))
    print(f"  path   : {registry.path}")
    print(f"  counts : {counts}")
    for record in records:
        title = record.desired_title or record.observed_title or "—"
        print(
            f"  #{record.display_id:<5} {record.status:<15} "
            f"{trunc(title, 58)}  {record.native_session_id}"
        )
    return 0


def cmd_naming_reopen(args) -> int:
    cfg = config_mod.load()
    registry = SessionRegistry(util.registry_path())
    try:
        record = _find_registry_record(registry, args.session)
        if record is None:
            util.log(f"no registered Codex session matches {args.session!r}", level="error")
            return 1
        read_only = cfg.dry_run or getattr(args, "dry_run", False)
        reopened = (
            record
            if read_only
            else registry.reopen(record.tool, record.native_session_id)
        )
    except RegistryError as exc:
        util.log(f"could not reopen structured session: {exc}", level="error")
        return 1
    payload = {
        "display_id": reopened.display_id,
        "native_session_id": reopened.native_session_id,
        "status": reopened.status,
        "dry_run": read_only,
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        verb = "would reopen" if read_only else "reopened"
        print(
            f"{verb} #{reopened.display_id} ({reopened.native_session_id}); "
            "its display ID is unchanged"
        )
    return 0


def cmd_naming_rollback(args) -> int:
    cfg = config_mod.load()
    registry = SessionRegistry(util.registry_path())
    try:
        record = _find_registry_record(registry, args.session)
    except RegistryError as exc:
        util.log(f"structured registry unavailable: {exc}", level="error")
        return 1
    if record is None:
        util.log(f"no registered Codex session matches {args.session!r}", level="error")
        return 1
    if record.status != "finalized":
        util.log(
            f"session #{record.display_id} is {record.status}, not finalized",
            level="error",
        )
        return 1
    if record.original_title is None:
        util.log(
            f"session #{record.display_id} had no restorable original title",
            level="error",
        )
        return 1
    read_only = cfg.dry_run or getattr(args, "dry_run", False)
    if read_only:
        payload = {
            "display_id": record.display_id,
            "native_session_id": record.native_session_id,
            "status": record.status,
            "would_restore_title": record.original_title,
            "dry_run": True,
        }
        if getattr(args, "json", False):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(
                f"would roll back #{record.display_id} to {record.original_title!r}; "
                "dry-run made no changes"
            )
        return 0

    codex = CodexAdapter(codex_home=cfg.codex_home)
    if not codex.available():
        util.log("Codex session store was not found", level="error")
        return 1
    try:
        session = next(
            item for item in codex.discover(0.0) if item.id == record.native_session_id
        )
    except StopIteration:
        util.log("the registered Codex session is not currently discoverable", level="error")
        return 1
    if session.native_title != record.desired_title:
        util.log(
            "rollback stopped because the native title was changed externally",
            level="error",
        )
        return 1
    try:
        registry.stage_rollback(record.tool, record.native_session_id)
        codex.writer.set_title(
            session.id,
            record.original_title,
            expected_title=record.desired_title,
            expected_updated_at=session.meta.get("updated_at"),
            expected_status=session.meta.get("status"),
        )
        rolled_back = registry.rolled_back(
            record.tool, record.native_session_id, observed=record.original_title
        )
    except Exception as exc:
        util.log(f"rollback failed safely: {exc}", level="error")
        return 1
    payload = {
        "display_id": rolled_back.display_id,
        "native_session_id": rolled_back.native_session_id,
        "status": rolled_back.status,
        "restored_title": record.original_title,
    }
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"rolled back #{rolled_back.display_id} to {record.original_title!r}")
    return 0


def cmd_config(args) -> int:
    path = config_mod.ensure_default()
    if args.path:
        print(path)
        return 0
    print(f"# {path}\n")
    print(path.read_text("utf-8"))
    return 0


def cmd_install(args) -> int:
    config_mod.ensure_default()
    return service.install()


def cmd_uninstall(args) -> int:
    return service.uninstall()


def _highlight(text: str, query: str) -> str:
    """Bold-yellow every case-insensitive occurrence of query in text."""
    if not query:
        return text
    low, q = text.lower(), query.lower()
    out, i = [], 0
    while True:
        j = low.find(q, i)
        if j < 0:
            out.append(text[i:])
            return "".join(out)
        out.append(text[i:j])
        out.append(_c(text[j : j + len(query)], "1;33"))
        i = j + len(query)


def _content_hit(adapter, session, q: str) -> str | None:
    """A short snippet around the first message that contains q, else None."""
    try:
        msgs = adapter.read_transcript(session)
    except Exception:
        return None
    for m in msgs:
        text = util.clean_text(m.text)
        idx = text.lower().find(q)
        if idx >= 0:
            snippet = text[max(0, idx - 30) : idx + len(q) + 40].strip()
            return trunc(snippet, 80)
    return None


def cmd_search(args) -> int:
    cfg = _apply_overrides(config_mod.load(), args)
    util.set_verbose(args.verbose)
    adapters = get_adapters(cfg)
    if not adapters:
        print("No supported tools found (Claude Code, Codex, Cursor).")
        return 1

    q = args.query.lower()
    since = util.now() - args.days * 86400 if args.days else 0.0
    now = util.now()
    hits: list[tuple] = []  # (last_active, label, session, snippet)
    for adapter in adapters:
        try:
            sessions = adapter.discover(since)
        except Exception as exc:
            util.log(f"{adapter.name}: search failed: {exc}", level="warn")
            continue
        for s in sessions:
            if q in (s.title or "").lower():
                hits.append((s.last_active, adapter.label, s, None))
            elif args.content:
                snippet = _content_hit(adapter, s, q)
                if snippet:
                    hits.append((s.last_active, adapter.label, s, snippet))
    hits.sort(key=lambda h: h[0], reverse=True)

    if getattr(args, "json", False):
        out = [
            {
                "tool": s.tool,
                "id": s.id,
                "title": s.title,
                "idle_seconds": round(s.idle_seconds(now)),
                "cwd": s.cwd,
                "snippet": snippet,
            }
            for _la, _label, s, snippet in hits[: args.limit]
        ]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if not hits:
        scope = "titles and content" if args.content else "titles"
        print(dim(f'No sessions matching "{args.query}" in {scope} (last {args.days}d).'))
        if not args.content:
            print(dim("Tip: add --content to also search message text."))
        return 0

    shown = hits[: args.limit]
    print()
    print(bold(f'🔍 "{args.query}" — {len(hits)} match{"" if len(hits) == 1 else "es"}'))
    print()
    for _last_active, label, s, snippet in shown:
        when = util.fmt_dur(s.idle_seconds(now))
        loc = dim("  " + os.path.basename(s.cwd.rstrip("/"))) if s.cwd else ""
        title = _highlight(trunc(s.title or "—", 50), args.query)
        print(f"  {label:<12} {when:>5}  {title}{loc}")
        if snippet:
            print(f"               {dim('…')} {_highlight(snippet, args.query)}")
    if len(hits) > len(shown):
        print(dim(f"  … and {len(hits) - len(shown)} more"))
    print()
    return 0


def cmd_stats(args) -> int:
    cfg = _apply_overrides(config_mod.load(), args)
    util.set_verbose(args.verbose)
    adapters = get_adapters(cfg)
    if not adapters:
        print("No supported tools found (Claude Code, Codex, Cursor).")
        return 1

    now = util.now()
    since = now - args.days * 86400 if args.days else 0.0
    state = StateStore()
    state.load()

    rows: list[dict] = []
    oldest: tuple[float, str] | None = None
    for adapter in adapters:
        try:
            sessions = adapter.discover(since)
        except Exception as exc:
            util.log(f"{adapter.name}: stats failed: {exc}", level="warn")
            continue
        untitled = sum(1 for s in sessions if not (s.title or "").strip())
        stale = sum(1 for s in sessions if s.idle_seconds(now) >= cfg.idle_seconds)
        for s in sessions:
            if oldest is None or s.last_active < oldest[0]:
                oldest = (s.last_active, adapter.label)
        rows.append(
            {
                "tool": adapter.name,
                "label": adapter.label,
                "sessions": len(sessions),
                "untitled": untitled,
                "stale": stale,
                "renamed": state.renamed_count(adapter.name),
            }
        )

    keys = ("sessions", "untitled", "stale", "renamed")
    total = {k: sum(r[k] for r in rows) for k in keys}

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "scope_days": args.days or None,
                    "tools": rows,
                    "total": total,
                    "oldest_active_seconds": round(now - oldest[0]) if oldest else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    scope = "all time" if not args.days else f"last {args.days}d"
    print()
    print(bold("rename stats") + dim(f"   ({scope})"))
    print()
    print(bold(f"  {'Tool':<13}{'Sessions':>10}{'Untitled':>10}{'Stale':>8}{'Renamed':>9}"))
    for r in rows:
        print(
            f"  {r['label']:<13}{r['sessions']:>10}{r['untitled']:>10}"
            f"{r['stale']:>8}{r['renamed']:>9}"
        )
    print(dim("  " + "─" * 48))
    print(
        f"  {'Total':<13}{total['sessions']:>10}{total['untitled']:>10}"
        f"{total['stale']:>8}{total['renamed']:>9}"
    )
    print()
    if oldest:
        print(dim(f"  Oldest active: {util.fmt_dur(now - oldest[0])} ago ({oldest[1]})"))
    print(
        dim(
            f"  Stale = idle ≥ {util.fmt_dur(cfg.idle_seconds)} → rename will "
            "rename these. Run `rename once` to apply."
        )
    )
    return 0


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #
def _add_common(sp: argparse.ArgumentParser) -> None:
    sp.add_argument(
        "--tool",
        action="append",
        choices=config_mod.ALL_TOOLS,
        help="limit to specific tool(s); repeatable",
    )
    sp.add_argument("--namer", choices=NAMER_NAMES, help="override the namer")
    sp.add_argument("--idle", type=int, metavar="SEC", help="idle threshold (seconds)")
    sp.add_argument(
        "--max-age-days",
        dest="max_age_days",
        type=int,
        help="only consider sessions active within N days",
    )
    sp.add_argument("-v", "--verbose", action="store_true", help="verbose logging")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rename",
        description="Auto-rename idle Claude Code, Codex & Cursor sessions "
        "to match what they're actually about.",
    )
    p.add_argument("-V", "--version", action="version", version=f"rename {__version__}")
    sub = p.add_subparsers(dest="cmd")

    pr = sub.add_parser("run", help="run the renamer (daemon by default)")
    _add_common(pr)
    pr.add_argument("--once", action="store_true", help="single pass, then exit")
    pr.add_argument("--interval", type=int, metavar="SEC", help="seconds between passes")
    pr.add_argument("--limit", type=int, metavar="N", help="max sessions to rename per pass")
    pr.add_argument("--dry-run", action="store_true", help="log changes without writing")
    pr.add_argument("--json", action="store_true", help="output the pass result as JSON")
    pr.add_argument(
        "--all", action="store_true", help="with --once: rename ALL eligible sessions now"
    )
    pr.add_argument(
        "--historical",
        action="store_true",
        help="also include sessions that existed before rename's first run "
        "(by default the daemon only touches conversations active after install)",
    )
    pr.set_defaults(func=cmd_run)

    po = sub.add_parser("once", help="single pass, then exit (alias for `run --once`)")
    _add_common(po)
    po.add_argument("--limit", type=int, metavar="N", help="max sessions to rename")
    po.add_argument("--dry-run", action="store_true", help="log changes without writing")
    po.add_argument("--json", action="store_true", help="output the pass result as JSON")
    po.add_argument(
        "--all",
        action="store_true",
        help="rename ALL eligible sessions now (idle 0, no age limit, no batch cap)",
    )
    po.add_argument(
        "--historical",
        action="store_true",
        help="also include sessions that existed before rename's first run; "
        "implies --all and ignores --max-age-days. Use this to opt-in to "
        "renaming your entire chat history (the GUI's 'Rename historical "
        "sessions' button does the same thing).",
    )
    po.add_argument(
        "--session",
        action="append",
        metavar="ID",
        help="rename only sessions with these ids (repeatable); bypasses idle and "
        "min-message gates so manual one-off renames work even on active or short chats",
    )
    po.set_defaults(func=cmd_run, once=True)

    pl = sub.add_parser("list", help="preview sessions and the titles rename would set")
    _add_common(pl)
    pl.add_argument("--limit", type=int, default=40, help="max rows per tool")
    pl.add_argument("--json", action="store_true", help="output JSON instead of a table")
    pl.set_defaults(func=cmd_list, dry_run=True)

    psr = sub.add_parser(
        "search", help="find sessions across all tools by title (or --content)"
    )
    psr.add_argument("query", help="text to search for")
    psr.add_argument(
        "--content", action="store_true", help="also search message text (slower)"
    )
    psr.add_argument(
        "--tool",
        action="append",
        choices=config_mod.ALL_TOOLS,
        help="limit to specific tool(s); repeatable",
    )
    psr.add_argument(
        "--days", type=int, default=90, help="how far back to search (default: 90)"
    )
    psr.add_argument("--limit", type=int, default=30, help="max results to show")
    psr.add_argument("--json", action="store_true", help="output JSON instead of a table")
    psr.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
    psr.set_defaults(func=cmd_search)

    pstat = sub.add_parser("stats", help="overview of your sessions across all tools")
    pstat.add_argument(
        "--tool",
        action="append",
        choices=config_mod.ALL_TOOLS,
        help="limit to specific tool(s); repeatable",
    )
    pstat.add_argument("--days", type=int, default=0, help="limit to last N days (0 = all)")
    pstat.add_argument("--json", action="store_true", help="output JSON instead of a table")
    pstat.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
    pstat.set_defaults(func=cmd_stats)

    ps = sub.add_parser("status", help="show config, detected tools and daemon status")
    ps.add_argument("--json", action="store_true", help="output JSON instead of text")
    ps.set_defaults(func=cmd_status)

    pn = sub.add_parser("naming", help="inspect or operate structured Codex names")
    pn_sub = pn.add_subparsers(dest="naming_cmd", required=True)
    pns = pn_sub.add_parser("status", help="show the permanent naming registry")
    pns.add_argument("--json", action="store_true", help="output JSON instead of text")
    pns.set_defaults(func=cmd_naming_status)
    pnr = pn_sub.add_parser("reopen", help="allow a protected session to classify again")
    pnr.add_argument("session", help="native session id or #display-id")
    pnr.add_argument("--json", action="store_true", help="output JSON instead of text")
    pnr.add_argument("--dry-run", action="store_true", help="show the action without writing")
    pnr.set_defaults(func=cmd_naming_reopen)
    pnb = pn_sub.add_parser("rollback", help="restore the title captured before naming")
    pnb.add_argument("session", help="native session id or #display-id")
    pnb.add_argument("--json", action="store_true", help="output JSON instead of text")
    pnb.add_argument("--dry-run", action="store_true", help="show the action without writing")
    pnb.set_defaults(func=cmd_naming_rollback)

    pc = sub.add_parser("config", help="create/show the config file")
    pc.add_argument("--path", action="store_true", help="print the config path only")
    pc.set_defaults(func=cmd_config)

    pi = sub.add_parser("install", help="install the background service")
    pi.set_defaults(func=cmd_install)

    pu = sub.add_parser("uninstall", help="remove the background service")
    pu.set_defaults(func=cmd_uninstall)

    return p


_DEFAULTS = {
    "once": False,
    "interval": None,
    "dry_run": False,
    "historical": False,
    "session": None,
    "idle": None,
    "namer": None,
    "tool": None,
    "max_age_days": None,
    "verbose": False,
    "limit": 40,
    "path": False,
    "json": False,
    "all": False,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0
    for attr, default in _DEFAULTS.items():
        if not hasattr(args, attr):
            setattr(args, attr, default)
    return args.func(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
