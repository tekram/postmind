"""Local web interface for postmind — runs on localhost, nothing leaves your machine."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from postmind import __version__
from postmind.config import CREDENTIALS_PATH, DATA_DIR, TOKEN_PATH, get_settings

_THIS_DIR = Path(__file__).parent
_TEMPLATES_DIR = _THIS_DIR / "templates"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Auto-start daemon and trigger initial sync based on settings."""
    import logging as _log
    import threading as _t

    _startup_log = _log.getLogger(__name__)
    s = get_settings()

    if s.auto_start_daemon:
        # _watch_thread/_watch_stop_event/_watch_interval are module globals
        # defined in the Watch daemon section below — they exist at call time.
        global _watch_thread, _watch_interval  # type: ignore[name-defined]
        _watch_interval = s.daemon_interval_minutes
        if not (_watch_thread and _watch_thread.is_alive()):
            _watch_stop_event.clear()  # type: ignore[name-defined]

            def _run_daemon() -> None:
                from postmind.core.daemon import start_daemon_background

                start_daemon_background(
                    stop_event=_watch_stop_event,  # type: ignore[name-defined]
                    interval_minutes=_watch_interval,
                )

            _watch_thread = _t.Thread(target=_run_daemon, daemon=True, name="postmind-watch")
            _watch_thread.start()
            _startup_log.info("postmind: daemon auto-started (interval=%dm)", _watch_interval)

    if s.auto_sync_on_first_run:

        def _maybe_sync() -> None:
            try:
                from postmind.config import get_active_account
                from postmind.core.storage import AccountRepo, get_session

                email = get_active_account()
                if not email:
                    return
                row = AccountRepo(get_session()).get(email)
                if row is None or row.last_synced_at is None:
                    task_id = f"auto-{uuid.uuid4().hex[:8]}"
                    _sync_tasks[task_id] = {  # type: ignore[name-defined]
                        "status": "running",
                        "step": 0,
                        "message": "Auto-syncing inbox…",
                        "count": 0,
                        "total": 0,
                        "error": None,
                        "started_at": time.time(),
                        "next_url": "/stats",
                        "detail": "",
                        "complete": False,
                    }
                    _startup_log.info("postmind: auto-sync started for %s", email)
                    _sync_worker(task_id, "inbox", 1000, False)  # type: ignore[name-defined]
            except Exception as exc:
                _startup_log.warning("postmind: auto-sync failed: %s", exc)

        _t.Thread(target=_maybe_sync, daemon=True, name="postmind-auto-sync").start()

    yield


app = FastAPI(title="postmind", docs_url=None, redoc_url=None, lifespan=_lifespan)
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# ── HTTP MCP endpoint — mount at /mcp so `postmind serve` also acts as an MCP host ──
# Claude Code / Claude Desktop can connect via: { "url": "http://127.0.0.1:8484/mcp" }
# This is the easiest on-ramp for users who already run the web UI — no separate
# subprocess or extra install step needed.
try:
    from postmind.core.agent_mcp import build_server as _build_mcp_server

    _mcp_server = _build_mcp_server()  # account_email=None → uses active account
    app.mount("/mcp", _mcp_server.sse_app())  # SSE transport; broadly supported
except Exception:
    pass  # [mcp] extra not installed — web UI still works fine without it


@app.middleware("http")
async def _same_origin_guard(request: Request, call_next):
    """Block cross-origin state-changing requests (CSRF defense).

    postmind serves on localhost with ambient auth (token file + active account)
    and no per-request CSRF token, so a malicious page the user visits could
    auto-submit a cross-origin form to e.g. /agent/send. We reject any mutating
    request whose Origin/Referer host doesn't match the server host. Requests with
    neither header (curl, the CLI, tests) are allowed — those aren't browser CSRF.
    """
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        from urllib.parse import urlparse

        from starlette.responses import PlainTextResponse

        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin:
            src = urlparse(origin).netloc
            if src and src != request.url.netloc:
                return PlainTextResponse("Cross-origin request blocked.", status_code=403)
    return await call_next(request)


# In-memory scan cache — keyed by "latest", short TTL
_scan_cache: dict[str, dict] = {}
_CACHE_TTL = 300  # 5 minutes

# In-memory review cache — keyed by token, used by stage_trash_query
_REVIEW_CACHE: dict[str, dict] = {}  # token -> {account_email, description, emails, expires}
_REVIEW_TTL = 1800  # seconds

# MCP pool cache — keyed by account_email; invalidated when mcp_servers config changes
_mcp_pools: dict[str, Any] = {}


def _review_put(account_email: str, description: str, emails: list[dict]) -> str:
    import secrets

    # Drop expired entries so the dict can't grow without bound.
    now = time.time()
    for tok in [t for t, e in _REVIEW_CACHE.items() if e["expires"] < now]:
        _REVIEW_CACHE.pop(tok, None)
    token = secrets.token_urlsafe(16)
    _REVIEW_CACHE[token] = {
        "account_email": account_email,
        "description": description,
        "emails": emails,
        "expires": now + _REVIEW_TTL,
    }
    return token


def _review_get(token: str) -> dict | None:
    entry = _REVIEW_CACHE.get(token)
    if entry and time.time() < entry["expires"]:
        return entry
    return None


def _fmt_size(num_bytes: int) -> str:
    mb = (num_bytes or 0) / (1024 * 1024)
    if mb >= 0.1:
        return f"{mb:.1f} MB"
    return f"{(num_bytes or 0) // 1024} KB"


# In-memory sync task state: task_id → state dict
_sync_tasks: dict[str, dict] = {}
_active_sync_task_id: str | None = None

_oauth_tasks: dict[str, dict] = {}  # task_id → {status, email, error}

_active_web_account: str | None = None  # email override set by the web UI switcher

_executor = ThreadPoolExecutor(max_workers=6)

# Per-account MCP client pools — lazily initialized when the agent runs
_mcp_pools: dict[str, Any] = {}  # account_email → MCPClientPool
_main_event_loop: asyncio.AbstractEventLoop | None = None


@app.on_event("startup")
async def _startup():
    global _main_event_loop
    _main_event_loop = asyncio.get_event_loop()
    # Best-effort: provision memory MCP server for the active account on startup
    try:
        from postmind.config import get_active_account
        from postmind.core.mcp_client import bootstrap_memory_for_account

        active = get_active_account()
        if active:
            bootstrap_memory_for_account(active)
    except Exception:
        pass


class _NullPool:
    def get_tools(self) -> list[dict]:
        return []


async def _get_mcp_pool(account_email: str):
    """Return (or lazily build) the MCP pool for account_email."""
    from postmind.config import load_account_config
    from postmind.core.mcp_client import build_pool_for_account

    if account_email not in _mcp_pools:
        cfg = load_account_config(account_email) if account_email else {}
        if cfg.get("mcp_servers"):
            _mcp_pools[account_email] = await build_pool_for_account(account_email)
        else:
            _mcp_pools[account_email] = None
    return _mcp_pools.get(account_email)


def _maybe_synthesize_rules(account_email: str) -> None:
    """Check for trash-pattern candidates and synthesize rule proposals if AI is on.

    Best-effort, never raises. Designed to be called in a background executor
    thread after a trash action so it never blocks the response.
    """
    if not account_email or _ai_mode() == "off":
        return
    try:
        from postmind.core.ai_engine import AIEngine
        from postmind.core.storage import RuleDefinition, RuleRepo, UserActionRepo, get_session

        session = get_session()
        candidates = UserActionRepo(session).candidates_for_rule_synthesis(account_email)
        if not candidates:
            return

        ai = AIEngine()
        rule_repo = RuleRepo(session)
        for c in candidates[:3]:  # at most 3 new proposals per check
            try:
                nl_rule = ai.synthesize_rule_from_actions(
                    sender_email=c["sender_email"],
                    sender_name=c["sender_name"],
                    trash_count=c["trash_count"],
                    sample_subjects=c["sample_subjects"],
                )
                rule_repo.create(
                    RuleDefinition(
                        account_email=account_email,
                        name=f"Auto-trash: {c['sender_name']}",
                        natural_language=nl_rule.natural_language,
                        gmail_query=nl_rule.gmail_query,
                        action=nl_rule.action,
                        action_params_json=__import__("json").dumps(nl_rule.action_params),
                        ai_explanation=nl_rule.explanation,
                        is_active=False,
                        proposed_at=__import__("datetime").datetime.now(
                            __import__("datetime").timezone.utc
                        ),
                    )
                )
            except Exception:
                pass  # one bad synthesis doesn't block others
    except Exception:
        pass


# ── Helpers ───────────────────────────────────────────────────────────────────


def _cache_set(groups, profile: dict, account_email: str) -> None:
    email = _get_web_account() or "default"
    _scan_cache[email] = {
        "groups": groups,
        "profile": profile,
        "account_email": account_email,
        "scanned_at": datetime.now(timezone.utc).strftime("%H:%M UTC"),
        "expires": time.time() + _CACHE_TTL,
    }


def _cache_get() -> dict | None:
    email = _get_web_account() or "default"
    entry = _scan_cache.get(email)
    if entry and time.time() < entry["expires"]:
        return entry
    return None


def _get_web_account() -> str | None:
    """Return the email address the web UI is currently scoped to.

    Falls back to the first registered account if the resolved pointer is
    dangling (e.g. it referenced an account that has since been removed).
    """
    from postmind.config import get_active_account
    from postmind.core.account_registry import list_accounts

    email = _active_web_account or get_active_account()
    accounts = list_accounts()
    if accounts and not any(a.email == email for a in accounts):
        return accounts[0].email
    return email


def _is_authed() -> bool:
    from postmind.config import token_path_for

    email = _get_web_account()
    if not email:
        return TOKEN_PATH.exists()  # legacy fallback for unmigrated installs
    from postmind.core.account_registry import list_accounts

    acct = next((a for a in list_accounts() if a.email == email), None)
    if not acct:
        return TOKEN_PATH.exists()
    if acct.provider == "imap":
        return True  # IMAP auth checked at connection time
    return token_path_for(email).exists()


def _ai_mode() -> str:
    try:
        return get_settings().ai_mode
    except Exception:
        return "off"


def _provider_name() -> str:
    try:
        return get_settings().provider
    except Exception:
        return "gmail"


def _chat_mode() -> str:
    """Effective backend for the chat assistant — its own setting, or the
    global ai_mode when left to inherit."""
    try:
        s = get_settings()
        return s.chat_ai_mode or s.ai_mode
    except Exception:
        return "off"


def _chat_engine_kwargs() -> dict:
    """Override kwargs for AIEngine so the assistant uses its configured backend/model."""
    s = get_settings()
    return {
        "mode": _chat_mode(),
        "cloud_model": s.chat_cloud_model or s.ai_model,
        "ollama_model": s.chat_ollama_model or s.ollama_model,
    }


def _base() -> dict:
    """Base template context — request passed separately to TemplateResponse."""
    from postmind.core.account_registry import list_accounts

    accounts = list_accounts()
    current_email = _get_web_account()
    return {
        "version": __version__,
        "ai_mode": _ai_mode(),
        "chat_mode": _chat_mode(),
        "provider": _provider_name(),
        "is_authed": _is_authed(),
        "accounts": [
            {"email": a.email, "display_name": a.display_name, "provider": a.provider}
            for a in accounts
        ],
        "active_account_email": current_email,
        "multi_account": len(accounts) > 1,
    }


def _resp(request: Request, name: str, ctx: dict, status: int = 200) -> HTMLResponse:
    """Render a template using Starlette 1.x API."""
    return templates.TemplateResponse(request, name, context=ctx, status_code=status)


def _build_provider():
    from postmind.config import load_account_config
    from postmind.core.providers.factory import get_provider

    email = _get_web_account()
    if email:
        cfg = load_account_config(email)
        provider_name = cfg.get("provider", "gmail")
    else:
        # Legacy fallback — read from global settings
        provider_name = get_settings().provider
        cfg = {}

    if provider_name == "imap":
        import os

        pw = os.environ.get("POSTMIND_IMAP_PASSWORD", "")
        s = get_settings()
        return get_provider(
            "imap",
            imap_server=cfg.get("imap_server") or s.imap_server,
            imap_user=cfg.get("imap_user") or s.imap_user,
            imap_password=pw,
            imap_port=cfg.get("imap_port") or s.imap_port,
            imap_folder=cfg.get("imap_folder") or s.imap_folder,
        )

    return get_provider("gmail", account_email=email)


def _enrich_groups(groups) -> list[dict]:
    from postmind.core.sender_stats import (
        classify_sender_risk,
        compute_confidence_score,
        confidence_safety_label,
        risk_tier_icon,
    )

    enriched = []
    for g in groups:
        conf = compute_confidence_score(g)
        risk = classify_sender_risk(g)
        size_str = (
            f"{g.total_size_mb} MB"
            if g.total_size_mb >= 0.1
            else f"{g.total_size_bytes // 1024} KB"
        )
        enriched.append(
            {
                "sender_email": g.sender_email,
                "sender_name": g.sender_name or g.sender_email,
                "count": g.count,
                "size_str": size_str,
                "size_mb": g.total_size_mb,
                "oldest": g.earliest_date.strftime("%b %Y"),
                "oldest_ts": int(g.earliest_date.timestamp()),
                "has_unsubscribe": g.has_unsubscribe,
                "confidence": conf,
                "safety_label": confidence_safety_label(conf),
                "tier_icon": risk_tier_icon(conf),
                "risk": risk,
                "impact_score": g.impact_score,
                "sample_subjects": g.sample_subjects,
            }
        )
    return enriched


# ── Dashboard ─────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    ctx = _base()
    ctx["active"] = "dashboard"

    from postmind.core.account_registry import list_accounts as _list_accts

    if not _list_accts() and not _is_authed():
        return RedirectResponse("/onboarding", status_code=302)

    # First-run: once an account has synced data but hasn't seen the welcome
    # summary yet, send them there instead of the empty/standard dashboard.
    _acct = _get_web_account()
    if _acct:
        from postmind.core.storage import AccountRepo as _AR
        from postmind.core.storage import EmailRepo as _ER
        from postmind.core.storage import get_session as _gs

        _s = _gs()
        try:
            _row = _AR(_s).get(_acct)
            _has = bool(_ER(_s).get_inbox(_acct, limit=1))
            if _has and _row is not None and _row.welcomed_at is None:
                return RedirectResponse("/welcome", status_code=302)
        finally:
            _s.close()

    from postmind.core.sender_stats import (
        best_next_step,
        fetch_sender_groups_from_db,
        generate_recommendations,
        group_by_domain,
        reclaimable_mb,
    )
    from postmind.core.storage import AccountRepo, EmailRecord, EmailRepo, get_session

    account_email = _get_web_account() or ""

    cached = _cache_get()
    if cached:
        groups = cached["groups"]
        scanned_at = cached["scanned_at"]
        account_email = cached["account_email"]
        profile = cached["profile"]
        total_emails = 0
    else:
        session = get_session()
        try:
            has_data = bool(account_email and EmailRepo(session).get_inbox(account_email, limit=1))
            if has_data:
                groups = fetch_sender_groups_from_db(
                    account_email=account_email,
                    scope="inbox",
                    min_count=1,
                    top_n=50,
                    sort_by="score",
                )
                acct_row = AccountRepo(session).get(account_email)
                scanned_at = (
                    acct_row.last_synced_at.strftime("%d %b %Y")
                    if (acct_row and acct_row.last_synced_at)
                    else "local cache"
                )
                total_emails = (
                    session.query(EmailRecord)
                    .filter(
                        EmailRecord.account_email == account_email, EmailRecord.is_inbox.is_(True)
                    )
                    .count()
                )
                profile = {}
            else:
                groups = None
                scanned_at = None
                total_emails = 0
                profile = {}
        finally:
            session.close()

    if groups:
        domain_groups = group_by_domain(groups)
        domain_map = {d.domain: d for d in domain_groups}
        recs = generate_recommendations(groups, top_n=5, domain_map=domain_map)
        bns = best_next_step(recs)
        total_reclaimable = reclaimable_mb(recs)

        from datetime import datetime as _dt
        from datetime import timezone as _tz

        from postmind.core.storage import DailyBriefRepo

        _today = _dt.now(_tz.utc).date().isoformat()
        _db_rec = DailyBriefRepo(get_session()).get_today(account_email, _today)
        ctx.update(
            {
                "has_scan": True,
                "scanned_at": scanned_at,
                "account_email": account_email,
                "profile": profile,
                "top_senders": _enrich_groups(groups[:5]),
                "total_reclaimable": total_reclaimable,
                "sender_count": len(groups),
                "best_next": bns,
                "total_emails": total_emails,
                "daily_brief_preview": {
                    "exists": _db_rec is not None,
                    "snippet": (_db_rec.content[:200] + "…") if _db_rec else None,
                    "ai_used": _db_rec.ai_used if _db_rec else False,
                    "generated_at": _db_rec.generated_at.strftime("%H:%M") if _db_rec else None,
                },
            }
        )
    else:
        ctx["has_scan"] = False
        ctx["daily_brief_preview"] = {
            "exists": False,
            "snippet": None,
            "ai_used": False,
            "generated_at": None,
        }

    return _resp(request, "dashboard.html", ctx)


# ── Daily Brief ───────────────────────────────────────────────────────────────


def _render_brief_html(content: str) -> str:
    """Convert a brief's Markdown to safe, styled HTML.

    The brief is small and LLM-generated, so we do a tight, allow-listed
    conversion (escape first, then re-introduce only a handful of tags) rather
    than pull in a full Markdown dependency. Handles bold, inline code, bullet
    lists, and paragraph/line breaks — the entire vocabulary the brief prompt
    asks the model to emit.
    """
    import html as _html
    import re as _re

    if not content:
        return '<p class="text-ink-tertiary text-sm">No brief content.</p>'

    def _inline(text: str) -> str:
        text = _html.escape(text)
        # Bold before italics so the ** in **x** isn't eaten by the single-* rule.
        text = _re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
        text = _re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", text)
        text = _re.sub(
            r"`(.+?)`",
            r'<code class="bg-surface-2 rounded-chip px-1 text-[12px] font-mono">\1</code>',
            text,
        )
        return text

    blocks: list[str] = []
    bullets: list[str] = []
    para: list[str] = []

    def _flush_bullets() -> None:
        if bullets:
            items = "".join(f"<li>{b}</li>" for b in bullets)
            blocks.append(
                '<ul class="list-disc pl-5 space-y-1 text-ink text-sm leading-relaxed">'
                f"{items}</ul>"
            )
            bullets.clear()

    def _flush_para() -> None:
        if para:
            blocks.append('<p class="text-ink text-sm leading-relaxed">' + " ".join(para) + "</p>")
            para.clear()

    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            _flush_bullets()
            _flush_para()
            continue
        m = _re.match(r"^(?:[-*•]|\d+\.)\s+(.*)", line)
        if m:
            _flush_para()
            bullets.append(_inline(m.group(1)))
        else:
            _flush_bullets()
            para.append(_inline(line))

    _flush_bullets()
    _flush_para()
    return '<div class="space-y-3">' + "".join(blocks) + "</div>"


def _render_brief_status(brief) -> str:
    """Render the brief's deterministic status line from its stored count columns.

    Always shows unread; appends the non-zero clauses (new since yesterday,
    high-priority, overdue follow-ups). Computed here — never trusted to the LLM —
    so the numbers always match the stat cards above.
    """
    unread = getattr(brief, "unread_count", 0) or 0
    new = getattr(brief, "new_since_yesterday", 0) or 0
    high = getattr(brief, "high_priority_count", 0) or 0
    overdue = getattr(brief, "overdue_followups_count", 0) or 0

    parts = [f"{unread} unread"]
    if new:
        parts.append(f"{new} new since yesterday")
    if high:
        parts.append(f"{high} high-priority")
    if overdue:
        parts.append(f"{overdue} overdue follow-up{'s' if overdue != 1 else ''}")

    line = " · ".join(parts)
    return (
        '<p class="text-ink text-sm leading-relaxed">'
        '<span class="font-semibold text-ink">Inbox:</span> '
        f'<span class="text-ink-subtle">{line}</span></p>'
    )


def _render_digest_panes(brief) -> tuple[str, str, str, str]:
    """Render Newsletter and Promotions tab panes from brief digest JSON.

    Returns (nl_pane_html, pr_pane_html, nl_badge_html, pr_badge_html).
    """
    import html as _html
    import json as _json
    from datetime import timezone

    _icon_keep = (
        '<svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">'
        '<path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>'
    )
    _icon_eye = (
        '<svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.75">'
        '<path stroke-linecap="round" stroke-linejoin="round" d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>'
        '<circle cx="12" cy="12" r="3"/></svg>'
    )

    def _preview_btn(gmail_id: str) -> str:
        if not gmail_id:
            return ""
        gid = _html.escape(gmail_id)
        return (
            f"<button onclick=\"event.stopPropagation();_briefPreview('{gid}')\" "
            f'class="p-1 rounded text-ink-tertiary hover:text-accent hover:bg-accent-subtle transition-colors" '
            f'title="Preview email">{_icon_eye}</button>'
        )

    trash_after_iso = ""
    if brief and brief.digest_trash_after:
        ta = brief.digest_trash_after
        if ta.tzinfo is None:
            ta = ta.replace(tzinfo=timezone.utc)
        trash_after_iso = _html.escape(ta.isoformat())

    def _countdown_badge(exempted: bool) -> str:
        if exempted or not trash_after_iso:
            return '<span class="pm-badge text-[10px] text-success border-success-border bg-success-subtle">Kept</span>'
        return (
            f'<span class="digest-countdown pm-badge text-[10px] text-ink-tertiary" '
            f'data-trash-after="{trash_after_iso}">Trashing in 48h</span>'
        )

    def _keep_btn(sender_email: str, exempted: bool) -> str:
        se = _html.escape(sender_email)
        active = "text-success hover:text-ink-tertiary"
        inactive = "text-ink-tertiary hover:text-success"
        cls = active if exempted else inactive
        return (
            f'<button class="digest-keep-btn p-1 rounded {cls} hover:bg-surface-2 transition-colors" '
            f'data-sender="{se}" data-exempted="{"1" if exempted else "0"}" '
            f'title="{"Un-keep this sender" if exempted else "Keep — never auto-trash"}">'
            f"{_icon_keep}</button>"
        )

    # ── Newsletters ───────────────────────────────────────────────────────────
    nl_items: list[dict] = []
    if brief and brief.newsletters_json:
        try:
            nl_items = _json.loads(brief.newsletters_json)
        except Exception:
            pass

    if not nl_items:
        nl_pane = (
            '<div class="py-8 text-center text-ink-tertiary text-sm">'
            "No newsletters in the last 24 hours."
            "</div>"
        )
    else:
        cards = []
        for item in nl_items:
            se = (item.get("sender_email") or "").lower()
            sn = _html.escape(item.get("sender") or se)
            exempted = item.get("exempted", False)
            email_ids = item.get("email_ids", [])
            count = len(email_ids)
            first_id = email_ids[0] if email_ids else ""
            bullets = item.get("summary_bullets") or []
            bullet_html = "".join(
                f'<li class="text-ink-subtle text-xs leading-relaxed">{_html.escape(str(b))}</li>'
                for b in bullets[:3]
            )
            cards.append(
                f'<div class="py-3 border-b border-hairline last:border-0" data-nl-sender="{_html.escape(se)}">'
                f'<div class="flex items-start justify-between gap-3">'
                f'  <div class="min-w-0">'
                f'    <p class="text-sm font-semibold text-ink">{sn}</p>'
                f'    <p class="text-xs text-ink-tertiary">{count} email{"s" if count != 1 else ""}</p>'
                f"  </div>"
                f'  <div class="flex items-center gap-1.5 shrink-0">'
                f"    {_countdown_badge(exempted)}"
                f"    {_preview_btn(first_id)}"
                f"    {_keep_btn(se, exempted)}"
                f"  </div>"
                f"</div>"
                f'<ul class="list-disc pl-4 mt-2 space-y-0.5">{bullet_html}</ul>'
                f"</div>"
            )
        nl_pane = "".join(cards)

    nl_badge = (
        f'<span class="ml-1 text-[10px] text-ink-tertiary">({len(nl_items)})</span>'
        if nl_items
        else ""
    )

    # ── Promotions ────────────────────────────────────────────────────────────
    pr_items: list[dict] = []
    if brief and brief.promotions_json:
        try:
            pr_items = _json.loads(brief.promotions_json)
        except Exception:
            pass

    if not pr_items:
        pr_pane = (
            '<div class="py-8 text-center text-ink-tertiary text-sm">'
            "No promotional emails in the last 24 hours."
            "</div>"
        )
    else:
        rows = []
        for item in pr_items:
            se = (item.get("sender_email") or "").lower()
            sn = _html.escape(item.get("sender") or se)
            exempted = item.get("exempted", False)
            email_ids = item.get("email_ids", [])
            count = len(email_ids)
            first_id = email_ids[0] if email_ids else ""
            offer = _html.escape(item.get("offer_line") or "Promotional offer")
            click_handler = (
                f"onclick=\"if(!event.target.closest('button'))_briefPreview('{_html.escape(first_id)}')\" "
                f'style="cursor:pointer" '
                if first_id
                else ""
            )
            rows.append(
                f'<div class="py-3 border-b border-hairline last:border-0 flex items-center gap-3 '
                f'hover:bg-surface-2 rounded-button -mx-1 px-1 transition-colors" '
                f'data-pr-sender="{_html.escape(se)}" {click_handler}>'
                f'  <div class="min-w-0 flex-1">'
                f'    <p class="text-sm font-semibold text-ink">{sn}</p>'
                f'    <p class="text-xs text-ink-subtle mt-0.5">{offer}</p>'
                f'    <p class="text-xs text-ink-tertiary">{count} email{"s" if count != 1 else ""}</p>'
                f"  </div>"
                f'  <div class="flex items-center gap-1.5 shrink-0">'
                f"    {_countdown_badge(exempted)}"
                f"    {_preview_btn(first_id)}"
                f"    {_keep_btn(se, exempted)}"
                f"  </div>"
                f"</div>"
            )
        pr_pane = "".join(rows)

    pr_badge = (
        f'<span class="ml-1 text-[10px] text-ink-tertiary">({len(pr_items)})</span>'
        if pr_items
        else ""
    )

    return nl_pane, pr_pane, nl_badge, pr_badge


