# Execution progress

This dated work log records commands, evidence, and unverified boundaries for
the structured Codex naming work order.

## P0 — source audit and baseline

- Fork: `https://github.com/Kkverse404/rename`
- Upstream: `https://github.com/study8677/rename`
- Branch: `feat/global-session-naming`
- Baseline HEAD: `abfb0151942bd0fbea32e10e5d1e6ae3fb7640f1`
- Baseline worktree: clean before dependency setup.
- Baseline test: `uv run pytest -q` -> exit 0, `83 passed`.
- Runtime probe: Python 3.11.15, Codex CLI 0.153.4. Model-list returned
  `gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna` with the
  expected reasoning-effort choices.
- Source finding: the legacy Codex adapter reads and writes
  `Path.home() / ".codex"` SQLite directly; it does not honor `CODEX_HOME`,
  compare the old title, check row count, or read back the write.
- Windows finding: `shutil.which("claude")` resolves a `.CMD` launcher on this
  machine while the current subprocess runner executes the bare name and fails.

## P1 — isolated Codex writer proof

- Official documentation confirms stable `thread/name/set` support for loaded
  threads and persisted rollouts, followed by `thread/name/updated`.
- Local generated protocol schema confirms `ThreadSetNameParams` requires
  string fields `threadId` and `name`.
- Initial isolated probe rejected the documentation example value
  `sandbox="readOnly"`; this CLI version requires `sandbox="read-only"`.
- Retried with temporary `CODEX_HOME` and a synthetic empty thread:
  `thread/name/set` returned success, same-process `thread/read` returned the
  new title, and a fresh app-server process returned the same title.
- The probe did not call a model or touch the user's real Codex store.
- Unverified: whether a separately launched app-server pushes an immediate
  live update into an already-running Codex Desktop window. This remains a P7
  acceptance item and cannot be inferred from persistence alone.

## P2 — frozen contract

- `docs/STRUCTURED_NAMING.md` defines the opt-in `off`/`preview`/`apply`
  modes, permanent display identity, title grammar, bounded classifier result,
  recovery states, native-name compare-and-set rules, manual-edit protection,
  reopen, and rollback.
- Codex explicit `name` is kept separate from the generated `title`/`preview`
  fallback. User-facing reads use the effective display value; conflict and
  recovery checks use the raw native name.
- The app-server currently exposes a read-then-set protocol, not a server-side
  atomic compare-and-set operation. The client narrows and detects conflicts,
  but a different writer can still race between its read and set calls.

## P3 — independent modules

- Added a SQLite registry with monotonic, non-recycled display IDs, immutable
  thread identities, transactional allocation, write staging, recovery data,
  and an append-only operation log.
- Added strict classifier parsing and validation around a bounded synthetic
  transcript. The program owns ID allocation, module allowlisting, evidence
  validation, title formatting, and all state transitions.
- Added a short-lived Codex app-server writer with initialization, native-name
  comparison, `thread/name/set`, read-back verification, and explicit protocol,
  conflict, timeout, and verification errors. There is no SQLite write fallback.
- Added shared Windows batch-launch quoting and a process-wide daemon lock.

## P4 — serial integration

- Integrated the workflow into the engine, configuration, CLI, status/list
  output, daemon loop, and Windows GUI while retaining the legacy path when the
  mode is off.
- `preview`, global `dry_run`, status/list, and GUI refresh perform no model
  call, allocation, registry write, or native title write.
- The daemon reloads configuration on each pass while preserving explicit CLI
  overrides, and only one daemon loop can own a state directory at a time.
- Registered sessions remain protected when structured naming is disabled;
  finalized, manually overridden, and rolled-back sessions require an explicit
  operator action before another attempt.

## P5 — verification and synthetic model evaluation

- The expanded suite covers allocation concurrency, registry corruption,
  permanent identities, native/fallback names, both halves of recovery,
  rollback interruption, manual edits, pure preview/dry-run paths, JSON stdout,
  Windows Unicode/space/ampersand launch paths, explicit percent rejection,
  `CODEX_HOME`, daemon locking/reload, GUI configuration, and legacy behavior.
- Latest full automated run before delivery cleanup: `175 passed`.
- Ruff and `git diff --check` passed.
- A live `gpt-5.6-terra` evaluation used eight synthetic conversations only:
  `8/8` cases passed, including ambiguous work, prompt injection, convergence,
  latest-task selection, and long-context selection. This is a semantic smoke
  test, not evidence about production conversations.

## P6 — independent review and repair

- Three independent reviews covered data/recovery, Windows/regression, and
  requirements/complexity. All reported findings were repaired and received a
  regression test.
- Closure checks include stable IDs and zero model calls across interrupted
  writes/rollbacks, correct runtime reload of the CLI limit override, truthful
  JSON/GUI changed status, and safe handling of an externally owned daemon.

## P7 — Windows acceptance

- The Windows CLI path, `.CMD` launcher handling, `CODEX_HOME`, named mutex,
  isolated app-server persistence, configuration editing, and packaged entry
  points were exercised without the user's real Codex store.
- Source-tree `uv run rename` is unreliable from this machine's non-ASCII path
  because the editable-install path is decoded incorrectly by the toolchain.
  An isolated wheel install is the packaging acceptance path and does not have
  that editable-path dependency.
- Live Codex Desktop display refresh, interactive human rename, and rollback in
  a real Desktop thread were deliberately not exercised. Persistence in a
  separate app-server process does not prove that UI behavior.

## P8 — readiness

- `CORE_READY`: yes.
- `WINDOWS_CLI_READY`: yes, subject to the documented app-server client-side
  race boundary.
- `WINDOWS_APP_READY`: unproven. Automated GUI/configuration paths and isolated
  rendering pass, but the real Desktop interaction boundary remains untested.
- At completion of the planned implementation, no background service was
  installed, no global configuration or real thread was changed, and no
  commit, push, pull request, deployment, or release was created.

## Follow-up delivery

- On explicit user request, the completed work was committed on
  `feat/global-session-naming` and pushed to the fork remote
  `https://github.com/Kkverse404/rename`.
- The upstream remote remained fetch-only with its push URL disabled. No pull
  request, deployment, service installation, global configuration change, or
  real-thread mutation was performed.

## Follow-up title refinement

- The visible grammar was simplified to `#ID- summary`; project/module labels
  remain internal validation metadata and are no longer shown in Codex titles.
- The classifier now asks for only the current task and key object. The program
  bounds Chinese summaries to 24 characters and other summaries to eight words
  and 64 characters, and removes trailing title punctuation.
- Final verification after the refinement: Ruff passed, `176 passed`,
  `git diff --check` passed, and the live synthetic `gpt-5.6-terra` evaluation
  passed `8/8` with concise raw summaries before program-side bounding.

## Follow-up Windows startup recovery

- Live diagnosis found that the Windows login-startup daemon inherited a PATH
  without the Codex Desktop app-local `bin` directory. The daemon stayed alive,
  but structured decisions safely remained pending with
  `ClassifierUnavailableError`.
- Classifier and writer executable lookup now share one resolver that falls back
  to the current Codex Desktop installation. Cached classifier failures retry
  after five minutes without weakening the unchanged-content cache for genuinely
  ambiguous tasks.

## Resolved-title lifecycle

- Managed Codex titles now add `（已解决）` only when the bounded classifier
  identifies direct user confirmation or a completion report with verification
  and no remaining required work.
- Finalized unresolved sessions are reviewed only after new activity. The
  original summary and rollback target remain unchanged, external title edits
  stay protected, and unresolved decisions are cached until activity changes.
