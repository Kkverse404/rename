# Structured Codex naming contract

Structured naming is an opt-in Codex workflow. Its canonical configuration,
state transitions, and operator commands are defined here.

## Modes

- `off`: preserve the legacy behavior for unregistered sessions. Sessions
  already present in the permanent registry remain protected from legacy
  renaming.
- `preview`: discover and display status without model calls or persistent
  changes.
- `apply`: allocate IDs, evaluate eligible threads, and write validated titles.

The default is `off`. Global `dry_run = true` has the same no-side-effect
guarantee as `preview` for this workflow.

## Codex lifecycle hook

Codex can invoke a single-session naming pass from an asynchronous `Stop`
hook. The hook command is `rename codex-hook`; it reads the Codex hook event
from standard input, resolves the event to its persisted sidebar thread, and
processes only that thread without emitting hook output. Native thread IDs and
exact transcript paths take precedence; a recent same-directory thread is the
bounded fallback for desktop execution IDs. An event with no safe match is
skipped. Configure the command as a user-level hook when Codex is the only
enabled adapter. Do not run the polling daemon at the same time.

Codex requires non-managed hooks to be reviewed and trusted after they are
added or changed. Inspect the effective hook in `/hooks` before disabling the
polling daemon.

## Identity and title

The permanent key is `(tool, native_session_id)`. `display_id` is a monotonic
integer allocated transactionally by the registry and is never changed,
deleted, or recycled because a thread is archived, too old to discover, or
temporarily unavailable.

The program, not the model, formats the final title:

```text
#{display_id}- {summary}
```

The summary is a compact, single-line task phrase and cannot contain a managed
ID prefix or conversational wording. It keeps the task action and key object
instead of repeating the user's request. Chinese summaries are bounded to 16
characters; other languages are bounded to eight words and 64 characters.
Trailing title punctuation is removed. The validated classifier module remains
internal registry metadata and is never included in the visible title.

Codex stores two distinct values: an explicit app-server `thread.name` and a
generated `title`/`preview` fallback. List, search, status, and the GUI display
the non-empty explicit name first, then the fallback. Compare-and-set,
recovery, and manual-edit protection compare only the native explicit name
(which may be null). The registry stores the original effective display title
separately so an originally unnamed thread can still be rolled back without
confusing the fallback with the native CAS value.

## Classifier result

The classifier returns one JSON object:

```json
{
  "ready": true,
  "module": "Confidence",
  "summary": "修复 Batch Writer 并发写入",
  "reason_code": "explicit_goal",
  "evidence_message_ids": ["user-3"],
  "confidence": 0.91
}
```

`ready` is accepted only when the internal module, summary, reason, and evidence
all validate and confidence meets the configured floor. The classifier receives
a bounded transcript excerpt and never controls the display ID, title format,
state transition, or write permission.

Ambiguous or insufficient input stays cached until the conversation changes.
Classifier execution failures use a separate five-minute retry backoff, so a
temporarily unavailable executable can recover without another user message.

## State transitions

```text
unregistered -> pending -> write_pending -> finalized
                    |             |
                    |             +-> pending (verified native write absent)
                    +-> pending (unclear or invalid decision)

finalized -> manual_override (native title changed externally)
manual_override -> pending        (explicit reopen only)
finalized -> rolled_back          (explicit rollback only)
rolled_back -> pending            (explicit reopen only)
```

The registry persists `write_pending` before native mutation. On restart:

- native title equals the desired title: finalize without reallocating or
  calling the model;
- native explicit name still equals the expected old name: retry the staged write;
- native title equals neither: mark `manual_override` and do not overwrite it.

## Native write

Codex titles are written through the app-server `thread/name/set` method. The
writer reads the current explicit name first, compares it with the expected
native name, writes, and then verifies with `thread/read`. Protocol, timeout,
conflict, and verification failures remain explicit; there is no live SQLite
fallback.
When a bare `codex` command is not on `PATH`, Windows also checks the current
Codex Desktop installation under `%LOCALAPPDATA%\OpenAI\Codex\bin`. The same
resolver is used for classification and native writes, and selects the newest
installed `codex.exe` after app updates.
On Windows, batch launchers are quoted as a complete `cmd.exe` command line;
paths or options containing `%` are rejected because cmd expands that character
even inside quotes. Conversation content and title JSON remain on stdin.

## Operator actions

- Status and list operations are pure reads.
- `reopen` preserves the display ID and returns a protected thread to pending;
  on a pending thread it also clears a cached unclear/error decision for an
  explicit retry.
- `rollback` restores the title captured before the managed write when that
  title is available and the current title still matches the managed title.
  If the thread originally had no explicit native name, this writes its
  captured fallback display title as the new explicit name; the visible title
  is restored, while the native value intentionally changes from null.
  Rollback intent is stored before the native write; if the process stops after
  restoration, the next apply pass completes `rolled_back` without a model call.
- `once --session` does not implicitly reopen finalized, manually overridden,
  or rolled-back records.