def _render_brief_links(brief, account_email: str) -> str:
    """Render the brief's identified emails as the "What needs attention" list.

    Each row deep-links into Gmail and has inline Trash / Archive / Reply buttons.
    IMAP accounts get plain rows (no Gmail deep-link) but still get action buttons.
    """
    import html as _html
    import json as _json
    from urllib.parse import quote as _quote

    # SVG icon snippets reused per row
    _icon_external = (
        '<svg class="w-3.5 h-3.5 shrink-0 mt-0.5 text-ink-tertiary group-hover:text-accent transition-colors" '
        'fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.75">'
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"/></svg>'
    )
    _icon_reply = (
        '<svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.75">'
        '<path stroke-linecap="round" stroke-linejoin="round" d="M3 10h10a8 8 0 018 8v2M3 10l6 6M3 10l6-6"/></svg>'
    )
    _icon_archive = (
        '<svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.75">'
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M5 8h14M5 8a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v0a2 2 0 01-2 2M5 8v10a2 2 0 002 2h10a2 2 0 002-2V8M10 12h4"/></svg>'
    )
    _icon_trash = (
        '<svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.75">'
        '<polyline points="3 6 5 6 21 6"/>'
        '<path stroke-linecap="round" stroke-linejoin="round" d="M19 6l-1 14H6L5 6M10 11v6M14 11v6M9 6V4h6v2"/></svg>'
    )
    _icon_spin = (
        '<svg class="w-3.5 h-3.5 animate-spin" viewBox="0 0 24 24" fill="none">'
        '<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"/>'
        '<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"/></svg>'
    )

    items = []
    raw = getattr(brief, "items_json", None)
    if raw:
        try:
            items = [i for i in _json.loads(raw) if isinstance(i, dict)]
        except (ValueError, TypeError):
            items = []

    deals = []
    raw_deals = getattr(brief, "deals_json", None)
    if raw_deals:
        try:
            deals = [i for i in _json.loads(raw_deals) if isinstance(i, dict)]
        except (ValueError, TypeError):
            deals = []

    if not items and not deals:
        return (
            '<div class="mt-5 pt-4 border-t border-hairline">'
            '<p class="text-ink-subtle text-[11px] font-semibold uppercase tracking-[0.06em] mb-2">What needs attention</p>'
            '<p class="text-ink-tertiary text-sm">Nothing needs your attention right now.</p>'
            "</div>"
        )

    from postmind.config import load_account_config

    is_gmail = load_account_config(account_email).get("provider", "gmail") == "gmail"

    # Bulk action bar
    all_ids_js = _json.dumps(
        [str(item.get("gmail_id") or "") for item in items if item.get("gmail_id")]
    )
    bulk_bar = (
        '<div class="flex items-center justify-between mb-2">'
        '<p class="text-ink-subtle text-[11px] font-semibold uppercase tracking-[0.06em]">What needs attention</p>'
        '<div class="flex gap-1.5">'
        f"<button onclick=\"_briefBulk('archive', {_html.escape(all_ids_js)})\" "
        'title="Archive all" '
        'class="flex items-center gap-1 text-[11px] text-ink-tertiary hover:text-accent border border-hairline hover:border-accent-border bg-transparent hover:bg-accent-subtle rounded px-2 py-0.5 transition-colors">'
        f"{_icon_archive}<span>Archive all</span></button>"
        f"<button onclick=\"_briefBulk('trash', {_html.escape(all_ids_js)})\" "
        'title="Trash all" '
        'class="flex items-center gap-1 text-[11px] text-ink-tertiary hover:text-danger border border-hairline hover:border-danger-border bg-transparent hover:bg-danger-bg rounded px-2 py-0.5 transition-colors">'
        f"{_icon_trash}<span>Trash all</span></button>"
        "</div></div>"
    )

    from datetime import datetime as _dt
    from datetime import timezone as _tz

    def _fmt_date(ms: int) -> str:
        if not ms:
            return ""
        try:
            dt = _dt.fromtimestamp(ms / 1000, tz=_tz.utc)
            now_utc = _dt.now(_tz.utc)
            if dt.date() == now_utc.date():
                return dt.strftime("%-I:%M %p")
            elif (now_utc.date() - dt.date()).days < 7:
                return dt.strftime("%a %-I:%M %p")
            else:
                return dt.strftime("%b %-d")
        except Exception:
            return ""

    _DEAL_SCORE_BADGE = {
        3: '<span class="shrink-0 text-[10px] font-semibold px-1.5 py-0.5 rounded bg-success-bg text-success border border-success-border" title="High-value deal">★★★</span>',
        2: '<span class="shrink-0 text-[10px] font-semibold px-1.5 py-0.5 rounded bg-accent-subtle text-accent border border-accent-border" title="Concrete offer">★★</span>',
        1: '<span class="shrink-0 text-[10px] font-semibold px-1.5 py-0.5 rounded bg-surface-2 text-ink-tertiary border border-hairline" title="Promo">★</span>',
    }

    def _build_rows(item_list: list, list_id: str, show_deal_score: bool = False) -> str:
        rows = []
        for item in item_list:
            sender = _html.escape(str(item.get("sender") or "")[:80])
            subject = _html.escape(str(item.get("subject") or "(no subject)")[:120])
            gid = _html.escape(str(item.get("gmail_id") or ""))
            is_unread = item.get("is_unread", True)
            sent_label = _html.escape(_fmt_date(item.get("internal_date") or 0))
            deal_score = item.get("deal_score", 0) if show_deal_score else 0

            unread_dot = (
                '<span class="shrink-0 w-1.5 h-1.5 rounded-full bg-accent mt-1.5" title="Unread"></span>'
                if is_unread
                else '<span class="shrink-0 w-1.5 h-1.5 mt-1.5"></span>'
            )
            sent_span = (
                f'<span class="text-ink-tertiary text-[11px] tabular-nums whitespace-nowrap">{sent_label}</span>'
                if sent_label
                else ""
            )
            score_badge = _DEAL_SCORE_BADGE.get(deal_score, "") if deal_score else ""

            subject_weight = "font-semibold" if is_unread else "font-medium text-ink-subtle"
            text_content = (
                f'<span class="min-w-0 flex-1">'
                f'<span class="block text-ink text-sm {subject_weight} truncate">{subject}</span>'
                f'<span class="flex items-center gap-1.5 text-ink-tertiary text-xs truncate">'
                f'<span class="truncate">{sender}</span>'
                f"{('<span>·</span>' + sent_span) if sent_span else ''}"
                f"</span>"
                f"</span>"
            )

            btn_base = "p-1 rounded text-ink-tertiary transition-colors"
            if is_gmail and gid:
                if show_deal_score:
                    # Route deal opens through tracking endpoint
                    open_url = (
                        f"/brief/deal-open?gid={_quote(str(item.get('gmail_id') or ''), safe='')}"
                    )
                else:
                    open_url = (
                        "https://mail.google.com/mail/u/0/"
                        f"?authuser={_quote(account_email, safe='@')}#all/{_quote(str(item.get('gmail_id') or ''), safe='')}"
                    )
                open_btn = (
                    f'<a href="{open_url}" target="_blank" rel="noopener noreferrer" '
                    f'title="Open in Gmail" '
                    f'class="{btn_base} hover:text-accent hover:bg-accent-subtle">'
                    f"{_icon_reply}</a>"
                )
            else:
                open_btn = ""

            archive_btn = (
                f'<button data-gid="{gid}" data-action="archive" '
                f'onclick="event.stopPropagation();_briefAction(this)" title="Archive" '
                f'class="{btn_base} hover:text-accent hover:bg-accent-subtle">'
                f"{_icon_archive}</button>"
            )
            trash_btn = (
                f'<button data-gid="{gid}" data-action="trash" '
                f'onclick="event.stopPropagation();_briefAction(this)" title="Trash" '
                f'class="{btn_base} hover:text-danger hover:bg-danger-bg">'
                f"{_icon_trash}</button>"
            )

            subject_short = _html.escape(str(item.get("subject") or "(no subject)")[:40])
            ask_btn = (
                f'<button type="button" '
                f'class="brief-agent-chip inline-flex items-center gap-1 text-[10px] text-ink-tertiary '
                f'hover:text-accent px-1.5 py-0.5 rounded transition-colors" '
                f'data-subject="{subject}" '
                f'data-sender="{sender}" '
                f'data-gmail-id="{gid}" '
                f"onclick=\"briefAskAgent('Summarize the thread from {sender} about {subject_short}')\">"
                f'<svg class="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">'
                f'<path stroke-linecap="round" stroke-linejoin="round" d="M8 12h.01M12 12h.01M16 12h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>'
                f"</svg>"
                f"Ask"
                f"</button>"
            )
            actions = (
                f'<span class="shrink-0 flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">'
                f"{open_btn}{archive_btn}{trash_btn}{ask_btn}"
                f"</span>"
            )

            rows.append(
                f'<div class="brief-item group flex items-start gap-2.5 -mx-2 px-2 py-1.5 rounded-button '
                f'hover:bg-surface-2 transition-colors cursor-pointer" data-gmail-id="{gid}" '
                f"onclick=\"if(!event.target.closest('button,a'))_briefPreview('{gid}')\">"
                f"{unread_dot}{score_badge or _icon_external}{text_content}{actions}"
                f"</div>"
            )
        return f'<div class="space-y-0.5" id="{list_id}">{"".join(rows)}</div>'

    # Inbox pane
    if items:
        inbox_pane = bulk_bar + _build_rows(items, "brief-items-list", show_deal_score=False)
    else:
        inbox_pane = (
            '<p class="text-ink-tertiary text-sm">Nothing needs your attention right now.</p>'
        )

    # ── Newsletter & Promotions panes ─────────────────────────────────────────
    nl_pane, pr_pane, nl_badge, pr_badge = _render_digest_panes(brief)

    tab_bar = (
        '<div class="flex gap-0 mb-3 border-b border-hairline">'
        '<button onclick="_briefTab(\'inbox\')" id="tab-btn-inbox" '
        'class="px-3 py-1.5 text-xs font-semibold border-b-2 border-accent text-accent -mb-px bg-transparent">'
        "Inbox</button>"
        '<button onclick="_briefTab(\'newsletters\')" id="tab-btn-newsletters" '
        'class="px-3 py-1.5 text-xs font-semibold border-b-2 border-transparent text-ink-subtle -mb-px bg-transparent">'
        f"Newsletters{nl_badge}</button>"
        '<button onclick="_briefTab(\'promotions\')" id="tab-btn-promotions" '
        'class="px-3 py-1.5 text-xs font-semibold border-b-2 border-transparent text-ink-subtle -mb-px bg-transparent">'
        f"Promotions{pr_badge}</button>"
        "</div>"
    )

    return (
        f'<div class="mt-5 pt-4 border-t border-hairline">'
        f"{tab_bar}"
        f'<div id="brief-tab-inbox">{inbox_pane}</div>'
        f'<div id="brief-tab-newsletters" style="display:none">{nl_pane}</div>'
        f'<div id="brief-tab-promotions" style="display:none">{pr_pane}</div>'
        f"</div>"
    )


@app.get("/brief", response_class=HTMLResponse)
async def brief_page(request: Request):
    ctx = _base()
    ctx["active"] = "brief"
    account_email = _get_web_account() or ""

    if not account_email:
        return RedirectResponse("/onboarding", status_code=302)

    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from postmind.core.storage import DailyBriefRepo, get_session

    # Fast DB-only lookup so the page renders instantly. When no brief exists
    # today, the template auto-fires POST /brief/generate via HTMX instead.
    # Deliberate trade-off: unlike get_or_generate(force=False), this never
    # auto-refreshes a brief older than 1h — use "Generate Now" or the daemon.
    today_str = _dt.now(_tz.utc).date().isoformat()
    session = get_session()
    repo = DailyBriefRepo(session)
    brief = repo.get_today(account_email, today_str)
    recent = repo.list_recent(account_email, limit=7)
    session.close()

    trash_iso = ""
    if brief and brief.digest_trash_after:
        from datetime import timezone as _tz2

        ta = brief.digest_trash_after
        if ta.tzinfo is None:
            ta = ta.replace(tzinfo=_tz2.utc)
        trash_iso = ta.isoformat()

    ctx.update(
        {
            "brief": brief,
            "brief_status_html": _render_brief_status(brief) if brief else "",
            "brief_links_html": _render_brief_links(brief, account_email) if brief else "",
            "brief_html": _render_brief_html(brief.content) if brief else "",
            "digest_trash_after_iso": trash_iso,
            "recent": recent,
            "today_str": today_str,
            "account_email": account_email,
            "ai_mode": _ai_mode(),
            "auto_generate": brief is None,
            "oob": False,
        }
    )
    return _resp(request, "daily_brief.html", ctx)


@app.get("/brief/context")
async def brief_context(request: Request):
    """Return today's brief items as agent-consumable context."""
    import json as _json

    from postmind.core.daily_brief import DailyBriefGenerator

    account_email = _get_web_account() or ""
    if not account_email:
        return {"items": [], "summary": ""}
    try:
        loop = asyncio.get_event_loop()
        brief = await loop.run_in_executor(
            _executor, lambda: DailyBriefGenerator(account_email).get_or_generate(force=False)
        )
        if not brief:
            return {"items": [], "summary": ""}
        items = []
        if brief.items_json:
            items = [i for i in _json.loads(brief.items_json) if isinstance(i, dict)]
        return {
            "items": items[:20],
            "summary": brief.content or "",
            "unread_count": brief.unread_count or 0,
            "high_priority_count": brief.high_priority_count or 0,
        }
    except Exception:
        return {"items": [], "summary": ""}


@app.post("/brief/generate", response_class=HTMLResponse)
async def brief_generate(request: Request):
    """On-demand generation — called by "Generate Now" button via HTMX POST."""
    import html as _html

    account_email = _get_web_account() or ""
    if not account_email:
        return HTMLResponse("<p class='text-warning text-sm'>No active account.</p>")

    loop = asyncio.get_event_loop()

    def _gen():
        from postmind.core.daily_brief import DailyBriefGenerator

        return DailyBriefGenerator(account_email).get_or_generate(force=True)

    try:
        brief = await loop.run_in_executor(_executor, _gen)
    except Exception as exc:
        # No OOB swap here: the stat-card skeleton stays put and the error
        # replaces #brief-content only. The wrapper must keep id="brief-content"
        # so "Generate Now" (hx-target="#brief-content") can still retry.
        return HTMLResponse(
            f'<div id="brief-content" class="px-5 py-5">'
            f"<div class='text-danger text-sm p-3 bg-danger-bg border border-danger-border rounded-card'>"
            f"Generation failed: {_html.escape(str(exc))}</div></div>"
        )

    ai_badge = (
        '<span class="pm-badge text-accent border-accent-border bg-accent-subtle">AI generated</span>'
        if brief.ai_used
        else '<span class="pm-badge">Stats summary</span>'
    )
    gen_time = brief.generated_at.strftime("%H:%M UTC") if brief.generated_at else ""
    status_html = _render_brief_status(brief)
    links_html = _render_brief_links(brief, account_email)
    content_html = _render_brief_html(brief.content)

    trash_iso = ""
    if brief.digest_trash_after:
        from datetime import timezone as _tz

        ta = brief.digest_trash_after
        if ta.tzinfo is None:
            ta = ta.replace(tzinfo=_tz.utc)
        trash_iso = ta.isoformat()

    # Emit a script to re-init digest countdowns after HTMX swap
    digest_init = (
        f'<script>if(typeof _digestRefreshBadges==="function")_digestRefreshBadges("{trash_iso}");</script>'
        if trash_iso
        else ""
    )

    # Out-of-band swap so the stat cards refresh alongside #brief-content.
    # Must stay a top-level sibling in the response (HTMX OOB requirement) and
    # precede digest_init, whose script reads the freshly swapped DOM.
    stat_cards_oob = templates.env.get_template("_brief_stat_cards.html").render(
        brief=brief, oob=True, auto_generate=False
    )

    return HTMLResponse(
        f'<div id="brief-content" class="px-5 py-5">'
        f'<div class="flex items-center gap-2 mb-4">{ai_badge}'
        f'<span class="text-ink-tertiary text-xs">Generated at {gen_time}</span></div>'
        f"{status_html}{links_html}{content_html}"
        f"</div>"
        f"{stat_cards_oob}"
        f"{digest_init}"
    )


# ── Welcome (smart first-run cleanup summary) ──────────────────────────────────


@app.get("/welcome", response_class=HTMLResponse)
async def welcome(request: Request):
    """First-run summary: scan the synced back-catalogue, build a deterministic
    cleanup plan, optionally narrate it with the LLM, and present one big safe win
    plus a few secondary buckets. The 'Review & clean' buttons deep-link into the
    existing confirm-first purge/archive flow with the bucket's senders preselected.
    """
    ctx = _base()
    ctx["active"] = "dashboard"
    account_email = _get_web_account() or ""

    if not account_email:
        return RedirectResponse("/onboarding", status_code=302)

    def _build():
        from postmind.core.sender_stats import (
            build_cleanup_plan,
            cleanup_plan_digest,
            compute_impact_scores,
            fetch_sender_groups_from_db,
        )
        from postmind.core.storage import AccountRepo, get_session

        # Whole back-catalogue is the point on first run — scope "anywhere".
        groups = fetch_sender_groups_from_db(
            account_email=account_email,
            scope="anywhere",
            min_count=1,
            top_n=1000,
            sort_by="score",
        )
        compute_impact_scores(groups)
        plan = build_cleanup_plan(groups)

        # Cache the groups so the purge preview can resolve the bucket senders.
        _cache_set(groups, {"emailAddress": account_email}, account_email)

        session = get_session()
        try:
            row = AccountRepo(session).get(account_email)
            synced_at = (
                row.last_synced_at.strftime("%d %b %Y") if (row and row.last_synced_at) else None
            )
        finally:
            session.close()
        return plan, cleanup_plan_digest(plan), synced_at

    try:
        loop = asyncio.get_event_loop()
        plan, digest, synced_at = await loop.run_in_executor(_executor, _build)
    except Exception as exc:
        return _resp(request, "error.html", {"error": str(exc)})

    # Optional LLM narration — presentation only. Numbers/senders stay server-side;
    # we apply only the returned title/rationale text by bucket key.
    intro = ""
    if _ai_mode() != "off" and plan.has_opportunity:
        try:
            from postmind.core.ai_engine import AIEngine

            def _narrate():
                engine = AIEngine()
                return engine.summarize_cleanup_plan(digest, plan.total_emails, plan.total_senders)

            narration = await loop.run_in_executor(_executor, _narrate)
            intro = narration.get("intro", "")
            text_by_key = narration.get("buckets", {})
            for bucket in ([plan.headline] if plan.headline else []) + plan.secondary:
                t = text_by_key.get(bucket.key)
                if t:
                    bucket.title = t.get("title", bucket.title)
                    bucket.rationale = t.get("rationale", bucket.rationale)
        except Exception:
            intro = ""  # any AI failure → deterministic plan stands on its own

    # Mark welcomed so subsequent visits to / go to the normal dashboard.
    def _mark():
        from postmind.core.storage import AccountRepo, get_session

        AccountRepo(get_session()).mark_welcomed(account_email)

    try:
        await loop.run_in_executor(_executor, _mark)
    except Exception:
        pass

    ctx.update(
        {
            "plan": plan,
            "intro": intro,
            "synced_at": synced_at,
            "account_email": account_email,
            "undo_days": get_settings().undo_window_days,
        }
    )
    return _resp(request, "welcome.html", ctx)


# ── Cleanup (Smart Batches) ─────────────────────────────────────────────────────


@app.get("/cleanup", response_class=HTMLResponse)
async def cleanup(request: Request):
    """Smart Cleanup Batches: group the synced back-catalogue into a handful of
    high-confidence, semantically-named batches and present them as a fast,
    keyboard-driven approve/skip card stack. Approvals commit through the existing
    confirm-first purge/archive + undo machinery via POST /cleanup/confirm.
    Phase 1 is fully deterministic — no LLM call."""
    ctx = _base()
    ctx["active"] = "cleanup"
    account_email = _get_web_account() or ""

    if not account_email:
        return RedirectResponse("/onboarding", status_code=302)

    def _build():
        from postmind.core.sender_stats import (
            AUTO_SELECT_THRESHOLD,
            build_cleanup_batches,
            compute_impact_scores,
            fetch_sender_groups_from_db,
        )
        from postmind.core.storage import (
            ClassificationCacheRepo,
            CleanupFeedbackRepo,
            get_session,
        )

        groups = fetch_sender_groups_from_db(
            account_email=account_email,
            scope="anywhere",
            min_count=1,
            top_n=1000,
            sort_by="score",
        )
        compute_impact_scores(groups)

        # Overlay cached AI categories (no new LLM call — just a join).
        all_ids = [mid for g in groups for mid in g.message_ids]
        categories = ClassificationCacheRepo(get_session()).get_many(all_ids)

        # Learning loop: per-sender confidence nudge from past decisions
        # (best-effort — a feedback failure must never break the page).
        try:
            priors = CleanupFeedbackRepo(get_session()).sender_priors(account_email)
        except Exception:
            priors = {}

        plan = build_cleanup_batches(groups, categories, sender_priors=priors)

        # Offer to automate batches the user has cleaned across >=3 sessions.
        try:
            session_counts = CleanupFeedbackRepo(get_session()).batch_session_counts(account_email)
        except Exception:
            session_counts = {}
        RULE_OFFER_THRESHOLD = 3
        rule_offer_keys = {
            b.key
            for b in plan.batches
            if b.key in ("promos-unopened", "old-clutter")
            and session_counts.get(b.key, 0) >= RULE_OFFER_THRESHOLD
        }

        # Cache the groups so /cleanup/confirm can resolve the batch senders.
        _cache_set(groups, {"emailAddress": account_email}, account_email)
        return plan, AUTO_SELECT_THRESHOLD, rule_offer_keys

    try:
        loop = asyncio.get_event_loop()
        plan, auto_threshold, rule_offer_keys = await loop.run_in_executor(_executor, _build)
    except Exception as exc:
        return _resp(request, "error.html", {"error": str(exc)})

    # Phase 2 — optional semantic layer. One body-free LLM call gives the batches
    # warmer, subject-aware names; we apply only the returned title/rationale text
    # by batch key. Numbers, senders, actions, and confidence stay server-side, and
    # any failure (AI off, model error, off-target keys) degrades cleanly to the
    # deterministic Phase-1 names.
    if _ai_mode() != "off" and plan.has_opportunity:
        try:
            from postmind.core.ai_engine import AIEngine
            from postmind.core.sender_stats import cleanup_batches_digest

            digest = cleanup_batches_digest(plan)

            def _name():
                return AIEngine().propose_batches(digest)

            named = await loop.run_in_executor(_executor, _name)
            text_by_key = named.get("batches", {})
            for batch in plan.batches:
                t = text_by_key.get(batch.key)
                if t:
                    batch.title = t.get("title", batch.title)
                    batch.rationale = t.get("rationale", batch.rationale)
        except Exception:
            pass  # any AI failure → deterministic Phase-1 batches stand on their own

    ctx.update(
        {
            "plan": plan,
            "account_email": account_email,
            "undo_days": get_settings().undo_window_days,
            "auto_threshold": auto_threshold,
            "rule_offer_keys": rule_offer_keys,
        }
    )
    return _resp(request, "cleanup.html", ctx)


# Batch keys the user can promote into a recurring rule (learning loop). Each
# maps to (rule name, gmail query, action) consumed by RuleRepo.create below.
_BATCH_RULE_TEMPLATES = {
    "promos-unopened": ("Auto-clear old promotions", "category:promotions older_than:90d", "trash"),
    "old-clutter": ("Auto-archive year-old mail", "older_than:365d", "archive"),
}


@app.post("/cleanup/confirm", response_class=HTMLResponse)
async def cleanup_confirm(request: Request):
    """Commit the approved batches. The form posts two multi-value fields —
    ``trash_senders`` and ``archive_senders`` — one entry per approved sender.
    We loop once per distinct action (≤2 undo entries), reusing the exact
    confirm/undo idioms from /purge/confirm."""
    form = await request.form()
    trash_senders = form.getlist("trash_senders")
    archive_senders = form.getlist("archive_senders")
    feedback_raw = form.get("feedback_json", "")
    create_rule_key = form.get("create_rule", "")

    # Parse the learning-loop feedback payload (best-effort — bad JSON or shape
    # must never block the purge). Expect a list of dicts with sender_email /
    # batch_key / action / decision.
    feedback_items: list[dict] = []
    try:
        import json as _json

        parsed = _json.loads(feedback_raw) if feedback_raw else []
        if isinstance(parsed, list):
            feedback_items = [i for i in parsed if isinstance(i, dict)]
    except Exception:
        feedback_items = []

    create_rule_key = create_rule_key if create_rule_key in _BATCH_RULE_TEMPLATES else ""

    # Only short-circuit when there is genuinely nothing to do: no senders to
    # purge, no feedback to record, and no rule to create.
    if not trash_senders and not archive_senders and not feedback_items and not create_rule_key:
        return RedirectResponse("/cleanup", status_code=303)

    from postmind.core.storage import BlocklistRepo
    from postmind.core.storage import get_session as _gs

    cached = _cache_get()
    if cached:
        groups = cached["groups"]
        account_email = cached["account_email"]
    else:
        from postmind.core.sender_stats import fetch_sender_groups_from_db
        from postmind.core.storage import EmailRepo

        account_email = _get_web_account() or ""
        if account_email and EmailRepo(_gs()).get_inbox(account_email, limit=1):
            groups = fetch_sender_groups_from_db(
                account_email=account_email, scope="inbox", min_count=1, top_n=500, sort_by="score"
            )
        else:
            return _resp(
                request, "error.html", {"error": "No inbox data. Please run a Sync first."}
            )

    blocked_set = BlocklistRepo(_gs()).blocked_emails(account_email) if account_email else set()

    plan_actions = [("trash", trash_senders), ("archive", archive_senders)]

    def _do_cleanup():
        from postmind.core.storage import (
            CleanupFeedbackRepo,
            RuleDefinition,
            RuleRepo,
            UndoLogRepo,
            get_session,
        )

        # Record feedback first (best-effort — never blocks the purge/rule).
        if feedback_items:
            try:
                CleanupFeedbackRepo(get_session()).record_many(account_email, feedback_items)
            except Exception:
                pass

        # Promote a repeatedly-cleaned batch into a recurring rule (best-effort).
        if create_rule_key:
            try:
                name, query, rule_action = _BATCH_RULE_TEMPLATES[create_rule_key]
                rule = RuleDefinition(
                    account_email=account_email,
                    name=name,
                    natural_language="Created from the Clean Up page",
                    gmail_query=query,
                    action=rule_action,
                    ai_explanation=f"Auto-created after repeatedly clearing the '{create_rule_key}' batch.",
                )
                RuleRepo(get_session()).create(rule)
            except Exception:
                pass

        client = _build_provider()
        total = 0
        last_undo_id = None
        actions_done = []

        for action, senders in plan_actions:
            if not senders:
                continue
            sender_set = set(senders)
            selected_groups = [
                g
                for g in groups
                if g.sender_email in sender_set and g.sender_email not in blocked_set
            ]
            if not selected_groups:
                continue
            all_ids = [mid for g in selected_groups for mid in g.message_ids]
            if not all_ids:
                continue

            verb = "Archived" if action == "archive" else "Purged"
            entry = UndoLogRepo(get_session()).record(
                account_email=account_email,
                operation=action,
                message_ids=all_ids,
                description=(
                    f"{verb} {len(all_ids)} emails from {len(selected_groups)} sender(s): "
                    + ", ".join(g.sender_email for g in selected_groups[:3])
                    + ("…" if len(selected_groups) > 3 else "")
                ),
                metadata={"senders": [g.sender_email for g in selected_groups]},
            )
            if action == "archive":
                client.batch_archive(all_ids)
            else:
                client.batch_trash(all_ids)

            total += len(all_ids)
            last_undo_id = entry.id
            actions_done.append(action)

        return last_undo_id, total, actions_done

    try:
        loop = asyncio.get_event_loop()
        undo_id, count, actions_done = await loop.run_in_executor(_executor, _do_cleanup)
        _scan_cache.pop(_get_web_account() or "default", None)
    except Exception as exc:
        return _resp(request, "error.html", {"error": str(exc)})

    if not actions_done:
        # No purge happened. If we still recorded feedback or created a rule,
        # that's a successful no-op submit — just return to the page rather than
        # surfacing an error.
        if feedback_items or create_rule_key:
            return RedirectResponse("/cleanup", status_code=303)
        return _resp(
            request,
            "error.html",
            {"error": "Nothing to do — selected senders are protected or no longer in the scan."},
        )

    result_action = actions_done[0] if len(set(actions_done)) == 1 else "mixed"
    return RedirectResponse(
        f"/undo?purged={count}&undo_id={undo_id}&action={result_action}", status_code=303
    )


# ── Stats ─────────────────────────────────────────────────────────────────────


def _parse_age_filter(since: str) -> tuple[int | None, int | None]:
    """Parse the Stats "age" filter value into (newer_than_days, older_than_days).

    ``"30d"`` → newer than 30 days (mail from the last month).
    ``"older:365d"`` → older than 365 days (the stale back-catalogue).
    Empty/unrecognised → (None, None), i.e. no age restriction.
    """
    if not since:
        return None, None
    if since.startswith("older:"):
        val = since[len("older:") :]
        if val.endswith("d") and val[:-1].isdigit():
            return None, int(val[:-1])
        return None, None
    if since.endswith("d") and since[:-1].isdigit():
        return int(since[:-1]), None
    return None, None


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    ctx = _base()
    ctx["active"] = "stats"
    ctx["sort_by"] = request.query_params.get("sort", "score")
    ctx["scope"] = request.query_params.get("scope", "inbox")
    ctx["since"] = request.query_params.get("since", "")
    ctx["promo_only"] = request.query_params.get("promo_only", "")
    return _resp(request, "stats.html", ctx)


@app.get("/stats/data", response_class=HTMLResponse)
async def stats_data(
    request: Request,
    sort: str = "score",
    scope: str = "inbox",
    since: str = "",
    top: int = 100,
    promo_only: str = "",
):
    if not _is_authed():
        return _resp(
            request,
            "stats_error.html",
            {"error": "Not authenticated. Run postmind auth in your terminal first."},
        )

    def _scan():
        from postmind.core.sender_stats import (
            fetch_sender_groups,
            fetch_sender_groups_from_db,
            generate_recommendations,
            group_by_domain,
            reclaimable_mb,
        )
        from postmind.core.storage import BlocklistRepo, EmailRepo, get_session

        account_email = _get_web_account() or ""
        valid_sort = sort if sort in ("score", "count", "size", "oldest") else "score"
        newer_days, older_days = _parse_age_filter(since)

        data_source = "Gmail API"
        total_emails_in_scope = 0

        session = get_session()
        try:
            has_local_data = bool(
                account_email and EmailRepo(session).get_inbox(account_email, limit=1)
            )

            if has_local_data:
                # Prefer the local cache: it holds the full synced history, so age
                # filters (esp. "older than") cover the whole back-catalogue rather
                # than just the most recent ~1000 messages the API path samples.
                db_scope = "inbox" if scope != "anywhere" else "anywhere"
                groups = fetch_sender_groups_from_db(
                    account_email=account_email,
                    scope=db_scope,
                    min_count=1,
                    top_n=top,
                    sort_by=valid_sort,
                    newer_than_days=newer_days,
                    older_than_days=older_days,
                    promo_only=bool(promo_only),
                )
                profile = {"emailAddress": account_email}
                data_source = "local cache"
                import time as _time

                from postmind.core.storage import EmailRecord

                db_q = session.query(EmailRecord).filter(EmailRecord.account_email == account_email)
                if db_scope == "inbox":
                    db_q = db_q.filter(EmailRecord.is_inbox.is_(True))
                _now_ms = int(_time.time() * 1000)
                if newer_days:
                    db_q = db_q.filter(
                        EmailRecord.internal_date >= _now_ms - newer_days * 86_400_000
                    )
                if older_days:
                    db_q = db_q.filter(
                        EmailRecord.internal_date <= _now_ms - older_days * 86_400_000
                    )
                total_emails_in_scope = db_q.count()
            else:
                client = _build_provider()
                profile = client.get_profile()
                account_email = profile.get("emailAddress", "")
                query = "in:anywhere -in:trash -in:spam" if scope == "anywhere" else "in:inbox"
                if newer_days:
                    query += f" newer_than:{newer_days}d"
                if older_days:
                    query += f" older_than:{older_days}d"
                groups = fetch_sender_groups(
                    client,
                    query=query,
                    max_messages=1000,
                    min_count=1,
                    top_n=top,
                    sort_by=valid_sort,
                )
                total_emails_in_scope = sum(g.count for g in groups)

            blocked = BlocklistRepo(session).blocked_emails(account_email)
        finally:
            session.close()

        if blocked:
            groups = [g for g in groups if g.sender_email not in blocked]

        _cache_set(groups, profile, account_email)

        domain_groups = group_by_domain(groups)
        domain_map = {d.domain: d for d in domain_groups}
        recs = generate_recommendations(groups, top_n=5, domain_map=domain_map)
        total_reclaimable = reclaimable_mb(recs)

        # Date range from groups
        all_dates = [g.earliest_date for g in groups if g.earliest_date]
        date_from = min(all_dates).strftime("%d %b %Y") if all_dates else ""
        latest_dates = [g.latest_date for g in groups if g.latest_date]
        date_to = max(latest_dates).strftime("%d %b %Y") if latest_dates else ""

        return {
            "senders": _enrich_groups(groups),
            "total_reclaimable": total_reclaimable,
            "account_email": account_email,
            "total_scanned": sum(g.count for g in groups),
            "total_emails": total_emails_in_scope,
            "date_from": date_from,
            "date_to": date_to,
            "data_source": data_source,
            "scanned_at": datetime.now(timezone.utc).strftime("%H:%M"),
        }

    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(_executor, _scan)
    except Exception as exc:
        return _resp(request, "stats_error.html", {"error": str(exc)})

    return _resp(request, "stats_table.html", data)


