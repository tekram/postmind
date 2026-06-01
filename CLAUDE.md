# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Quick Start

```bash
# Set up development environment
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -e ".[dev]"
pre-commit install

# Run tests (all pass without API key or credentials)
pytest tests/ -v

# Run a single test file or test
pytest tests/test_purge.py -v
pytest tests/test_purge.py::test_sender_grouping -v

# Lint and format checks (same as CI)
make check          # runs lint, format, security, test
make lint           # ruff check postmind/
make format         # ruff format --check postmind/ tests/
make security       # bandit -r postmind/ -ll -q
make test           # pytest

# Fix lint and format issues in place
make fix            # ruff check --fix + ruff format
```

## Architecture Overview

postmind is a privacy-first email management tool with both CLI and web UI. The architecture has five key layers:

### 1. **Pluggable Email Provider Interface** (`postmind/core/providers/`)

All email operations go through an abstract `EmailProvider` interface (base.py). This enables:
- **Gmail** (OAuth, Gmail API) — via `GmailProvider`
- **IMAP** (Outlook, Fastmail, iCloud, custom servers) — via `IMAPProvider`
- Easy to add new backends without touching core logic

The factory pattern (`providers/factory.py`) selects the right provider at the CLI boundary based on user config. Core code never imports provider implementations directly—only the abstract interface.

**Key insight**: Multi-account support is handled by passing `account_email` through the factory, which loads the right OAuth token or IMAP credentials from `~/.postmind/accounts/<email>.json`.

### 2. **Local SQLite Storage** (`postmind/core/storage.py`)

All state is local and persistent:
- **Accounts** — registered email accounts
- **EmailRecord** — cached message metadata (subject, sender, size, labels, AI classification)
- **UndoLogEntry** — reversible operations; auto-purged after 30 days
- **RuleDefinition** — user-defined automation rules (NL or manual)
- **Agent** — per-account heartbeat daemon config
- **FollowUp** — sent emails being tracked for follow-up
- **ClassificationCacheRecord** — cached AI triages (keyed by gmail_id)
- **SenderBlocklist** — protected senders (banks, family, etc.)

Uses SQLAlchemy ORM with session-based access (`get_session()`). Tests use an in-memory SQLite instance via the `clean_db` fixture in `conftest.py`.

### 3. **AI Mode Enforcement** (`postmind/core/ai/mode.py`)

Privacy-first AI architecture with three modes (set via `POSTMIND_AI_MODE` env var or `postmind config ai-mode`):

- **off** (default) — no AI calls; everything is local rule-based
- **local** — only Ollama/llama.cpp; nothing leaves the machine
- **cloud** — Anthropic Claude API allowed; sends subjects/snippets externally

Every AI call must invoke `require_cloud()` or `require_local()` first, which raises `AIModeError` with actionable guidance if the mode doesn't permit it. This prevents silent fallbacks and keeps users in control.

**Key insight**: The chat assistant can run a different backend than the main app (e.g., cloud triage but local chat) via `POSTMIND_CHAT_AI_MODE`.

### 4. **AI Engine & Tool Registry** (`postmind/core/ai_engine.py`, `postmind/core/agent_tools.py`)

**AIEngine** handles:
- Email classification (priority, category, suggested action, explanation)
- NL→Gmail query translation (bulk operations, rules)
- Bulk operation parsing (preview → user confirmation → undo logging)

**Agent tools** define what the **Super Agent** (NL command center in web UI) can do:
- **READ tools** — inbox overview, storage analysis, sender search, automation list (run immediately)
- **WRITE tools** — trash, archive, label, unsubscribe, send, create rules (staged as cards for user confirmation)

Critical design: WRITE tools never execute inside the LLM loop. They stage an action and return a card the user must confirm. Confirmation targets are always server-resolved (sender emails, message IDs computed by our code), never free-form text from the model—this prevents prompt injection from untrusted email bodies.

### 5. **CLI & Web UI** (`postmind/cli/main.py`, `postmind/web/server.py`)

**CLI** (Typer-based):
- Thin interface—parsing and I/O only
- All business logic deferred to `postmind/core/`
- Sub-apps: `postmind accounts`, `postmind agents`
- Commands: `setup`, `auth`, `stats`, `purge`, `undo`, `sync`, `triage`, `digest`, `serve`, etc.

