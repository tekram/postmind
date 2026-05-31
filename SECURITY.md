# Security

postmind's core cleanup workflow (`stats`, `purge`, `undo`, `sync`, `unsubscribe`, `follow-up`, `rules --run`) runs entirely on your machine — no backend, no telemetry, nothing sent externally.

Optional AI commands (`triage`, `bulk`, `avoid`, `digest`, `rules --add`) send only email subjects and 300-character snippets to Anthropic's API. Full email bodies are never transmitted.

## Design

- All state lives in `~/.postmind/` (SQLite + token), never uploaded anywhere
- OAuth token is written `chmod 0o600` (owner read-only)
- AI features send only email subjects and snippets to Anthropic — never full body content
- Full data flow documented in [PRIVACY.md](PRIVACY.md)

## Reporting a vulnerability

Please report security vulnerabilities privately via [GitHub's private vulnerability reporting](../../security/advisories/new) rather than opening a public issue. Do not include sensitive details in public issue titles or comments.
