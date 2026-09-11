# Structured Codex session naming execution plan

This work order adds an opt-in Codex naming mode to `rename`. It assigns a
machine-local, monotonic display ID to each managed Codex thread, waits until
the task is explicit, then freezes a title in this form:

```text
#{display_id}- {summary}
```

The Codex thread UUID remains unchanged. The registry is global to one local
`rename` state directory across projects and worktrees.

## Delivery boundaries

- Preserve every existing adapter and the existing `Namer.generate() -> str`
  interface when structured naming is off.
- Structured naming is Codex-only in V1 and defaults to `off`.
- `list`, `status`, GUI refresh, and every dry-run path are read-only: they do
  not call a model, allocate an ID, write the registry, or rename a thread.
- Tests use temporary state directories, temporary `CODEX_HOME` directories,
  synthetic rollouts, and fake classifiers unless explicitly labelled as a
  live-model or Windows App acceptance check.
- Never fall back from the Codex app-server protocol to a live SQLite title
  update.
- Do not install a background service, change global configuration, push a
  branch, open a pull request, or publish a release as part of this work.

## Module ownership

The public seam is a structured naming workflow called once by the engine.
The workflow owns allocation, classification validation, state transitions,
write staging, conflict handling, and recovery.

- `session_registry.py`: permanent identity, transactional state, operation log.
- `session_naming.py`: policy, state machine, title formatting, recovery.
- `namers/structured_codex.py`: one bounded structured Codex classification.
- `adapters/codex_writer.py`: app-server handshake, compare, write, and read-back.
- `engine.py`, `config.py`, `models.py`, `cli.py`: serial integration only.
- Windows GUI and documentation: expose the same modes and do not bypass the
  workflow through `once --session`.

## Execution stages

1. **P0 — source audit and baseline**
   Record fork, remotes, branch, HEAD, dirty state, runtime/model capability,
   source call graph, and baseline tests.
2. **P1 — safe Windows writer proof**
   Validate `thread/name/set` and `thread/read` in an isolated `CODEX_HOME`,
   including a fresh app-server process. Record the unverified Desktop boundary.
3. **P2 — freeze the contract**
   Freeze identity, state transitions, classifier JSON, title grammar, preview,
   manual edit, conflict, recovery, reopen, and rollback semantics.
4. **P3 — implement independent modules**
   Build the registry, classifier, writer, and focused tests. Model calls occur
   outside registry transactions.
5. **P4 — serial integration**
   Route every engine/CLI/GUI entry through the workflow while retaining the
   legacy path for unmanaged sessions.
6. **P5 — verification and model evaluation**
   Run unit, regression, concurrency, fault-injection, Windows path, preview,
   and synthetic semantic-quality checks. Run a live model evaluation only on
   synthetic prompts and report it separately from fake-classifier coverage.
7. **P6 — independent review and repair**
   Review data/recovery, Windows/regression, and requirements/complexity in
   independent passes. Findings include severity, location, reproduction,
   impact, minimal fix, regression test, and closure evidence.
8. **P7 — Windows acceptance**
   Verify CLI and isolated app-server persistence. Verify live Desktop display,
   reopen, restart, human rename, and rollback only when an isolated Desktop
   thread can be used safely; otherwise report `WINDOWS_APP_READY` as unproven.
9. **P8 — delivery preparation**
   Re-run all gates, document enable/disable/recovery, clean temporary files,
   and report `CORE_READY`, `WINDOWS_CLI_READY`, and `WINDOWS_APP_READY`
   independently.

## Release gates

- Concurrent allocation cannot duplicate or reuse display IDs.
- Historical or temporarily undiscoverable threads retain their identity.
- Ambiguous work stays pending without a title write.
- A ready decision must pass internal-module, evidence, summary, and reason validation;
  model-reported confidence alone never authorizes a write.
- A write is staged before the native title changes, read back afterward, and
  recoverable after either half of the two-store operation.
- An externally edited managed title permanently yields to the user until an
  explicit reopen; the display ID does not change.
- Disabling structured naming does not let the legacy engine overwrite a title
  already protected by the registry.
- Existing adapters, search/stats output, historical gates, batching, and
  failure-isolation behavior continue to pass their regression tests.
