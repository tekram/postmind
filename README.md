# postmind

**Clean, triage, and understand your inbox — locally and privately.**

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![CI](https://github.com/tekram/postmind/actions/workflows/ci.yml/badge.svg)](https://github.com/tekram/postmind/actions/workflows/ci.yml)

---

## What is postmind?

postmind is a privacy-first email management tool with both a CLI and a web UI. It helps you:

- **Bulk-clean** years of inbox clutter in seconds — everything goes to Trash, never permanent
- **Triage** unread email with AI — priority, category, and a one-line reason per message
- **Manage multiple accounts** — Gmail and IMAP side-by-side
- **Run heartbeat agents** — per-account background watchers that act on your inbox automatically
- **Deep-sync locally** — cache your full mailbox for fast offline queries

All core features run entirely on your machine. AI is opt-in and off by default.

---

## Installation

```bash
pip install postmind
```

Requires Python 3.11+.

---

## First-time setup

### Gmail

```bash
# Step 1 — get credentials.json (one-time, ~10 minutes)
# Go to console.cloud.google.com
# → New project → Enable Gmail API → Create OAuth 2.0 Client ID (Desktop app)
# → Download JSON → save to ~/.postmind/credentials.json

# Step 2 — authenticate
postmind auth    # opens browser, stores token at ~/.postmind/token.json

# Step 3 — run
postmind stats
```

> **"This app isn't verified"** is expected. You're authorising your own app to access your own inbox.
> Click **Advanced → Go to postmind (unsafe)** to continue.

### IMAP (Outlook, Fastmail, iCloud, any IMAP server)

```bash
postmind setup    # guided — enter server, username, port, folder
```

Set your password in the shell (never stored on disk):

```bash
export POSTMIND_IMAP_PASSWORD="your-app-password"
```

postmind will prompt securely if the variable isn't set.

### Multiple accounts

```bash
postmind accounts add          # add a second Gmail or IMAP account
postmind accounts list         # see all connected accounts
postmind accounts switch <email>   # switch active account
```

---

## Quick Demo

```bash
postmind stats          # rank your inbox clutter by storage impact
postmind purge          # bulk-delete what you picked — goes to Trash
postmind undo           # reverse anything, up to 30 days later
postmind serve          # launch the web UI at http://localhost:8000
```

---

## Web UI

```bash
postmind serve
# → http://localhost:8000
```

| Page | What it does |
|---|---|
| **Dashboard** | Inbox overview — stats at a glance |
| **Stats** | Sender rankings by storage impact |
| **Triage** | AI-classified unread inbox — priority, category, action |
| **Purge preview** | Review what will be trashed before confirming |
| **Accounts** | Add / switch / remove Gmail and IMAP accounts |
| **Agents** | Create and manage per-account heartbeat agents |
| **Watch** | Control the heartbeat daemon (start / stop) |
| **Sync** | Trigger a local cache sync from the browser |
| **Undo** | Review and reverse recent operations |

---

## Safety Guarantees

| Guarantee | How it works |
|---|---|
| Trash first | Every delete sends mail to Trash, not permanent deletion |
| Full undo | `postmind undo` reverses any operation within 30 days |
| No cloud required | `stats`, `purge`, `undo`, `setup` are 100% local |
| AI is optional | AI is `off` by default — you enable it explicitly |
| Dry-run available | `purge --json` shows what would be deleted before you confirm |

---

## Commands Overview

### Core (no API key needed)

| Command | What it does |
|---|---|
| `postmind setup` | Guided first-time setup: connect Gmail or IMAP |
| `postmind auth` | Re-authenticate with Gmail (OAuth browser flow) |
| `postmind quickstart` | One-shot scan → safest first cleanup action |
| `postmind stats` | Rank all senders by storage impact with confidence scores |
| `postmind stats --since 30d` | Scope the scan to the last N days |
| `postmind stats --scope anywhere` | Include archived and sent mail |
| `postmind purge` | Interactive bulk delete — pick senders, confirm, done |
| `postmind purge --domain example.com` | Target one domain directly |
| `postmind protect invoices@bank.com` | Protect a sender from future purge operations |
| `postmind undo` | List recent operations and reverse any of them |
| `postmind sync` | Pull inbox into local cache for faster repeated queries |
| `postmind sync --deep` | Full mailbox sync — all years, in batches |
| `postmind doctor` | Health check — auth, connection, storage, config |
| `postmind privacy` | Show exactly what data is stored and what leaves your machine |
| `postmind config ai-mode off\|local\|cloud` | Set AI mode persistently |
| `postmind serve` | Launch the web UI |

### Multi-account

| Command | What it does |
|---|---|
| `postmind accounts list` | List all connected accounts |
| `postmind accounts add` | Add a new Gmail or IMAP account |
| `postmind accounts switch <email>` | Switch active account |
| `postmind accounts remove <email>` | Remove an account |

### Heartbeat agents

| Command | What it does |
|---|---|
| `postmind agents list` | List all agents |
| `postmind agents create <email>` | Create a heartbeat agent for an account |
| `postmind agents pause <email>` | Pause an agent |
| `postmind agents resume <email>` | Resume a paused agent |

### AI triage (requires `postmind config ai-mode cloud` or `local`)

| Command | What it does |
|---|---|
| `postmind triage` | Classify unread inbox — priority, category, why, suggested action |
| `postmind bulk "archive newsletters older than 60 days"` | Natural language bulk operation |
| `postmind avoid` | Surface emails you've viewed repeatedly but never acted on |
| `postmind digest` | Weekly inbox summary — patterns, action items, one cleanup suggestion |

### Local AI (no Anthropic key)

```bash
postmind stats --ai-backend ollama --ai-model phi3   # requires Ollama running locally
```

---

## Privacy

**Data never leaves your machine unless you explicitly enable cloud AI.**

- All data stored in `~/.postmind/` — no telemetry, no analytics, no external sync
- OAuth token written `chmod 0600` — owner read-only
- `stats`, `purge`, `undo`, `setup` are fully local — no API key, no network calls
- AI mode is shown in every command output:
  - `AI: OFF   no data leaves your machine` (default)
  - `AI: LOCAL  runs on your machine — nothing sent externally`
  - `AI: CLOUD  email data may be sent to Anthropic`
- Cloud AI sends only email subjects and 300-character snippets — never full body content

**Revoke access at any time:**
- Google: [myaccount.google.com/permissions](https://myaccount.google.com/permissions) → remove postmind
- Local: `rm ~/.postmind/token.json`

See [PRIVACY.md](PRIVACY.md) for the full data flow.

---

## Configuration

Settings via `~/.postmind/.env` or environment variables:

| Variable | Default | Description |
|---|---|---|
| `POSTMIND_AI_MODE` | `off` | AI mode: `off` · `local` · `cloud` |
| `ANTHROPIC_API_KEY` | *(not set)* | Required for cloud AI features |
| `POSTMIND_AI_MODEL` | `claude-sonnet-4-6` | Claude model for cloud AI |
| `POSTMIND_DRY_RUN` | `false` | Preview without executing |
| `POSTMIND_UNDO_WINDOW_DAYS` | `30` | How long undo logs are kept |
| `POSTMIND_DIR` | `~/.postmind` | Data directory |
| `POSTMIND_PROVIDER` | `gmail` | Active provider |
| `POSTMIND_IMAP_SERVER` | *(not set)* | IMAP server hostname |
| `POSTMIND_IMAP_USER` | *(not set)* | IMAP username |
| `POSTMIND_IMAP_PORT` | `993` | IMAP SSL port |
| `POSTMIND_IMAP_FOLDER` | `INBOX` | IMAP folder to scan |
| `POSTMIND_IMAP_PASSWORD` | *(not set)* | IMAP password — **never stored on disk** |

---

## Troubleshooting

```bash
postmind doctor    # diagnoses auth, connection, storage, config
```

| Symptom | Fix |
|---|---|
| "Gmail connection expired" | `postmind auth` |
| "Token file not found" | `postmind auth` |
| "Cannot write to ~/.postmind/" | `chmod 700 ~/.postmind` |
| "Rate limit hit" | Wait 60s, retry with `--max-scan 300` |
| Scan feels slow | `postmind stats --max-scan 500` |
| IMAP connection failed | Re-run `postmind setup` to update settings |
| Switched to Gmail but still prompted for IMAP password | Re-run `postmind setup` and choose Gmail |

---

## Testing

```bash
# Zero credentials required — all AI paths use MockAIEngine
pytest tests/ -v
```

---

## Contributing

Bug reports and feature requests via [GitHub Issues](../../issues).
See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup.

---

## Credits

postmind is a fork of [mailtrim](https://github.com/sadhgurutech/mailtrim) by [@sadhgurutech](https://github.com/sadhgurutech).

The original mailtrim project built the core Gmail/IMAP cleanup engine, the safety-first trash-only deletion model, confidence scoring, undo system, and privacy guarantees. This fork adds a web UI, multi-account support, AI triage, heartbeat agents, and deep sync.

If the lightweight CLI is all you need, check out [mailtrim](https://github.com/sadhgurutech/mailtrim) — it ships to PyPI and has no web dependencies.

---

## License

[MIT](LICENSE) — free to use, modify, distribute.