# ── Purge ─────────────────────────────────────────────────────────────────────


def _render_purge_preview(
    request: Request, senders: list[str], action: str = "trash"
) -> HTMLResponse:
    """Render the confirm-first preview for the given senders from the current
    scan cache. Shared by the POST form flow and the GET deep-link the chat
    assistant produces. ``action`` is "trash" (move to Trash) or "archive"
    (remove from inbox); both still require the explicit confirm button."""
    if not senders:
        return RedirectResponse("/stats", status_code=303)
    if action not in ("trash", "archive"):
        action = "trash"

    cached = _cache_get()
    if cached:
        groups = cached["groups"]
    else:
        # Cache expired or was never populated (common when request comes from the
        # Super Agent without a prior Stats scan). Fall back to the local DB — same
        # data, just fetched on-demand rather than from the in-memory cache.
        from postmind.core.sender_stats import fetch_sender_groups_from_db
        from postmind.core.storage import EmailRepo, get_session

        account_email = _get_web_account() or ""
        if account_email and EmailRepo(get_session()).get_inbox(account_email, limit=1):
            groups = fetch_sender_groups_from_db(
                account_email=account_email,
                scope="inbox",
                min_count=1,
                top_n=500,
                sort_by="score",
            )
        else:
            return _resp(
                request,
                "error.html",
                {"error": "No inbox data found. Please run a Sync first."},
            )

    sender_set = set(senders)
    selected_groups = [g for g in groups if g.sender_email in sender_set]
    if not selected_groups:
        return _resp(
            request,
            "error.html",
            {
                "error": "None of those senders were found in your inbox data. Please run a Sync and try again."
            },
        )
    total_count = sum(g.count for g in selected_groups)
    total_mb = round(sum(g.total_size_bytes for g in selected_groups) / (1024 * 1024), 1)

    ctx = _base()
    ctx.update(
        {
            "active": "stats",
            "selected": _enrich_groups(selected_groups),
            "senders": [g.sender_email for g in selected_groups],
            "total_count": total_count,
            "total_mb": total_mb,
            "action": action,
            "undo_days": get_settings().undo_window_days,
        }
    )
    return _resp(request, "purge_preview.html", ctx)


@app.post("/purge/preview", response_class=HTMLResponse)
async def purge_preview(request: Request):
    form = await request.form()
    return _render_purge_preview(request, form.getlist("senders"), form.get("action", "trash"))


@app.get("/purge/preview", response_class=HTMLResponse)
async def purge_preview_get(request: Request):
    """Deep-link entrypoint (e.g. from the chat assistant): renders the same
    confirm-first preview. Read-only — nothing happens until the user confirms."""
    return _render_purge_preview(
        request,
        request.query_params.getlist("senders"),
        request.query_params.get("action", "trash"),
    )


@app.post("/purge/confirm", response_class=HTMLResponse)
async def purge_confirm(request: Request):
    form = await request.form()
    senders = form.getlist("senders")
    action = form.get("action", "trash")
    if action not in ("trash", "archive"):
        action = "trash"

    if not senders:
        return RedirectResponse("/stats", status_code=303)

    from postmind.core.storage import BlocklistRepo
    from postmind.core.storage import get_session as _gs

    cached = _cache_get()
    if cached:
        groups = cached["groups"]
        account_email = cached["account_email"]
    else:
        # Cache expired — rebuild from DB (same fix as purge preview)
        from postmind.core.sender_stats import fetch_sender_groups_from_db
        from postmind.core.storage import EmailRepo

        account_email = _get_web_account() or ""
        if account_email and EmailRepo(_gs()).get_inbox(account_email, limit=1):
            groups = fetch_sender_groups_from_db(
                account_email=account_email,
                scope="inbox",
                min_count=1,
                top_n=500,
                sort_by="score",
            )
        else:
            return _resp(
                request, "error.html", {"error": "No inbox data. Please run a Sync first."}
            )
    # Enforce protected senders at confirm time (not just at stage time): a sender
    # blocked after the cache was populated must never be touched.
    blocked_set = BlocklistRepo(_gs()).blocked_emails(account_email) if account_email else set()
    selected_groups = [
        g for g in groups if g.sender_email in senders and g.sender_email not in blocked_set
    ]

    if not selected_groups:
        return _resp(
            request,
            "error.html",
            {"error": "Nothing to do — selected senders are protected or no longer in the scan."},
        )

    def _do_purge():
        from postmind.core.storage import EmailRepo, UndoLogRepo, get_session

        client = _build_provider()
        all_ids = [mid for g in selected_groups for mid in g.message_ids]

        verb = "Archived" if action == "archive" else "Purged"
        # Record undo BEFORE the operation so a crash/partial failure still leaves
        # a reversible log entry (matches BulkEngine.execute ordering). The undo
        # path keys off `operation` — "archive" restores INBOX, "trash" untrashes.
        session = get_session()
        entry = UndoLogRepo(session).record(
            account_email=account_email,
            operation=action,
            message_ids=all_ids,
            description=(
                f"{verb} {len(all_ids)} emails from {len(selected_groups)} sender(s): "
                + ", ".join(g.sender_email for g in selected_groups[:3])
                + ("…" if len(selected_groups) > 3 else "")
            ),
            metadata={"senders": [g.sender_email for g in selected_groups]},
        )
        if action == "archive":
            client.batch_archive(all_ids)
        else:
            client.batch_trash(all_ids)
        # Keep local DB in sync so subsequent stats scans don't re-include these.
        EmailRepo(session).mark_trashed(all_ids)
        return entry.id, len(all_ids)

    try:
        loop = asyncio.get_event_loop()
        undo_id, count = await loop.run_in_executor(_executor, _do_purge)
        _scan_cache.pop(_get_web_account() or "default", None)
    except Exception as exc:
        return _resp(request, "error.html", {"error": str(exc)})

    return RedirectResponse(
        f"/undo?purged={count}&undo_id={undo_id}&action={action}", status_code=303
    )


# ── Undo ─────────────────────────────────────────────────────────────────────


@app.get("/undo", response_class=HTMLResponse)
async def undo_page(request: Request):
    purged = request.query_params.get("purged")
    restored = request.query_params.get("restored")
    undo_id = request.query_params.get("undo_id")
    purged_action = request.query_params.get("action", "trash")

    def _get_entries():
        from postmind.core.storage import UndoLogRepo, get_session

        client = _build_provider()
        account_email = client.get_email_address()
        return UndoLogRepo(get_session()).list_recent(account_email), account_email

    try:
        loop = asyncio.get_event_loop()
        entries, account_email = await loop.run_in_executor(_executor, _get_entries)
    except Exception as exc:
        return _resp(request, "error.html", {"error": str(exc)})

    now = datetime.now(timezone.utc)
    rows = []
    for e in entries:
        expires_at = (
            e.expires_at.replace(tzinfo=timezone.utc)
            if e.expires_at.tzinfo is None
            else e.expires_at
        )
        executed_at = (
            e.executed_at.replace(tzinfo=timezone.utc)
            if e.executed_at.tzinfo is None
            else e.executed_at
        )
        rows.append(
            {
                "id": e.id,
                "operation": e.operation,
                "description": e.description,
                "count": len(e.message_ids),
                "executed_at": executed_at.strftime("%b %d, %Y %H:%M"),
                "expires_in": max(0, (expires_at - now).days),
                "senders": e.op_metadata.get("senders", []),
            }
        )

    ctx = _base()
    ctx.update(
        {
            "active": "undo",
            "entries": rows,
            "account_email": account_email,
            "purged": purged,
            "purged_action": purged_action,
            "restored": restored,
            "undo_id": undo_id,
            "undo_days": get_settings().undo_window_days,
        }
    )
    return _resp(request, "undo.html", ctx)


@app.post("/undo/{entry_id}", response_class=HTMLResponse)
async def undo_restore(request: Request, entry_id: int):
    def _do_undo():
        from postmind.core.bulk_engine import BulkEngine
        from postmind.core.storage import UndoLogRepo, get_session

        # Security check: ensure the undo entry belongs to the current account
        entry = UndoLogRepo(get_session()).get(entry_id)
        if entry and hasattr(entry, "account_email"):
            current_acct = _get_web_account()
            if current_acct and entry.account_email and entry.account_email != current_acct:
                raise HTTPException(status_code=404, detail="Operation not found")

        client = _build_provider()
        account_email = client.get_email_address()
        engine = BulkEngine(client, account_email)
        return engine.undo(entry_id)

    try:
        loop = asyncio.get_event_loop()
        count = await loop.run_in_executor(_executor, _do_undo)
    except HTTPException:
        raise
    except Exception as exc:
        return _resp(request, "error.html", {"error": str(exc)})

    return RedirectResponse(f"/undo?restored={count}", status_code=303)


# ── Settings ──────────────────────────────────────────────────────────────────


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    ctx = _base()
    ctx["active"] = "settings"
    ctx["success"] = request.query_params.get("success")

    try:
        from postmind.config import load_account_config

        s = get_settings()
        email = _get_web_account()
        acct_cfg = load_account_config(email) if email else {}
        ctx.update(
            {
                "ai_mode": s.ai_mode,
                "provider": acct_cfg.get("provider", s.provider),
                "imap_server": acct_cfg.get("imap_server", s.imap_server),
                "imap_user": acct_cfg.get("imap_user", s.imap_user),
                "undo_days": s.undo_window_days,
                "has_api_key": bool(s.anthropic_api_key),
                "cloud_provider": s.cloud_provider,
                "ollama_base_url": s.ollama_base_url,
                "ollama_model": s.ollama_model,
                "has_ollama_key": bool(s.ollama_api_key),
                "chat_ai_mode": s.chat_ai_mode,  # "" = inherit
                "chat_cloud_model": s.chat_cloud_model or s.ai_model,
                "chat_ollama_model": s.chat_ollama_model or s.ollama_model,
                "agent_autopilot": s.agent_autopilot,
                "agent_power_mode": s.agent_power_mode,
                "deep_task_mode": s.deep_task_mode,
                "deep_task_model": s.deep_task_model,
                "extended_thinking": s.extended_thinking,
                "thinking_budget_tokens": s.thinking_budget_tokens,
                "auto_start_daemon": s.auto_start_daemon,
                "daemon_interval_minutes": s.daemon_interval_minutes,
                "auto_sync_on_first_run": s.auto_sync_on_first_run,
                "periodic_sync_hours": s.periodic_sync_hours,
            }
        )
    except Exception:
        ctx.update(
            {
                "ai_mode": "off",
                "provider": "gmail",
                "imap_server": "",
                "imap_user": "",
                "undo_days": 30,
                "has_api_key": False,
                "has_ollama_key": False,
                "cloud_provider": "anthropic",
                "ollama_base_url": "http://localhost:11434",
                "ollama_model": "llama3.2",
                "chat_ai_mode": "",
                "chat_cloud_model": "claude-sonnet-4-6",
                "chat_ollama_model": "qwen2.5:32b",
                "agent_autopilot": False,
                "agent_power_mode": False,
                "deep_task_mode": "cloud",
                "deep_task_model": "",
                "extended_thinking": False,
                "thinking_budget_tokens": 8000,
                "auto_start_daemon": True,
                "daemon_interval_minutes": 30,
                "auto_sync_on_first_run": True,
                "periodic_sync_hours": 6,
            }
        )

    total = sum(f.stat().st_size for f in DATA_DIR.rglob("*") if f.is_file())
    ctx.update(
        {
            "data_dir": str(DATA_DIR),
            "credentials_exist": CREDENTIALS_PATH.exists(),
            "token_exists": _is_authed(),
            "data_size_mb": round(total / (1024 * 1024), 1),
        }
    )
    return _resp(request, "settings.html", ctx)


@app.get("/settings/mcp-servers")
async def settings_mcp_servers_get(request: Request):
    """Return the MCP servers config for the active account as JSON."""
    from postmind.config import load_account_config

    email = _get_web_account() or ""
    cfg = load_account_config(email) if email else {}
    servers = cfg.get("mcp_servers") or []
    pool = _mcp_pools.get(email)
    status = pool.status() if pool else []
    status_map = {s["name"]: s for s in status}
    enriched = []
    for s in servers:
        name = s.get("name", "")
        enriched.append(
            {
                **s,
                "connected": status_map.get(name, {}).get("connected", False),
                "tool_count": status_map.get(name, {}).get("tool_count", 0),
            }
        )
    return {"servers": enriched}


@app.post("/settings/mcp-servers")
async def settings_mcp_servers_post(request: Request):
    """Save (replace) the full mcp_servers list for the active account."""
    from postmind.config import load_account_config, save_account_config

    email = _get_web_account() or ""
    if not email:
        return JSONResponse({"error": "No active account."}, status_code=400)
    try:
        body = await request.json()
        servers = body.get("servers") or []
    except Exception:
        return JSONResponse({"error": "Invalid JSON."}, status_code=400)
    for s in servers:
        if not s.get("name"):
            return JSONResponse({"error": "Each server must have a 'name'."}, status_code=400)
        if not s.get("command") and not s.get("url"):
            return JSONResponse(
                {"error": f"Server '{s['name']}' needs 'command' or 'url'."}, status_code=400
            )
    cfg = load_account_config(email)
    cfg["mcp_servers"] = servers
    save_account_config(email, cfg)
    _mcp_pools.pop(email, None)
    return {"ok": True, "count": len(servers)}


@app.post("/settings/mcp-servers/test")
async def settings_mcp_servers_test(request: Request):
    """Test-connect a single MCP server config and return its tool list."""
    from postmind.core.mcp_client import MCPClientSession, _parse_config

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON."}, status_code=400)
    cfg = _parse_config(body)
    if not cfg.name:
        return JSONResponse({"error": "Missing 'name'."}, status_code=400)
    if not cfg.command and not cfg.url:
        return JSONResponse({"error": "Need 'command' or 'url'."}, status_code=400)
    sess = MCPClientSession(cfg)
    await sess.connect()
    if not sess.connected:
        return {
            "connected": False,
            "tools": [],
            "error": "Could not connect — check the command/URL and try again.",
        }
    tools = [t["name"].removeprefix(f"mcp_{cfg.name}_") for t in sess.tools]
    await sess.close()
    return {"connected": True, "tool_count": len(tools), "tools": tools[:20]}


_MCP_TEMPLATES = [
    {
        "id": "memory",
        "name": "Memory",
        "icon": "brain",
        "tagline": "Persistent contact knowledge across sessions",
        "description": "Remembers facts about senders so the agent personalizes replies and recalls context automatically. Local file — nothing leaves your machine.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "env_vars": [],
        "setup_steps": [
            "Requires Node.js (npx). Run: npx -y @modelcontextprotocol/server-memory to verify."
        ],
        "free": True,
        "requires_auth": False,
    },
    {
        "id": "google-calendar",
        "name": "Google Calendar",
        "icon": "calendar",
        "tagline": "Email → calendar events, free/busy lookup",
        "description": "Create events from meeting-request emails, check availability before scheduling, draft confirmation replies.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@cocal/google-calendar-mcp"],
        "env_vars": ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI"],
        "setup_steps": [
            "Create a Google OAuth app at console.cloud.google.com with Calendar API enabled.",
            "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET as environment variables.",
            "Run the server once to complete OAuth: npx -y @cocal/google-calendar-mcp",
        ],
        "free": True,
        "requires_auth": True,
    },
    {
        "id": "linear",
        "name": "Linear",
        "icon": "layers",
        "tagline": "Turn emails into Linear issues in one command",
        "description": "Create issues, search projects, update statuses directly from email context. Uses Linear's official OAuth — no API key needed.",
        "transport": "http",
        "url": "https://mcp.linear.app/mcp",
        "env_vars": ["LINEAR_ACCESS_TOKEN"],
        "setup_steps": [
            "Go to linear.app → Settings → API → Personal API Keys.",
            "Create a key and set it as LINEAR_ACCESS_TOKEN in your environment.",
        ],
        "free": True,
        "requires_auth": True,
    },
    {
        "id": "brave-search",
        "name": "Brave Search",
        "icon": "search",
        "tagline": "Research senders and topics before replying",
        "description": "Web, news, and image search. Agent uses it to research unfamiliar senders, verify claims, and pull live context before drafting replies. 2,000 free queries/month.",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@brave/brave-search-mcp-server"],
        "env_vars": ["BRAVE_API_KEY"],
        "setup_steps": [
            "Get a free API key at brave.com/search/api (2,000 queries/month free).",
            "Set BRAVE_API_KEY as an environment variable.",
        ],
        "free": True,
        "requires_auth": True,
    },
    {
        "id": "hubspot",
        "name": "HubSpot",
        "icon": "users",
        "tagline": "Email → CRM contacts, deals, and activity notes",
        "description": "Look up senders in HubSpot, create contacts, log email activity, update deal stages — all from the agent.",
        "transport": "http",
        "url": "https://mcp.hubspot.com",
        "env_vars": ["HUBSPOT_ACCESS_TOKEN"],
        "setup_steps": [
            "Go to HubSpot → Settings → Integrations → Private Apps.",
            "Create a private app with CRM read/write scopes.",
            "Copy the access token and set it as HUBSPOT_ACCESS_TOKEN.",
        ],
        "free": False,
        "requires_auth": True,
    },
    {
        "id": "slack",
        "name": "Slack",
        "icon": "message-square",
        "tagline": "Post summaries, search threads, send DM alerts",
        "description": "Summarize email threads and post to Slack channels. Search Slack for context. Send DM alerts for high-priority emails.",
        "transport": "http",
        "url": "https://slack.com/api/mcp",
        "env_vars": ["SLACK_USER_TOKEN"],
        "setup_steps": [
            "Create a Slack app at api.slack.com/apps with scopes: channels:read, chat:write, search:read.",
            "Install to your workspace and copy the user OAuth token.",
            "Set it as SLACK_USER_TOKEN.",
        ],
        "free": True,
        "requires_auth": True,
    },
]


@app.get("/settings/mcp-templates")
async def mcp_templates(request: Request):
    """Return the curated list of MCP server templates."""
    from postmind.config import load_account_config

    email = _get_web_account() or ""
    configured_names = set()
    if email:
        cfg = load_account_config(email)
        configured_names = {s.get("name") for s in (cfg.get("mcp_servers") or [])}
    return {"templates": [{**t, "configured": t["id"] in configured_names} for t in _MCP_TEMPLATES]}


@app.post("/settings/mcp-servers/from-template/{template_id}")
async def mcp_add_from_template(template_id: str, request: Request):
    """Add a server from a template. For stdio servers, adds the config directly.
    For HTTP servers, requires the user to have set env vars first."""
    from postmind.config import load_account_config, save_account_config

    email = _get_web_account() or ""
    if not email:
        return JSONResponse({"error": "No active account."}, status_code=400)

    tmpl = next((t for t in _MCP_TEMPLATES if t["id"] == template_id), None)
    if not tmpl:
        return JSONResponse({"error": f"Unknown template '{template_id}'."}, status_code=404)

    cfg = load_account_config(email)
    servers = cfg.get("mcp_servers") or []
    if any(s.get("name") == tmpl["id"] for s in servers):
        return {"ok": True, "already_configured": True}

    # Build the server entry
    entry: dict = {"name": tmpl["id"]}
    if tmpl["transport"] == "stdio":
        entry["command"] = tmpl["command"]
        entry["args"] = tmpl["args"]
        # Pass env vars from the process environment if they're set
        import os

        env = {k: os.environ.get(k, "") for k in tmpl.get("env_vars", [])}
        env = {k: v for k, v in env.items() if v}  # drop unset vars
        if env:
            entry["env"] = env
    else:
        entry["url"] = tmpl["url"]

    servers.append(entry)
    cfg["mcp_servers"] = servers
    save_account_config(email, cfg)
    _mcp_pools.pop(email, None)  # invalidate pool so it reconnects
    return {"ok": True, "added": entry}


@app.post("/settings/clear-data")
async def settings_clear_data(request: Request):
    import shutil

    form = await request.form()
    keep_auth = form.get("keep_auth") == "1"

    _auth_names = {"credentials.json", "token.json", "tokens", "accounts", "active_account"}

    for item in list(DATA_DIR.iterdir()):
        if keep_auth and item.name in _auth_names:
            continue
        try:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        except Exception:
            pass

    _scan_cache.clear()
    return RedirectResponse("/settings?success=clear_data", status_code=303)


@app.post("/accounts/switch")
async def web_switch_account(request: Request):
    global _active_web_account
    form = await request.form()
    email = (form.get("email") or "").strip()
    from postmind.core.account_registry import list_accounts

    if email and any(a.email == email for a in list_accounts()):
        _active_web_account = email
        _scan_cache.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request):
    from postmind.config import token_path_for
    from postmind.core.account_registry import list_accounts
    from postmind.core.storage import AccountRepo, get_session

    _acct_session = get_session()
    _acct_repo = AccountRepo(_acct_session)
    # Repair any account whose timestamp was lost to an interrupted big sync,
    # so the page reports the truth instead of "Never".
    for a in list_accounts():
        try:
            _acct_repo.backfill_last_synced(a.email)
        except Exception:
            pass
    all_db = {r.email: r for r in _acct_repo.list_all()}
    accounts_detail = []
    for a in list_accounts():
        db_row = all_db.get(a.email)
        token_ok = token_path_for(a.email).exists() if a.provider == "gmail" else True
        last_sync = db_row.last_synced_at if db_row else None
        accounts_detail.append(
            {
                "email": a.email,
                "display_name": a.display_name,
                "provider": a.provider,
                "token_ok": token_ok,
                "last_synced": last_sync.strftime("%b %d, %H:%M") if last_sync else "Never",
                "imap_server": a.imap_server,
                "is_active_web": a.email == _get_web_account(),
            }
        )
    ctx = _base()
    ctx.update(
        {
            "active": "accounts",
            "accounts_detail": accounts_detail,
            "added": request.query_params.get("added"),
            "removed": request.query_params.get("removed"),
        }
    )
    return _resp(request, "accounts.html", ctx)


@app.post("/accounts/remove")
async def accounts_remove(request: Request):
    global _active_web_account
    form = await request.form()
    email = (form.get("email") or "").strip()
    if not email:
        raise HTTPException(status_code=400)
    from postmind.config import (
        ACTIVE_ACCOUNT_PATH,
        get_active_account,
        set_active_account,
        token_path_for,
    )
    from postmind.core.account_registry import list_accounts
    from postmind.core.storage import AccountRepo, get_session

    token = token_path_for(email)
    if token.exists():
        token.unlink()
    AccountRepo(get_session()).deactivate(email)
    if _active_web_account == email:
        _active_web_account = None
    _scan_cache.pop(email, None)
    # Repoint the persisted active-account pointer if it referenced the removed
    # account, otherwise the top bar keeps showing a dangling/removed account.
    if get_active_account() == email:
        remaining = list_accounts()  # already filtered to active accounts
        if remaining:
            set_active_account(remaining[0].email)
        else:
            ACTIVE_ACCOUNT_PATH.unlink(missing_ok=True)
    return RedirectResponse("/accounts?removed=1", status_code=303)


@app.get("/accounts/add", response_class=HTMLResponse)
async def accounts_add_page(request: Request):
    ctx = _base()
    ctx["active"] = "accounts"
    ctx["tab"] = request.query_params.get("tab", "gmail")
    ctx["has_credentials"] = CREDENTIALS_PATH.exists()
    return _resp(request, "accounts_add.html", ctx)


@app.post("/accounts/add/gmail/start", response_class=HTMLResponse)
async def gmail_add_start(request: Request):
    if not CREDENTIALS_PATH.exists():
        return HTMLResponse(
            '<p class="text-red-600 text-sm">credentials.json not found in ~/.postmind/. Download it from Google Cloud Console first.</p>'
        )
    task_id = uuid.uuid4().hex[:10]
    _oauth_tasks[task_id] = {"status": "running", "email": None, "error": None}

    def _run_oauth():
        state = _oauth_tasks[task_id]
        try:
            import shutil

            from postmind.config import TOKENS_DIR, set_active_account, token_path_for
            from postmind.core.account_registry import register_gmail
            from postmind.core.gmail_client import authenticate

            tmp = TOKENS_DIR / f"_tmp_{task_id}.json"
            creds = authenticate(token_path=tmp)
            from googleapiclient.discovery import build

            svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
            profile = svc.users().getProfile(userId="me").execute()
            email = profile["emailAddress"]
            dest = token_path_for(email)
            shutil.move(str(tmp), str(dest))
            dest.chmod(0o600)
            register_gmail(email)
            set_active_account(email)
            state["status"] = "done"
            state["email"] = email
            try:
                from postmind.core.mcp_client import bootstrap_memory_for_account

                bootstrap_memory_for_account(email)
            except Exception:
                pass
        except Exception as exc:
            state["status"] = "error"
            state["error"] = str(exc)

    _executor.submit(_run_oauth)
    return HTMLResponse(f"""<div id="oauth-status"
     hx-get="/accounts/add/gmail/poll/{task_id}"
     hx-trigger="every 2s" hx-target="this" hx-swap="outerHTML">
  <p class="text-slate-500 text-sm animate-pulse">Opening browser for Google sign-in…</p>
</div>""")


@app.get("/accounts/add/gmail/poll/{task_id}", response_class=HTMLResponse)
async def gmail_add_poll(task_id: str):
    state = _oauth_tasks.get(task_id, {"status": "error", "error": "Task expired"})
    if state["status"] == "done":
        _scan_cache.clear()
        resp = HTMLResponse("")
        resp.headers["HX-Redirect"] = "/accounts?added=1"
        return resp
    if state["status"] == "error":
        return HTMLResponse(f"""<div class="bg-red-50 border border-red-200 rounded-xl p-4">
  <p class="text-red-800 text-sm font-medium">Authentication failed</p>
  <p class="text-red-600 text-xs mt-1">{state["error"]}</p>
</div>""")
    return HTMLResponse(f"""<div id="oauth-status"
     hx-get="/accounts/add/gmail/poll/{task_id}"
     hx-trigger="every 2s" hx-target="this" hx-swap="outerHTML">
  <p class="text-slate-500 text-sm animate-pulse">Waiting for browser sign-in&hellip;</p>
</div>""")


def _test_and_register_imap(server, user, password, port, folder, display_name):
    """Live-test an IMAP connection then register it as the active account.

    Password is used only for the connection test and is never written to disk
    (consistent with the existing /accounts/add/imap behavior).
    """
    from postmind.config import set_active_account
    from postmind.core.account_registry import register_imap
    from postmind.core.providers.factory import get_provider

    provider = get_provider(
        "imap",
        imap_server=server,
        imap_user=user,
        imap_password=password,
        imap_port=port,
        imap_folder=folder,
    )
    provider.get_profile()
    register_imap(user, server, user, port, folder, display_name or user)
    set_active_account(user)
    try:
        from postmind.core.mcp_client import bootstrap_memory_for_account

        bootstrap_memory_for_account(user)
    except Exception:
        pass


@app.post("/accounts/add/imap", response_class=HTMLResponse)
async def imap_add(request: Request):
    form = await request.form()
    server = (form.get("imap_server") or "").strip()
    user = (form.get("imap_user") or "").strip()
    password = (form.get("imap_password") or "").strip()
    port = int(form.get("imap_port") or "993")
    folder = (form.get("imap_folder") or "INBOX").strip() or "INBOX"
    display_name = (form.get("display_name") or "").strip()
    if not server or not user or not password:
        ctx = _base()
        ctx.update(
            {
                "active": "accounts",
                "tab": "imap",
                "has_credentials": CREDENTIALS_PATH.exists(),
                "error": "Server, username, and password are required.",
            }
        )
        return _resp(request, "accounts_add.html", ctx)

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            _executor, _test_and_register_imap, server, user, password, port, folder, display_name
        )
    except Exception as exc:
        ctx = _base()
        ctx.update(
            {
                "active": "accounts",
                "tab": "imap",
                "has_credentials": CREDENTIALS_PATH.exists(),
                "error": str(exc),
            }
        )
        return _resp(request, "accounts_add.html", ctx)
    return RedirectResponse("/accounts?added=1", status_code=303)


