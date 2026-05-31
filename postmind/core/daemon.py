"""Background heartbeat daemon — periodic triage and rule application per account."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _triage_account(email: str) -> None:
    """Run one heartbeat cycle for a single account: fetch new mail, classify, apply rules."""
    from postmind.config import get_settings, load_account_config, token_path_for
    from postmind.core.account_registry import list_accounts
    from postmind.core.storage import AccountRepo, AgentRepo, EmailRepo, get_session

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

    found_count = 0
    try:
        from postmind.core.providers.factory import get_provider
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
            AgentRepo(get_session()).update_after_run(email, found_count=0, status="idle")
            return

        messages = provider.get_messages_batch(ids)
        found_count = len(messages)
        logger.info("Heartbeat %s: %d unread messages", email, found_count)

        # Run AI classification if AI mode is enabled
        settings = get_settings()
        if settings.ai_mode in ("cloud", "local"):
            try:
                from postmind.core.ai_engine import AIEngine
                ai = AIEngine()
                classified = ai.classify_emails(messages)
                logger.info("Heartbeat %s: classified %d emails", email, len(classified))
            except Exception as exc:
                logger.warning("Heartbeat %s: AI classification failed: %s", email, exc)

        AccountRepo(get_session()).update_last_synced(email)
        AgentRepo(get_session()).update_after_run(email, found_count=found_count, status="idle")

    except Exception as exc:
        logger.error("Heartbeat %s failed: %s", email, exc, exc_info=True)
        AgentRepo(get_session()).update_after_run(
            email,
            found_count=found_count,
            status="error",
            error=str(exc),
        )


def start_daemon(interval_minutes: int | None = None, *, run_immediately: bool = False) -> None:
    """Start the daemon. interval_minutes overrides per-agent config if provided."""
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    except ImportError:
        raise ImportError(
            "APScheduler is required for mailtrim watch. "
            "Install it with: pip install apscheduler"
        )

    from postmind.config import DB_PATH
    from postmind.core.storage import AgentRepo, get_session

    agents = AgentRepo(get_session()).list_all()
    active_agents = [a for a in agents if a.is_active]

    if not active_agents:
        # Fall back to accounts if no agents configured yet
        from postmind.core.account_registry import list_accounts
        accounts = list_accounts()
        if not accounts:
            raise RuntimeError("No accounts or agents registered.")
        # Auto-register agents for all accounts
        repo = AgentRepo(get_session())
        for acct in accounts:
            repo.register(
                account_email=acct.email,
                name=acct.email.split("@")[0].title(),
                interval_minutes=interval_minutes or 30,
            )
        active_agents = AgentRepo(get_session()).list_all()

    jobstore = SQLAlchemyJobStore(url=f"sqlite:///{DB_PATH}")
    scheduler = BlockingScheduler(
        jobstores={"default": jobstore},
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60},
    )

    for agent in active_agents:
        effective_interval = interval_minutes if interval_minutes is not None else agent.interval_minutes
        job_id = f"heartbeat_{agent.account_email}"
        scheduler.add_job(
            _triage_account,
            "interval",
            minutes=effective_interval,
            args=[agent.account_email],
            id=job_id,
            replace_existing=True,
            next_run_time=datetime.now(timezone.utc) if run_immediately else None,
        )
        logger.info(
            "Scheduled heartbeat for %s every %d min",
            agent.account_email,
            effective_interval,
        )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(wait=False)


def start_daemon_background(stop_event=None, interval_minutes: int | None = None) -> None:
    """Non-blocking variant for use inside the FastAPI web process."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.jobstores.memory import MemoryJobStore
    except ImportError:
        raise ImportError("Install apscheduler: pip install apscheduler")

    from postmind.core.storage import AgentRepo, get_session
    from postmind.core.account_registry import list_accounts

    agents = AgentRepo(get_session()).list_all()
    active = [a for a in agents if a.is_active]
    if not active:
        accounts = list_accounts()
        if not accounts:
            raise RuntimeError("No accounts or agents registered.")
        repo = AgentRepo(get_session())
        for acct in accounts:
            repo.register(acct.email, acct.email.split("@")[0].title(), interval_minutes or 30)
        active = [a for a in AgentRepo(get_session()).list_all() if a.is_active]

    scheduler = BackgroundScheduler(jobstores={"default": MemoryJobStore()})
    for agent in active:
        mins = interval_minutes or agent.interval_minutes
        scheduler.add_job(
            _triage_account,
            "interval",
            minutes=mins,
            args=[agent.account_email],
            id=f"heartbeat_{agent.account_email}",
            replace_existing=True,
        )

    scheduler.start()
    if stop_event:
        stop_event.wait()  # blocks this thread until stop() is called
        scheduler.shutdown(wait=False)
    else:
        import time
        try:
            while True:
                time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            scheduler.shutdown(wait=False)
