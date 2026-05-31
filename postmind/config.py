"""Central configuration — reads from env vars and ~/.postmind/config.toml."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Default data directory: ~/.postmind/
DATA_DIR = Path(os.environ.get("POSTMIND_DIR", Path.home() / ".postmind"))
DB_PATH = DATA_DIR / "postmind.db"
CREDENTIALS_PATH = DATA_DIR / "credentials.json"  # OAuth client secret (downloaded from GCP)
TOKEN_PATH = DATA_DIR / "token.json"  # OAuth access/refresh token (generated)
UNDO_LOG_DIR = DATA_DIR / "undo_logs"

TOKENS_DIR = DATA_DIR / "tokens"
ACCOUNTS_DIR = DATA_DIR / "accounts"
ACTIVE_ACCOUNT_PATH = DATA_DIR / "active_account"


def token_path_for(email: str) -> Path:
    """Return the per-account OAuth token path."""
    safe = email.lower().replace("/", "_")
    return TOKENS_DIR / f"{safe}.json"


def get_active_account() -> str | None:
    """Return the active account email. Env var override takes precedence (for --account flag)."""
    override = os.environ.get("_POSTMIND_OVERRIDE_ACCOUNT") or os.environ.get("_MAILTRIM_OVERRIDE_ACCOUNT")
    if override:
        return override.strip() or None
    try:
        return ACTIVE_ACCOUNT_PATH.read_text().strip() or None
    except FileNotFoundError:
        return None


def set_active_account(email: str) -> None:
    ACTIVE_ACCOUNT_PATH.write_text(email.strip() + "\n")
    ACTIVE_ACCOUNT_PATH.chmod(0o600)


def load_account_config(email: str) -> dict:
    """Return per-account config (provider, imap settings). Defaults to gmail."""
    safe = email.lower().replace("/", "_")
    p = ACCOUNTS_DIR / f"{safe}.json"
    if not p.exists():
        return {"provider": "gmail"}
    import json as _json
    return _json.loads(p.read_text())


def save_account_config(email: str, config: dict) -> None:
    """Persist per-account config (provider, imap settings) to disk."""
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    ACCOUNTS_DIR.chmod(0o700)
    safe = email.lower().replace("/", "_")
    p = ACCOUNTS_DIR / f"{safe}.json"
    import json as _json
    p.write_text(_json.dumps(config, indent=2))
    p.chmod(0o600)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="POSTMIND_",
        env_file=str(DATA_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Anthropic
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    ai_model: str = "claude-sonnet-4-6"

    # Gmail OAuth scopes
    gmail_scopes: list[str] = Field(
        default=[
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.send",
        ]
    )

    # Behaviour
    dry_run: bool = False  # Global dry-run override
    undo_window_days: int = 30  # How long undo logs are kept
    avoidance_view_threshold: int = 3  # Views before an email is "avoided"
    follow_up_default_days: int = 3  # Default follow-up reminder window

    # Deprecated: per-account provider and IMAP settings now live in
    # ~/.postmind/accounts/<email>.json via save_account_config()/load_account_config().
    # These fields remain for backward-compat fallback on unmigrated installs
    # (web/server.py and cli/main.py use them as the last-resort tier in a
    # four-way priority: CLI flag → per-account config → these Settings → default).
    # Do NOT remove until all callers have migrated to per-account config.
    provider: str = "gmail"
    imap_server: str = ""
    imap_user: str = ""
    imap_port: int = 993
    imap_folder: str = "INBOX"

    # AI mode — controls which AI backends are permitted.
    # "off"   → no AI calls at all (default, privacy-safe)
    # "local" → only local backends (Ollama, llama.cpp) — nothing leaves the machine
    # "cloud" → external API calls allowed (Anthropic); sends email data externally
    ai_mode: str = "off"

    # Ollama (local AI backend)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"

    # Rate limiting
    gmail_batch_size: int = 50  # Max messages per Gmail batch request
    ai_max_classify_batch: int = 20  # Emails per AI classification call


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        # Restrict the data directory so other local users cannot browse it.
        # Credentials and tokens stored here are sensitive.
        DATA_DIR.chmod(0o700)
        UNDO_LOG_DIR.mkdir(parents=True, exist_ok=True)
        TOKENS_DIR.mkdir(parents=True, exist_ok=True)
        TOKENS_DIR.chmod(0o700)
        _settings = Settings()
    return _settings