# ── Onboarding ────────────────────────────────────────────────────────────────


@app.get("/onboarding", response_class=HTMLResponse)
async def onboarding(request: Request):
    from postmind.core.account_registry import list_accounts

    step = int(request.query_params.get("step", "1"))
    tab = request.query_params.get("tab", "gmail")
    if tab not in ("gmail", "imap"):
        tab = "gmail"
    s = get_settings()
    ctx = _base()
    ctx.update(
        {
            "step": step,
            "tab": tab,
            "has_credentials": CREDENTIALS_PATH.exists(),
            "has_accounts": len(list_accounts()) > 0,
            "ai_mode": s.ai_mode,
            "ollama_base_url": s.ollama_base_url,
            "ollama_model": s.ollama_model,
            "has_api_key": bool(s.anthropic_api_key),
        }
    )
    return _resp(request, "onboarding.html", ctx)


@app.post("/onboarding/connect/imap", response_class=HTMLResponse)
async def onboarding_connect_imap(request: Request):
    """Register an IMAP account during onboarding, reusing the shared
    test-and-register logic, then advance the wizard to the AI step."""
    form = await request.form()
    server = (form.get("imap_server") or "").strip()
    user = (form.get("imap_user") or "").strip()
    password = (form.get("imap_password") or "").strip()
    port = int(form.get("imap_port") or "993")
    folder = (form.get("imap_folder") or "INBOX").strip() or "INBOX"
    display_name = (form.get("display_name") or "").strip()

    def _render_error(msg: str) -> HTMLResponse:
        from postmind.core.account_registry import list_accounts

        s = get_settings()
        ctx = _base()
        ctx.update(
            {
                "step": 1,
                "tab": "imap",
                "has_credentials": CREDENTIALS_PATH.exists(),
                "has_accounts": len(list_accounts()) > 0,
                "ai_mode": s.ai_mode,
                "ollama_base_url": s.ollama_base_url,
                "ollama_model": s.ollama_model,
                "has_api_key": bool(s.anthropic_api_key),
                "imap_error": msg,
            }
        )
        return _resp(request, "onboarding.html", ctx)

    if not server or not user or not password:
        return _render_error("Server, username, and password are required.")

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            _executor, _test_and_register_imap, server, user, password, port, folder, display_name
        )
    except Exception as exc:
        return _render_error(str(exc))

    _scan_cache.clear()
    return RedirectResponse("/onboarding?step=2", status_code=303)


@app.post("/onboarding/ai-mode")
async def onboarding_ai_mode(request: Request):
    """Persist the chosen AI mode during onboarding (same mechanism as
    /settings/ai-mode), then advance to the final step."""
    form = await request.form()
    _persist_ai_mode(form)
    return RedirectResponse("/onboarding?step=3", status_code=303)


@app.post("/onboarding/upload-credentials", response_class=HTMLResponse)
async def upload_credentials(request: Request):
    form = await request.form()
    file = form.get("credentials_file")
    if not file or not hasattr(file, "read"):
        return RedirectResponse("/onboarding?step=1&error=no_file", status_code=303)
    content = await file.read()
    import json as _json

    try:
        data = _json.loads(content)
        if "web" not in data and "installed" not in data:
            raise ValueError("Not a valid OAuth client secret file")
    except Exception:
        return RedirectResponse("/onboarding?step=1&error=invalid", status_code=303)
    CREDENTIALS_PATH.write_bytes(content)
    CREDENTIALS_PATH.chmod(0o600)
    return RedirectResponse("/onboarding?step=2", status_code=303)


# ── Watch daemon ──────────────────────────────────────────────────────────────


import threading as _threading

_watch_thread: "_threading.Thread | None" = None
_watch_stop_event = _threading.Event()
_watch_interval: int = 30


@app.get("/watch", response_class=HTMLResponse)
async def watch_page(request: Request):
    from postmind.core.storage import AgentRepo, get_session

    ctx = _base()
    ctx["active"] = "watch"
    ctx["is_running"] = bool(_watch_thread and _watch_thread.is_alive())
    ctx["interval"] = _watch_interval
    ctx["started"] = request.query_params.get("started") == "1"
    ctx["stopped"] = request.query_params.get("stopped") == "1"
    ctx["agents"] = [
        {
            "email": a.account_email,
            "name": a.name,
            "interval": a.interval_minutes,
            "is_active": a.is_active,
            "status": a.status,
            "last_run": a.last_run_at.strftime("%H:%M") if a.last_run_at else "never",
            "last_found": a.last_found_count,
        }
        for a in AgentRepo(get_session()).list_all()
    ]
    return _resp(request, "watch.html", ctx)


@app.post("/watch/start")
async def watch_start(request: Request):
    global _watch_thread, _watch_interval
    form = await request.form()
    _watch_interval = max(1, int(form.get("interval") or "30"))
    if _watch_thread and _watch_thread.is_alive():
        return RedirectResponse("/watch", status_code=303)
    _watch_stop_event.clear()

    def _run():
        try:
            from postmind.core.daemon import start_daemon_background

            start_daemon_background(stop_event=_watch_stop_event)
        except Exception:
            pass

    _watch_thread = _threading.Thread(target=_run, daemon=True, name="postmind-watch")
    _watch_thread.start()
    return RedirectResponse("/watch?started=1", status_code=303)


@app.post("/watch/stop")
async def watch_stop(request: Request):
    global _watch_thread
    _watch_stop_event.set()
    if _watch_thread:
        _watch_thread.join(timeout=5)
        _watch_thread = None
    return RedirectResponse("/watch?stopped=1", status_code=303)


@app.get("/watch/status", response_class=HTMLResponse)
async def watch_status(request: Request):
    from postmind.core.storage import AgentRepo, get_session

    is_running = bool(_watch_thread and _watch_thread.is_alive())
    agents = AgentRepo(get_session()).list_all()
    rows = ""
    for a in agents:
        last = a.last_run_at.strftime("%H:%M") if a.last_run_at else "never"
        dot_color = (
            "bg-teal-400"
            if a.is_active and a.status != "error"
            else ("bg-red-400" if a.status == "error" else "bg-slate-300")
        )
        rows += f'<tr><td class="py-2 px-4 text-sm font-medium text-slate-800">{a.name}</td><td class="py-2 px-4 text-xs text-slate-500">{a.account_email}</td><td class="py-2 px-4"><span class="w-2 h-2 rounded-full {dot_color} inline-block"></span></td><td class="py-2 px-4 text-xs text-slate-500">{last}</td><td class="py-2 px-4 text-xs text-slate-500">{a.last_found_count}</td></tr>'
    status_badge = (
        '<span class="inline-flex items-center gap-1.5 text-xs font-medium text-teal-700 bg-teal-50 border border-teal-200 px-2 py-0.5 rounded-full"><span class="w-1.5 h-1.5 rounded-full bg-teal-400 animate-pulse"></span>Running</span>'
        if is_running
        else '<span class="inline-flex items-center gap-1.5 text-xs font-medium text-slate-500 bg-slate-50 border border-slate-200 px-2 py-0.5 rounded-full"><span class="w-1.5 h-1.5 rounded-full bg-slate-400"></span>Stopped</span>'
    )
    return HTMLResponse(
        f'<div id="watch-status" hx-get="/watch/status" hx-trigger="every 10s" hx-swap="outerHTML"><div class="flex items-center justify-between mb-4"><span class="text-sm font-medium text-slate-700">Daemon status</span>{status_badge}</div><table class="w-full"><thead><tr class="text-xs text-slate-400 uppercase tracking-wide border-b border-slate-100"><th class="py-2 px-4 text-left">Agent</th><th class="py-2 px-4 text-left">Account</th><th class="py-2 px-4 text-left">Status</th><th class="py-2 px-4 text-left">Last run</th><th class="py-2 px-4 text-left">Found</th></tr></thead><tbody>{rows}</tbody></table></div>'
    )


# ── Agents ────────────────────────────────────────────────────────────────────


@app.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request):
    ctx = _base()
    ctx["active"] = "agents"
    from postmind.config import get_settings
    from postmind.core.account_registry import list_accounts
    from postmind.core.storage import AgentRepo, get_session

    agents = AgentRepo(get_session()).list_all()
    accounts = list_accounts()
    registered_emails = {a.account_email for a in agents}
    unregistered = [a for a in accounts if a.email not in registered_emails]
    settings = get_settings()
    ctx["agents"] = [
        {
            "name": a.name,
            "email": a.account_email,
            "interval": a.interval_minutes,
            "is_active": a.is_active,
            "status": a.status,
            "last_run_at": a.last_run_at.strftime("%H:%M") if a.last_run_at else "never",
            "last_found": a.last_found_count,
            "error": a.error_message,
            "voice_style": a.voice_style or "professional",
            "user_context": a.user_context or "",
            "writing_guidelines": a.writing_guidelines or "",
            "run_rules": a.run_rules if a.run_rules is not None else True,
            "run_followups": a.run_followups if a.run_followups is not None else True,
            "run_avoidance": a.run_avoidance if a.run_avoidance is not None else False,
            "run_daily_brief": a.run_daily_brief if a.run_daily_brief is not None else False,
            "run_autodraft": getattr(a, "run_autodraft", False) or False,
        }
        for a in agents
    ]
    ctx["unregistered_accounts"] = [{"email": a.email} for a in unregistered]
    ctx["has_accounts"] = len(accounts) > 0
    ctx["ai_mode"] = settings.ai_mode
    ctx["daemon_running"] = bool(_watch_thread and _watch_thread.is_alive())
    ctx["created_name"] = request.query_params.get("created", "")
    return _resp(request, "agents.html", ctx)


@app.get("/agents/daemon-badge", response_class=HTMLResponse)
async def agents_daemon_badge(request: Request):
    is_running = bool(_watch_thread and _watch_thread.is_alive())
    if is_running:
        return HTMLResponse(
            '<span id="daemon-badge" hx-get="/agents/daemon-badge" hx-trigger="every 15s" hx-swap="outerHTML" '
            'class="inline-flex items-center gap-1.5 text-xs font-medium text-teal-700 bg-teal-50 border border-teal-200 px-2 py-0.5 rounded-full">'
            '<span class="w-1.5 h-1.5 rounded-full bg-teal-400 animate-pulse"></span>Daemon running</span>'
        )
    return HTMLResponse(
        '<span id="daemon-badge" hx-get="/agents/daemon-badge" hx-trigger="every 15s" hx-swap="outerHTML" '
        'class="inline-flex items-center gap-1.5 text-xs font-medium text-amber-700 bg-amber-50 border border-amber-200 px-2 py-0.5 rounded-full">'
        '<span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span>'
        '<a href="/watch" class="underline underline-offset-2">Daemon stopped — start it to run agents</a></span>'
    )


@app.post("/agents/create")
async def agents_create(request: Request):
    form = await request.form()
    email = (form.get("email") or "").strip()
    name = (form.get("name") or email.split("@")[0].title()).strip()
    interval = int(form.get("interval") or 30)
    if not email:
        raise HTTPException(status_code=400, detail="Email required")
    from postmind.core.storage import AgentRepo, get_session

    AgentRepo(get_session()).register(email, name, max(1, min(1440, interval)))
    from urllib.parse import quote

    return RedirectResponse(f"/agents?created={quote(name)}", status_code=303)


@app.post("/agents/toggle")
async def agents_toggle(request: Request):
    form = await request.form()
    email = (form.get("email") or "").strip()
    active = form.get("active") == "true"
    from postmind.core.storage import AgentRepo, get_session

    AgentRepo(get_session()).set_active(email, active)
    return RedirectResponse("/agents", status_code=303)


@app.post("/agents/delete")
async def agents_delete_route(request: Request):
    form = await request.form()
    email = (form.get("email") or "").strip()
    from postmind.core.storage import AgentRepo, get_session

    AgentRepo(get_session()).delete(email)
    return RedirectResponse("/agents", status_code=303)


@app.post("/agents/soul")
async def agents_soul(request: Request):
    form = await request.form()
    email = (form.get("email") or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="Email required")
    from postmind.core.storage import AgentRepo, get_session

    AgentRepo(get_session()).update_soul(
        account_email=email,
        voice_style=(form.get("voice_style") or "").strip() or None,
        user_context=(form.get("user_context") or "").strip() or None,
        writing_guidelines=(form.get("writing_guidelines") or "").strip() or None,
    )
    return RedirectResponse("/agents", status_code=303)


@app.post("/agents/features")
async def agents_features(request: Request):
    form = await request.form()
    email = (form.get("email") or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="Email required")
    from postmind.core.storage import AgentRepo, get_session

    AgentRepo(get_session()).update_features(
        account_email=email,
        run_rules=form.get("run_rules") == "on",
        run_followups=form.get("run_followups") == "on",
        run_avoidance=form.get("run_avoidance") == "on",
        run_daily_brief=form.get("run_daily_brief") == "on",
        run_autodraft=form.get("run_autodraft") == "on",
    )
    return RedirectResponse("/agents", status_code=303)


@app.post("/agents/compose", response_class=HTMLResponse)
async def agents_compose(request: Request):
    form = await request.form()
    email = (form.get("email") or "").strip()
    intent = (form.get("intent") or "").strip()
    recipient_context = (form.get("recipient_context") or "").strip()
    thread_snippet = (form.get("thread_snippet") or "").strip()

    if not email or not intent:
        return HTMLResponse("<p class='text-red-500 text-sm'>Email and intent are required.</p>")

    from postmind.core.storage import AgentRepo, get_session

    agent = AgentRepo(get_session()).get_by_email(email)
    soul = {}
    if agent:
        soul = {
            "voice_style": agent.voice_style,
            "user_context": agent.user_context,
            "writing_guidelines": agent.writing_guidelines,
        }

    try:
        from postmind.core.ai_engine import AIEngine

        ai = AIEngine()
        draft = ai.compose_email(
            intent=intent,
            recipient_context=recipient_context,
            thread_snippet=thread_snippet,
            soul=soul,
        )
        # Escape for safe HTML insertion
        import html

        escaped = html.escape(draft)
        return HTMLResponse(
            f"<pre class='whitespace-pre-wrap text-sm text-slate-800 bg-slate-50 "
            f"border border-slate-200 rounded-lg p-4 mt-3 font-mono'>{escaped}</pre>"
        )
    except ValueError as exc:
        return HTMLResponse(f"<p class='text-amber-600 text-sm mt-3'>{html.escape(str(exc))}</p>")
    except Exception as exc:
        import html

        return HTMLResponse(
            f"<p class='text-red-500 text-sm mt-3'>Error: {html.escape(str(exc))}</p>"
        )


def _write_env(updates: dict[str, str]) -> None:
    """Upsert KEY=value pairs into ~/.postmind/.env and reset the settings cache."""
    env_file = DATA_DIR / ".env"
    lines: list[str] = env_file.read_text().splitlines(keepends=True) if env_file.exists() else []
    for key, value in updates.items():
        new_line = f"{key}={value}\n"
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = new_line
                break
        else:
            lines.append(new_line)
    env_file.write_text("".join(lines))
    import postmind.config as _cfg

    _cfg._settings = None


def _persist_ai_mode(form) -> str:
    """Write AI-mode settings to DATA_DIR/.env and reset the cached settings.

    Shared by /settings/ai-mode and the onboarding "enable AI" step. Returns the
    chosen mode. Raises HTTPException(400) on an invalid mode.
    """
    mode = form.get("mode", "off")
    if mode not in ("off", "local", "cloud"):
        raise HTTPException(status_code=400, detail="Invalid AI mode")

    updates: dict[str, str] = {"POSTMIND_AI_MODE": mode}
    if mode == "local":
        url = (form.get("ollama_base_url") or "http://localhost:11434").strip()
        model = (form.get("ollama_model") or "qwen2.5:32b").strip()
        updates["POSTMIND_OLLAMA_BASE_URL"] = url
        updates["POSTMIND_OLLAMA_MODEL"] = model
        ollama_key = (form.get("ollama_api_key") or "").strip()
        if ollama_key:
            updates["POSTMIND_OLLAMA_API_KEY"] = ollama_key
    elif mode == "cloud":
        cloud_provider = (form.get("cloud_provider") or "anthropic").strip()
        if cloud_provider not in ("anthropic", "ollama"):
            cloud_provider = "anthropic"
        updates["POSTMIND_CLOUD_PROVIDER"] = cloud_provider
        if cloud_provider == "anthropic":
            api_key = (form.get("anthropic_api_key") or "").strip()
            if api_key:
                updates["ANTHROPIC_API_KEY"] = api_key
        else:
            url = (form.get("ollama_base_url") or "https://ollama.com").strip()
            model = (form.get("ollama_model") or "").strip()
            updates["POSTMIND_OLLAMA_BASE_URL"] = url
            if model:
                updates["POSTMIND_OLLAMA_MODEL"] = model
            ollama_key = (form.get("ollama_api_key") or "").strip()
            if ollama_key:
                updates["POSTMIND_OLLAMA_API_KEY"] = ollama_key

    _write_env(updates)
    return mode


@app.post("/settings/ai-mode")
async def update_ai_mode(request: Request):
    form = await request.form()
    _persist_ai_mode(form)
    return RedirectResponse("/settings?success=ai_mode", status_code=303)


@app.post("/settings/chat")
async def update_chat_settings(request: Request):
    """Configure the floating assistant's LLM backend independently of global AI mode."""
    form = await request.form()
    mode = (form.get("chat_mode") or "").strip()  # "" = inherit
    if mode not in ("", "inherit", "off", "local", "cloud"):
        raise HTTPException(status_code=400, detail="Invalid chat mode")
    if mode == "inherit":
        mode = ""

    updates: dict[str, str] = {"POSTMIND_CHAT_AI_MODE": mode}
    if mode == "cloud":
        model = (form.get("chat_cloud_model") or "").strip()
        updates["POSTMIND_CHAT_CLOUD_MODEL"] = model
    elif mode == "local":
        model = (form.get("chat_ollama_model") or "").strip()
        updates["POSTMIND_CHAT_OLLAMA_MODEL"] = model

    _write_env(updates)
    return RedirectResponse("/settings?success=chat", status_code=303)


@app.post("/settings/agent")
async def update_agent_settings(request: Request):
    """Toggle Super Agent autopilot and power mode settings."""
    form = await request.form()
    on = form.get("agent_autopilot") == "on"
    power_mode = form.get("agent_power_mode") == "on"
    _write_env(
        {
            "POSTMIND_AGENT_AUTOPILOT": "true" if on else "false",
            "POSTMIND_AGENT_POWER_MODE": "true" if power_mode else "false",
        }
    )
    return RedirectResponse("/settings?success=agent", status_code=303)


@app.post("/settings/deep-task")
async def update_deep_task_settings(request: Request):
    """Configure the Super Agent deep task backend (used for complex multi-step requests)."""
    form = await request.form()
    mode = (form.get("deep_task_mode") or "cloud").strip()
    if mode not in ("cloud", "local", "off"):
        raise HTTPException(status_code=400, detail="Invalid deep task mode")
    model = (form.get("deep_task_model") or "").strip()
    _write_env({"POSTMIND_DEEP_TASK_MODE": mode, "POSTMIND_DEEP_TASK_MODEL": model})
    return RedirectResponse("/settings?success=deep_task", status_code=303)


@app.post("/settings/thinking")
async def update_thinking_settings(request: Request):
    """Toggle extended thinking and configure the token budget."""
    form = await request.form()
    enabled = form.get("extended_thinking") == "on"
    raw_budget = form.get("thinking_budget_tokens") or "8000"
    try:
        budget = max(1024, min(int(raw_budget), 100_000))
    except (ValueError, TypeError):
        budget = 8000
    _write_env(
        {
            "POSTMIND_EXTENDED_THINKING": "true" if enabled else "false",
            "POSTMIND_THINKING_BUDGET_TOKENS": str(budget),
        }
    )
    return RedirectResponse("/settings?success=thinking", status_code=303)


@app.post("/settings/daemon")
async def update_daemon_settings(request: Request):
    """Configure background sync / daemon behaviour."""
    form = await request.form()
    auto_daemon = form.get("auto_start_daemon") == "on"
    auto_sync = form.get("auto_sync_on_first_run") == "on"
    interval = max(1, int(form.get("daemon_interval_minutes") or "30"))
    periodic_sync = max(0, int(form.get("periodic_sync_hours") or "6"))
    _write_env(
        {
            "POSTMIND_AUTO_START_DAEMON": "true" if auto_daemon else "false",
            "POSTMIND_AUTO_SYNC_ON_FIRST_RUN": "true" if auto_sync else "false",
            "POSTMIND_DAEMON_INTERVAL_MINUTES": str(interval),
            "POSTMIND_PERIODIC_SYNC_HOURS": str(periodic_sync),
        }
    )
    return RedirectResponse("/settings?success=daemon", status_code=303)


# ── Protected senders ─────────────────────────────────────────────────────────


@app.get("/settings/blocked", response_class=HTMLResponse)
async def blocked_list(request: Request):
    def _get():
        from postmind.core.storage import BlocklistRepo, get_session

        client = _build_provider()
        acct = client.get_email_address()
        entries = BlocklistRepo(get_session()).list_all(acct)
        return entries, acct

    try:
        loop = asyncio.get_event_loop()
        entries, acct = await loop.run_in_executor(_executor, _get)
    except Exception as exc:
        return _resp(request, "error.html", {"error": str(exc)})

    ctx = _base()
    ctx.update(
        {
            "active": "settings",
            "entries": [{"email": e.sender_email, "domain": e.sender_domain} for e in entries],
            "account_email": acct,
        }
    )
    return _resp(request, "blocked.html", ctx)


@app.post("/settings/blocked/add")
async def blocked_add(request: Request):
    form = await request.form()
    sender = (form.get("sender_email") or "").strip()
    if not sender:
        return RedirectResponse("/settings/blocked", status_code=303)

    def _add():
        from postmind.core.storage import BlocklistRepo, get_session

        client = _build_provider()
        acct = client.get_email_address()
        BlocklistRepo(get_session()).add(acct, sender)

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_executor, _add)
    return RedirectResponse("/settings/blocked?added=1", status_code=303)


@app.post("/settings/blocked/remove")
async def blocked_remove(request: Request):
    form = await request.form()
    sender = (form.get("sender_email") or "").strip()
    if not sender:
        return RedirectResponse("/settings/blocked", status_code=303)

    def _remove():
        from postmind.core.storage import BlocklistRepo, get_session

        client = _build_provider()
        acct = client.get_email_address()
        BlocklistRepo(get_session()).remove(acct, sender)

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_executor, _remove)
    return RedirectResponse("/settings/blocked", status_code=303)


# ── Sync ─────────────────────────────────────────────────────────────────────


def _humanize_bytes(n: int) -> str:
    """Compact human-readable byte size, e.g. 4.2 MB."""
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < step or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= step
    return f"{n:.1f} TB"


def _humanize_ago(dt) -> str:
    """Relative 'time ago' string from a UTC datetime."""
    from datetime import datetime, timezone

    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 60:
        return "just now"
    mins = secs / 60
    if mins < 60:
        return f"{int(mins)} min ago"
    hours = mins / 60
    if hours < 24:
        return f"{int(hours)} hr ago"
    days = hours / 24
    if days < 30:
        return f"{int(days)} day{'s' if int(days) != 1 else ''} ago"
    months = days / 30
    if months < 12:
        return f"{int(months)} mo ago"
    return f"{int(months / 12)} yr ago"


def _sync_overview(account_email: str) -> dict:
    """Cache freshness + size stats for the Sync page.

    Reads everything from local SQLite (instant) except the mailbox total,
    which needs one best-effort Gmail profile call to compute coverage.
    """
    from datetime import timezone

    import postmind.config as _cfg
    from postmind.core.storage import AccountRepo, EmailRecord, get_session

    overview: dict = {"has_cache": False}
    if not account_email:
        return overview

    session = get_session()
    try:
        # Repair a missing timestamp from a big/interrupted sync so the page
        # tells the truth instead of "Never".
        AccountRepo(session).backfill_last_synced(account_email)
        acct = AccountRepo(session).get(account_email)

        base = session.query(EmailRecord).filter(EmailRecord.account_email == account_email)
        total = base.count()
        if total == 0:
            return overview

        inbox = base.filter(EmailRecord.is_inbox.is_(True)).count()
        sent = base.filter(EmailRecord.label_ids_json.like('%"SENT"%')).count()
        archived = max(total - inbox - sent, 0)

        last_dt = acct.last_synced_at if acct else None

        # Best-effort live mailbox total for a coverage %. Network call, Gmail
        # only — keep it cheap and never let a failure break the page.
        mailbox_total = 0
        try:
            from postmind.config import load_account_config

            if load_account_config(account_email).get("provider", "gmail") == "gmail":
                from postmind.core.gmail_client import GmailClient

                mailbox_total = GmailClient().get_profile().get("messagesTotal", 0) or 0
        except Exception:
            mailbox_total = 0

        coverage_pct = min(round(total / mailbox_total * 100), 100) if mailbox_total else 0

        db_path = _cfg.DB_PATH
        try:
            db_size = db_path.stat().st_size
        except Exception:
            db_size = 0

        return {
            "has_cache": True,
            "last_synced_ago": _humanize_ago(last_dt) if last_dt else None,
            "last_synced_abs": (
                last_dt.astimezone(timezone.utc).strftime("%b %d, %Y · %H:%M UTC")
                if last_dt
                else None
            ),
            "total_cached": total,
            "inbox_cached": inbox,
            "sent_cached": sent,
            "archived_cached": archived,
            "mailbox_total": mailbox_total,
            "coverage_pct": coverage_pct,
            "db_size": _humanize_bytes(db_size),
        }
    finally:
        session.close()