**Web UI** (FastAPI + Jinja2):
- Single-page app on localhost (no external sync)
- In-memory caches: scan results (5-min TTL), sync tasks, OAuth tasks
- CSRF protection via same-origin guard (blocks cross-origin POST/PUT/PATCH/DELETE)
- Onboarding wizard guides first-time users through Gmail/IMAP + optional AI setup
- **Super Agent** — NL command center with tool execution and action cards

## Key Design Principles

1. **Privacy by default**: All data in `~/.postmind/` (SQLite + tokens); nothing external without explicit `ai_mode=cloud`
2. **Reversibility**: Deletes go to Trash; 30-day undo window via `postmind undo`
3. **Safety first**: Destructive operations require confirmation; sensitive senders (banks, healthcare) are flagged
4. **Transparency**: Every AI decision has a one-line explanation; `AI_MODE` is shown in all output
5. **Beginner-friendly**: Works without API key; helpful error messages; guided setup

## Important Configuration & Paths

| File/Path | Purpose |
|-----------|---------|
| `~/.postmind/` | Data directory (see `POSTMIND_DIR` env var) |
| `~/.postmind/postmind.db` | SQLite database (all local state) |
| `~/.postmind/tokens/` | Per-account OAuth tokens (chmod 0o700) |
| `~/.postmind/accounts/` | Per-account config (provider, IMAP settings as JSON) |
| `~/.postmind/undo_logs/` | Undo entries, auto-purged after 30 days |
| `~/.postmind/.env` | Optional env overrides (never commit with real values) |
| `pyproject.toml` | Python version (3.11+), dependencies, entry point |

**Settings** (`postmind/config.py`):
- Read from env vars (prefix `POSTMIND_`) and `~/.postmind/.env`
- `POSTMIND_AI_MODE`: `off` (default) / `local` / `cloud`
- `POSTMIND_AI_MODEL`: Claude model (default `claude-sonnet-4-6`)
- `ANTHROPIC_API_KEY`: Required for cloud AI only
- `POSTMIND_UNDO_WINDOW_DAYS`: How long undo logs are kept (default 30)
- `POSTMIND_AGENT_AUTOPILOT`: Auto-execute low-risk actions without confirmation (default false)

## Testing Strategy

All tests use `MockAIEngine` (not real API calls)—zero credentials needed:

```bash
pytest tests/ -v                              # All tests
pytest tests/test_purge.py -v                 # Single file
pytest tests/test_purge.py::test_abc -v       # Single test
pytest tests/ --cov=postmind --cov-report=term-missing  # With coverage
```

Tests that touch the database must use the `clean_db` fixture (in-memory SQLite per test). Example:

```python
@pytest.fixture(autouse=True)
def _use_clean_db(clean_db):
    pass
```

Key test files:
- `test_purge.py` — sender aggregation, impact scoring, confidence, parsing
- `test_ai_trust_boundary.py` — ai_mode enforcement, cloud warnings
- `test_mock_ai.py` — MockAIEngine paths (all AI without API key)
- `test_provider_resolution.py` — Gmail vs IMAP provider selection
- `test_super_agent_*.py` — web UI agent tool execution and streaming
- `test_imap_*.py` — IMAP-specific flows

## Code Patterns & Conventions

### Provider Usage

Always use the factory to select providers:

```python
from postmind.core.providers.factory import get_provider

# For CLI/web (uses account registry and tokens)
provider = get_provider("gmail", account_email=email)

# Direct instantiation only for tests
from postmind.core.providers.gmail import GmailProvider
provider = GmailProvider()  # Uses default auth
```

### AI Mode Checks

Before any AI call, enforce the mode:

```python
from postmind.core.ai.mode import require_cloud, require_local
from postmind.config import get_settings

settings = get_settings()
require_cloud(settings.ai_mode)  # Raises AIModeError if mode is off/local
# now safe to call Anthropic API
```

### Session & Repository Access

Use the session-based repository pattern for storage:

