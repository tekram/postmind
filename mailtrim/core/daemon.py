"""Background heartbeat daemon — periodic triage and rule application per account."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _triage_account(email: str) -> None:
    """Run one heartbeat cycle for a single account: fetch new mail, classify, apply rules."""
    from mailtrim.config import get_settings, load_account_config, token_path_for
    from mailtrim.core.account_registry import list_accounts
    from mailtrim.core.storage import AccountRepo, EmailRepo, get_session

    logger.info("Heartbeat: %s", email)

    # Verify account is still registered and has a valid token
    accounts = list_accounts()
    acct = next((a for a in accounts if a.email == email), None)
    if not acct:
        logger.warning("Account %s not found — skipping heartbeat", email)
        return

    if acct.provider == "gmail":
        token = token_path_for(email)
        if not token.exists():
            logger.warning("No token for %s — skipping heartbeat (run: mailtrim auth)", email)
            return

    try:
        from mailtrim.core.providers.factory import get_provider
        cfg = load_account_config(email)
        provider_name = cfg.get("provider", "gmail")

        if provider_name == "imap":
            import os
            pw = os.environ.get("MAILTRIM_IMAP_PASSWORD", "")
            provider = get_provider(
                "imap",
                imap_server=cfg.get("imap_server", ""),
                imap_user=cfg.get("imap_user", ""),
                imap_password=pw,
                imap_port=cfg.get("imap_port", 993),
                imap_folder=cfg.get("imap_folder", "INBOX"),
            )
        else:
            provider = get_provider("gmail", account_email=email)

        # Fetch unread inbox emails (limit 20 per heartbeat to stay fast)
        ids = provider.list_message_ids(query="in:inbox is:unread", max_results=20)
        if not ids:
            logger.info("Heartbeat %s: inbox clear", email)
            AccountRepo(get_session()).update_last_synced(email)
            return

        messages = provider.get_messages_batch(ids)
        logger.info("Heartbeat %s: %d unread messages", email, len(messages))

        # Run AI classification if AI mode is enabled
        settings = get_settings()
        if settings.ai_mode in ("cloud", "local"):
            try:
                from mailtrim.core.ai_engine import AIEngine
                ai = AIEngine()
                classified = ai.classify_emails(messages)
                logger.info("Heartbeat %s: classified %d emails", email, len(classified))
            except Exception as exc:
                logger.warning("Heartbeat %s: AI classification failed: %s", email, exc)

        AccountRepo(get_session()).update_last_synced(email)

    except Exception as exc:
        logger.error("Heartbeat %s failed: %s", email, exc, exc_info=True)


def start_daemon(interval_minutes: int = 30, *, run_immediately: bool = False) -> None:
    """Start the APScheduler background daemon. Blocks until interrupted."""
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    except ImportError:
        raise ImportError(
            "APScheduler is required for mailtrim watch. "
            "Install it with: pip install apscheduler"
        )

    from mailtrim.config import DB_PATH
    from mailtrim.core.account_registry import list_accounts

    accounts = list_accounts()
    if not accounts:
        raise RuntimeError(
            "No accounts registered. Run `mailtrim setup` or `mailtrim accounts add` first."
        )

    jobstore = SQLAlchemyJobStore(url=f"sqlite:///{DB_PATH}")
    scheduler = BlockingScheduler(
        jobstores={"default": jobstore},
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60},
    )

    for acct in accounts:
        job_id = f"heartbeat_{acct.email}"
        scheduler.add_job(
            _triage_account,
            "interval",
            minutes=interval_minutes,
            args=[acct.email],
            id=job_id,
            replace_existing=True,
            next_run_time=datetime.now(timezone.utc) if run_immediately else None,
        )
        logger.info("Scheduled heartbeat for %s every %d min", acct.email, interval_minutes)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(wait=False)