@app.get("/sync/stale-check", response_class=HTMLResponse)
async def sync_stale_check():
    """HTMX endpoint — returns warning banner HTML when inbox cache is stale, empty otherwise."""
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    from datetime import timezone as _tz

    account_email = _get_web_account() or ""
    if not account_email:
        return HTMLResponse("")

    try:
        from postmind.core.storage import AccountRepo, get_session

        session = get_session()
        acct = AccountRepo(session).get(account_email)
        session.close()
        last_synced = acct.last_synced_at if acct else None

        if last_synced is None:
            age_str = "never synced"
        else:
            if last_synced.tzinfo is None:
                last_synced = last_synced.replace(tzinfo=_tz.utc)
            age = _dt.now(_tz.utc) - last_synced
            if age <= _td(hours=24):
                return HTMLResponse("")  # fresh — no banner
            hours = int(age.total_seconds() // 3600)
            age_str = f"{hours}h ago" if hours < 48 else f"{age.days}d ago"
    except Exception:
        return HTMLResponse("")

    return HTMLResponse(
        "<div id='stale-sync-banner' "
        "class='flex items-center gap-3 px-5 py-2.5 bg-warning-bg border-b border-warning-border text-warning text-xs'>"
        "<svg class='w-3.5 h-3.5 shrink-0' fill='none' viewBox='0 0 24 24' stroke='currentColor' stroke-width='2'>"
        "<path stroke-linecap='round' stroke-linejoin='round' d='M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z'/>"
        "</svg>"
        f"<span>Inbox data is stale &mdash; last synced <strong>{age_str}</strong>. "
        "Data may be outdated.</span>"
        "<a href='/sync' class='ml-1 font-medium underline hover:text-warning shrink-0'>Sync now</a>"
        "<button onclick=\"document.getElementById('stale-sync-banner').remove()\" "
        "class='ml-auto shrink-0 p-0.5 rounded hover:bg-warning/10 transition-colors' aria-label='Dismiss'>"
        "<svg class='w-3.5 h-3.5' fill='none' viewBox='0 0 24 24' stroke='currentColor' stroke-width='2'>"
        "<path stroke-linecap='round' stroke-linejoin='round' d='M6 18L18 6M6 6l12 12'/>"
        "</svg>"
        "</button>"
        "</div>"
    )


@app.get("/sync", response_class=HTMLResponse)
async def sync_page(request: Request):
    ctx = _base()
    ctx["active"] = "sync"
    ctx["overview"] = _sync_overview(_get_web_account() or "")
    return _resp(request, "sync.html", ctx)


_DEEP_RANGES = [
    ("older_than:10y", "10y+"),
    ("older_than:5y newer_than:10y", "5–10y"),
    ("older_than:3y newer_than:5y", "3–5y"),
    ("older_than:2y newer_than:3y", "2–3y"),
    ("older_than:1y newer_than:2y", "1–2y"),
    ("older_than:6m newer_than:1y", "6–12mo"),
    ("newer_than:6m", "<6mo"),
]


def _sync_worker(task_id: str, scope: str, limit: int | None, deep: bool) -> None:
    """Core sync logic — runs in a thread pool. State is tracked via _sync_tasks[task_id]."""
    import json as _json

    from sqlalchemy import select as _select

    from postmind.core.gmail_client import GmailClient
    from postmind.core.storage import (
        AccountRepo,
        EmailRecord,
        EmailRepo,
        UndoLogRepo,
        get_session,
    )
    from postmind.core.storage import EmailRecord as _ER

    state = _sync_tasks[task_id]
    try:
        client = GmailClient()
        profile = client.get_profile()
        account_email = profile.get("emailAddress", "")
        mailbox_total = profile.get("messagesTotal", 0)
        state["message"] = f"Connected to {account_email}"
        state["step"] = 1

        base_query = "in:anywhere -in:trash -in:spam" if scope == "anywhere" else "in:inbox"
        session = get_session()
        repo = EmailRepo(session)
        chunk_size = 50

        # Ensure the account exists in the DB so update_last_synced (called
        # per chunk below) actually records the timestamp instead of
        # silently no-opping on a missing row.
        if account_email:
            try:
                AccountRepo(session).register(account_email)
            except Exception:
                pass

        # Load existing IDs once for dedup across all ranges
        existing_ids: set[str] = set(
            session.execute(_select(_ER.gmail_id).where(_ER.account_email == account_email))
            .scalars()
            .all()
        )

        ranges = _DEEP_RANGES if deep else [("", "")]
        # Deep sync means "get everything" — never let the numeric limit
        # truncate a date range, or large ranges (e.g. 50k+ in 10y+) would
        # be silently capped to the N most-recent emails. The limit only
        # caps quick (non-deep) syncs.
        range_limit = None if deep else limit
        total_saved = 0
        total_skipped = 0
        total_new = 0
        total_failed = 0
        found_ids: set[str] = set()

        for range_filter, range_label in ranges:
            if range_filter:
                q = f"{base_query} {range_filter}"
                state["message"] = f"Fetching IDs [{range_label}]…"
            else:
                q = base_query

            try:
                ids = client.list_message_ids(query=q, max_results=range_limit)
            except Exception as exc:
                state["message"] = f"Warning: could not fetch IDs for {range_label}: {exc}"
                continue

            if not ids:
                continue

            found_ids.update(ids)
            new_ids = [i for i in ids if i not in existing_ids]
            skipped = len(ids) - len(new_ids)
            total_skipped += skipped
            total_new += len(new_ids)
            # Drive the progress bar: how many new emails we intend to fetch.
            state["total"] = total_new

            range_note = f" [{range_label}]" if deep else ""
            state["message"] = f"Found {len(ids):,}{range_note} — syncing {len(new_ids):,} new…"
            state["step"] = 2

            for i in range(0, len(new_ids), chunk_size):
                chunk_ids = new_ids[i : i + chunk_size]
                try:
                    messages = client.get_messages_metadata_batch(chunk_ids)
                except Exception:
                    time.sleep(2)
                    try:
                        messages = client.get_messages_metadata_batch(chunk_ids)
                    except Exception:
                        total_failed += len(chunk_ids)
                        continue
                records = [
                    EmailRecord(
                        account_email=account_email,
                        gmail_id=msg.id,
                        thread_id=msg.thread_id,
                        subject=msg.headers.subject,
                        sender_email=msg.sender_email,
                        sender_name=msg.sender_name,
                        snippet=msg.snippet or "",
                        label_ids_json=_json.dumps(msg.label_ids),
                        internal_date=msg.internal_date,
                        size_estimate=msg.size_estimate,
                        is_unread=msg.is_unread,
                        is_inbox=msg.is_inbox,
                        list_unsubscribe=msg.headers.list_unsubscribe or "",
                    )
                    for msg in messages
                ]
                repo.upsert_many(records)
                # Stamp freshness as we go, not just at the end — a large
                # mailbox sync gets rate-limited / interrupted, and we want
                # the cache to still report when it was last touched.
                try:
                    AccountRepo(session).update_last_synced(account_email)
                except Exception:
                    pass
                existing_ids.update(r.gmail_id for r in records)
                total_saved += len(records)
                # Items the batch silently dropped (per-message API errors).
                total_failed += len(chunk_ids) - len(records)
                state["count"] = total_saved
                state["message"] = (
                    f"Synced {total_saved:,} emails{range_note}…"
                    if not deep
                    else f"Synced {total_saved:,} total — current batch {range_label}…"
                )
                state["step"] = 3

        UndoLogRepo(session).purge_expired()

        # Record the sync timestamp so re-syncs and the daemon can reason
        # about freshness.
        try:
            AccountRepo(session).update_last_synced(account_email)
        except Exception:
            pass

        elapsed = int(time.time() - state["started_at"])
        state["status"] = "done"
        skip_note = f", {total_skipped:,} already cached" if total_skipped else ""
        state["message"] = f"Synced {total_saved:,} emails in {elapsed}s{skip_note}"
        state["count"] = total_saved

        # Honest completeness check: did we cache everything we listed for
        # this scope? Surface the gap instead of always claiming "up to date".
        cached_in_scope = len(found_ids & existing_ids)
        gap = len(found_ids) - cached_in_scope
        if total_failed:
            state["detail"] = (
                f"{total_failed:,} emails could not be fetched — run sync again to retry."
            )
            state["complete"] = False
        elif gap > 0:
            state["detail"] = f"{gap:,} emails still not cached — re-run sync to finish."
            state["complete"] = False
        elif not deep and mailbox_total and len(existing_ids) < mailbox_total * 0.9:
            # Quick sync only covered part of the mailbox.
            state["detail"] = (
                f"{len(existing_ids):,} of ~{mailbox_total:,} cached. "
                "For your full mailbox, run a Deep sync of All mail."
            )
            state["complete"] = False
        else:
            state["detail"] = f"Local cache is up to date — {len(existing_ids):,} emails cached."
            state["complete"] = True

    except Exception as exc:
        state["status"] = "error"
        state["error"] = str(exc)
        state["message"] = str(exc)
    finally:
        global _active_sync_task_id
        _active_sync_task_id = None


@app.post("/sync/start", response_class=HTMLResponse)
async def sync_start(request: Request):
    form = await request.form()
    scope = form.get("scope", "inbox")
    raw_limit = int(form.get("limit", "1000"))
    limit = None if raw_limit == 0 else raw_limit
    deep = form.get("deep") == "1"
    # Where to send the user when the sync finishes. Only same-site relative paths
    # are allowed, so this can't be turned into an open redirect.
    next_url = form.get("next", "/stats")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/stats"

    global _active_sync_task_id
    task_id = uuid.uuid4().hex[:8]
    _sync_tasks[task_id] = {
        "status": "running",
        "step": 0,
        "message": "Connecting…",
        "count": 0,
        "total": 0,
        "error": None,
        "started_at": time.time(),
        "next_url": next_url,
        "detail": "Local cache is up to date",
        "complete": True,
    }
    _active_sync_task_id = task_id
    _executor.submit(_sync_worker, task_id, scope, limit, deep)

    # Return the polling fragment immediately
    html = f"""
<div id="sync-progress"
     hx-get="/sync/poll/{task_id}"
     hx-trigger="every 1s"
     hx-target="this"
     hx-swap="outerHTML">
  <div class="flex items-center gap-3 text-slate-500 text-sm py-2">
    <div class="w-4 h-4 border-2 border-teal-500 border-t-transparent rounded-full animate-spin shrink-0"></div>
    Connecting…
  </div>
</div>"""
    return HTMLResponse(html)


@app.get("/sync/active")
async def sync_active():
    from fastapi.responses import JSONResponse

    if _active_sync_task_id and _active_sync_task_id in _sync_tasks:
        state = _sync_tasks[_active_sync_task_id]
        return JSONResponse(
            {
                "task_id": _active_sync_task_id,
                "status": state["status"],
                "message": state["message"],
                "count": state["count"],
            }
        )
    return JSONResponse({"task_id": None})


@app.get("/sync/poll/{task_id}", response_class=HTMLResponse)
async def sync_poll(task_id: str):
    state = _sync_tasks.get(task_id)
    if not state:
        return HTMLResponse('<p class="text-red-500 text-sm">Task not found.</p>')

    status = state["status"]
    msg = state["message"]
    count = state["count"]
    total = state["total"]
    pct = int((count / total) * 100) if total > 0 else 0

    if status == "error":
        return HTMLResponse(f"""
<div id="sync-result" class="bg-red-50 border border-red-200 rounded-xl p-4">
  <p class="text-red-800 font-medium text-sm">Sync failed</p>
  <p class="text-red-600 text-sm mt-1">{state["error"]}</p>
</div>""")

    if status == "done":
        next_url = state.get("next_url", "/stats")
        cta = "See your cleanup plan →" if next_url == "/welcome" else "View Stats →"
        complete = state.get("complete", True)
        detail = state.get("detail", "Local cache is up to date")
        box = "bg-green-50 border-green-200" if complete else "bg-amber-50 border-amber-200"
        head = "text-green-800" if complete else "text-amber-800"
        sub = "text-green-600" if complete else "text-amber-700"
        icon = "✓" if complete else "⚠"
        return HTMLResponse(f"""
<div id="sync-result" class="{box} border rounded-xl p-4">
  <div class="flex items-center justify-between">
    <div>
      <p class="{head} font-medium text-sm">{icon} {msg}</p>
      <p class="{sub} text-xs mt-0.5">{detail}</p>
    </div>
    <a href="{next_url}" class="bg-teal-600 hover:bg-teal-700 text-white text-xs font-medium px-4 py-2 rounded-lg transition-colors whitespace-nowrap">
      {cta}
    </a>
  </div>
</div>""")

    # Still running — keep polling
    bar_width = pct if pct > 0 else 5
    return HTMLResponse(f"""
<div id="sync-progress"
     hx-get="/sync/poll/{task_id}"
     hx-trigger="every 1s"
     hx-target="this"
     hx-swap="outerHTML">
  <div class="space-y-2">
    <div class="flex items-center gap-3">
      <div class="w-4 h-4 border-2 border-teal-500 border-t-transparent rounded-full animate-spin shrink-0"></div>
      <span class="text-slate-600 text-sm">{msg}</span>
    </div>
    {
        f'''<div class="w-full bg-slate-100 rounded-full h-1.5">
      <div class="bg-teal-500 h-1.5 rounded-full transition-all" style="width:{bar_width}%"></div>
    </div>
    <p class="text-slate-400 text-xs">{count:,} / {total:,} emails — {pct}%</p>'''
        if total > 0
        else ""
    }
  </div>
</div>""")


# ── Triage ────────────────────────────────────────────────────────────────────

_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
_CATEGORY_ICONS = {
    "action_required": "⚡",
    "conversation": "💬",
    "newsletter": "📰",
    "notification": "🔔",
    "receipt": "🧾",
    "calendar": "📅",
    "social": "👥",
    "spam": "🗑",
    "other": "📧",
}

# Messages fetched for the triage page that still need classification, stashed so
# the /triage/classify-stream endpoint can pick them up without a second Gmail
# fetch. token → {"messages": [Message...], "created": float}.
_triage_pending: dict[str, dict] = {}
_TRIAGE_PENDING_TTL = 600  # 10 minutes


def _prune_triage_pending() -> None:
    cutoff = time.time() - _TRIAGE_PENDING_TTL
    for tok in [t for t, v in _triage_pending.items() if v["created"] < cutoff]:
        _triage_pending.pop(tok, None)


def _cls_payload(
    gmail_id: str,
    priority: str,
    category: str,
    explanation: str,
    suggested_action: str,
    requires_reply: bool,
    deadline_hint: str,
) -> dict:
    """Build the classification dict the template + stream both render from."""
    return {
        "id": gmail_id,
        "priority": priority,
        "category": category,
        "category_icon": _CATEGORY_ICONS.get(category, "📧"),
        "explanation": explanation,
        "suggested_action": suggested_action,
        "requires_reply": bool(requires_reply),
        "deadline_hint": deadline_hint,
    }


def _fetch_triage_messages(scope: str, limit: int):
    """Fetch the inbox slice to triage (no classification). Returns (messages, account_email)."""
    from postmind.core.gmail_client import GmailClient, Message, MessageHeader

    account_email = _get_web_account() or ""
    if scope == "all":
        # Read from the local synced DB — no Gmail API calls needed.
        from postmind.core.storage import EmailRepo, get_session

        repo = EmailRepo(get_session())
        records = repo.get_inbox(account_email=account_email, limit=limit)
        messages = []
        for r in records:
            from_ = f"{r.sender_name} <{r.sender_email}>" if r.sender_name else r.sender_email
            messages.append(
                Message(
                    id=r.gmail_id,
                    thread_id=r.thread_id or "",
                    snippet=r.snippet or "",
                    headers=MessageHeader(subject=r.subject or "", from_=from_),
                    label_ids=[],
                    size_estimate=r.size_estimate or 0,
                    internal_date=0,
                )
            )
        return messages, account_email

    client = GmailClient()
    profile = client.get_profile()
    account_email = profile.get("emailAddress", "")
    ids = client.list_message_ids(query="in:inbox is:unread", max_results=limit)
    if not ids:
        return [], account_email
    return client.get_messages_batch(ids), account_email


@app.get("/triage", response_class=HTMLResponse)
async def triage_page(request: Request):
    ctx = _base()
    ctx["active"] = "triage"

    if _ai_mode() == "off":
        return _resp(
            request,
            "triage.html",
            {**ctx, "ai_off": True, "results": [], "scope": "unread", "limit": 20},
        )

    if not _is_authed():
        return _resp(
            request,
            "triage.html",
            {
                **ctx,
                "ai_off": False,
                "auth_error": True,
                "results": [],
                "scope": "unread",
                "limit": 20,
            },
        )

    limit = int(request.query_params.get("limit", "20"))
    scope = request.query_params.get("scope", "unread")  # "unread" or "all"

    def _run():
        """Fetch the inbox slice and apply any cached classifications.

        Returns the rows to render immediately (cached rows carry their
        classification; uncached rows render a placeholder and are streamed in
        afterwards) plus the messages still needing classification.
        """
        from postmind.core.storage import (
            ClassificationCacheRepo,
            UserActionRepo,
            get_session,
        )

        messages, account_email = _fetch_triage_messages(scope, limit)
        if not messages:
            return [], [], account_email

        session = get_session()
        cached = ClassificationCacheRepo(session).get_many([m.id for m in messages])

        # Load behavioral signals once for the sort step
        action_repo = UserActionRepo(session)
        _trash_senders = action_repo.high_trash_senders(account_email)
        _replied_senders = action_repo.replied_senders(account_email)

        rows = []
        pending = []
        for m in messages:
            meta = {
                "id": m.id,
                "thread_id": m.thread_id or "",
                "subject": m.headers.subject or "(no subject)",
                "sender_name": m.sender_name or m.sender_email,
                "sender_email": m.sender_email,
                "snippet": (m.snippet or "")[:200],
            }
            c = cached.get(m.id)
            if c:
                meta["cls"] = _cls_payload(m.id, **c)
            else:
                meta["cls"] = None
                pending.append(m)
            rows.append(meta)

        def _triage_sort_key(r: dict) -> tuple:
            priority_score = _PRIORITY_ORDER.get(r["cls"]["priority"], 3) if r["cls"] else 99
            se = (r.get("sender_email") or "").lower()
            # Replied senders float up (-1), high-trash senders sink (+1)
            behavioral = -1 if se in _replied_senders else (1 if se in _trash_senders else 0)
            return (priority_score, behavioral)

        rows.sort(key=_triage_sort_key)
        return rows, pending, account_email

    try:
        loop = asyncio.get_event_loop()
        rows, pending, account_email = await loop.run_in_executor(_executor, _run)
    except Exception as exc:
        ctx["error"] = str(exc)
        return _resp(request, "triage.html", {**ctx, "ai_off": False, "results": []})

    pending_token = None
    if pending:
        _prune_triage_pending()
        pending_token = uuid.uuid4().hex
        _triage_pending[pending_token] = {"messages": pending, "created": time.time()}

    ctx.update(
        {
            "ai_off": False,
            "auth_error": False,
            "results": rows,
            "pending_token": pending_token,
            "pending_count": len(pending),
            "account_email": account_email,
            "limit": limit,
            "scope": scope,
            "scanned_at": datetime.now(timezone.utc).strftime("%H:%M"),
        }
    )
    return _resp(request, "triage.html", ctx)


@app.get("/triage/classify-stream")
async def triage_classify_stream(request: Request):
    """Stream classifications for the pending messages stashed under ``token``.

    Server-Sent Events: one ``{"type":"row", ...}`` event per classified message
    (emitted batch-by-batch as the parallel LLM calls complete), then ``done``.
    Results are persisted to the classification cache so re-opening Triage is
    instant for these messages.
    """
    token = request.query_params.get("token", "")
    entry = _triage_pending.pop(token, None) if token else None

    if _ai_mode() == "off" or not entry:

        async def _empty():
            yield _sse({"type": "done"})

        return StreamingResponse(_empty(), media_type="text/event-stream")

    messages = entry["messages"]
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    _SENTINEL = object()

    def _produce():
        from concurrent.futures import ThreadPoolExecutor as _Pool
        from concurrent.futures import as_completed

        from postmind.core.ai_engine import AIEngine, _chunks
        from postmind.core.storage import ClassificationCacheRepo, get_session

        def _put(item):
            loop.call_soon_threadsafe(queue.put_nowait, item)

        try:
            ai = AIEngine()
            settings = get_settings()
            # Local LLMs benefit from small batches: results trickle in quickly
            # instead of one big call that blocks all cards until done.
            batch_size = 3 if _ai_mode() == "local" else settings.ai_max_classify_batch
            chunks = list(_chunks(messages, batch_size))
            workers = max(1, min(settings.ai_classify_parallelism, len(chunks)))
            cache_repo = ClassificationCacheRepo(get_session())

            # Load behavioral priors once per classify session
            from postmind.core.storage import UserActionRepo

            _acct = _get_web_account() or ""
            _priors = UserActionRepo(get_session()).sender_action_counts(_acct) if _acct else {}

            def _do(chunk):
                try:
                    return ai.classify_batch(chunk, sender_priors=_priors)
                except Exception:
                    # A batch that fails to classify shouldn't hang the row — fall
                    # back to a neutral classification so the UI resolves.
                    from postmind.core.ai_engine import ClassifiedEmail

                    return [
                        ClassifiedEmail(
                            gmail_id=m.id,
                            category="other",
                            priority="medium",
                            explanation="Could not classify automatically.",
                            suggested_action="keep",
                            requires_reply=False,
                            deadline_hint="",
                        )
                        for m in chunk
                    ]

            with _Pool(max_workers=workers) as pool:
                futures = [pool.submit(_do, ch) for ch in chunks]
                for fut in as_completed(futures):
                    classified = fut.result()
                    cache_repo.upsert_many(
                        [
                            {
                                "gmail_id": c.gmail_id,
                                "category": c.category,
                                "priority": c.priority,
                                "explanation": c.explanation,
                                "suggested_action": c.suggested_action,
                                "requires_reply": c.requires_reply,
                                "deadline_hint": c.deadline_hint,
                            }
                            for c in classified
                        ]
                    )
                    for c in classified:
                        _put(
                            {
                                "type": "row",
                                **_cls_payload(
                                    c.gmail_id,
                                    c.priority,
                                    c.category,
                                    c.explanation,
                                    c.suggested_action,
                                    c.requires_reply,
                                    c.deadline_hint,
                                ),
                            }
                        )
        except Exception as exc:
            _put({"type": "error", "message": str(exc)})
        finally:
            _put(_SENTINEL)

    async def _event_stream():
        future = loop.run_in_executor(_executor, _produce)
        try:
            while True:
                item = await queue.get()
                if item is _SENTINEL:
                    break
                if await request.is_disconnected():
                    break
                yield _sse(item)
            if not await request.is_disconnected():
                yield _sse({"type": "done"})
        finally:
            if not future.done():
                try:
                    await asyncio.wrap_future(future)
                except Exception:
                    pass

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/triage/trash")
async def triage_trash(request: Request):
    """Trash a single email from the triage page and return JSON for inline removal."""
    form = await request.form()
    gmail_id = (form.get("gmail_id") or "").strip()
    if not gmail_id:
        return JSONResponse({"ok": False, "error": "No message ID"}, status_code=400)

    account_email = _get_web_account() or ""

    def _do():
        from postmind.core.storage import (
            ClassificationCacheRepo,
            EmailRepo,
            UndoLogRepo,
            UserActionRepo,
            get_session,
        )

        session = get_session()
        client = _build_provider()
        entry = UndoLogRepo(session).record(
            account_email=account_email,
            operation="trash",
            message_ids=[gmail_id],
            description="Trashed 1 email from Triage",
            metadata={},
        )
        client.batch_trash([gmail_id])

        # Record behavioral signal
        rec = EmailRepo(session).get(gmail_id)
        cls = ClassificationCacheRepo(session).get_many([gmail_id]).get(gmail_id, {})
        if rec:
            UserActionRepo(session).record(
                account_email=account_email,
                gmail_id=gmail_id,
                sender_email=rec.sender_email or "",
                sender_name=rec.sender_name or "",
                subject=rec.subject or "",
                action="trash",
                source="triage",
                ai_category=cls.get("category", ""),
                ai_priority=cls.get("priority", ""),
            )
        return entry.id

    try:
        loop = asyncio.get_event_loop()
        undo_id = await loop.run_in_executor(_executor, _do)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    # Fire-and-forget: check if this trash action triggers a rule proposal
    loop = asyncio.get_event_loop()
    loop.run_in_executor(_executor, _maybe_synthesize_rules, account_email)

    return JSONResponse({"ok": True, "undo_id": undo_id})


@app.post("/brief/action")
async def brief_action(request: Request):
    """Trash or archive one or many emails directly from the Daily Brief page."""
    form = await request.form()
    action = (form.get("action") or "").strip()  # trash | archive | bulk_trash | bulk_archive
    gmail_id = (form.get("gmail_id") or "").strip()
    gmail_ids_raw = form.getlist("gmail_ids[]")

    ids = [i for i in ([gmail_id] if gmail_id else gmail_ids_raw) if i]
    if not ids:
        return JSONResponse({"ok": False, "error": "No message IDs"}, status_code=400)

    base_action = action.removeprefix("bulk_")  # "trash" or "archive"
    if base_action not in ("trash", "archive"):
        return JSONResponse({"ok": False, "error": f"Unknown action: {action}"}, status_code=400)

    account_email = _get_web_account() or ""
    verb = "Trashed" if base_action == "trash" else "Archived"
    desc = f"{verb} {len(ids)} email{'s' if len(ids) != 1 else ''} from Daily Brief"

    def _do():
        from postmind.core.storage import (
            ClassificationCacheRepo,
            EmailRecord,
            UndoLogRepo,
            UserActionRepo,
            get_session,
        )

        session = get_session()
        client = _build_provider()
        entry = UndoLogRepo(session).record(
            account_email=account_email,
            operation=base_action,
            message_ids=ids,
            description=desc,
            metadata={},
        )
        if base_action == "trash":
            client.batch_trash(ids)
        else:
            client.batch_archive(ids)

        # Record behavioral signals (batch lookup to avoid N+1)
        email_recs = {
            r.gmail_id: r
            for r in session.query(EmailRecord).filter(EmailRecord.gmail_id.in_(ids)).all()
        }
        cls_map = ClassificationCacheRepo(session).get_many(ids)
        repo = UserActionRepo(session)
        for gid in ids:
            rec = email_recs.get(gid)
            cls = cls_map.get(gid, {})
            if rec:
                repo.record(
                    account_email=account_email,
                    gmail_id=gid,
                    sender_email=rec.sender_email or "",
                    sender_name=rec.sender_name or "",
                    subject=rec.subject or "",
                    action=base_action,
                    source="brief",
                    ai_category=cls.get("category", ""),
                    ai_priority=cls.get("priority", ""),
                )
        return entry.id

    try:
        loop = asyncio.get_event_loop()
        undo_id = await loop.run_in_executor(_executor, _do)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    # Fire-and-forget rule synthesis check when trash actions are involved
    if base_action == "trash":
        loop = asyncio.get_event_loop()
        loop.run_in_executor(_executor, _maybe_synthesize_rules, account_email)

    return JSONResponse({"ok": True, "undo_id": undo_id, "count": len(ids)})


@app.get("/brief/deal-open")
async def brief_deal_open(gid: str = ""):
    """Record that the user opened a deal email, then redirect to Gmail."""
    if not gid:
        return RedirectResponse("https://mail.google.com/", status_code=302)

    account_email = _get_web_account() or ""

    def _record():
        from postmind.core.storage import EmailRecord, UserActionRepo, get_session

        session = get_session()
        rec = session.query(EmailRecord).filter_by(gmail_id=gid).first()
        if rec and account_email:
            UserActionRepo(session).record(
                account_email=account_email,
                gmail_id=gid,
                sender_email=rec.sender_email or "",
                sender_name=rec.sender_name or "",
                subject=rec.subject or "",
                action="deal_opened",
                source="deals",
            )

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(_executor, _record)
    except Exception:
        pass

    from urllib.parse import quote as _q

    gmail_url = (
        "https://mail.google.com/mail/u/0/"
        f"?authuser={_q(account_email, safe='@')}#all/{_q(gid, safe='')}"
    )
    return RedirectResponse(gmail_url, status_code=302)


@app.post("/digest/exempt")
async def digest_exempt_add(request: Request):
    """Permanently exempt a sender from digest auto-trash and mark them in today's brief JSON."""
    import json as _json

    from postmind.core.storage import DailyBrief, DigestExemptionRepo, get_session

    account_email = _get_web_account() or ""
    if not account_email:
        return JSONResponse({"ok": False, "error": "no account"}, status_code=401)

    body = await request.json()
    sender_email = (body.get("sender_email") or "").lower().strip()
    if not sender_email:
        return JSONResponse({"ok": False, "error": "missing sender_email"}, status_code=400)

    def _exempt():
        from datetime import datetime, timezone

        session = get_session()
        DigestExemptionRepo(session).add(account_email, sender_email)
        today_str = datetime.now(timezone.utc).date().isoformat()
        brief = (
            session.query(DailyBrief)
            .filter_by(account_email=account_email, brief_date=today_str)
            .first()
        )
        if brief:
            for col in ("newsletters_json", "promotions_json"):
                raw = getattr(brief, col, None)
                if not raw:
                    continue
                try:
                    items = _json.loads(raw)
                    changed = False
                    for item in items:
                        if item.get("sender_email", "").lower() == sender_email:
                            item["exempted"] = True
                            changed = True
                    if changed:
                        setattr(brief, col, _json.dumps(items))
                except Exception:
                    pass
            session.commit()

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_executor, _exempt)
    return {"ok": True}


@app.delete("/digest/exempt")
async def digest_exempt_remove(request: Request):
    """Remove a sender exemption so they're included in future digest cleanups."""
    import json as _json

    from postmind.core.storage import DailyBrief, DigestExemptionRepo, get_session

    account_email = _get_web_account() or ""
    if not account_email:
        return JSONResponse({"ok": False, "error": "no account"}, status_code=401)

    body = await request.json()
    sender_email = (body.get("sender_email") or "").lower().strip()
    if not sender_email:
        return JSONResponse({"ok": False, "error": "missing sender_email"}, status_code=400)

    def _unexempt():
        from datetime import datetime, timezone

        session = get_session()
        DigestExemptionRepo(session).remove(account_email, sender_email)
        today_str = datetime.now(timezone.utc).date().isoformat()
        brief = (
            session.query(DailyBrief)
            .filter_by(account_email=account_email, brief_date=today_str)
            .first()
        )
        if brief:
            for col in ("newsletters_json", "promotions_json"):
                raw = getattr(brief, col, None)
                if not raw:
                    continue
                try:
                    items = _json.loads(raw)
                    changed = False
                    for item in items:
                        if item.get("sender_email", "").lower() == sender_email:
                            item["exempted"] = False
                            changed = True
                    if changed:
                        setattr(brief, col, _json.dumps(items))
                except Exception:
                    pass
            session.commit()

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_executor, _unexempt)
    return {"ok": True}


@app.post("/digest/undo-all")
async def digest_undo_all(request: Request):
    """Exempt ALL senders in today's digest — cancels auto-trash for today."""
    import json as _json

    from postmind.core.storage import DailyBrief, DigestExemptionRepo, get_session

    account_email = _get_web_account() or ""
    if not account_email:
        return JSONResponse({"ok": False, "error": "no account"}, status_code=401)

    def _undo_all():
        from datetime import datetime, timezone

        session = get_session()
        today_str = datetime.now(timezone.utc).date().isoformat()
        brief = (
            session.query(DailyBrief)
            .filter_by(account_email=account_email, brief_date=today_str)
            .first()
        )
        if not brief:
            return
        repo = DigestExemptionRepo(session)
        for col in ("newsletters_json", "promotions_json"):
            raw = getattr(brief, col, None)
            if not raw:
                continue
            try:
                items = _json.loads(raw)
                for item in items:
                    item["exempted"] = True
                    se = item.get("sender_email", "")
                    if se:
                        repo.add(account_email, se)
                setattr(brief, col, _json.dumps(items))
            except Exception:
                pass
        brief.digest_trash_after = None
        session.commit()

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_executor, _undo_all)
    return {"ok": True}