```python
from postmind.core.storage import get_session, EmailRepo, UndoLogRepo

session = get_session()
email_repo = EmailRepo(session)
undo_repo = UndoLogRepo(session)

# Read
messages = email_repo.find_by_sender("spam@example.com")

# Write
undo_repo.log_operation(account_email, "trash", message_ids, ...)
```

### CLI Command Structure

New commands go in `cli/main.py`. Keep the CLI thin—defer logic to `core/`:

```python
@app.command(name="mycommand")
def my_command(
    email: str = typer.Option(..., help="Account email"),
    dry_run: bool = typer.Option(False, help="Preview without executing"),
) -> None:
    """Short help text."""
    from postmind.core.my_feature import do_work  # Lazy import — fast startup
    result = do_work(email, dry_run=dry_run)
    console.print(f"[green]Done: {result}[/green]")
```

### Account & Provider Selection

Multi-account is handled via environment override or active account file:

```python
from postmind.config import get_active_account, load_account_config
from postmind.core.providers.factory import get_provider

email = get_active_account()  # From ~/.postmind/active_account
cfg = load_account_config(email)  # Per-account provider config

provider = get_provider(
    cfg.get("provider", "gmail"),
    account_email=email,
    # IMAP-specific:
    imap_server=cfg.get("imap_server", ""),
    imap_user=cfg.get("imap_user", ""),
    imap_password=os.environ.get("POSTMIND_IMAP_PASSWORD", ""),
)
```

## Deployment & Release

- **CI**: GitHub Actions (push to main, all PRs) — lint, format, security scan, test, build check on Python 3.11 & 3.13
- **Release**: `pyproject.toml` version bump; `publish.yml` workflow builds and uploads to PyPI
- **Pre-commit hooks**: `pre-commit install` runs ruff lint/format + bandit before commit

## Common Workflows

### Adding a New Core Feature

1. Create `postmind/core/my_feature.py` with business logic
2. Import lazily in the relevant CLI command (keeps startup fast)
3. Add tests in `tests/test_my_feature.py` using `clean_db` fixture
4. Update `CONTRIBUTING.md` if it's a significant addition

### Adding a New CLI Command

1. Add a function in `cli/main.py` decorated with `@app.command()`
2. Parse flags/options via Typer
3. Call core functions (lazy import)
4. Use Rich for output (console, tables, panels)
5. Test with `pytest tests/test_cli_*.py` (or extend existing test file)

### Modifying the Database Schema

1. Add columns or tables to `storage.py` SQLAlchemy models
2. SQLAlchemy creates new tables automatically
3. For new columns on existing tables, add migration logic to `_run_migrations()` (handles ALTER TABLE + idempotent failures)
4. Test migrations with `clean_db` fixture

### Adding a New Provider

1. Create `postmind/core/providers/newprovider.py` implementing the `EmailProvider` ABC
2. Implement the eight required methods: `list_message_ids`, `get_messages_batch`, `get_messages_metadata`, `batch_trash`, `batch_delete_permanent`, `batch_archive`, `batch_label`, `batch_untrash`, `get_profile`
3. Update `factory.py` to instantiate the new provider
4. Add provider selection logic to `cli/main.py` setup flow
5. Write tests (can mock the provider's underlying API)

## Notes on Security & Privacy

- **Token storage**: OAuth tokens written to `~/.postmind/tokens/<email>.json` with chmod 0o700 (owner read-only)
- **IMAP passwords**: Never stored on disk; read from `POSTMIND_IMAP_PASSWORD` env var at runtime
- **Email body**: Only subjects and 300-char snippets sent to Claude (never full body)
- **CSRF protection**: Web UI uses same-origin guard; blocks cross-origin state-changing requests
- **Undo safety**: Trash operations are recoverable for 30 days; permanent delete only via explicit `batch_delete_permanent()` call

## Debugging & Troubleshooting

```bash
# Health check
postmind doctor

# Show data directory & privacy guarantees
postmind privacy

# Enable debug logging (for local dev)
export LOGLEVEL=DEBUG
pytest tests/test_xyz.py -v -s

# Test with real Gmail (requires credentials)
export ANTHROPIC_API_KEY=sk-ant-...
pytest tests/test_xyz.py -v -m integration
```

The test suite is designed to run without real credentials. Use `MockAIEngine` for all AI paths unless testing against live Anthropic API.
