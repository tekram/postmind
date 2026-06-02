"""Account registry — registered accounts, active account selection, and one-time migration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AccountInfo:
    email: str
    provider: str       # "gmail" | "imap"
    display_name: str
    imap_server: str = ""
    imap_port: int = 993
    imap_folder: str = "INBOX"
    imap_user: str = ""
    is_active: bool = True


def list_accounts() -> list[AccountInfo]:
    """Return registered (active) accounts ordered by registration time.

    Removed accounts are soft-deleted (is_active=False) and excluded here so
    they no longer appear in the UI/CLI; re-adding reactivates the same row.
    """
    from postmind.core.storage import AccountRepo, get_session
    from postmind.config import load_account_config
    rows = AccountRepo(get_session()).list_all()
    result = []
    for row in rows:
        if not row.is_active:
            continue
        cfg = load_account_config(row.email)
        result.append(AccountInfo(
            email=row.email,
            provider=cfg.get("provider", "gmail"),
            display_name=row.display_name or row.email,
            imap_server=cfg.get("imap_server", ""),
            imap_port=cfg.get("imap_port", 993),
            imap_folder=cfg.get("imap_folder", "INBOX"),
            imap_user=cfg.get("imap_user", ""),
            is_active=row.is_active,
        ))
    return result


def get_active() -> AccountInfo | None:
    """Return the currently active account, or the first registered if none set."""
    from postmind.config import get_active_account
    email = get_active_account()
    accounts = list_accounts()
    if not accounts:
        return None
    if email:
        match = next((a for a in accounts if a.email == email), None)
        if match:
            return match
    return accounts[0]


def switch_to(email: str) -> None:
    """Switch the active account. Raises ValueError if not registered."""
    from postmind.config import set_active_account
    accounts = list_accounts()
    if not any(a.email == email for a in accounts):
        raise ValueError(
            f"Account {email!r} is not registered. Run: postmind accounts add"
        )
    set_active_account(email)


def register_gmail(email: str, display_name: str = "") -> None:
    """Register a Gmail account in the registry."""
    from postmind.config import save_account_config
    from postmind.core.storage import AccountRepo, get_session
    save_account_config(email, {"provider": "gmail"})
    AccountRepo(get_session()).register(
        email=email, provider="gmail", display_name=display_name or email
    )


def register_imap(
    email: str,
    imap_server: str,
    imap_user: str,
    imap_port: int = 993,
    imap_folder: str = "INBOX",
    display_name: str = "",
) -> None:
    """Register an IMAP account in the registry."""
    from postmind.config import save_account_config
    from postmind.core.storage import AccountRepo, get_session
    save_account_config(email, {
        "provider": "imap",
        "imap_server": imap_server,
        "imap_user": imap_user,
        "imap_port": imap_port,
        "imap_folder": imap_folder,
    })
    AccountRepo(get_session()).register(
        email=email, provider="imap", display_name=display_name or email
    )


def migrate_legacy_token() -> None:
    """One-time: move ~/.postmind/token.json → tokens/<email>.json and register the account."""
    from postmind.config import DATA_DIR, TOKEN_PATH, token_path_for, set_active_account

    legacy = DATA_DIR / "token.json"
    if not legacy.exists():
        return

    from postmind.core.storage import AccountRepo, get_session
    if AccountRepo(get_session()).count() > 0:
        return  # already migrated

    try:
        from postmind.config import get_settings
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        settings = get_settings()
        creds = Credentials.from_authorized_user_file(str(legacy), settings.gmail_scopes)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())

        from googleapiclient.discovery import build
        svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        profile = svc.users().getProfile(userId="me").execute()
        email = profile["emailAddress"]
    except Exception:
        return  # can't migrate without live credentials; user will re-auth

    dest = token_path_for(email)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(legacy.read_text())
    dest.chmod(0o600)
    register_gmail(email)
    set_active_account(email)
    legacy.rename(DATA_DIR / "token.json.migrated")