@app.get("/email/preview/{gmail_id}")
async def email_preview(gmail_id: str):
    """Return subject, sender, and body text for a single email (used by brief preview modal)."""
    if not gmail_id:
        return JSONResponse({"ok": False, "error": "No message ID"}, status_code=400)

    account_email = _get_web_account() or ""

    def _fetch():
        from postmind.core.storage import EmailRecord, get_session

        # Attempt fast path: snippet from local cache
        session = get_session()
        rec = session.query(EmailRecord).filter_by(gmail_id=gmail_id).first()
        snippet = rec.snippet if rec else ""

        provider = _build_provider()
        from postmind.core.providers.gmail import GmailProvider

        if not isinstance(provider, GmailProvider):
            # IMAP — no per-message body fetch; return snippet only
            return {
                "ok": True,
                "subject": rec.subject if rec else "",
                "sender": rec.sender_name or rec.sender_email if rec else "",
                "body_text": snippet,
                "is_snippet": True,
            }

        msg = provider.gmail_client.get_message(gmail_id)
        return {
            "ok": True,
            "subject": msg.headers.subject or (rec.subject if rec else ""),
            "sender": msg.sender_name or msg.sender_email or (rec.sender_name if rec else ""),
            "body_text": msg.body_text or snippet,
            "is_snippet": not msg.body_text,
        }

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(_executor, _fetch)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    return JSONResponse(result)


# ── Rule proposals ────────────────────────────────────────────────────────────


@app.get("/rules/proposals", response_class=HTMLResponse)
async def rules_proposals_fragment(request: Request):
    """HTMX fragment: proposed rules banner for /brief and /triage pages."""
    account_email = _get_web_account() or ""
    if not account_email:
        return HTMLResponse("")

    from postmind.core.storage import RuleRepo, get_session

    proposals = RuleRepo(get_session()).list_proposed(account_email)
    if not proposals:
        return HTMLResponse("")

    import html as _html

    cards = []
    for p in proposals[:3]:
        name = _html.escape(p.name or "")
        explanation = _html.escape(p.ai_explanation or "")
        cards.append(
            f'<div class="flex items-start justify-between gap-3 py-2.5 border-b border-hairline last:border-0" '
            f'id="proposal-{p.id}">'
            f'<div class="min-w-0 flex-1">'
            f'<p class="text-sm font-medium text-ink truncate">{name}</p>'
            f'<p class="text-xs text-ink-subtle mt-0.5">{explanation}</p>'
            f"</div>"
            f'<div class="shrink-0 flex gap-1.5">'
            f'<button hx-post="/rules/proposals/{p.id}/confirm" hx-target="#proposal-{p.id}" hx-swap="outerHTML" '
            f'class="pm-btn text-xs py-1 px-2.5">Create rule</button>'
            f'<button hx-post="/rules/proposals/{p.id}/dismiss" hx-target="#proposal-{p.id}" hx-swap="outerHTML" '
            f'class="pm-btn-secondary text-xs py-1 px-2.5">Dismiss</button>'
            f"</div></div>"
        )

    inner = "".join(cards)
    return HTMLResponse(
        f'<div id="rule-proposals" class="pm-card mt-4 px-4 py-3 border-l-4 border-accent">'
        f'<div class="flex items-center gap-2 mb-2">'
        f'<svg class="w-4 h-4 text-accent shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.75">'
        f'<path stroke-linecap="round" stroke-linejoin="round" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"/></svg>'
        f'<p class="text-sm font-semibold text-ink">Suggested automations based on your actions</p>'
        f"</div>"
        f"{inner}"
        f"</div>"
    )


@app.post("/rules/proposals/{rule_id}/confirm", response_class=HTMLResponse)
async def rule_proposal_confirm(rule_id: int, request: Request):
    from postmind.core.storage import RuleRepo, get_session

    RuleRepo(get_session()).confirm_proposal(rule_id)
    return HTMLResponse('<div class="py-2 text-xs text-success">✓ Rule created and active.</div>')


@app.post("/rules/proposals/{rule_id}/dismiss", response_class=HTMLResponse)
async def rule_proposal_dismiss(rule_id: int, request: Request):
    from postmind.core.storage import RuleRepo, get_session

    RuleRepo(get_session()).dismiss_proposal(rule_id)
    return HTMLResponse("")


# ── Assistant (floating chat) ──────────────────────────────────────────────────

# Each page is keyed by its route path (the stable identifier the `navigate`
# tool emits) and carries a human-friendly display name plus a description.
# The name — never the path — is what the assistant shows the user.
_PAGES = {
    "/": {"name": "Dashboard", "desc": "inbox overview at a glance"},
    "/brief": {
        "name": "Daily Brief",
        "desc": "today's AI-generated morning summary of important emails, follow-ups, and action items",
    },
    "/agent": {
        "name": "Super Agent",
        "desc": "natural-language command center that can clean up, unsubscribe, send, and automate (with confirm-first cards)",
    },
    "/stats": {"name": "Stats", "desc": "senders ranked by storage impact, with a Purge button"},
    "/triage": {
        "name": "Triage",
        "desc": "AI-classified unread inbox (priority, category, action)",
    },
    "/drafts": {
        "name": "Drafts",
        "desc": "AI-drafted replies in your voice, parked in Gmail for review before sending",
    },
    "/agents": {
        "name": "Agents",
        "desc": "per-account heartbeat watchers and their voice/soul config",
    },
    "/sync": {"name": "Sync", "desc": "pull the mailbox into the local cache"},
    "/accounts": {"name": "Accounts", "desc": "add / switch / remove Gmail and IMAP accounts"},
    "/watch": {"name": "Watch", "desc": "start/stop the heartbeat daemon that runs agents"},
    "/undo": {"name": "Undo History", "desc": "reverse recent operations within the undo window"},
    "/settings": {"name": "Settings", "desc": "AI mode, protected senders, data location"},
}


def _chat_overview_text(account_email: str) -> str:
    """Compact, live snapshot of the inbox for grounding the assistant."""
    from postmind.core.sender_stats import (
        fetch_sender_groups_from_db,
        generate_recommendations,
        group_by_domain,
        reclaimable_mb,
    )
    from postmind.core.storage import EmailRepo, get_session

    cached = _cache_get()
    groups = None
    source = ""
    if cached:
        groups = cached["groups"]
        source = "recent scan"
    elif account_email and EmailRepo(get_session()).get_inbox(account_email, limit=1):
        groups = fetch_sender_groups_from_db(
            account_email=account_email, scope="inbox", min_count=1, top_n=100, sort_by="score"
        )
        source = "local cache"

    if not groups:
        return (
            "No inbox data yet — there's nothing to quote numbers from. Offer the user the "
            'Sync page via the `navigate` tool (label "Sync your inbox") so they can pull '
            "their mailbox in first."
        )

    domain_map = {d.domain: d for d in group_by_domain(groups)}
    recs = generate_recommendations(groups, top_n=5, domain_map=domain_map)
    total_count = sum(g.count for g in groups)
    lines = [
        f"Inbox snapshot (source: {source}) for {account_email or 'active account'}:",
        f"- {len(groups)} senders, {total_count:,} emails in scope",
        f"- ~{reclaimable_mb(recs):.0f} MB reclaimable from the top cleanup suggestions",
        "- Top senders by impact:",
    ]
    for g in groups[:8]:
        size = (
            f"{g.total_size_mb:.1f} MB"
            if g.total_size_mb >= 0.1
            else f"{g.total_size_bytes // 1024} KB"
        )
        lines.append(f"  • {g.display_name} <{g.sender_email}> — {g.count} emails, {size}")
    return "\n".join(lines)


def _chat_search_senders(query: str, account_email: str) -> str:
    from postmind.core.sender_stats import fetch_sender_groups_from_db
    from postmind.core.storage import EmailRepo, get_session

    cached = _cache_get()
    if cached:
        groups = cached["groups"]
    elif account_email and EmailRepo(get_session()).get_inbox(account_email, limit=1):
        groups = fetch_sender_groups_from_db(
            account_email=account_email, scope="inbox", min_count=1, top_n=500, sort_by="score"
        )
    else:
        return "No scan data available — ask the user to run a Sync or open Stats first."

    q = query.lower().strip()
    matches = [
        g
        for g in groups
        if q in (g.sender_email or "").lower()
        or q in (g.sender_name or "").lower()
        or q in (g.domain or "").lower()
    ]
    if not matches:
        return f"No senders matching '{query}' in the current scan."
    lines = [f"{len(matches)} sender(s) matching '{query}':"]
    for g in matches[:12]:
        size = (
            f"{g.total_size_mb:.1f} MB"
            if g.total_size_mb >= 0.1
            else f"{g.total_size_bytes // 1024} KB"
        )
        lines.append(f"- {g.display_name} <{g.sender_email}> — {g.count} emails, {size}")
    return "\n".join(lines)


_CHAT_TOOLS = [
    {
        "name": "get_inbox_overview",
        "description": "Get a live snapshot of the user's inbox: total senders, emails, reclaimable storage, and the top senders by impact. Call this whenever the user asks about the state of their inbox, what's cluttering it, or what to clean up.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_senders",
        "description": "Search the user's senders by name, email address, or domain substring. Use when the user asks about email from a specific person, company, or domain.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Name, email, or domain to search for."}
            },
            "required": ["query"],
        },
    },
    {
        "name": "draft_email",
        "description": "Draft an email in the user's voice. Returns a Subject line and body. Use when the user asks to write or reply to an email.",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "description": "What the email should accomplish."},
                "recipient_context": {
                    "type": "string",
                    "description": "Who it's to and any relevant context.",
                },
                "thread_snippet": {
                    "type": "string",
                    "description": "The message being replied to, if any.",
                },
            },
            "required": ["intent"],
        },
    },
    {
        "name": "propose_cleanup",
        "description": "Stage a cleanup of specific senders and give the user a button into the purge preview, where they confirm before anything is trashed. Use when the user agrees to clean up particular senders/domains. You do NOT delete anything — you only stage the selection and link to the confirm-first preview. Provide either explicit sender emails, or a query to match senders from the current scan, or both.",
        "input_schema": {
            "type": "object",
            "properties": {
                "senders": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Exact sender email addresses to stage for cleanup.",
                },
                "query": {
                    "type": "string",
                    "description": "Optionally match senders by name/email/domain substring instead of (or in addition to) explicit addresses.",
                },
            },
        },
    },
    {
        "name": "navigate",
        "description": "Give the user a button to jump to a postmind page. Use to guide them to where an action happens (e.g. send them to Stats to purge, Triage to classify, Sync to refresh data, Settings to enable AI).",
        "input_schema": {
            "type": "object",
            "properties": {
                "page": {
                    "type": "string",
                    "enum": [
                        "/",
                        "/agent",
                        "/stats",
                        "/triage",
                        "/drafts",
                        "/agents",
                        "/sync",
                        "/accounts",
                        "/watch",
                        "/undo",
                        "/settings",
                    ],
                },
                "label": {
                    "type": "string",
                    "description": "Short button label, e.g. 'Open Stats'.",
                },
            },
            "required": ["page", "label"],
        },
    },
]


def _chat_resolve_senders(emails: list[str], query: str, account_email: str):
    """Resolve explicit emails + an optional substring query to SenderGroups
    present in the current scan cache or local DB. Returns (groups, error_text)."""
    from postmind.core.sender_stats import fetch_sender_groups_from_db
    from postmind.core.storage import EmailRepo, get_session

    cached = _cache_get()
    if cached:
        groups = cached["groups"]
    elif account_email and EmailRepo(get_session()).get_inbox(account_email, limit=1):
        groups = fetch_sender_groups_from_db(
            account_email=account_email, scope="inbox", min_count=1, top_n=500, sort_by="score"
        )
    else:
        return [], "No scan data available — ask the user to open Stats or run a Sync first."

    wanted = {e.strip().lower() for e in (emails or []) if e.strip()}
    q = (query or "").strip().lower()
    matched = []
    for g in groups:
        em = (g.sender_email or "").lower()
        if em in wanted:
            matched.append(g)
        elif q and (q in em or q in (g.sender_name or "").lower() or q in (g.domain or "").lower()):
            matched.append(g)
    return matched, None


def _resolve_action_targets(emails: list[str], query: str, account_email: str):
    """Resolve targets for a WRITE action and apply safety filters.

    Returns ``(staged, blocked, sensitive, err)`` where:
    - ``staged``    — SenderGroups safe to act on (blocked senders removed).
    - ``blocked``   — sender emails skipped because they are on the BlocklistRepo.
    - ``sensitive`` — staged sender emails flagged "sensitive" (bank/legal/health);
                      surfaced as a warning and pre-unchecked on the confirm card.
    - ``err``       — error text if no scan data is available.

    Centralises the BlocklistRepo + risk guards so every stage_* tool and every
    confirm endpoint applies them identically (no duplicated logic).
    """
    from postmind.core.sender_stats import classify_sender_risk
    from postmind.core.storage import BlocklistRepo, get_session

    matched, err = _chat_resolve_senders(emails or [], query or "", account_email)
    if err:
        return [], [], [], err

    blocked_set = (
        BlocklistRepo(get_session()).blocked_emails(account_email) if account_email else set()
    )
    staged = []
    blocked = []
    sensitive = []
    for g in matched:
        if g.sender_email in blocked_set:
            blocked.append(g.sender_email)
            continue
        staged.append(g)
        if classify_sender_risk(g) == "sensitive":
            sensitive.append(g.sender_email)
    return staged, blocked, sensitive, None


def _enrich_targets(groups) -> list[dict]:
    """Compact per-sender summary for an action card (server-resolved targets)."""
    from postmind.core.sender_stats import classify_sender_risk

    out = []
    for g in groups:
        size = (
            f"{g.total_size_mb:.1f} MB"
            if g.total_size_mb >= 0.1
            else f"{g.total_size_bytes // 1024} KB"
        )
        out.append(
            {
                "sender_email": g.sender_email,
                "sender_name": g.display_name,
                "count": g.count,
                "size_str": size,
                "sensitive": classify_sender_risk(g) == "sensitive",
            }
        )
    return out


def _split_draft(draft: str) -> tuple[str, str]:
    """Split a composed draft ('Subject: ...\\n\\n<body>') into (subject, body)."""
    text = (draft or "").strip()
    subject = ""
    body = text
    if text.lower().startswith("subject:"):
        first, _, rest = text.partition("\n")
        subject = first.split(":", 1)[1].strip()
        body = rest.lstrip("\n")
    return subject, body


def _build_chat_system(page: str, account_email: str, ai_mode: str) -> str:
    here_info = _PAGES.get(page)
    here = f"{here_info['name']} — {here_info['desc']}" if here_info else "the app"
    overview = _chat_overview_text(account_email)
    pages = "\n".join(f"  {info['name']} — {info['desc']}" for info in _PAGES.values())
    return f"""\
You are the postmind Assistant — a friendly, concise helper embedded in postmind, a \
privacy-first email management tool that runs locally. You help the user understand and \
tidy their inbox, draft emails, and find their way around the app.

Be brief and practical. Prefer 1–3 short sentences or a tight bullet list. Quote real \
numbers from the inbox snapshot below rather than guessing. Never claim to have deleted, \
archived, or sent anything — you cannot act directly. When the user agrees to clean up \
specific senders, use `propose_cleanup` to stage them; it gives the user a button into the \
confirm-first purge preview (deletes go to Trash and are undoable) — you must never imply \
the cleanup already happened. For anything else, use `navigate` to point the user to the \
right page. When they want an email written, use `draft_email`.

Never write a URL or route path (like `/sync` or `/stats`) in your reply — those \
are not commands the user types. To send the user to a page, always call the `navigate` \
tool, which renders a clickable button; refer to pages by name ("the Sync page", "Stats"), \
never by path. If the inbox snapshot below shows no data yet, don't tell the user to "sync" \
in prose — call `navigate` to "/sync" with the label "Sync your inbox".

You are read-only and can only stage a trash cleanup. For anything that ACTS on the inbox — \
archiving, labeling, marking read, unsubscribing, sending email, or creating agents/rules — \
hand off to the Super Agent: briefly say it can do that and use `navigate` to "/agent" with \
a label like "Open Super Agent". Don't attempt those yourself.

The user is currently on: {here}.
Active account: {account_email or "none connected yet"}. AI mode: {ai_mode}.

Pages you can navigate to:
{pages}

Live inbox snapshot:
{overview}"""


@app.post("/chat")
async def chat_endpoint(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"reply": "Sorry — I couldn't read that request.", "actions": []}
    if not isinstance(body, dict):
        body = {}
    raw_messages = body.get("messages", [])
    page = (body.get("page") or "/").split("?")[0]

    # Sanitise + cap history.
    messages = [
        {"role": m["role"], "content": str(m["content"])[:4000]}
        for m in raw_messages
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content")
    ][-12:]
    if not messages:
        return {
            "reply": "Hi! Ask me about your inbox, have me draft an email, or tell me what you'd like to clean up.",
            "actions": [],
        }

    mode = _chat_mode()
    if mode == "off":
        return {
            "reply": "I'm an AI assistant, but the assistant's AI is currently off (your data stays fully local). Choose a local or cloud backend under Settings → Chat Assistant and I can help triage your inbox, draft emails, and suggest cleanups.",
            "actions": [{"label": "Open Settings", "href": "/settings"}],
        }

    account_email = _get_web_account() or ""
    actions: list[dict] = []
    engine_kwargs = _chat_engine_kwargs()

    def _run():
        from postmind.core.ai_engine import AIEngine

        ai = AIEngine(**engine_kwargs)
        system = _build_chat_system(page, account_email, mode)

        def _executor_tool(name: str, tool_input: dict) -> str:
            if name == "get_inbox_overview":
                return _chat_overview_text(account_email)
            if name == "search_senders":
                return _chat_search_senders(tool_input.get("query", ""), account_email)
            if name == "draft_email":
                from postmind.core.storage import AgentRepo, get_session

                soul = {}
                agent = (
                    AgentRepo(get_session()).get_by_email(account_email) if account_email else None
                )
                if agent:
                    soul = {
                        "voice_style": agent.voice_style,
                        "user_context": agent.user_context,
                        "writing_guidelines": agent.writing_guidelines,
                    }
                try:
                    return ai.compose_email(
                        intent=tool_input.get("intent", ""),
                        recipient_context=tool_input.get("recipient_context", ""),
                        thread_snippet=tool_input.get("thread_snippet", ""),
                        soul=soul,
                    )
                except ValueError as exc:
                    return str(exc)
            if name == "propose_cleanup":
                from urllib.parse import urlencode

                matched, err = _chat_resolve_senders(
                    tool_input.get("senders") or [], tool_input.get("query", ""), account_email
                )
                if err:
                    return err
                if not matched:
                    return "No matching senders found in the current scan — nothing staged."
                total = sum(g.count for g in matched)
                mb = sum(g.total_size_bytes for g in matched) / (1024 * 1024)
                href = "/purge/preview?" + urlencode([("senders", g.sender_email) for g in matched])
                if not any(a["href"] == href for a in actions):
                    actions.append({"label": f"Review & confirm ({total} emails)", "href": href})
                names = ", ".join(g.sender_email for g in matched[:5]) + (
                    "…" if len(matched) > 5 else ""
                )
                return (
                    f"Staged {len(matched)} sender(s) — {total} emails, ~{mb:.0f} MB ({names}). "
                    "Added a button to the purge preview; the user must confirm there before anything moves to Trash."
                )
            if name == "navigate":
                page_path = tool_input.get("page", "/")
                if page_path in _PAGES:
                    label = tool_input.get("label") or "Open page"
                    if not any(a["href"] == page_path for a in actions):
                        actions.append({"label": label, "href": page_path})
                    return f"Added a '{label}' button to the {_PAGES[page_path]['name']} page."
                return "Unknown page."
            return f"Unknown tool: {name}"

        # Cloud always supports tools; local attempts native Ollama tool-use
        # (qwen2.5/llama3.1+) and degrades to plain conversation inside
        # AIEngine.chat if the model can't. Same pattern as the Super Agent.
        return ai.chat(messages, system=system, tools=_CHAT_TOOLS, tool_executor=_executor_tool)

    try:
        loop = asyncio.get_event_loop()
        reply = await loop.run_in_executor(_executor, _run)
    except Exception as exc:
        return {"reply": f"Sorry — I hit an error: {exc}", "actions": []}

    return {"reply": reply, "actions": actions}


# ── Super Agent ────────────────────────────────────────────────────────────────


@app.get("/agent", response_class=HTMLResponse)
async def agent_page(request: Request):
    ctx = _base()
    ctx["active"] = "agent"
    # Add connected MCP server names for the header status display
    try:
        from postmind.config import load_account_config

        email = _get_web_account() or ""
        cfg = load_account_config(email) if email else {}
        ctx["mcp_server_names"] = [
            s.get("name") for s in (cfg.get("mcp_servers") or []) if s.get("name")
        ]
    except Exception:
        ctx["mcp_server_names"] = []
    return _resp(request, "agent.html", ctx)


@app.get("/agent/history")
async def agent_history(request: Request):
    """Return recent conversation turns for the active account."""
    from postmind.core.storage import AgentConversationRepo, get_session

    account_email = _get_web_account() or ""
    if not account_email:
        return {"turns": []}
    repo = AgentConversationRepo(get_session())
    rows = repo.get_recent(account_email, hours=24, limit=48)
    return {
        "turns": [
            {
                "role": r.role,
                "content": r.content,
                "actions": json.loads(r.actions_json) if r.actions_json else [],
                "cards": json.loads(r.cards_json) if r.cards_json else [],
                "ts": r.created_at,
            }
            for r in rows
        ]
    }


@app.delete("/agent/history")
async def agent_history_clear(request: Request):
    """Clear conversation history for the active account."""
    from postmind.core.storage import AgentConversationRepo, get_session

    account_email = _get_web_account() or ""
    if not account_email:
        return {"ok": True, "cleared": 0}
    cleared = AgentConversationRepo(get_session()).clear(account_email)
    return {"ok": True, "cleared": cleared}


@app.get("/agent/memory")
async def agent_memory_status(request: Request):
    """Return the memory server status and known entity count for the active account."""
    import json as _json

    from postmind.config import memory_dir_for

    account_email = _get_web_account() or ""
    if not account_email:
        return {"configured": False, "entity_count": 0, "entities": []}
    try:
        mem_dir = memory_dir_for(account_email)
        mem_file = mem_dir / "memory.json"
        if not mem_file.exists():
            return {"configured": True, "entity_count": 0, "entities": [], "file": str(mem_file)}
        data = _json.loads(mem_file.read_text())
        entities = data.get("entities") or []
        return {
            "configured": True,
            "entity_count": len(entities),
            "entities": [
                {
                    "name": e.get("name"),
                    "type": e.get("entityType"),
                    "observations": len(e.get("observations", [])),
                }
                for e in entities[:20]
            ],
            "file": str(mem_file),
        }
    except Exception as exc:
        return {"configured": False, "entity_count": 0, "error": str(exc)}


@app.get("/agent/suggestions")
async def agent_suggestions(request: Request):
    """Return context-aware example prompt chips based on current inbox state."""
    from postmind.core.sender_stats import fetch_sender_groups_from_db
    from postmind.core.storage import EmailRepo, get_session

    account_email = _get_web_account() or ""
    defaults = [
        "What's eating my storage?",
        "Find my largest emails",
        "Show me newsletters I never open",
        "Find newsletters older than 2 years and let me review them",
        "Create an agent that archives newsletters weekly",
    ]
    if not account_email:
        return {"chips": defaults}
    try:
        session = get_session()
        has_data = bool(EmailRepo(session).get_inbox(account_email, limit=1))
        if not has_data:
            return {"chips": defaults}

        groups = fetch_sender_groups_from_db(
            account_email=account_email, scope="inbox", min_count=1, top_n=50, sort_by="score"
        )
        if not groups:
            return {"chips": defaults}

        chips = []
        total_mb = sum(g.total_size_bytes for g in groups) / (1024 * 1024)

        # Chip 1: storage if significant
        if total_mb > 100:
            chips.append(f"What's eating my {total_mb:.0f} MB of storage?")
        else:
            chips.append("What's eating my storage?")

        # Chip 2: biggest sender
        top = groups[0]
        chips.append(
            f"Show me emails from {top.display_name} ({top.count} emails, {top.total_size_mb:.0f} MB)"
        )

        # Chip 3: subscription count
        from postmind.core import agent_tools as _at

        unsub_rows = _at.find_unopened_subscriptions(session, account_email, min_count=3, limit=5)
        if len(unsub_rows) >= 3:
            chips.append(f"Unsubscribe me from {len(unsub_rows)} newsletters I never open")
        else:
            chips.append("Show me newsletters I never open")

        # Chip 4: always-useful
        chips.append("Find newsletters older than 2 years and let me review them")

        # Chip 5: automation
        chips.append("Create an agent that archives newsletters weekly")

        return {"chips": chips[:5]}
    except Exception:
        return {"chips": defaults}


def _mcp_guidance_for(account_email: str) -> str:
    """Build MCP-specific usage guidance based on which servers are configured.

    Returns an empty string if no MCP servers are configured, or a multi-line
    block describing each connected server and how to use it proactively.
    """
    if not account_email:
        return ""
    try:
        from postmind.config import load_account_config

        cfg = load_account_config(account_email)
        servers = cfg.get("mcp_servers") or []
        if not servers:
            return ""
    except Exception:
        return ""

    parts = []
    server_names = {s.get("name", "") for s in servers}

    if "memory" in server_names:
        parts.append(
            "Memory (mcp_memory_*): You have persistent memory across sessions.\n"
            "- BEFORE composing any reply: call mcp_memory_search_nodes with the sender's name/email to recall prior context.\n"
            "- AFTER a useful interaction: call mcp_memory_create_entities or mcp_memory_add_observations to store:\n"
            "  sender's role/company, communication preferences, ongoing topics, action items.\n"
            "- Use mcp_memory_create_relations to link people to their organizations.\n"
            "- Example entity: {name: 'Alice Chen', type: 'Person', observations: ['VP of Eng at Acme', 'prefers bullet points', 'timezone: PST']}"
        )

    if "google-calendar" in server_names or "calendar" in server_names:
        parts.append(
            "Calendar (mcp_google-calendar_* or mcp_calendar_*): You have Google Calendar access.\n"
            "- For meeting-request emails: extract date/time/attendees, call mcp_*_create_event, draft a confirmation reply.\n"
            "- Before scheduling: call mcp_*_list_events or mcp_*_get_free_busy to check availability.\n"
            "- Proactively offer to create calendar events when an email contains a date+time+purpose."
        )

    if "linear" in server_names:
        parts.append(
            "Linear (mcp_linear_*): You can create and search Linear issues.\n"
            "- For support/bug/feature-request emails: offer to create a Linear issue with title + description extracted from the thread.\n"
            "- Use mcp_linear_search_issues to check if a duplicate already exists before creating.\n"
            "- Always show the user the issue title and description and confirm before calling mcp_linear_create_issue (it's a write)."
        )

    if "brave-search" in server_names or "search" in server_names:
        parts.append(
            "Web search (mcp_brave-search_* or mcp_search_*): You can search the web.\n"
            "- Before replying to an email from an unfamiliar company/person: call mcp_*_web_search to research them.\n"
            "- When an email references a product, news event, or technical claim: verify it with a search.\n"
            "- Use search results to enrich replies with accurate, current information."
        )

    if "hubspot" in server_names:
        parts.append(
            "HubSpot CRM (mcp_hubspot_*): You have CRM access.\n"
            "- For inbound emails: call mcp_hubspot_search_contacts to look up the sender before replying.\n"
            "- For sales/deal emails: offer to log the interaction as a CRM note (confirm first — it's a write).\n"
            "- Use CRM context (company, deal stage, last contact) to personalize replies."
        )

    if "slack" in server_names:
        parts.append(
            "Slack (mcp_slack_*): You can search and post to Slack.\n"
            "- To notify a team: summarize the email thread and offer to post it to a channel (confirm first).\n"
            "- Use mcp_slack_search_messages to find Slack context about a topic mentioned in an email.\n"
            "- For high-priority emails: offer to send a Slack DM alert (confirm before sending)."
        )

    # Any other configured servers — generic guidance
    other = server_names - {
        "memory",
        "google-calendar",
        "calendar",
        "linear",
        "brave-search",
        "search",
        "hubspot",
        "slack",
    }
    for name in sorted(other):
        parts.append(
            f"{name} (mcp_{name}_*): External tools are available. "
            "Use them proactively when relevant to the user's email task."
        )

    if not parts:
        return ""

    header = "\nConnected external tools — use these proactively:"
    return header + "\n" + "\n\n".join(parts)


