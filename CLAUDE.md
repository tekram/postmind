# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

postmind is a privacy-first email management tool (Gmail + IMAP) with a Typer CLI and a
FastAPI web UI. Core cleanup/triage features run 100% locally; AI is opt-in and off by
default. AI can target either a cloud model (Anthropic Claude) or a local model (Ollama).

## Commands

```bash
make install-dev   # pip install -e ".[dev]" + pre-commit install
make check         # full local CI equivalent: lint + format + security + test
make fix           # auto-fix lint + format in place (run before committing)

make lint          # ruff check postmind/
make format        # ruff format --check postmind/ tests/
make security      # bandit -r postmind/ -ll -q
make test          # pytest tests/ -q --tb=short
```

Run a single test:
```bash
python -m pytest tests/test_purge.py -v
python -m pytest tests/test_purge.py::test_name -v
```

CI runs on Python 3.11 and 3.13. Tests need no API key (they use `MockAIEngine`).
`pre-commit` blocks direct commits to `main` — work on a feature branch.

Run the app:
```bash
postmind serve     # web UI at http://127.0.0.1:8484 (default port; requires .[web] extra)
postmind <cmd>     # CLI; see README "Commands Overview" for the full list
```

If `postmind` isn't on PATH (common in a fresh shell), use `.venv/bin/postmind serve`.
`make test` assumes `python` resolves; fall back to `.venv/bin/python -m pytest tests/ -q --tb=short`.

## Architecture

**Provider abstraction is the core seam.** All pipeline code (stats, purge, bulk, triage)
targets the abstract `EmailProvider` interface in `core/providers/base.py` — eight methods
covering read/write/account. Concrete `GmailProvider` and `IMAPProvider` are constructed
*only* in `core/providers/factory.py::get_provider()`; nothing else imports a concrete
provider. Adding a backend means implementing the interface + a factory branch, with no
changes to scoring or CLI output. Capability gaps (labels, threads, unsubscribe, untrash)
are gated via `provider.supports(capability)`.

**Config & per-account state** (`config.py`): everything lives under `~/.postmind/`
(`POSTMIND_DIR` overrides). The active account is a single email in
`~/.postmind/active_account`; per-account provider/IMAP settings are JSON in
`~/.postmind/accounts/<email>.json` (via `load_account_config`/`save_account_config`);
Gmail OAuth tokens are per-account under `~/.postmind/tokens/<email>.json`. The legacy
flat `provider`/`imap_*` fields on `Settings` are a deprecated fallback — provider
resolution is a 4-way priority: **CLI flag → per-account config → `Settings` → default**.
`get_settings()` is a cached singleton; tests reset it via `config._settings = None`.

**AI mode is enforced in one place** (`core/ai/mode.py`). Three modes: `off` (default, no
AI calls), `local` (Ollama only — nothing leaves the machine), `cloud` (Anthropic, sends
subjects + 300-char snippets externally). Callers MUST gate calls with `require_local()` /
`require_cloud()`, which raise `AIModeError` with actionable guidance. `AIEngine`
(`core/ai_engine.py`) accepts mode/model overrides so the floating chat assistant and Super
Agent can run on a *different* backend than global `ai_mode` (the `chat_*` settings). Cloud
Ollama is treated as `local` transport but is an explicit external opt-in.

**Storage** (`core/storage.py`): local SQLite via SQLAlchemy 2.0 at `~/.postmind/postmind.db`.
Holds accounts, cached email metadata (`EmailRecord` — avoids re-fetching from Gmail),
agents, rules, blocklist, and undo logs. Repo classes (`AccountRepo`, `EmailRepo`,
`AgentRepo`, `RuleRepo`, `UndoLogRepo`, `BlocklistRepo`) wrap a `get_session()`.

**Safety model (destructive ops):** every delete goes to **Trash**, never permanent.
`BulkEngine` (`core/bulk_engine.py`) does NL → preview → execute → writes an **undo log**
(30-day window, `UNDO_LOG_DIR`). All destructive operations are reversible via `postmind undo`.

**Super Agent / agent tools** (`core/agent_tools.py`): single source of truth for the
agent's tool *schemas* and stateless analysis helpers. Critical trust boundary:
- READ tools run immediately and return text the model reasons over.
- WRITE tools NEVER execute inside the agent loop — they **stage** an action and emit a
  confirm card. Confirmation targets (sender emails, message IDs) are **always
  server-resolved by our code**, never free-form text from the model — this contains
  prompt injection from untrusted email bodies. Request-scoped execution (provider,
  account, scan cache, action accumulators) lives in `web/server.py`.

**Web UI** (`web/server.py`, ~4.9k lines): FastAPI + Jinja2 templates in `web/templates/`,
HTMX-style partial responses. Routes: `/`, `/stats`, `/triage`, `/agent`, `/sync`, `/undo`,
`/settings`, `/accounts`, `/agents`, `/watch`, `/onboarding`, `/chat`, `/brief`. Long
operations (sync, Gmail OAuth add) use background tasks with `/poll/{task_id}` endpoints.
Inline actions (triage swipe, brief trash/archive) POST to small JSON endpoints
(`/triage/trash`, `/brief/action`) and remove rows client-side on success.

**Daily Brief** (`core/daily_brief.py`): `DailyBriefGenerator.get_or_generate()` caches one
brief per calendar day (UTC) in `daily_briefs` table. Re-generates automatically if the
cached brief is older than 1 hour. On generation: (1) classifies up to 9 unclassified unread
emails on-demand (3-email batches for local LLMs), (2) builds `high_priority_items` from the
classification cache, (3) adds `recent_unclassified` — the 20 most-recent unread emails from
the last 7 days with no cache entry, deduped by sender name — so new arrivals always surface
even with a large classification backlog. Both lists are merged into `items_json` (cap 50)
and rendered as actionable deep-links by `_render_brief_links` in `server.py`.

**Heartbeat daemon** (`core/daemon.py`): APScheduler-based per-account background watcher
that periodically fetches new mail, classifies, and applies rules/follow-ups. Feature
toggles per agent (`run_rules`, `run_followups`, `run_avoidance`, `run_daily_brief`) live on
the `Agent` record. Controlled from CLI (`postmind agents …`) or the `/watch` page.

**Sender scoring** (`core/sender_stats.py`, largest core module): aggregates cached email
into per-sender stats ranked by storage impact, with confidence scoring and risk flags
(sensitive senders like banks/legal/health are flagged; protected senders are skipped).

**Cleanup feedback loop** (`CleanupFeedbackRecord` in `core/storage.py`): every approve/skip
decision in the cleanup flow is recorded via `CleanupFeedbackRepo.record_many()`. These
priors are loaded back at session start (`sender_priors()`) and fed to `AIEngine` so the
classifier can bias toward the user's past decisions. This is the existing hook for
learning from user behavior — extend here for new signal types.

## Conventions

- Ruff is the only linter/formatter (line-length 100 enforced by formatter, not lint;
  double quotes; rules `E,F,W,I`). Bandit `-ll` for security.
- New AI-dependent code paths must work under `MockAIEngine` (`core/mock_ai.py`) so tests
  run without an API key.
- Tests are fully isolated from the real `~/.postmind`: `conftest.py` autouse fixtures
  repoint every `config` path constant at a temp dir and reset the settings singleton.
  Use the `clean_db` fixture for anything touching storage (fresh in-memory SQLite).
- Any new destructive action must be reversible (write an undo log) and route deletes to
  Trash, never permanent deletion.
