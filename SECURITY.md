# Security Policy

`rename` reads and writes your local AI coding session stores, so its safety and
privacy are a first-class concern.

## What rename does with your data

- **No telemetry, ever, and it only changes titles.** rename never phones home;
  it appends or updates a single title field per session and never edits, deletes,
  or reorders your conversations.
- **Titling uses your own logged-in CLI by default.** The default `auto` namer
  asks the `claude` / `codex` tool you're already signed into to write the title,
  so a short transcript excerpt is sent through that provider — there is no API
  key to paste. Those calls are ephemeral and must not create extra sessions in
  your Claude Code / Codex history. The `anthropic` / `openai` namers do the same
  via a key you set.
- **Fully offline option.** Set `namer = "heuristic"` and keep
  `structured_naming.mode = "off"` or `"preview"`; then no naming transcript
  leaves your machine. Structured `apply` has its own configured Codex model
  and is not made offline by the legacy `namer` setting.
- **Conservative writes.** Session discovery uses read-only stores. Structured
  Codex naming writes titles only through the app-server compare/write/read
  protocol; it never falls back to updating the live Codex SQLite database.
  Its permanent registry stages the intended title before native mutation, so
  a restart can recover either half of the write.
- **Read-only previews.** `list`, `status`, GUI refresh, `preview`, and dry-run
  paths do not allocate structured IDs, call a model, or change titles.

## Reporting a vulnerability

Please report security issues **privately** via GitHub Security Advisories
(the "Report a vulnerability" button under the repository's **Security** tab),
not a public issue.

Include what you found, the affected version (`rename --version`), and steps to
reproduce. ⚠️ Please **redact any private session content** from your report.

We aim to acknowledge reports within a few days and to fix verified issues
promptly.

## Supported versions

rename is pre-1.0; security fixes land on the latest `main` and the newest
release.