def _build_agent_system(account_email: str, mode: str, brief_context: str = "") -> str:
    overview = _chat_overview_text(account_email)
    autopilot = "ON" if _autopilot_on() else "OFF"
    brief_block = ""
    if brief_context:
        brief_block = (
            "You are acting as the Super Agent embedded in the user's Daily Brief page.\n"
            f"Today's brief context:\n{brief_context}\n\n"
            "Use this context to answer questions about today's brief. When the user asks about "
            '"the emails in my brief" or "the top items", refer to the above. '
            "You still have access to all tools — use them to read threads, draft replies, and take actions.\n\n"
        )
    system_prompt = f"""\
You are the postmind Super Agent — an autonomous but careful email assistant. The user \
describes an outcome in plain English and you use tools to achieve it: analyze storage, \
search senders, find large emails, clean up the inbox, and create automation (heartbeat \
agents and rules).

Operating rules:
- Use READ tools freely to gather facts before acting. Quote real numbers.
- Tool chaining rules — follow these automatically without asking the user:
  - "what should I delete / clean up / what's wasting space / find emails to delete": ALWAYS call \
find_cleanup_candidates FIRST to show a categorized report. Pass any excluded senders the user \
mentioned (e.g. exclude_senders=["saudahmirza@gmail.com"]). Show the full report, then ask which \
category or sender they want to act on. NEVER stage a deletion before showing the report.
  - "summarize thread from X" / "summarize X's emails": call find_and_summarize_thread(search_query="from:X") directly.
  - "read/open/show thread": call find_emails_by_topic to get thread_ids first, then get_thread.
  - "summarize emails about Y": call find_and_summarize_thread(search_query="Y") directly.
  - Never ask the user to provide a message_id or thread_id — always fetch it yourself using search tools.
  - When you have a message_id from find_largest_messages, use it directly with read_email.
  - When you have a thread_id from any tool result, use it directly with get_thread or summarize_thread.
  - Prefer find_and_summarize_thread for any "summarize X's emails / thread about Y" request.
- You CANNOT delete, archive, label, mark-read, unsubscribe, send, or create anything \
directly. The WRITE tools — stage_trash, stage_archive, stage_label, stage_mark_read, \
stage_unsubscribe, send_email, create_agent, create_rule — only STAGE an action and show \
the user a confirmation card or button. NOTHING happens until the user clicks Confirm on \
that card. Never claim an action is done; say you've prepared it for their confirmation.
- draft_email writes a draft (text only, sends nothing); to actually send, follow it with \
send_email, which still requires confirmation. There is no auto-send.
- Trash, archive, label, and mark-read all go through the same confirm card and are \
undoable for 30 days. Unsubscribe is external and NOT undoable — say so; the optional \
trash of the back-catalog IS undoable.
- Protected senders are skipped automatically; sensitive senders (banks, legal, health) \
are flagged and pre-unchecked on the card.
- Be concise. After staging something, tell the user to review and confirm the card. Ask a \
brief clarifying question only when genuinely ambiguous (e.g. which account, or the label \
name).
- CRITICAL — Gmail API query rules for newsletters:
  - NEVER use 'has:list-unsubscribe' in any query — it is a web-UI-only operator that \
returns 0 results via the Gmail API. The agent has been wrong when it uses this.
  - For newsletters/subscriptions/promotions use the broad category query: \
'(category:promotions OR category:updates OR category:forums)'. This is more comprehensive \
than 'category:promotions' alone — many newsletters land in Updates or Forums tabs.
  - Correct: stage_trash_query(gmail_query="(category:promotions OR category:updates OR category:forums) older_than:3y") \
  - Wrong: stage_trash_query(gmail_query="has:list-unsubscribe older_than:3y") \
  - Note: stage_trash_query auto-rewrites has:list-unsubscribe for you, but always use the category form directly.
  - For "newsletters I never opened": call find_unopened_subscriptions (local DB, high unread \
ratio senders), then offer stage_trash or stage_unsubscribe on those senders.
  - For "find large marketing emails": use 'category:promotions' or 'has:attachment larger:5M'.
- When stage_trash_query returns nothing: do NOT give up. Try a shorter time window \
(e.g. older_than:1y if 3y returned nothing), or widen the query. stage_trash_query \
searches live Gmail — it does NOT depend on local sync. Only tell the user nothing was \
found after trying at least one fallback with a shorter window.
- When the user refers to "the Nth one" or "that email" from a prior search result, use \
the message_id from that prior result directly with read_email or stage_trash. NEVER \
re-run find_largest_messages or any search tool to re-locate a previously listed email.
- AUTOPILOT is currently {autopilot}. When ON, stage_archive/stage_label/stage_mark_read \
execute immediately (no card) because they are fully reversible — tell the user what you \
did and that it's undoable. Trash, unsubscribe, and send ALWAYS require a confirm card even \
under autopilot.
- Never write a URL or route path (like `/sync` or `/stats`) in your reply — refer to \
pages by name ("the Sync page", "Stats"). Those are not commands the user types.

Active account: {account_email or "none connected yet"}. AI mode: {mode}.

Live inbox snapshot:
{overview}"""
    mcp_block = _mcp_guidance_for(account_email)
    if mcp_block:
        system_prompt += "\n" + mcp_block
    return brief_block + system_prompt


