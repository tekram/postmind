# postmind

**Clean your inbox safely in seconds.**
Everything goes to Trash. Undo anytime.

[![PyPI](https://img.shields.io/pypi/v/postmind.svg)](https://pypi.org/project/postmind/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![CI](https://github.com/tekram/postmind/actions/workflows/ci.yml/badge.svg)](https://github.com/tekram/postmind/actions/workflows/ci.yml)

---

## Quick Demo

```bash
pip install postmind
postmind setup          # connect Gmail or IMAP — guided, ~2 minutes
postmind stats          # rank your inbox clutter by impact
postmind purge          # bulk-delete what you picked — goes to Trash
```

That's the whole workflow. No API key. No subscription. Nothing sent to any server.

---

## Why postmind?

- **Finds what's actually filling your inbox** — ranks senders by storage impact, not just count
- **Bulk cleanup in seconds** — delete 300+ emails from one sender in a single command
- **Nothing is permanently deleted** — everything goes to Trash, recoverable for 30 days
- **Privacy-first** — core commands run entirely on your machine; AI is opt-in and off by default
- **Works with Gmail and IMAP** — Outlook, Fastmail, iCloud, any IMAP server

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

## 60-Second Quickstart

**First time:**

```bash
pip install postmind
postmind setup    # walks you through Gmail auth and runs your first scan
```

**After setup:**

```bash
postmind stats                          # see your inbox ranked by clutter
postmind purge                          # interactive: pick senders, confirm, done
postmind purge --domain linkedin.com    # target one sender directly
postmind undo                           # reverse anything you just did
```

**Already set up? Jump straight to cleanup:**

```bash
postmind quickstart    # one command — scans, ranks, shows the safest first action
```

---

## Example Output

### `postmind stats`

```
Provider: Gmail
✨ Scan complete — analyzed 2,341 emails across 41 senders in 4s
  AI: OFF  no data leaves your machine

34% of your inbox is clutter — caused by just 3 senders. 87.4 MB gone in one command.

┌─────────────────────────────────────────────────────────────────────────────┐
│  TOTAL RECLAIMABLE SPACE                                                    │
│  You can safely free ~87.4 MB (34% of scanned inbox)                        │
│  from your top 3 senders · Each cleanup takes ~3-5s                         │
│  All deletions go to Trash — undo anytime                                   │
└─────────────────────────────────────────────────────────────────────────────┘

 #  Impact    Sender                Emails  Size    Oldest      Risk
 1  100       LinkedIn Jobs            312  44 MB   847d ago    Safe to clean
 2   82       Substack Weekly          183  26 MB   512d ago    Safe to clean
 3   29       Shopify Receipts          94  12 MB   203d ago    Safe to clean
```

### `postmind purge`

```
  Top Email Offenders  (589 emails · 82 MB)

 # │ Sender              │ Emails │ Size  │ Latest  │ Sample subject
───┼─────────────────────┼────────┼───────┼─────────┼─────────────────────────
 1 │ LinkedIn Jobs       │   312  │  44MB │ Apr 03  │ 12 new jobs matching…
 2 │ Substack Weekly     │   183  │  26MB │ Apr 01  │ This week: AI is eating…
 3 │ Shopify Receipts    │    94  │  12MB │ Mar 28  │ Your order has shipped

Your selection: 1,2

Move 495 emails to Trash? (undo available for 30 days) [y/N]: y
✓ Moved 495 emails to Trash.  postmind undo 1  — to reverse
```

### `postmind undo`

```
  Recent operations

  #1  Apr 05  495 emails trashed  (LinkedIn Jobs + Substack)
  #2  Apr 03   94 emails trashed  (Shopify Receipts)

Restore which operation? 1

✓ Restored 495 emails.
```

---

## Privacy

**Data never leaves your machine unless you explicitly enable cloud AI.**

- All data stored in `~/.postmind/` — no telemetry, no analytics, no external sync
- OAuth token written `chmod 0600` — owner read-only
- `stats`, `purge`, `undo`, `setup` are fully local — no API key, no network calls
- **AI mode** is shown in every command output:
  - `AI: OFF   no data leaves your machine` (default)
  - `AI: LOCAL  runs on your machine — nothing sent externally`
  - `AI: CLOUD  email data may be sent to Anthropic`
- When cloud AI is enabled, a warning appears **before** any data is sent
- Cloud AI features send only email subjects and 300-character snippets — never full body content

**Revoke access at any time:**
- Google: [myaccount.google.com/permissions](https://myaccount.google.com/permissions) → remove postmind
- Local: `rm ~/.postmind/token.json`

See [PRIVACY.md](PRIVACY.md) for the full data flow.

---

## Commands Overview

### Core (no API key needed)

| Command | What it does |
|---|---|
| `postmind setup` | Guided first-time setup: connect Gmail or IMAP, run first scan |
| `postmind auth` | Re-authenticate with Gmail (OAuth browser flow) |
| `postmind quickstart` | One-shot scan → shows your safest first cleanup action |
| `postmind stats` | Rank all senders by storage impact with confidence scores |
| `postmind stats --since 30d` | Scope the scan to the last N days |
| `postmind stats --scope anywhere` | Include archived and sent mail, not just inbox |
| `postmind stats --share` | Generate a shareable summary (Twitter/plain) |
| `postmind purge` | Interactive bulk delete — pick senders, confirm, done |
| `postmind purge --domain example.com` | Target one domain directly |
| `postmind purge --sort size` | Show largest senders first |
| `postmind protect invoices@bank.com` | Protect a sender from future purge operations |
| `postmind undo` | List recent operations and reverse any of them |
| `postmind undo 3` | Reverse operation #3 specifically |
| `postmind version` | Show installed version (`--version` / `-V` also works) |
| `postmind doctor` | Health check — auth, Gmail connection, storage, config |
| `postmind sync` | Pull inbox into local cache for faster repeated queries |
| `postmind unsubscribe email@sender.com` | Unsubscribe via List-Unsubscribe header |
| `postmind privacy` | Show exactly what data is stored and what (if anything) leaves your machine |
| `postmind config ai-mode off\|local\|cloud` | Set AI mode persistently |

### Optional AI (requires `postmind config ai-mode cloud`)

| Command | What it does |
|---|---|
| `postmind triage` | Classify unread inbox — priority, category, why, suggested action |
| `postmind bulk "archive newsletters older than 60 days"` | Natural language bulk operation |
| `postmind avoid` | Surface emails you've viewed repeatedly but never acted on |
| `postmind digest` | Weekly inbox summary — patterns, action items, one cleanup suggestion |

### AI enrichment (local — no Anthropic key)

```bash
postmind stats --ai-backend ollama --ai-model phi3   # requires Ollama
postmind purge --ai-backend llama                     # requires llama.cpp at localhost:8080
```

---

## Setup

### Gmail (OAuth)

```bash
# 1. Get credentials.json from Google Cloud Console (one-time, ~10 minutes)
#    console.cloud.google.com → New project → Gmail API → OAuth 2.0 Client ID (Desktop)
#    Download JSON → save to ~/.postmind/credentials.json

# 2. Authenticate
postmind auth    # opens browser once, stores token locally

# 3. Run
postmind stats
```

> **"This app isn't verified"** — expected. You're authorising your own app to access your own inbox. Click **Advanced → Go to postmind (unsafe)** to proceed.

### IMAP (Outlook, Fastmail, iCloud, self-hosted)

```bash
postmind setup    # choose IMAP at the prompt — enter server, user, password
```

Setup saves your server, username, port, and folder to `~/.postmind/.env`.
After that, every command works with no flags:

```bash
postmind stats       # reads persisted IMAP config automatically
postmind purge       # same
postmind undo        # same
```

For the password, set it once in your shell environment (never stored on disk):

```bash
export POSTMIND_IMAP_PASSWORD="your-app-password"
```

Or postmind will prompt securely each time.

---

## Confidence Scores

`purge` shows a 0–100 score that estimates how safe bulk-deletion is:

| Signal | Weight |
|---|---|
| `List-Unsubscribe` header present | 30 pts — sender self-identifies as bulk/marketing |
| Age ≥ 180 days in inbox | up to 35 pts — emails sitting >6 months are rarely actionable |
| Volume ≥ 50 from one sender | up to 35 pts — high frequency = almost certainly automated |

🟢 ≥70 Safe to clean · 🟡 40–69 Needs review · 🔴 Sensitive (bank, health, legal — never auto-suggested)

Scores are heuristics. The 30-day undo exists precisely because no heuristic is perfect.

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
| `POSTMIND_PROVIDER` | `gmail` | Active provider — set automatically by `postmind setup` |
| `POSTMIND_IMAP_SERVER` | *(not set)* | IMAP server hostname — set automatically by `postmind setup` |
| `POSTMIND_IMAP_USER` | *(not set)* | IMAP username — set automatically by `postmind setup` |
| `POSTMIND_IMAP_PORT` | `993` | IMAP SSL port |
| `POSTMIND_IMAP_FOLDER` | `INBOX` | IMAP folder to scan |
| `POSTMIND_IMAP_PASSWORD` | *(not set)* | IMAP password — **never stored on disk**, set in your shell |

**Set AI mode:**

```bash
postmind config ai-mode off     # default — no AI, nothing sent anywhere
postmind config ai-mode local   # local models only (Ollama, llama.cpp)
postmind config ai-mode cloud   # Anthropic Claude — requires ANTHROPIC_API_KEY
```

---

## Troubleshooting

```bash
postmind doctor    # diagnoses auth, Gmail connection, storage, config
```

| Symptom | Fix |
|---|---|
| "Gmail connection expired" | `postmind auth` |
| "Token file not found" | `postmind auth` |
| "Cannot write to ~/.postmind/" | `chmod 700 ~/.postmind` |
| "Rate limit hit" | Wait 60s, retry with `--max-scan 300` |
| Scan feels slow | `postmind stats --max-scan 500` |
| Not seeing enough senders | `postmind stats --scope anywhere` |
| IMAP connection failed | Re-run `postmind setup` to update server/user settings |
| Switched to Gmail but still prompted for IMAP password | Re-run `postmind setup` and choose Gmail — this clears stale IMAP settings from `.env` |
| IMAP undo restores fewer emails than expected | Normal on non-Gmail IMAP — UIDs are folder-specific; check Trash manually for any remaining emails |
| IMAP purge returns 0 emails moved | Server may lack a Trash folder; run `postmind doctor` to check |

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

## License

[MIT](LICENSE) — free to use, modify, distribute.
