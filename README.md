# mailtrim

**Clean your inbox safely. Triage with AI. Everything stays on your machine.**

[![PyPI](https://img.shields.io/pypi/v/mailtrim.svg)](https://pypi.org/project/mailtrim/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![CI](https://github.com/tekram/mailtrim/actions/workflows/ci.yml/badge.svg)](https://github.com/tekram/mailtrim/actions/workflows/ci.yml)

---

## What it does

mailtrim gives you two ways to manage your inbox:

**CLI** — run anywhere, no browser needed:
```bash
mailtrim stats     # rank senders by storage impact
mailtrim purge     # bulk-delete by sender — goes to Trash, undo anytime
mailtrim triage    # AI classifies every unread: priority, action, why
```

**Web UI** — full interface at `localhost:8484`:
```bash
pip install "mailtrim[web]"
mailtrim serve
```

Both run entirely on your machine. No subscription. No telemetry. AI is off by default.

---

## Safety guarantees

| Guarantee | How it works |
|---|---|
| Trash first | Every delete moves mail to Trash — never permanent |
| Full undo | `mailtrim undo` reverses any operation within 30 days |
| No cloud required | `stats`, `purge`, `undo`, `setup` are 100% local |
| AI is opt-in | AI is `off` by default — enable explicitly |
| Sensitive senders protected | Banks, healthcare, legal senders are flagged and never auto-suggested |

---

## Quickstart

```bash
pip install mailtrim
mailtrim setup     # connect Gmail or IMAP — guided, ~2 minutes
mailtrim stats     # see your inbox ranked by clutter
mailtrim purge     # interactive: pick senders, confirm, done
```

Or open the web UI:

```bash
pip install "mailtrim[web]"
mailtrim serve     # opens http://localhost:8484 in your browser
```

---

## Example output

### `mailtrim stats`

```
Provider: Gmail  ·  AI: OFF  (nothing leaves your machine)

34% of your inbox is clutter — caused by just 3 senders. 87.4 MB gone in one command.

 #  Impact  Sender                Emails   Size    Oldest      Risk
 1  100     LinkedIn Jobs            312   44 MB   847d ago    🟢 Safe
 2   82     Substack Weekly          183   26 MB   512d ago    🟢 Safe
 3   29     Shopify Receipts          94   12 MB   203d ago    🟢 Safe
```

### `mailtrim triage` (cloud or local AI)

```
⚡ HIGH  Reply needed — your manager asked a direct question     [reply]
         From: boss@company.com · action_required

· MED   Newsletter — weekly digest you subscribed to            [unsubscribe]
         From: digest@substack.com · newsletter

  LOW   Automated notification, no action needed                [archive]
         From: noreply@github.com · notification
```

### `mailtrim purge`

```
  Your selection: LinkedIn Jobs (312 emails · 44 MB)

  Move 312 emails to Trash? (undo available for 30 days) [y/N]: y
  ✓ Moved 312 emails to Trash.   mailtrim undo 1  — to reverse
```

---

## Web UI

`mailtrim serve` starts a local web server at `http://localhost:8484`.

| Page | What it does |
|---|---|
| **Dashboard** | Inbox summary — reclaimable space, top senders, best next action |
| **Stats** | Full sender table with filters, sort, and bulk purge |
| **Triage** | AI card grid — priority, category, explanation, suggested action per email |
| **Sync** | Cache inbox metadata locally for instant repeated stats/purge |
| **Undo History** | Reverse any operation within its 30-day window |
| **Settings** | AI mode, Ollama config, provider, protected senders |

The web UI uses HTMX for live updates (sync progress, stats loading) with no JavaScript framework.

---

## AI features

AI is off by default. Enable with `mailtrim config ai-mode cloud` or `mailtrim config ai-mode local`.

### Cloud (Anthropic)

```bash
mailtrim config ai-mode cloud    # requires ANTHROPIC_API_KEY
mailtrim triage                  # classify unread inbox
mailtrim bulk "archive all newsletters older than 60 days"
mailtrim digest                  # weekly summary — patterns, action items, one cleanup suggestion
mailtrim avoid                   # surface emails you've seen but never acted on
```

Only email subjects and snippets (≤300 characters) are sent to Anthropic — never full body content.

### Local (Ollama)

```bash
ollama pull llama3.2
mailtrim config ai-mode local    # zero network calls — runs on your machine
mailtrim triage
```

In the web UI, go to **Settings → AI Mode → Local** to set your Ollama URL and model.
Any model in `ollama list` works — `llama3.2`, `mistral`, `gemma3`, etc.

### What AI classifies

Each email gets:
- **Priority**: high / medium / low
- **Category**: action_required · conversation · newsletter · notification · receipt · calendar · social · spam
- **Explanation**: one sentence (why it was classified this way)
- **Suggested action**: reply · archive · unsubscribe · delete · keep · delegate
- **Deadline hint**: extracted time pressure, e.g. "by Friday" or "this week"

---

## Commands

### Core (no API key needed)

| Command | What it does |
|---|---|
| `mailtrim setup` | Guided first-time setup — connect Gmail or IMAP |
| `mailtrim auth` | Re-authenticate with Gmail (OAuth browser flow) |
| `mailtrim quickstart` | One-shot scan → safest first cleanup action |
| `mailtrim stats` | Rank senders by storage impact with confidence scores |
| `mailtrim stats --since 30d` | Scope scan to the last N days |
| `mailtrim stats --scope anywhere` | Include archived and sent, not just inbox |
| `mailtrim purge` | Interactive bulk delete — pick senders, confirm, done |
| `mailtrim purge --domain example.com` | Target one domain directly |
| `mailtrim protect invoices@bank.com` | Protect a sender from purge |
| `mailtrim undo` | List and reverse recent operations |
| `mailtrim sync` | Cache inbox metadata locally for faster repeated queries |
| `mailtrim unsubscribe email@sender.com` | Unsubscribe via List-Unsubscribe header |
| `mailtrim doctor` | Health check — auth, Gmail connection, storage, config |
| `mailtrim privacy` | Show exactly what data stays local vs. what leaves your machine |
| `mailtrim serve` | Start the local web UI at http://localhost:8484 |

### AI commands (cloud or local mode)

| Command | What it does |
|---|---|
| `mailtrim triage` | Classify unread inbox — priority, category, suggested action |
| `mailtrim bulk "<instruction>"` | Natural language bulk operation |
| `mailtrim avoid` | Surface emails you've viewed repeatedly but never acted on |
| `mailtrim digest` | Weekly inbox summary — patterns, follow-ups, cleanup suggestion |
| `mailtrim rules --add "<rule>"` | Create a recurring automation rule |

---

## Setup

### Gmail (OAuth)

```bash
# 1. Get credentials.json from Google Cloud Console (~10 minutes, one-time)
#    console.cloud.google.com → New project → Gmail API → OAuth 2.0 Client ID (Desktop app)
#    Download JSON → save to ~/.mailtrim/credentials.json

# 2. Authenticate
mailtrim auth    # opens browser once, saves token locally

# 3. Run
mailtrim stats
```

> **"This app isn't verified"** — expected. You're authorising your own app to access your own inbox. Click **Advanced → Go to mailtrim (unsafe)** to proceed.

### IMAP (Outlook, Fastmail, iCloud, self-hosted)

```bash
mailtrim setup    # choose IMAP — enter server, user, password
```

Your server/user/port are saved to `~/.mailtrim/.env`. The password is never stored on disk:

```bash
export MAILTRIM_IMAP_PASSWORD="your-app-password"   # set in your shell profile
```

---

## Confidence scores

`purge` shows a 0–100 score estimating how safe bulk-deletion is:

| Signal | Weight |
|---|---|
| `List-Unsubscribe` header present | +30 pts |
| Age ≥ 180 days in inbox | up to +35 pts |
| Volume ≥ 50 from one sender | up to +35 pts |
| Transactional keywords (invoice, receipt, order) | −25 pts |

🟢 ≥70 Safe · 🟡 40–69 Review · 🔴 Sensitive (bank, health, legal — never auto-suggested)

Scores are heuristics. The 30-day undo exists because no heuristic is perfect.

---

## Privacy

**Nothing leaves your machine unless you explicitly enable cloud AI.**

- All data in `~/.mailtrim/` — no telemetry, no external sync
- OAuth token stored `chmod 0600`
- `stats`, `purge`, `undo`, `setup`, `serve` are fully local
- Cloud AI sends only subjects + snippets (≤300 chars per email), never full body
- AI mode shown in every command output:
  - `AI: OFF   no data leaves your machine` (default)
  - `AI: LOCAL  runs on your machine — nothing sent externally`
  - `AI: CLOUD  subjects + snippets sent to Anthropic`

Revoke Gmail access: [myaccount.google.com/permissions](https://myaccount.google.com/permissions) → remove mailtrim

---

## Configuration

Settings via `~/.mailtrim/.env` or environment variables (`MAILTRIM_` prefix):

| Variable | Default | Description |
|---|---|---|
| `MAILTRIM_AI_MODE` | `off` | AI mode: `off` · `local` · `cloud` |
| `ANTHROPIC_API_KEY` | *(not set)* | Required for cloud AI |
| `MAILTRIM_AI_MODEL` | `claude-sonnet-4-6` | Claude model for cloud AI |
| `MAILTRIM_OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama endpoint for local AI |
| `MAILTRIM_OLLAMA_MODEL` | `llama3.2` | Ollama model name |
| `MAILTRIM_DRY_RUN` | `false` | Preview mode — no changes made |
| `MAILTRIM_UNDO_WINDOW_DAYS` | `30` | How long undo logs are kept |
| `MAILTRIM_DIR` | `~/.mailtrim` | Data directory |
| `MAILTRIM_PROVIDER` | `gmail` | Active provider — set by `mailtrim setup` |
| `MAILTRIM_IMAP_SERVER` | *(not set)* | IMAP server hostname |
| `MAILTRIM_IMAP_USER` | *(not set)* | IMAP username |
| `MAILTRIM_IMAP_PORT` | `993` | IMAP SSL port |
| `MAILTRIM_IMAP_FOLDER` | `INBOX` | IMAP folder to scan |

---

## Data layout

```
~/.mailtrim/
├── .env                  # persisted config (MAILTRIM_* vars)
├── credentials.json      # OAuth client secret from Google Cloud
├── token.json            # OAuth access/refresh token (chmod 0600)
├── mailtrim.db           # SQLite — emails, undo logs, rules, blocklist, follow-ups
└── undo_logs/            # per-operation restore data
```

---

## Installation options

```bash
pip install mailtrim              # CLI only
pip install "mailtrim[web]"       # + web UI (mailtrim serve)
pip install "mailtrim[headless]"  # + Playwright for unsubscribe browser fallback
pip install "mailtrim[web,headless]"  # everything
```

Requires Python 3.11+.

---

## Troubleshooting

```bash
mailtrim doctor    # diagnoses auth, connection, storage, config
```

| Symptom | Fix |
|---|---|
| "Gmail connection expired" | `mailtrim auth` |
| "Token file not found" | `mailtrim auth` |
| "Cannot write to ~/.mailtrim/" | `chmod 700 ~/.mailtrim` |
| Scan feels slow | `mailtrim stats --max-scan 500` |
| Not seeing enough senders | `mailtrim stats --scope anywhere` |
| IMAP connection failed | Re-run `mailtrim setup` |
| Web UI won't start | `pip install "mailtrim[web]"` then retry |
| Ollama triage times out | Model too large — try `llama3.2` (3B), check `ollama ps` |

---

## Testing

```bash
pip install "mailtrim[dev]"
pytest tests/ -v    # no credentials needed — AI paths use MockAIEngine
```

---

## Contributing

Bug reports and feature requests via [GitHub Issues](../../issues).

---

## License

[MIT](LICENSE)