_DEEP_TASK_RE = re.compile(
    r"""
    \b(every|all|each)\b.{0,40}\b(thread|email|sender|vendor)\b
  | \bfind.{0,60}(and|then).{0,60}(draft|write|send|reply)\b
  | \bfor\s+each\b
  | \b(silent|quiet|no\s+response|no\s+reply).{0,60}(follow[- ]?up|draft)\b
  | \bscan\s+(my|the)\s+(inbox|email).{0,40}(and|then)\b
  | \bsummariz\w+.{0,40}all\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _is_deep_task(messages: list[dict]) -> bool:
    """Return True if the last user message looks like a long multi-step task."""
    user_msgs = [m["content"] for m in messages if m["role"] == "user"]
    return bool(user_msgs and _DEEP_TASK_RE.search(user_msgs[-1]))


def _parse_agent_messages(body) -> list[dict]:
    """Extract and clamp the chat history from a request body."""
    if not isinstance(body, dict):
        body = {}
    raw_messages = body.get("messages", [])
    return [
        {"role": m["role"], "content": str(m["content"])[:4000]}
        for m in raw_messages
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content")
    ][-12:]


def _agent_mode_guidance(mode: str) -> dict | None:
    """For ``off`` mode, return the guidance payload to return as-is.

    ``None`` means proceed with the agent loop. Both cloud and local proceed —
    local attempts Ollama native tool-use and degrades to plain conversation if
    the model can't (handled inside ``AIEngine.chat``). Only ``off`` is blocked."""
    if mode == "off":
        return {
            "reply": "The assistant's AI is off (your data stays fully local). Choose a local or cloud backend under Settings → Chat Assistant to use the Super Agent.",
            "actions": [{"label": "Open Settings", "href": "/settings"}],
            "cards": [],
        }
    return None


def _agent_tools_for(account_email: str) -> list[dict]:
    """Return the tool list for the Super Agent: base tools + power tools + MCP tools."""
    from postmind.core import agent_tools

    tools = list(agent_tools.ALL_TOOLS)
    if get_settings().agent_power_mode:
        tools.append(agent_tools.RUN_SQL_TOOL)
    tools += (_mcp_pools.get(account_email) or _NullPool()).get_tools()
    return tools


def _build_agent_tool_executor(account_email: str, ai, actions: list[dict], cards: list[dict]):
    """Build the Super Agent tool-executor closure.

    Shared by the non-streaming ``POST /agent`` and the streaming
    ``POST /agent/stream``. READ tools return a summary string; WRITE tools append
    a staged confirm card (``cards``) or deep-link action (``actions``) — both
    accumulators are mutated in place — and return a summary string for the model.
    """
    from urllib.parse import urlencode

    from postmind.core import agent_tools

    def _executor_tool(name: str, tool_input: dict) -> str:
        from postmind.core.agent_service import AgentService

        # Build once per executor call; provider + groups are lazy so this is cheap.
        svc = AgentService(account_email=account_email, ai=ai)
        try:
            svc._provider = _build_provider()
        except Exception:
            pass  # provider will be built lazily by svc if needed

        if name == "get_inbox_overview":
            return svc.inbox_overview()
        if name == "search_senders":
            return svc.search_senders(tool_input.get("query", ""))
        if name == "analyze_storage":
            cached = _cache_get()
            if cached:
                svc._groups_cache = cached["groups"]
            return svc.analyze_storage(
                tool_input.get("group_by", "sender"),
                int(tool_input.get("top_n", 10) or 10),
            )
        if name == "find_largest_messages":
            try:
                return svc.find_largest_messages(
                    tool_input.get("query", ""),
                    int(tool_input.get("limit", 10) or 10),
                )
            except Exception as exc:
                return f"Couldn't fetch message sizes: {exc}"
        if name == "read_email":
            try:
                return svc.read_email(tool_input.get("message_id", ""))
            except Exception as exc:
                return f"Couldn't fetch email: {exc}"
        if name == "get_thread":
            try:
                return svc.get_thread(tool_input.get("thread_id", ""))
            except Exception as exc:
                return f"Couldn't fetch thread: {exc}"
        if name == "find_emails_by_topic":
            try:
                return svc.find_emails_by_topic(
                    tool_input.get("topic", ""),
                    int(tool_input.get("limit", 10) or 10),
                )
            except Exception as exc:
                return f"Search failed: {exc}"
        if name == "summarize_thread":
            try:
                return svc.summarize_thread(tool_input.get("thread_id", ""))
            except Exception as exc:
                return f"Couldn't summarize thread: {exc}"
        if name == "find_and_summarize_thread":
            try:
                return svc.find_and_summarize_thread(
                    tool_input.get("search_query", ""),
                    int(tool_input.get("result_index", 0) or 0),
                )
            except Exception as exc:
                return f"Couldn't find and summarize: {exc}"
        if name == "find_unopened_subscriptions":
            return svc.find_unopened_subscriptions(
                int(tool_input.get("min_count", 3) or 3),
                int(tool_input.get("limit", 15) or 15),
            )
        if name == "list_automation":
            return svc.list_automation()
        if name == "find_cleanup_candidates":
            return svc.find_cleanup_candidates(
                exclude_senders=tool_input.get("exclude_senders") or [],
                top_n=int(tool_input.get("top_n", 8) or 8),
            )
        if name == "run_sql":
            return svc.run_sql(tool_input.get("query", ""))
        if name == "stage_trash":
            matched, err = _chat_resolve_senders(
                tool_input.get("senders") or [], tool_input.get("query", ""), account_email
            )
            if err:
                return err
            if not matched:
                return "No matching senders found in the current scan — nothing staged."
            sender_emails = [g.sender_email for g in matched]
            description = (
                sender_emails[0] if len(sender_emails) == 1 else f"{len(sender_emails)} senders"
            )
            try:
                provider = _build_provider()
            except Exception as exc:
                return f"Couldn't reach the mailbox: {exc}"
            if not provider.supports("labels"):
                # IMAP: no per-email review drawer — fall back to purge preview link.
                total = sum(g.count for g in matched)
                mb = sum(g.total_size_bytes for g in matched) / (1024 * 1024)
                href = "/purge/preview?" + urlencode([("senders", g.sender_email) for g in matched])
                if not any(a.get("href") == href for a in actions):
                    actions.append({"label": f"Review & confirm ({total} emails)", "href": href})
                names = ", ".join(sender_emails[:5]) + ("…" if len(matched) > 5 else "")
                return f"Staged {len(matched)} sender(s) — {total} emails, ~{mb:.0f} MB ({names}). Open the Review link to confirm before anything moves to Trash."
            # Gmail: resolve individual email IDs so the review drawer can show them.
            from_parts = " OR ".join(f"from:{e}" for e in sender_emails[:25])
            gmail_query = f"in:inbox ({from_parts})"
            try:
                emails = agent_tools.resolve_trash_query(provider, gmail_query, limit=300)
            except Exception as exc:
                return f"Couldn't resolve emails for those senders: {exc}"
            if not emails:
                return "No inbox emails found for those senders."
            token = _review_put(account_email, description, emails)
            sender_count = len({e["sender_email"] for e in emails})
            cards.append(
                {
                    "type": "trash_review",
                    "title": f"Review: {description}",
                    "fields": {
                        "token": token,
                        "total_count": len(emails),
                        "sender_count": sender_count,
                        "description": description,
                    },
                }
            )
            return (
                f"Staged {len(emails)} emails from {sender_count} sender(s) "
                f"for review. The user opens the review drawer and approves before anything moves to Trash."
            )
        if name == "stage_trash_query":
            gmail_query = (tool_input.get("gmail_query") or "").strip()
            description = (
                tool_input.get("description") or gmail_query or "matching emails"
            ).strip()
            newsletters_only = bool(tool_input.get("newsletters_only"))
            if not gmail_query:
                return "I need a search query (e.g. 'older_than:2y') to find emails to trash."
            try:
                provider = _build_provider()
            except Exception as exc:
                return f"Couldn't reach the mailbox to resolve that query: {exc}"
            if not provider.supports("labels"):
                return (
                    "Email-level trash review is Gmail-only right now. "
                    "For other accounts, name the senders and I'll stage a sender-level trash instead."
                )
            try:
                emails = agent_tools.resolve_trash_query(
                    provider, gmail_query, newsletters_only, limit=200
                )
            except Exception as exc:
                return f"Couldn't resolve that query: {exc}"
            if not emails:
                # If newsletters_only filtered everything, check whether the raw query
                # returns anything so we can give the agent actionable feedback.
                if newsletters_only:
                    try:
                        broader = agent_tools.resolve_trash_query(
                            provider, gmail_query, newsletters_only=False, limit=10
                        )
                    except Exception:
                        broader = []
                    if broader:
                        return (
                            f"The query '{gmail_query}' returned {len(broader)} email(s) but none "
                            f"carried a List-Unsubscribe header, so the newsletter filter removed them all. "
                            f"Try again with newsletters_only=false to review all matching emails, "
                            f"or pick a shorter time window (e.g. older_than:1y)."
                        )
                return (
                    f"Nothing matched '{gmail_query}' in Gmail. "
                    f"Suggestions: broaden the time window (e.g. older_than:1y instead of 2y), "
                    f"remove date filters to check if any matching emails exist at all, "
                    f"or ask the user to confirm they have emails that old in their account."
                )
            token = _review_put(account_email, description, emails)
            sender_count = len({e["sender_email"] for e in emails})
            cards.append(
                {
                    "type": "trash_review",
                    "title": f"Review: {description}",
                    "fields": {
                        "token": token,
                        "total_count": len(emails),
                        "sender_count": sender_count,
                        "description": description,
                    },
                }
            )
            return (
                f"Staged {len(emails)} emails from {sender_count} sender(s) matching '{gmail_query}' "
                f"for review. The user opens the review drawer and approves before anything moves to Trash."
            )
        if name in ("stage_archive", "stage_label", "stage_mark_read"):
            action = {
                "stage_archive": "archive",
                "stage_label": "label",
                "stage_mark_read": "mark_read",
            }[name]
            provider = None
            try:
                provider = _build_provider()
            except Exception:
                provider = None
            if provider is not None and not provider.supports("labels"):
                return f"This account's provider does not support {action.replace('_', ' ')} — only Gmail does. Try trashing instead."
            label_name = (tool_input.get("label_name") or "").strip()
            if action == "label" and not label_name:
                return "A label name is required to stage a label action."
            # Autopilot path is web-specific — resolve targets inline so autopilot
            # can call _execute_reversible_action directly (which is also web-specific).
            if _autopilot_on() and action in _AUTOPILOT_ACTIONS:
                staged, blocked, sensitive, err = _resolve_action_targets(
                    tool_input.get("senders") or [], tool_input.get("query", ""), account_email
                )
                if err:
                    return err
                if not staged:
                    extra = f" ({len(blocked)} protected sender(s) skipped)" if blocked else ""
                    return f"No matching senders to {action.replace('_', ' ')}{extra} — nothing staged."
                verb = action.replace("_", " ")
                title = {
                    "archive": "Archive emails",
                    "label": f"Label emails “{label_name}”",
                    "mark_read": "Mark emails as read",
                }[action]
                # Autopilot: auto-execute reversible actions without a confirm card
                # (opt-in, off by default; trash/unsubscribe/send never qualify). Even
                # under autopilot, SENSITIVE senders (bank/legal/health) keep the human
                # gate — they're routed to a confirm card, never auto-executed.
                sset = set(sensitive)
                auto = [g for g in staged if g.sender_email not in sset]
                held = [g for g in staged if g.sender_email in sset]
                parts = []
                if auto:
                    try:
                        _undo_id, count = _execute_reversible_action(
                            account_email, action, auto, label_name
                        )
                    except Exception as exc:
                        return f"Couldn't {verb}: {exc}"
                    if not any(a.get("href") == "/undo" for a in actions):
                        actions.append({"label": "Undo", "href": "/undo"})
                    parts.append(
                        f"Autopilot: {verb}d {count} emails from {len(auto)} sender(s); reversible from Undo for 30 days."
                    )
                if held:
                    cards.append(
                        {
                            "type": "bulk_action",
                            "title": title,
                            "fields": {
                                "action": action,
                                "label_name": label_name,
                                "targets": _enrich_targets(held),
                                "total_count": sum(g.count for g in held),
                                "blocked": blocked,
                                "sensitive": sensitive,
                                "undoable": True,
                            },
                        }
                    )
                    parts.append(
                        f"{len(held)} sensitive sender(s) (bank/legal/health) need your confirmation — shown as a card."
                    )
                if blocked:
                    parts.append(f"{len(blocked)} protected sender(s) skipped.")
                return " ".join(parts) or "Nothing to do."
            else:
                # Non-autopilot: delegate staging to AgentService for target resolution,
                # blocklist checks, and sensitive-sender flagging.
                from postmind.core.agent_service import AgentService

                svc = AgentService(account_email=account_email, ai=ai)
                cached = _cache_get()
                if cached:
                    svc._groups_cache = cached.get("groups", [])
                if provider is not None:
                    svc._provider = provider
                result = svc.stage_cleanup(
                    action,
                    tool_input.get("senders") or [],
                    tool_input.get("query", ""),
                    label_name,
                )
                if "error" in result:
                    return result["error"]
                # Build enriched targets from the scan cache for the confirm card.
                # The confirm endpoint re-resolves server-side from sender emails, so
                # the card only needs sender_email checkboxes plus display metadata.
                params = result.get("params", {})
                blocked = params.get("blocked", [])
                sensitive_set = set(params.get("sensitive", []))
                groups_lookup: dict[str, object] = {}
                if cached:
                    for g in cached.get("groups", []):
                        groups_lookup[(g.sender_email or "").lower()] = g
                targets = []
                for em in result.get("senders", []):
                    g = groups_lookup.get(em.lower())
                    if g is not None:
                        targets.append(
                            {
                                "sender_email": g.sender_email,
                                "sender_name": g.display_name,
                                "count": g.count,
                                "size_str": (
                                    f"{g.total_size_mb:.1f} MB"
                                    if g.total_size_mb >= 0.1
                                    else f"{g.total_size_bytes // 1024} KB"
                                ),
                                "sensitive": em in sensitive_set,
                            }
                        )
                    else:
                        targets.append(
                            {
                                "sender_email": em,
                                "sender_name": em,
                                "count": 0,
                                "size_str": "—",
                                "sensitive": em in sensitive_set,
                            }
                        )
                title = {
                    "archive": "Archive emails",
                    "label": f"Label emails “{label_name}”",
                    "mark_read": "Mark emails as read",
                }[action]
                staged_emails = result.get("senders", [])
                cards.append(
                    {
                        "type": "bulk_action",
                        "title": title,
                        "fields": {
                            "token": result["token"],
                            "action": action,
                            "label_name": label_name,
                            "targets": targets,
                            "total_count": result.get("email_count", 0),
                            "blocked": blocked,
                            "sensitive": params.get("sensitive", []),
                            "undoable": result.get("undoable", True),
                        },
                    }
                )
                note = f" ({len(blocked)} protected skipped)" if blocked else ""
                verb = action.replace("_", " ")
                return (
                    f"Staged a {verb} of {len(staged_emails)} sender(s), "
                    f"{result.get('email_count', 0)} emails{note}. "
                    f"Showed a confirmation card; nothing happens until the user confirms. "
                    f"Reversible for 30 days."
                )
        if name == "stage_unsubscribe":
            from postmind.core.agent_service import AgentService

            svc = AgentService(account_email=account_email, ai=ai)
            cached = _cache_get()
            if cached:
                svc._groups_cache = cached.get("groups", [])
            try:
                svc._provider = _build_provider()
            except Exception:
                pass
            result = svc.stage_unsubscribe(
                tool_input.get("senders") or [],
                tool_input.get("query", ""),
                bool(tool_input.get("also_trash", False)),
            )
            if "error" in result:
                return result["error"]
            # Build enriched targets from the scan cache for the confirm card.
            params = result.get("params", {})
            blocked = params.get("blocked", [])
            sensitive_set = set(params.get("sensitive", []))
            groups_lookup: dict[str, object] = {}
            if cached:
                for g in cached.get("groups", []):
                    groups_lookup[(g.sender_email or "").lower()] = g
            targets = []
            for em in result.get("senders", []):
                g = groups_lookup.get(em.lower())
                if g is not None:
                    targets.append(
                        {
                            "sender_email": g.sender_email,
                            "sender_name": g.display_name,
                            "count": g.count,
                            "size_str": (
                                f"{g.total_size_mb:.1f} MB"
                                if g.total_size_mb >= 0.1
                                else f"{g.total_size_bytes // 1024} KB"
                            ),
                            "sensitive": em in sensitive_set,
                        }
                    )
                else:
                    targets.append(
                        {
                            "sender_email": em,
                            "sender_name": em,
                            "count": 0,
                            "size_str": "—",
                            "sensitive": em in sensitive_set,
                        }
                    )
            cards.append(
                {
                    "type": "unsubscribe",
                    "title": "Unsubscribe from senders",
                    "fields": {
                        "token": result["token"],
                        "targets": targets,
                        "total_count": result.get("email_count", 0),
                        "blocked": blocked,
                        "sensitive": params.get("sensitive", []),
                    },
                }
            )
            sender_count = len(result.get("senders", []))
            note = f" ({len(blocked)} protected skipped)" if blocked else ""
            return (
                f"Staged unsubscribe from {sender_count} sender(s){note}. "
                f"Showed a confirmation card. Unsubscribe is external and NOT undoable; "
                f"trashing the back-catalog is optional and undoable. "
                f"Nothing happens until the user confirms."
            )
        if name == "draft_email":
            from postmind.core.storage import AgentRepo, get_session

            soul = {}
            agent = AgentRepo(get_session()).get_by_email(account_email) if account_email else None
            if agent:
                soul = {
                    "voice_style": agent.voice_style,
                    "user_context": agent.user_context,
                    "writing_guidelines": agent.writing_guidelines,
                }
            try:
                draft = ai.compose_email(
                    intent=tool_input.get("intent", ""),
                    recipient_context=tool_input.get("recipient_context", ""),
                    thread_snippet=tool_input.get("thread_snippet", ""),
                    soul=soul,
                )
            except ValueError as exc:
                return str(exc)
            subject, body = _split_draft(draft)
            cards.append(
                {
                    "type": "send_email",
                    "title": "Review draft & send",
                    "fields": {
                        "to": (tool_input.get("to") or "").strip(),
                        "subject": subject,
                        "body": body,
                    },
                }
            )
            return f"Drafted the email and showed an editable card. Subject: {subject}. The user can edit and must click Send — nothing is sent automatically."
        if name == "send_email":
            from postmind.core.agent_service import AgentService

            svc = AgentService(account_email=account_email, ai=ai)
            result = svc.stage_send(
                tool_input.get("to", ""),
                tool_input.get("subject", ""),
                tool_input.get("body", ""),
            )
            if "error" in result:
                return result["error"]
            p = result.get("params", {})
            cards.append(
                {
                    "type": "send_email",
                    "title": "Review & send",
                    "fields": {
                        "token": result["token"],
                        "to": p.get("to", ""),
                        "subject": p.get("subject", ""),
                        "body": p.get("body", ""),
                    },
                }
            )
            return f"Staged sending to {p.get('to', '')}. The user must confirm — nothing is sent until they click Send."
        if name == "create_agent":
            from postmind.core.agent_service import AgentService

            svc = AgentService(account_email=account_email, ai=ai)
            result = svc.stage_create_agent(
                email=tool_input.get("email", ""),
                name=tool_input.get("name", ""),
                interval_minutes=int(tool_input.get("interval_minutes", 30) or 30),
                voice_style=tool_input.get("voice_style", ""),
                user_context=tool_input.get("user_context", ""),
                run_rules=bool(tool_input.get("run_rules", True)),
                run_followups=bool(tool_input.get("run_followups", True)),
                run_avoidance=bool(tool_input.get("run_avoidance", False)),
            )
            if "error" in result:
                return result["error"]
            p = result.get("params", {})
            cards.append(
                {
                    "type": "create_agent",
                    "title": "Create heartbeat agent",
                    "fields": {
                        "token": result["token"],
                        "email": p.get("email", ""),
                        "name": p.get("name", ""),
                        "interval_minutes": p.get("interval_minutes", 30),
                        "voice_style": p.get("voice_style", ""),
                        "user_context": p.get("user_context", ""),
                        "run_rules": p.get("run_rules", True),
                        "run_followups": p.get("run_followups", True),
                        "run_avoidance": p.get("run_avoidance", False),
                    },
                }
            )
            return (
                f"Staged a heartbeat agent for {p.get('email', '')} "
                f"(every {p.get('interval_minutes', 30)}m). Showed the user a confirmation card."
            )
        if name == "create_rule":
            from postmind.core.agent_service import AgentService

            svc = AgentService(account_email=account_email, ai=ai)
            result = svc.stage_create_rule(tool_input.get("natural_language", ""))
            if "error" in result:
                return result["error"]
            p = result.get("params", {})
            cards.append(
                {
                    "type": "create_rule",
                    "title": "Create rule",
                    "fields": {
                        "token": result["token"],
                        "natural_language": p.get("natural_language", ""),
                        "gmail_query": p.get("gmail_query", ""),
                        "action": p.get("action", ""),
                        "explanation": p.get("explanation", ""),
                        "warnings": p.get("warnings", []),
                    },
                }
            )
            return f"Staged rule: {p.get('explanation', p.get('natural_language', ''))}. The user must confirm."
        if name == "run_sql":
            from postmind.core.agent_service import AgentService

            svc = AgentService(account_email=account_email)
            return svc.run_sql(tool_input.get("query", ""))
        # MCP consumer — route to an external server
        if name.startswith("mcp_") and _main_event_loop is not None:
            pool = _mcp_pools.get(account_email)
            if pool is not None:
                return pool.dispatch_sync(name, tool_input, _main_event_loop)
            return f"No MCP pool available for '{name}'."
        return f"Unknown tool: {name}"

    return _executor_tool


@app.post("/agent")
async def agent_endpoint(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"reply": "Sorry — I couldn't read that request.", "actions": [], "cards": []}
    messages = _parse_agent_messages(body)
    if not messages:
        return {
            "reply": "Tell me what you'd like to do — e.g. “what's eating my storage?”, “delete everything from blah.com”, or “create an agent that archives newsletters weekly.”",
            "actions": [],
            "cards": [],
        }

    mode = _chat_mode()
    guidance = _agent_mode_guidance(mode)
    if guidance is not None:
        return guidance

    account_email = _get_web_account() or ""
    actions: list[dict] = []
    cards: list[dict] = []
    engine_kwargs = _chat_engine_kwargs()

    # Lazy-build MCP pool for this account (no-op if no mcp_servers configured)
    if account_email and account_email not in _mcp_pools:
        try:
            _mcp_pools[account_email] = await _get_mcp_pool(account_email)
        except Exception:
            _mcp_pools[account_email] = None

    def _run():
        from postmind.core.ai_engine import AIEngine

        kwargs = dict(engine_kwargs)
        settings = get_settings()
        if mode == "cloud" and settings.extended_thinking:
            kwargs["thinking_budget"] = settings.thinking_budget_tokens
        ai = AIEngine(**kwargs)
        system = _build_agent_system(account_email, mode)
        executor_tool = _build_agent_tool_executor(account_email, ai, actions, cards)
        return ai.chat(
            messages,
            system=system,
            tools=_agent_tools_for(account_email),
            tool_executor=executor_tool,
            max_tool_iterations=12,
        )

    try:
        loop = asyncio.get_event_loop()
        reply = await loop.run_in_executor(_executor, _run)
    except Exception as exc:
        return {"reply": f"Sorry — I hit an error: {exc}", "actions": [], "cards": []}

    # Best-effort history save — never breaks the main response.
    try:
        import secrets as _secrets

        from postmind.core.storage import AgentConversationRepo, get_session

        _hist_repo = AgentConversationRepo(get_session())
        _session_id = body.get("session_id") or _secrets.token_hex(8)
        _last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        if _last_user:
            _hist_repo.save_turn(account_email, _session_id, "user", _last_user)
        if reply:
            _hist_repo.save_turn(
                account_email, _session_id, "assistant", reply, actions=actions, cards=cards
            )
    except Exception:
        pass

    return {"reply": reply, "actions": actions, "cards": cards}


def _sse(payload: dict) -> str:
    """Format a structured event as one SSE message."""
    return "data: " + json.dumps(payload) + "\n\n"


@app.post("/agent/stream")
async def agent_stream_endpoint(request: Request):
    """Streaming sibling of ``/agent`` — Server-Sent Events.

    Emits the same event protocol that ``AIEngine.chat_stream`` yields
    (``text_delta`` / ``tool_start`` / ``tool_result``), then a final ``final``
    event carrying the accumulated ``actions`` and ``cards`` so the client can
    render confirm cards exactly like the non-streaming path, then ``done``.

    Off/local modes emit a single ``guidance`` event mirroring the non-streaming
    JSON, then ``done``.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    messages = _parse_agent_messages(body)
    mode = _chat_mode()

    async def _empty_stream(payload: dict):
        yield _sse({"type": "guidance", **payload})
        yield _sse({"type": "done"})

    if not messages:
        return StreamingResponse(
            _empty_stream(
                {
                    "reply": "Tell me what you'd like to do — e.g. “what's eating my storage?”, “delete everything from blah.com”, or “create an agent that archives newsletters weekly.”",
                    "actions": [],
                    "cards": [],
                }
            ),
            media_type="text/event-stream",
        )

    guidance = _agent_mode_guidance(mode)
    if guidance is not None:
        return StreamingResponse(_empty_stream(guidance), media_type="text/event-stream")

    account_email = _get_web_account() or ""
    actions: list[dict] = []
    cards: list[dict] = []
    engine_kwargs = _chat_engine_kwargs()

    # Lazy-build MCP pool for this account (no-op if no mcp_servers configured)
    if account_email and account_email not in _mcp_pools:
        try:
            _mcp_pools[account_email] = await _get_mcp_pool(account_email)
        except Exception:
            _mcp_pools[account_email] = None

    # Bridge the sync chat_stream generator (run in the thread pool) to the async
    # SSE response via a queue. A sentinel marks completion.
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    _SENTINEL = object()

    def _produce():
        from postmind.core.ai_engine import AIEngine

        def _put(item):
            loop.call_soon_threadsafe(queue.put_nowait, item)

        try:
            ai = AIEngine(**engine_kwargs)
            system = _build_agent_system(account_email, mode)
            if body.get("brief_mode"):
                try:
                    import json as _json

                    from postmind.core.daily_brief import DailyBriefGenerator

                    brief = DailyBriefGenerator(account_email).get_or_generate(force=False)
                    if brief:
                        items = []
                        if brief.items_json:
                            items = _json.loads(brief.items_json)
                        brief_ctx_lines = [
                            f"Today's brief: {brief.unread_count} unread, {brief.high_priority_count} high priority."
                        ]
                        for item in items[:15]:
                            brief_ctx_lines.append(
                                f"- {item.get('priority', '?').upper()} | {item.get('sender', '')} | "
                                f"{item.get('subject', '')[:60]} | action: {item.get('suggested_action', '')} | "
                                f"gmail_id: {item.get('gmail_id', '')}"
                            )
                        brief_ctx = "\n".join(brief_ctx_lines)
                        system = _build_agent_system(account_email, mode, brief_context=brief_ctx)
                except Exception:
                    pass  # best-effort; system prompt already built without brief
            executor_tool = _build_agent_tool_executor(account_email, ai, actions, cards)

            # Determine whether to use the deep task path (higher iteration/token ceilings).
            settings = get_settings()
            deep_mode = settings.deep_task_mode  # "cloud" | "local" | "off"
            use_deep = (deep_mode != "off") and (body.get("deep") or _is_deep_task(messages))

            if mode == "cloud" and use_deep and deep_mode == "cloud":
                deep_kwargs = dict(engine_kwargs)
                if settings.deep_task_model:
                    deep_kwargs["cloud_model"] = settings.deep_task_model
                # Extended thinking: pass the configured budget so the deep
                # engine automatically upgrades to 16k for this path.
                if settings.extended_thinking:
                    deep_kwargs["thinking_budget"] = settings.thinking_budget_tokens
                ai_deep = AIEngine(**deep_kwargs)
                executor_deep = _build_agent_tool_executor(account_email, ai_deep, actions, cards)
                if not settings.extended_thinking:
                    # Static "working…" hint only when thinking isn't streaming live
                    _put(
                        {
                            "type": "thinking",
                            "text": "Working on it — this may take a moment for a complex task.",
                        }
                    )
                stream_iter = ai_deep.chat_stream_deep(
                    messages,
                    system=system,
                    tools=_agent_tools_for(account_email),
                    tool_executor=executor_deep,
                )
                for event in stream_iter:
                    etype = event.get("type")
                    if etype == "tool_start":
                        _put({"type": "tool_start", "name": event.get("name", "")})
                    else:
                        _put(event)
            elif mode == "cloud":
                for event in ai.chat_stream(
                    messages,
                    system=system,
                    tools=_agent_tools_for(account_email),
                    tool_executor=executor_tool,
                    max_tool_iterations=12,
                ):
                    # tool_start carries raw tool input; the client only needs the name.
                    if event.get("type") == "tool_start":
                        _put({"type": "tool_start", "name": event.get("name", "")})
                    else:
                        _put(event)
            elif use_deep and deep_mode == "local":
                # Deep local: non-streaming Ollama with higher iteration ceiling.
                deep_ollama_kwargs = {
                    "mode": "local",
                    "ollama_model": settings.deep_task_model
                    or engine_kwargs.get("ollama_model", ""),
                }
                ai_deep = AIEngine(**deep_ollama_kwargs)
                executor_deep = _build_agent_tool_executor(account_email, ai_deep, actions, cards)
                _put(
                    {
                        "type": "thinking",
                        "text": "Working on it — running locally, may take a moment.",
                    }
                )
                reply = ai_deep.chat(
                    messages,
                    system=system,
                    tools=agent_tools.ALL_TOOLS,
                    tool_executor=executor_deep,
                    max_tool_iterations=30,
                )
                _put({"type": "text_delta", "text": reply})
                _put({"type": "done"})
            else:
                # Local: no token streaming (Ollama tool-use isn't reliably
                # streamable). Run the non-streaming loop and emit the reply once.
                reply = ai.chat(
                    messages,
                    system=system,
                    tools=_agent_tools_for(account_email),
                    tool_executor=executor_tool,
                    max_tool_iterations=12,
                )
                _put({"type": "text_delta", "text": reply})
                _put({"type": "done"})
        except Exception as exc:
            _put({"type": "text_delta", "text": f"Sorry — I hit an error: {exc}"})
            _put({"type": "done"})
        finally:
            _put(_SENTINEL)

    async def _event_stream():
        future = loop.run_in_executor(_executor, _produce)
        try:
            while True:
                item = await queue.get()
                if item is _SENTINEL:
                    break
                if await request.is_disconnected():
                    break
                yield _sse(item)
            # Always emit final so cards/actions reach the client even if a
            # transient disconnect was detected mid-stream (false positives from
            # FastAPI's is_disconnected() would otherwise swallow the review card).
            try:
                yield _sse({"type": "final", "actions": actions, "cards": cards})
                yield _sse({"type": "done"})
            except Exception:
                pass
            # Best-effort: save the user turn to server-side history.
            try:
                import secrets as _secrets

                from postmind.core.storage import AgentConversationRepo, get_session

                _session_id = body.get("session_id") or _secrets.token_hex(8)
                _last_user = next(
                    (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
                )
                if _last_user:
                    AgentConversationRepo(get_session()).save_turn(
                        account_email, _session_id, "user", _last_user
                    )
            except Exception:
                pass
        finally:
            # On client disconnect we may break before the sentinel; ensure the
            # producer task is awaited so its thread isn't orphaned.
            if not future.done():
                try:
                    await asyncio.wrap_future(future)
                except Exception:
                    pass

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/agent/create-agent")
async def agent_create_agent(request: Request):
    """Confirm endpoint for the create_agent card — actually creates the agent."""
    form = await request.form()
    email = (form.get("email") or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="Email required")
    from postmind.core.storage import AgentRepo, get_session

    session = get_session()
    repo = AgentRepo(session)
    repo.register(
        email,
        (form.get("name") or email.split("@")[0].title()).strip(),
        max(1, min(1440, int(form.get("interval_minutes") or 30))),
    )
    repo.update_soul(
        account_email=email,
        voice_style=(form.get("voice_style") or "").strip() or None,
        user_context=(form.get("user_context") or "").strip() or None,
        writing_guidelines=None,
    )
    repo.update_features(
        account_email=email,
        run_rules=form.get("run_rules") == "on",
        run_followups=form.get("run_followups") == "on",
        run_avoidance=form.get("run_avoidance") == "on",
        run_daily_brief=form.get("run_daily_brief") == "on",
    )
    return RedirectResponse("/agents", status_code=303)


@app.post("/agent/create-rule")
async def agent_create_rule(request: Request):
    """Confirm endpoint for the create_rule card — actually creates the rule."""
    form = await request.form()
    nl = (form.get("natural_language") or "").strip()
    if not nl:
        raise HTTPException(status_code=400, detail="Rule text required")

    def _create():
        from postmind.core.bulk_engine import BulkEngine

        client = _build_provider()
        account_email = client.get_email_address()
        engine = BulkEngine(client, account_email)
        return engine.create_rule(nl)

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(_executor, _create)
    except Exception as exc:
        return _resp(request, "error.html", {"error": f"Couldn't create rule: {exc}"})
    return RedirectResponse("/agents", status_code=303)


# ── Generalized reversible bulk actions (archive / label / mark_read) ──────────

_AGENT_ACTIONS = {"archive", "label", "mark_read"}


def _render_action_preview(
    request: Request, action: str, senders: list[str], label_name: str = ""
) -> HTMLResponse:
    """Confirm-first preview for a reversible bulk action. Re-resolves the target
    set server-side from the scan cache — model text is never trusted for targets."""
    if action not in _AGENT_ACTIONS:
        return _resp(request, "error.html", {"error": f"Unknown action '{action}'."})
    if not senders:
        return RedirectResponse("/agent", status_code=303)

    account_email = _get_web_account() or ""
    staged, blocked, sensitive, err = _resolve_action_targets(senders, "", account_email)
    if err:
        return _resp(request, "error.html", {"error": err})
    if not staged:
        return _resp(
            request,
            "error.html",
            {
                "error": "None of those senders are in the current scan (or all are protected). Re-run Stats and try again."
            },
        )

    total_count = sum(g.count for g in staged)
    total_mb = round(sum(g.total_size_bytes for g in staged) / (1024 * 1024), 1)
    ctx = _base()
    ctx.update(
        {
            "active": "agent",
            "action": action,
            "label_name": label_name,
            "selected": _enrich_targets(staged),
            "senders": [g.sender_email for g in staged],
            "total_count": total_count,
            "total_mb": total_mb,
            "blocked": blocked,
            "sensitive": sensitive,
            "undo_days": get_settings().undo_window_days,
        }
    )
    return _resp(request, "agent_action_preview.html", ctx)


@app.get("/agent/action/preview", response_class=HTMLResponse)
async def agent_action_preview_get(request: Request):
    p = request.query_params
    return _render_action_preview(
        request, p.get("action", ""), p.getlist("senders"), p.get("label_name", "")
    )


@app.post("/agent/action/preview", response_class=HTMLResponse)
async def agent_action_preview_post(request: Request):
    form = await request.form()
    return _render_action_preview(
        request, form.get("action", ""), form.getlist("senders"), form.get("label_name", "")
    )


_AUTOPILOT_ACTIONS = (
    "archive",
    "label",
    "mark_read",
)  # reversible, undoable; never trash/unsubscribe/send


def _autopilot_on() -> bool:
    try:
        return bool(get_settings().agent_autopilot)
    except Exception:
        return False


def _execute_reversible_action(account_email: str, action: str, staged, label_name: str = ""):
    """Record undo, then execute a reversible bulk action (archive/label/mark_read).

    Shared by the confirm endpoint and autopilot. Records the undo entry BEFORE
    the provider call so the op is always reversible from /undo. Returns
    ``(undo_id, count)``.
    """
    from postmind.core.storage import UndoLogRepo, get_session

    client = _build_provider()
    if action in _AUTOPILOT_ACTIONS and not client.supports("labels"):
        raise ValueError("This account's provider does not support this action — only Gmail does.")
    all_ids = [mid for g in staged for mid in g.message_ids]
    params = {"label_name": label_name} if action == "label" else {}

    entry = UndoLogRepo(get_session()).record(
        account_email=account_email,
        operation=action,
        message_ids=all_ids,
        description=(
            f"{action} {len(all_ids)} emails from {len(staged)} sender(s): "
            + ", ".join(g.sender_email for g in staged[:3])
            + ("…" if len(staged) > 3 else "")
        ),
        metadata={"senders": [g.sender_email for g in staged], "action_params": params},
    )

    if action == "archive":
        client.batch_archive(all_ids)
    elif action == "mark_read":
        client.batch_label(all_ids, remove=["UNREAD"])
    elif action == "label":
        gc = getattr(client, "gmail_client", None)
        if gc is None:
            raise ValueError("Labels require a Gmail account.")
        label_id = gc.get_or_create_label(label_name)
        client.batch_label(all_ids, add=[label_id])
    return entry.id, len(all_ids)


@app.post("/agent/action/confirm", response_class=HTMLResponse)
async def agent_action_confirm(request: Request):
    form = await request.form()
    action = form.get("action", "")
    senders = form.getlist("senders")
    label_name = (form.get("label_name") or "").strip()

    if action not in _AGENT_ACTIONS:
        return _resp(request, "error.html", {"error": f"Unknown action '{action}'."})
    if action == "label" and not label_name:
        return _resp(request, "error.html", {"error": "A label name is required."})
    if not senders:
        return RedirectResponse("/agent", status_code=303)

    account_email = _get_web_account() or ""
    # Re-resolve server-side: filters protected senders and binds the execution
    # to OUR resolved message IDs, not free-form model/form text.
    staged, _blocked, _sensitive, err = _resolve_action_targets(senders, "", account_email)
    if err:
        return _resp(request, "error.html", {"error": err})
    if not staged:
        return _resp(
            request,
            "error.html",
            {"error": "Scan data expired or all senders protected. Re-run Stats."},
        )

    try:
        loop = asyncio.get_event_loop()
        undo_id, count = await loop.run_in_executor(
            _executor, _execute_reversible_action, account_email, action, staged, label_name
        )
        _scan_cache.pop(_get_web_account() or "default", None)
    except Exception as exc:
        return _resp(request, "error.html", {"error": str(exc)})

    return RedirectResponse(
        f"/undo?acted={count}&action={action}&undo_id={undo_id}", status_code=303
    )


@app.get("/agent/review/{token}")
async def agent_review_get(token: str):
    from postmind.core.sender_stats import is_sensitive_sender

    entry = _review_get(token)
    if not entry or entry["account_email"] != _get_web_account():
        raise HTTPException(status_code=404, detail="This review expired or was not found.")

    by_sender: dict[str, dict] = {}
    for e in entry["emails"]:
        sender = e["sender_email"]
        g = by_sender.get(sender)
        if g is None:
            g = by_sender[sender] = {
                "sender_email": sender,
                "sender_name": e.get("sender_name") or sender,
                "count": 0,
                "size_bytes": 0,
                "sensitive": is_sensitive_sender(sender, e.get("sender_name") or ""),
                "emails": [],
            }
        g["count"] += 1
        g["size_bytes"] += int(e.get("size_estimate") or 0)
        g["emails"].append(
            {
                "id": e["id"],
                "subject": e["subject"],
                "date": e.get("date", ""),
                "size_str": _fmt_size(e.get("size_estimate") or 0),
            }
        )
    groups = sorted(by_sender.values(), key=lambda g: g["size_bytes"], reverse=True)
    for g in groups:
        g["size_str"] = _fmt_size(g["size_bytes"])
    return JSONResponse(
        {
            "description": entry["description"],
            "total_count": len(entry["emails"]),
            "groups": groups,
        }
    )


@app.post("/agent/review/{token}/confirm")
async def agent_review_confirm(token: str, ids: list[str] = Form(default=[])):
    from postmind.core.storage import UndoLogRepo, get_session

    entry = _review_get(token)
    if not entry or entry["account_email"] != _get_web_account():
        raise HTTPException(status_code=404, detail="This review expired or was not found.")

    account_email = entry["account_email"]
    cached_ids = {e["id"] for e in entry["emails"]}
    # Trust boundary: only ids that were server-resolved into this token may run.
    selected = [i for i in (ids or []) if i in cached_ids]
    if not selected:
        return JSONResponse({"trashed": 0, "undo_href": "/undo"})

    # Consume the selected ids from the cache *before* awaiting the executor.
    # This runs without an intervening await, so it is atomic against other
    # coroutines and closes the double-trash window if the same token is
    # confirmed twice (e.g. a double-click or a duplicate request).
    consumed = set(selected)
    entry["emails"] = [e for e in entry["emails"] if e["id"] not in consumed]

    def _work() -> int:
        provider = _build_provider()
        UndoLogRepo(get_session()).record(
            account_email=account_email,
            operation="trash",
            message_ids=selected,
            description=f"Trashed {len(selected)} emails from review: {entry['description']}",
            metadata={"source": "agent_review", "description": entry["description"]},
        )
        provider.batch_trash(selected)
        return len(selected)

    try:
        count = await asyncio.get_event_loop().run_in_executor(_executor, _work)
    except Exception as exc:
        # Match the other destructive endpoints: surface a graceful error rather
        # than a 500. The undo log (if written) points at emails still in the
        # inbox, so an undo of it is a harmless no-op.
        return JSONResponse({"trashed": 0, "error": str(exc)}, status_code=500)
    return JSONResponse({"trashed": count, "undo_href": "/undo"})


# ── Unsubscribe (real engine, optional back-catalog trash) ─────────────────────


@app.post("/agent/unsubscribe/confirm", response_class=HTMLResponse)
async def agent_unsubscribe_confirm(request: Request):
    form = await request.form()
    senders = form.getlist("senders")
    also_trash = form.get("also_trash") == "on"
    if not senders:
        return RedirectResponse("/agent", status_code=303)

    account_email = _get_web_account() or ""
    staged, _blocked, _sensitive, err = _resolve_action_targets(senders, "", account_email)
    if err:
        return _resp(request, "error.html", {"error": err})
    if not staged:
        return _resp(
            request,
            "error.html",
            {"error": "Scan data expired or all senders protected. Re-run Stats."},
        )

    def _do_unsub():
        from postmind.core.storage import UndoLogRepo, get_session
        from postmind.core.unsubscribe import UnsubscribeEngine

        client = _build_provider()
        if not client.supports("unsubscribe"):
            raise ValueError(
                "This account's provider does not support unsubscribe — only Gmail does."
            )
        gc = getattr(client, "gmail_client", None)
        if gc is None:
            raise ValueError("Unsubscribe requires a Gmail account.")
        acct = client.get_email_address()

        # Fetch one representative message per sender (with List-Unsubscribe headers).
        messages = []
        for g in staged:
            ids = g.message_ids[:1]
            if not ids:
                continue
            msgs = client.get_messages_batch(ids)
            if msgs:
                messages.append(msgs[0])

        engine = UnsubscribeEngine(gc, acct)
        results = engine.batch_unsubscribe(messages)
        ok = sum(1 for r in results if r.success)

        undo_id = None
        trashed = 0
        if also_trash:
            all_ids = [mid for g in staged for mid in g.message_ids]
            if all_ids:
                entry = UndoLogRepo(get_session()).record(
                    account_email=acct,
                    operation="trash",
                    message_ids=all_ids,
                    description=f"Trash back-catalog of {len(staged)} unsubscribed sender(s)",
                    metadata={"senders": [g.sender_email for g in staged]},
                )
                client.batch_trash(all_ids)
                undo_id = entry.id
                trashed = len(all_ids)
        return ok, len(results), undo_id, trashed

    try:
        loop = asyncio.get_event_loop()
        ok, total, undo_id, trashed = await loop.run_in_executor(_executor, _do_unsub)
        _scan_cache.pop(_get_web_account() or "default", None)
    except Exception as exc:
        return _resp(request, "error.html", {"error": str(exc)})

    if undo_id is not None:
        return RedirectResponse(
            f"/undo?unsubscribed={ok}&of={total}&purged={trashed}&undo_id={undo_id}",
            status_code=303,
        )
    return RedirectResponse(f"/agent?unsubscribed={ok}&of={total}", status_code=303)


# ── Send email (always-confirm) ────────────────────────────────────────────────


@app.post("/agent/send", response_class=HTMLResponse)
async def agent_send(request: Request):
    form = await request.form()
    to = (form.get("to") or "").strip()
    subject = (form.get("subject") or "").strip()
    body = (form.get("body") or "").strip()
    # Exactly one well-formed recipient — reject comma/whitespace-separated lists
    # so a confirmed single-recipient draft can't fan out to extra addresses.
    import re as _re

    if not _re.fullmatch(r"[^\s@,]+@[^\s@,]+\.[^\s@,]+", to):
        return _resp(
            request, "error.html", {"error": "A single valid recipient address is required."}
        )
    if not body:
        return _resp(request, "error.html", {"error": "Email body is empty."})

    def _do_send():
        client = _build_provider()
        gc = getattr(client, "gmail_client", None)
        if gc is None:
            raise ValueError("Sending mail requires a Gmail account.")
        return gc.send(to=to, subject=subject, body=body)

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(_executor, _do_send)
    except Exception as exc:
        return _resp(request, "error.html", {"error": f"Couldn't send: {exc}"})
    return RedirectResponse(f"/agent?sent={quote(to)}", status_code=303)


# ── Autodraft — AI reply drafts parked for review ─────────────────────────────


def _autodraft_service():
    """Build an AutodraftService scoped to the current web account."""
    import os

    from postmind.core.ai_engine import AIEngine
    from postmind.core.autodraft import AutodraftService
    from postmind.core.mock_ai import MockAIEngine
    from postmind.core.storage import AgentRepo, get_session

    provider = _build_provider()
    account_email = _get_web_account() or ""
    agent = AgentRepo(get_session()).get_by_email(account_email) if account_email else None
    soul = {}
    if agent:
        soul = {
            "voice_style": agent.voice_style,
            "user_context": agent.user_context,
            "writing_guidelines": agent.writing_guidelines,
        }
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    ai = AIEngine() if key else MockAIEngine()
    return AutodraftService(provider, ai, account_email, soul=soul)


def _draft_to_view(d) -> dict:
    """Shape a DraftRecord for the template."""
    trigger_labels = {
        "reply_needed": "Reply needed",
        "meeting": "Meeting request",
        "followup": "Follow-up",
        "manual": "On demand",
    }
    return {
        "id": d.id,
        "to_email": d.to_email,
        "subject": d.subject,
        "body": d.body,
        "trigger": d.trigger,
        "trigger_label": trigger_labels.get(d.trigger, d.trigger.replace("_", " ").title()),
        "confidence": d.confidence,
        "status": d.status,
        "edited": d.status == "edited",
        "created_at": d.created_at.strftime("%d %b %H:%M") if d.created_at else "",
    }


@app.get("/drafts", response_class=HTMLResponse)
async def drafts_page(request: Request):
    ctx = _base()
    ctx["active"] = "drafts"
    account_email = _get_web_account() or ""

    provider_is_gmail = _provider_name() == "gmail"
    cloud_ready = _ai_mode() == "cloud"

    drafts: list[dict] = []
    if account_email:
        from postmind.core.storage import DraftRepo, get_session

        rows = DraftRepo(get_session()).list_open(account_email)
        drafts = [_draft_to_view(d) for d in rows]

    ctx.update(
        {
            "drafts": drafts,
            "provider_is_gmail": provider_is_gmail,
            "cloud_ready": cloud_ready,
            "is_authed": _is_authed(),
        }
    )
    return _resp(request, "drafts.html", ctx)


@app.get("/drafts/badge", response_class=HTMLResponse)
async def drafts_badge(request: Request):
    """Tiny HTMX-polled count pill for the sidebar."""
    account_email = _get_web_account() or ""
    count = 0
    if account_email:
        from postmind.core.storage import DraftRepo, get_session

        count = DraftRepo(get_session()).count_open(account_email)
    inner = (
        f'<span class="ml-auto pm-pill bg-accent/15 text-teal-300 border border-accent/30 tabular">{count}</span>'
        if count
        else ""
    )
    return HTMLResponse(
        f'<span id="drafts-badge" hx-get="/drafts/badge" hx-trigger="every 30s" '
        f'hx-swap="outerHTML" class="contents">{inner}</span>'
    )


@app.post("/drafts/create")
async def drafts_create(request: Request):
    form = await request.form()
    gmail_id = (form.get("gmail_id") or "").strip()
    instruction = (form.get("instruction") or "").strip()
    if not gmail_id:
        return _resp(request, "error.html", {"error": "No message selected to reply to."})

    def _do():
        service = _autodraft_service()
        draft = service.draft_reply(gmail_id, instruction=instruction, trigger="manual")

        # Reply is the strongest positive signal — record it
        try:
            from postmind.core.storage import (
                ClassificationCacheRepo,
                EmailRepo,
                UserActionRepo,
                get_session,
            )

            session = get_session()
            email_rec = EmailRepo(session).get(gmail_id)
            cls = ClassificationCacheRepo(session).get_many([gmail_id]).get(gmail_id, {})
            acct = _get_web_account() or ""
            if email_rec and acct:
                UserActionRepo(session).record(
                    account_email=acct,
                    gmail_id=gmail_id,
                    sender_email=email_rec.sender_email or "",
                    sender_name=email_rec.sender_name or "",
                    subject=email_rec.subject or "",
                    action="reply",
                    source="triage",
                    ai_category=cls.get("category", ""),
                    ai_priority=cls.get("priority", ""),
                )
        except Exception:
            pass  # never block draft creation for a signal-capture failure

        return draft

    try:
        loop = asyncio.get_event_loop()
        rec = await loop.run_in_executor(_executor, _do)
    except ValueError as exc:
        return _resp(request, "error.html", {"error": str(exc)})
    except Exception as exc:
        return _resp(request, "error.html", {"error": f"Couldn't draft a reply: {exc}"})
    return RedirectResponse(f"/drafts?created={rec.id}", status_code=303)


@app.post("/drafts/{draft_id}/save")
async def drafts_save(draft_id: int, request: Request):
    form = await request.form()
    subject = (form.get("subject") or "").strip()
    body = (form.get("body") or "").strip()
    if not body:
        return _resp(request, "error.html", {"error": "Draft body can't be empty."})

    def _do():
        from postmind.core.storage import DraftRepo, get_session

        repo = DraftRepo(get_session())
        rec = repo.get(draft_id)
        if not rec:
            raise ValueError("Draft not found.")
        client = _build_provider()
        gc = getattr(client, "gmail_client", None)
        if gc is not None and rec.gmail_draft_id:
            gc.update_draft(
                draft_id=rec.gmail_draft_id,
                to=rec.to_email,
                subject=subject,
                body=body,
                thread_id=rec.thread_id or None,
                in_reply_to=rec.in_reply_to_rfc_id or None,
            )
        rec.subject = subject
        rec.body = body
        rec.status = "edited"
        repo.s.commit()

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(_executor, _do)
    except Exception as exc:
        return _resp(request, "error.html", {"error": f"Couldn't save the draft: {exc}"})
    return RedirectResponse("/drafts?saved=1", status_code=303)


@app.post("/drafts/{draft_id}/send")
async def drafts_send(draft_id: int, request: Request):
    form = await request.form()
    # The form carries the latest (possibly edited) subject/body so a Send always
    # reflects what the user sees, even if they didn't Save first.
    subject = (form.get("subject") or "").strip()
    body = (form.get("body") or "").strip()
    if not body:
        return _resp(request, "error.html", {"error": "Email body is empty."})

    def _do():
        from postmind.core.storage import DraftRepo, get_session

        repo = DraftRepo(get_session())
        rec = repo.get(draft_id)
        if not rec:
            raise ValueError("Draft not found.")
        import re as _re

        if not _re.fullmatch(r"[^\s@,]+@[^\s@,]+\.[^\s@,]+", rec.to_email):
            raise ValueError("This draft has an invalid recipient address.")
        client = _build_provider()
        gc = getattr(client, "gmail_client", None)
        if gc is None:
            raise ValueError("Sending mail requires a Gmail account.")
        gc.send(
            to=rec.to_email,
            subject=subject or rec.subject,
            body=body,
            thread_id=rec.thread_id or None,
            in_reply_to=rec.in_reply_to_rfc_id or None,
        )
        if rec.gmail_draft_id:
            try:
                gc.delete_draft(rec.gmail_draft_id)  # remove the now-sent parked draft
            except Exception:
                pass
        repo.set_status(draft_id, "sent")
        return rec.to_email

    try:
        loop = asyncio.get_event_loop()
        to = await loop.run_in_executor(_executor, _do)
    except ValueError as exc:
        return _resp(request, "error.html", {"error": str(exc)})
    except Exception as exc:
        return _resp(request, "error.html", {"error": f"Couldn't send: {exc}"})
    return RedirectResponse(f"/drafts?sent={quote(to)}", status_code=303)


@app.post("/drafts/{draft_id}/dismiss")
async def drafts_dismiss(draft_id: int, request: Request):
    def _do():
        from postmind.core.storage import DraftRepo, get_session

        repo = DraftRepo(get_session())
        rec = repo.get(draft_id)
        if not rec:
            return
        client = _build_provider()
        gc = getattr(client, "gmail_client", None)
        if gc is not None and rec.gmail_draft_id:
            try:
                gc.delete_draft(rec.gmail_draft_id)
            except Exception:
                pass
        repo.set_status(draft_id, "dismissed")

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(_executor, _do)
    except Exception as exc:
        return _resp(request, "error.html", {"error": f"Couldn't dismiss the draft: {exc}"})
    return RedirectResponse("/drafts?dismissed=1", status_code=303)


@app.get("/drafts/{draft_id}/mailto")
def draft_mailto(draft_id: int):
    """Return a mailto: link for a local draft."""
    from postmind.core.autodraft import _build_mailto_url
    from postmind.core.storage import DraftRepo, get_session

    draft = DraftRepo(get_session()).get(draft_id)
    if not draft or draft.draft_type != "local":
        raise HTTPException(status_code=404, detail="Not a local draft")

    mailto_url = _build_mailto_url(
        to=draft.to_email,
        subject=draft.subject,
        body=draft.body,
    )
    return {"mailto_url": mailto_url}


@app.get("/drafts/{draft_id}/copy")
def draft_copy(draft_id: int):
    """Return draft text formatted for copy-to-clipboard."""
    from postmind.core.storage import DraftRepo, get_session

    draft = DraftRepo(get_session()).get(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    text = f"To: {draft.to_email}\nSubject: {draft.subject}\n\n{draft.body}"
    return {"text": text}
