"""Local web interface for postmind — runs on localhost, nothing leaves your machine."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from postmind import __version__
from postmind.config import CREDENTIALS_PATH, DATA_DIR, TOKEN_PATH, get_settings

_THIS_DIR = Path(__file__).parent
_TEMPLATES_DIR = _THIS_DIR / "templates"

app = FastAPI(title="postmind", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


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

# In-memory sync task state: task_id → state dict
_sync_tasks: dict[str, dict] = {}
_active_sync_task_id: str | None = None

_oauth_tasks: dict[str, dict] = {}  # task_id → {status, email, error}

_active_web_account: str | None = None  # email override set by the web UI switcher

_executor = ThreadPoolExecutor(max_workers=4)


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
    from postmind.config import token_path_for, TOKEN_PATH
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
            f"{g.total_size_mb} MB" if g.total_size_mb >= 0.1
            else f"{g.total_size_bytes // 1024} KB"
        )
        enriched.append({
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
        })
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
        from postmind.core.storage import AccountRepo as _AR, EmailRepo as _ER, get_session as _gs
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
                scanned_at = acct_row.last_synced_at.strftime("%d %b %Y") if (acct_row and acct_row.last_synced_at) else "local cache"
                total_emails = (
                    session.query(EmailRecord)
                    .filter(EmailRecord.account_email == account_email, EmailRecord.is_inbox.is_(True))
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

        from postmind.core.storage import DailyBriefRepo
        from datetime import datetime as _dt, timezone as _tz
        _today = _dt.now(_tz.utc).date().isoformat()
        _db_rec = DailyBriefRepo(get_session()).get_today(account_email, _today)
        ctx.update({
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
        })
    else:
        ctx["has_scan"] = False
        ctx["daily_brief_preview"] = {"exists": False, "snippet": None, "ai_used": False, "generated_at": None}

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
            blocks.append(
                '<p class="text-ink text-sm leading-relaxed">' + " ".join(para) + "</p>"
            )
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


def _render_brief_links(brief, account_email: str) -> str:
    """Render the brief's identified emails as the "What needs attention" list.

    Each row deep-links into Gmail's web UI for that message
    (``…/mail/u/<account>/#all/<id>``). IMAP accounts have no web equivalent,
    so their rows render as plain (non-link) text. When the brief stored no
    items, renders a single calm empty-state line instead of an empty list.
    """
    import html as _html
    import json as _json
    from urllib.parse import quote as _quote

    def _section(inner: str) -> str:
        return (
            '<div class="mt-5 pt-4 border-t border-hairline">'
            '<p class="text-ink-subtle text-[11px] font-semibold uppercase tracking-[0.06em] mb-2">'
            'What needs attention</p>'
            f'{inner}'
            '</div>'
        )

    items = []
    raw = getattr(brief, "items_json", None)
    if raw:
        try:
            items = [i for i in _json.loads(raw) if isinstance(i, dict)]
        except (ValueError, TypeError):
            items = []
    if not items:
        return _section(
            '<p class="text-ink-tertiary text-sm">Nothing needs your attention right now.</p>'
        )

    from postmind.config import load_account_config
    is_gmail = load_account_config(account_email).get("provider", "gmail") == "gmail"

    # External-link glyph, shown only for clickable (Gmail) rows.
    icon = (
        '<svg class="w-3.5 h-3.5 shrink-0 mt-0.5 text-ink-tertiary group-hover:text-accent transition-colors" '
        'fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.75">'
        '<path stroke-linecap="round" stroke-linejoin="round" '
        'd="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"/></svg>'
    )

    rows = []
    for item in items:
        sender = _html.escape(str(item.get("sender") or "")[:80])
        subject = _html.escape(str(item.get("subject") or "(no subject)")[:120])
        gid = str(item.get("gmail_id") or "")
        inner = (
            f'<span class="min-w-0 flex-1">'
            f'<span class="block text-ink text-sm font-medium truncate">{subject}</span>'
            f'<span class="block text-ink-tertiary text-xs truncate">{sender}</span>'
            f'</span>'
        )
        if is_gmail and gid:
            # Gmail's /u/<n>/ segment is a numeric account *index* (0 = default);
            # putting an email there yields a full-page "Temporary Error (404)".
            # We pin /u/0/ and disambiguate the account by email via ?authuser=
            # (verified to open the right message even with multiple accounts).
            # The message id is the API's hex id, which Gmail resolves in #all/<id>.
            url = (
                "https://mail.google.com/mail/u/0/"
                f"?authuser={_quote(account_email, safe='@')}#all/{_quote(gid, safe='')}"
            )
            rows.append(
                f'<a href="{url}" target="_blank" rel="noopener noreferrer" '
                f'class="group flex items-start gap-2.5 -mx-2 px-2 py-1.5 rounded-button '
                f'hover:bg-surface-2 transition-colors">{icon}{inner}</a>'
            )
        else:
            rows.append(
                f'<div class="flex items-start gap-2.5 px-0 py-1.5">{inner}</div>'
            )

    return _section(f'<div class="space-y-0.5">{"".join(rows)}</div>')


@app.get("/brief", response_class=HTMLResponse)
async def brief_page(request: Request):
    ctx = _base()
    ctx["active"] = "brief"
    account_email = _get_web_account() or ""

    if not account_email:
        return RedirectResponse("/onboarding", status_code=302)

    from postmind.core.storage import DailyBriefRepo, get_session
    from datetime import datetime as _dt, timezone as _tz

    session = get_session()
    today_str = _dt.now(_tz.utc).date().isoformat()
    brief = DailyBriefRepo(session).get_today(account_email, today_str)
    recent = DailyBriefRepo(session).list_recent(account_email, limit=7)
    session.close()

    ctx.update({
        "brief": brief,
        "brief_status_html": _render_brief_status(brief) if brief else "",
        "brief_links_html": _render_brief_links(brief, account_email) if brief else "",
        "brief_html": _render_brief_html(brief.content) if brief else "",
        "recent": recent,
        "today_str": today_str,
        "account_email": account_email,
        "ai_mode": _ai_mode(),
    })
    return _resp(request, "daily_brief.html", ctx)


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
        return HTMLResponse(
            f"<div class='text-danger text-sm p-3 bg-danger-bg border border-danger-border rounded-card'>"
            f"Generation failed: {_html.escape(str(exc))}</div>"
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
    return HTMLResponse(
        f'<div id="brief-content" class="px-5 py-5">'
        f'<div class="flex items-center gap-2 mb-4">{ai_badge}'
        f'<span class="text-ink-tertiary text-xs">Generated at {gen_time}</span></div>'
        f'{status_html}{links_html}{content_html}'
        f'</div>'
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
            account_email=account_email, scope="anywhere", min_count=1, top_n=1000,
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
                row.last_synced_at.strftime("%d %b %Y")
                if (row and row.last_synced_at) else None
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
                return engine.summarize_cleanup_plan(
                    digest, plan.total_emails, plan.total_senders
                )

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

    ctx.update({
        "plan": plan,
        "intro": intro,
        "synced_at": synced_at,
        "account_email": account_email,
        "undo_days": get_settings().undo_window_days,
    })
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
            account_email=account_email, scope="anywhere", min_count=1, top_n=1000,
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
            b.key for b in plan.batches
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

    ctx.update({
        "plan": plan,
        "account_email": account_email,
        "undo_days": get_settings().undo_window_days,
        "auto_threshold": auto_threshold,
        "rule_offer_keys": rule_offer_keys,
    })
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

    cached = _cache_get()
    if not cached:
        return _resp(request, "error.html", {"error": "Scan data expired. Re-open Clean Up."})

    from postmind.core.storage import BlocklistRepo, get_session as _gs
    groups = cached["groups"]
    account_email = cached["account_email"]
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
                g for g in groups
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
        return _resp(request, "error.html", {"error": "Nothing to do — selected senders are protected or no longer in the scan."})

    result_action = actions_done[0] if len(set(actions_done)) == 1 else "mixed"
    return RedirectResponse(f"/undo?purged={count}&undo_id={undo_id}&action={result_action}", status_code=303)


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
        val = since[len("older:"):]
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
            has_local_data = bool(account_email and EmailRepo(session).get_inbox(account_email, limit=1))

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
                    db_q = db_q.filter(EmailRecord.internal_date >= _now_ms - newer_days * 86_400_000)
                if older_days:
                    db_q = db_q.filter(EmailRecord.internal_date <= _now_ms - older_days * 86_400_000)
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


def _render_purge_preview(request: Request, senders: list[str], action: str = "trash") -> HTMLResponse:
    """Render the confirm-first preview for the given senders from the current
    scan cache. Shared by the POST form flow and the GET deep-link the chat
    assistant produces. ``action`` is "trash" (move to Trash) or "archive"
    (remove from inbox); both still require the explicit confirm button."""
    if not senders:
        return RedirectResponse("/stats", status_code=303)
    if action not in ("trash", "archive"):
        action = "trash"

    cached = _cache_get()
    if not cached:
        return _resp(request, "error.html", {"error": "Scan data expired. Please re-run Stats."})

    groups = cached["groups"]
    selected_groups = [g for g in groups if g.sender_email in senders]
    if not selected_groups:
        return _resp(request, "error.html", {"error": "None of those senders are in the current scan. Re-run Stats and try again."})
    total_count = sum(g.count for g in selected_groups)
    total_mb = round(sum(g.total_size_bytes for g in selected_groups) / (1024 * 1024), 1)

    ctx = _base()
    ctx.update({
        "active": "stats",
        "selected": _enrich_groups(selected_groups),
        "senders": [g.sender_email for g in selected_groups],
        "total_count": total_count,
        "total_mb": total_mb,
        "action": action,
        "undo_days": get_settings().undo_window_days,
    })
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

    cached = _cache_get()
    if not cached:
        return _resp(request, "error.html", {"error": "Scan data expired. Please re-run Stats."})

    from postmind.core.storage import BlocklistRepo, get_session as _gs
    groups = cached["groups"]
    account_email = cached["account_email"]
    # Enforce protected senders at confirm time (not just at stage time): a sender
    # blocked after the cache was populated must never be touched.
    blocked_set = BlocklistRepo(_gs()).blocked_emails(account_email) if account_email else set()
    selected_groups = [g for g in groups if g.sender_email in senders and g.sender_email not in blocked_set]

    if not selected_groups:
        return _resp(request, "error.html", {"error": "Nothing to do — selected senders are protected or no longer in the scan."})

    def _do_purge():
        from postmind.core.storage import UndoLogRepo, get_session

        client = _build_provider()
        all_ids = [mid for g in selected_groups for mid in g.message_ids]

        verb = "Archived" if action == "archive" else "Purged"
        # Record undo BEFORE the operation so a crash/partial failure still leaves
        # a reversible log entry (matches BulkEngine.execute ordering). The undo
        # path keys off `operation` — "archive" restores INBOX, "trash" untrashes.
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
        return entry.id, len(all_ids)

    try:
        loop = asyncio.get_event_loop()
        undo_id, count = await loop.run_in_executor(_executor, _do_purge)
        _scan_cache.pop(_get_web_account() or "default", None)
    except Exception as exc:
        return _resp(request, "error.html", {"error": str(exc)})

    return RedirectResponse(f"/undo?purged={count}&undo_id={undo_id}&action={action}", status_code=303)


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
        expires_at = e.expires_at.replace(tzinfo=timezone.utc) if e.expires_at.tzinfo is None else e.expires_at
        executed_at = e.executed_at.replace(tzinfo=timezone.utc) if e.executed_at.tzinfo is None else e.executed_at
        rows.append({
            "id": e.id,
            "operation": e.operation,
            "description": e.description,
            "count": len(e.message_ids),
            "executed_at": executed_at.strftime("%b %d, %Y %H:%M"),
            "expires_in": max(0, (expires_at - now).days),
            "senders": e.op_metadata.get("senders", []),
        })

    ctx = _base()
    ctx.update({
        "active": "undo",
        "entries": rows,
        "account_email": account_email,
        "purged": purged,
        "purged_action": purged_action,
        "restored": restored,
        "undo_id": undo_id,
        "undo_days": get_settings().undo_window_days,
    })
    return _resp(request, "undo.html", ctx)


@app.post("/undo/{entry_id}", response_class=HTMLResponse)
async def undo_restore(request: Request, entry_id: int):
    def _do_undo():
        from postmind.core.bulk_engine import BulkEngine
        from postmind.core.storage import UndoLogRepo, get_session

        # Security check: ensure the undo entry belongs to the current account
        entry = UndoLogRepo(get_session()).get(entry_id)
        if entry and hasattr(entry, 'account_email'):
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
        ctx.update({
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
        })
    except Exception:
        ctx.update({
            "ai_mode": "off", "provider": "gmail",
            "imap_server": "", "imap_user": "",
            "undo_days": 30, "has_api_key": False, "has_ollama_key": False,
            "cloud_provider": "anthropic",
            "ollama_base_url": "http://localhost:11434",
            "ollama_model": "llama3.2",
            "chat_ai_mode": "", "chat_cloud_model": "claude-sonnet-4-6",
            "chat_ollama_model": "qwen2.5:32b",
            "agent_autopilot": False,
        })

    total = sum(f.stat().st_size for f in DATA_DIR.rglob("*") if f.is_file())
    ctx.update({
        "data_dir": str(DATA_DIR),
        "credentials_exist": CREDENTIALS_PATH.exists(),
        "token_exists": _is_authed(),
        "data_size_mb": round(total / (1024 * 1024), 1),
    })
    return _resp(request, "settings.html", ctx)


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
    from postmind.core.account_registry import list_accounts
    from postmind.config import token_path_for
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
        accounts_detail.append({
            "email": a.email,
            "display_name": a.display_name,
            "provider": a.provider,
            "token_ok": token_ok,
            "last_synced": last_sync.strftime("%b %d, %H:%M") if last_sync else "Never",
            "imap_server": a.imap_server,
            "is_active_web": a.email == _get_web_account(),
        })
    ctx = _base()
    ctx.update({
        "active": "accounts",
        "accounts_detail": accounts_detail,
        "added": request.query_params.get("added"),
        "removed": request.query_params.get("removed"),
    })
    return _resp(request, "accounts.html", ctx)


@app.post("/accounts/remove")
async def accounts_remove(request: Request):
    global _active_web_account
    form = await request.form()
    email = (form.get("email") or "").strip()
    if not email:
        raise HTTPException(status_code=400)
    from postmind.core.storage import AccountRepo, get_session
    from postmind.config import token_path_for, get_active_account, set_active_account, ACTIVE_ACCOUNT_PATH
    from postmind.core.account_registry import list_accounts
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
        return HTMLResponse('<p class="text-red-600 text-sm">credentials.json not found in ~/.postmind/. Download it from Google Cloud Console first.</p>')
    task_id = uuid.uuid4().hex[:10]
    _oauth_tasks[task_id] = {"status": "running", "email": None, "error": None}

    def _run_oauth():
        state = _oauth_tasks[task_id]
        try:
            import shutil
            from postmind.core.gmail_client import authenticate
            from postmind.config import TOKENS_DIR, token_path_for, set_active_account
            from postmind.core.account_registry import register_gmail
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
    from postmind.core.providers.factory import get_provider
    from postmind.core.account_registry import register_imap
    from postmind.config import set_active_account
    provider = get_provider("imap", imap_server=server, imap_user=user, imap_password=password, imap_port=port, imap_folder=folder)
    provider.get_profile()
    register_imap(user, server, user, port, folder, display_name or user)
    set_active_account(user)


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
        ctx.update({"active": "accounts", "tab": "imap", "has_credentials": CREDENTIALS_PATH.exists(), "error": "Server, username, and password are required."})
        return _resp(request, "accounts_add.html", ctx)

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            _executor, _test_and_register_imap, server, user, password, port, folder, display_name
        )
    except Exception as exc:
        ctx = _base()
        ctx.update({"active": "accounts", "tab": "imap", "has_credentials": CREDENTIALS_PATH.exists(), "error": str(exc)})
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
    ctx.update({
        "step": step,
        "tab": tab,
        "has_credentials": CREDENTIALS_PATH.exists(),
        "has_accounts": len(list_accounts()) > 0,
        "ai_mode": s.ai_mode,
        "ollama_base_url": s.ollama_base_url,
        "ollama_model": s.ollama_model,
        "has_api_key": bool(s.anthropic_api_key),
    })
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
        ctx.update({
            "step": 1,
            "tab": "imap",
            "has_credentials": CREDENTIALS_PATH.exists(),
            "has_accounts": len(list_accounts()) > 0,
            "ai_mode": s.ai_mode,
            "ollama_base_url": s.ollama_base_url,
            "ollama_model": s.ollama_model,
            "has_api_key": bool(s.anthropic_api_key),
            "imap_error": msg,
        })
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
        dot_color = "bg-teal-400" if a.is_active and a.status != "error" else ("bg-red-400" if a.status == "error" else "bg-slate-300")
        rows += f'<tr><td class="py-2 px-4 text-sm font-medium text-slate-800">{a.name}</td><td class="py-2 px-4 text-xs text-slate-500">{a.account_email}</td><td class="py-2 px-4"><span class="w-2 h-2 rounded-full {dot_color} inline-block"></span></td><td class="py-2 px-4 text-xs text-slate-500">{last}</td><td class="py-2 px-4 text-xs text-slate-500">{a.last_found_count}</td></tr>'
    status_badge = '<span class="inline-flex items-center gap-1.5 text-xs font-medium text-teal-700 bg-teal-50 border border-teal-200 px-2 py-0.5 rounded-full"><span class="w-1.5 h-1.5 rounded-full bg-teal-400 animate-pulse"></span>Running</span>' if is_running else '<span class="inline-flex items-center gap-1.5 text-xs font-medium text-slate-500 bg-slate-50 border border-slate-200 px-2 py-0.5 rounded-full"><span class="w-1.5 h-1.5 rounded-full bg-slate-400"></span>Stopped</span>'
    return HTMLResponse(f'<div id="watch-status" hx-get="/watch/status" hx-trigger="every 10s" hx-swap="outerHTML"><div class="flex items-center justify-between mb-4"><span class="text-sm font-medium text-slate-700">Daemon status</span>{status_badge}</div><table class="w-full"><thead><tr class="text-xs text-slate-400 uppercase tracking-wide border-b border-slate-100"><th class="py-2 px-4 text-left">Agent</th><th class="py-2 px-4 text-left">Account</th><th class="py-2 px-4 text-left">Status</th><th class="py-2 px-4 text-left">Last run</th><th class="py-2 px-4 text-left">Found</th></tr></thead><tbody>{rows}</tbody></table></div>')


# ── Agents ────────────────────────────────────────────────────────────────────


@app.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request):
    ctx = _base()
    ctx["active"] = "agents"
    from postmind.core.storage import AgentRepo, get_session
    from postmind.core.account_registry import list_accounts
    from postmind.config import get_settings
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
        return HTMLResponse(f"<p class='text-red-500 text-sm mt-3'>Error: {html.escape(str(exc))}</p>")


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
    """Toggle Super Agent autopilot (auto-execute reversible actions, opt-in)."""
    form = await request.form()
    on = form.get("agent_autopilot") == "on"
    _write_env({"POSTMIND_AGENT_AUTOPILOT": "true" if on else "false"})
    return RedirectResponse("/settings?success=agent", status_code=303)


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
    ctx.update({
        "active": "settings",
        "entries": [{"email": e.sender_email, "domain": e.sender_domain} for e in entries],
        "account_email": acct,
    })
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
    from datetime import datetime, timezone

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

        base = session.query(EmailRecord).filter(
            EmailRecord.account_email == account_email
        )
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

                mailbox_total = GmailClient().get_profile().get(
                    "messagesTotal", 0
                ) or 0
        except Exception:
            mailbox_total = 0

        coverage_pct = (
            min(round(total / mailbox_total * 100), 100)
            if mailbox_total else 0
        )

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
                if last_dt else None
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

    def _run():
        import json as _json
        import time as _time

        from postmind.core.gmail_client import GmailClient
        from postmind.core.storage import AccountRepo, EmailRecord, EmailRepo, UndoLogRepo, get_session
        from sqlalchemy import select as _select
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
                session.execute(
                    _select(_ER.gmail_id).where(_ER.account_email == account_email)
                ).scalars().all()
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
                state["message"] = (
                    f"Found {len(ids):,}{range_note} — syncing {len(new_ids):,} new…"
                )
                state["step"] = 2

                for i in range(0, len(new_ids), chunk_size):
                    chunk_ids = new_ids[i : i + chunk_size]
                    try:
                        messages = client.get_messages_metadata_batch(chunk_ids)
                    except Exception:
                        _time.sleep(2)
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
                from postmind.core.storage import AccountRepo
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

    _executor.submit(_run)

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
        return JSONResponse({
            "task_id": _active_sync_task_id,
            "status": state["status"],
            "message": state["message"],
            "count": state["count"],
        })
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
  <p class="text-red-600 text-sm mt-1">{state['error']}</p>
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
    {f'''<div class="w-full bg-slate-100 rounded-full h-1.5">
      <div class="bg-teal-500 h-1.5 rounded-full transition-all" style="width:{bar_width}%"></div>
    </div>
    <p class="text-slate-400 text-xs">{count:,} / {total:,} emails — {pct}%</p>''' if total > 0 else ""}
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


def _cls_payload(gmail_id: str, priority: str, category: str, explanation: str,
                 suggested_action: str, requires_reply: bool, deadline_hint: str) -> dict:
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
            messages.append(Message(
                id=r.gmail_id,
                thread_id=r.thread_id or "",
                snippet=r.snippet or "",
                headers=MessageHeader(subject=r.subject or "", from_=from_),
                label_ids=[],
                size_estimate=r.size_estimate or 0,
                internal_date=0,
            ))
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
        return _resp(request, "triage.html", {**ctx, "ai_off": True, "results": [], "scope": "unread", "limit": 20})

    if not _is_authed():
        return _resp(request, "triage.html", {**ctx, "ai_off": False, "auth_error": True, "results": [], "scope": "unread", "limit": 20})

    limit = int(request.query_params.get("limit", "20"))
    scope = request.query_params.get("scope", "unread")  # "unread" or "all"

    def _run():
        """Fetch the inbox slice and apply any cached classifications.

        Returns the rows to render immediately (cached rows carry their
        classification; uncached rows render a placeholder and are streamed in
        afterwards) plus the messages still needing classification.
        """
        from postmind.core.storage import ClassificationCacheRepo, get_session

        messages, account_email = _fetch_triage_messages(scope, limit)
        if not messages:
            return [], [], account_email

        cached = ClassificationCacheRepo(get_session()).get_many([m.id for m in messages])

        rows = []
        pending = []
        for m in messages:
            meta = {
                "id": m.id,
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

        # Classified rows first (by priority); not-yet-classified rows trail them
        # in fetch order — the client reorders once the stream fills them in.
        rows.sort(key=lambda r: _PRIORITY_ORDER.get(r["cls"]["priority"], 3) if r["cls"] else 99)
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

    ctx.update({
        "ai_off": False,
        "auth_error": False,
        "results": rows,
        "pending_token": pending_token,
        "pending_count": len(pending),
        "account_email": account_email,
        "limit": limit,
        "scope": scope,
        "scanned_at": datetime.now(timezone.utc).strftime("%H:%M"),
    })
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
        from concurrent.futures import ThreadPoolExecutor as _Pool, as_completed
        from postmind.core.ai_engine import AIEngine, _chunks
        from postmind.core.storage import ClassificationCacheRepo, get_session

        def _put(item):
            loop.call_soon_threadsafe(queue.put_nowait, item)

        try:
            ai = AIEngine()
            settings = get_settings()
            chunks = list(_chunks(messages, settings.ai_max_classify_batch))
            workers = max(1, min(settings.ai_classify_parallelism, len(chunks)))
            cache_repo = ClassificationCacheRepo(get_session())

            def _do(chunk):
                try:
                    return ai.classify_batch(chunk)
                except Exception:
                    # A batch that fails to classify shouldn't hang the row — fall
                    # back to a neutral classification so the UI resolves.
                    from postmind.core.ai_engine import ClassifiedEmail
                    return [
                        ClassifiedEmail(
                            gmail_id=m.id, category="other", priority="medium",
                            explanation="Could not classify automatically.",
                            suggested_action="keep", requires_reply=False, deadline_hint="",
                        )
                        for m in chunk
                    ]

            with _Pool(max_workers=workers) as pool:
                futures = [pool.submit(_do, ch) for ch in chunks]
                for fut in as_completed(futures):
                    classified = fut.result()
                    cache_repo.upsert_many([
                        {
                            "gmail_id": c.gmail_id, "category": c.category, "priority": c.priority,
                            "explanation": c.explanation, "suggested_action": c.suggested_action,
                            "requires_reply": c.requires_reply, "deadline_hint": c.deadline_hint,
                        }
                        for c in classified
                    ])
                    for c in classified:
                        _put({"type": "row", **_cls_payload(
                            c.gmail_id, c.priority, c.category, c.explanation,
                            c.suggested_action, c.requires_reply, c.deadline_hint,
                        )})
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


# ── Assistant (floating chat) ──────────────────────────────────────────────────

# Each page is keyed by its route path (the stable identifier the `navigate`
# tool emits) and carries a human-friendly display name plus a description.
# The name — never the path — is what the assistant shows the user.
_PAGES = {
    "/": {"name": "Dashboard", "desc": "inbox overview at a glance"},
    "/brief": {"name": "Daily Brief", "desc": "today's AI-generated morning summary of important emails, follow-ups, and action items"},
    "/agent": {"name": "Super Agent", "desc": "natural-language command center that can clean up, unsubscribe, send, and automate (with confirm-first cards)"},
    "/stats": {"name": "Stats", "desc": "senders ranked by storage impact, with a Purge button"},
    "/triage": {"name": "Triage", "desc": "AI-classified unread inbox (priority, category, action)"},
    "/agents": {"name": "Agents", "desc": "per-account heartbeat watchers and their voice/soul config"},
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
            "Sync page via the `navigate` tool (label \"Sync your inbox\") so they can pull "
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
        size = f"{g.total_size_mb:.1f} MB" if g.total_size_mb >= 0.1 else f"{g.total_size_bytes // 1024} KB"
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
        g for g in groups
        if q in (g.sender_email or "").lower()
        or q in (g.sender_name or "").lower()
        or q in (g.domain or "").lower()
    ]
    if not matches:
        return f"No senders matching '{query}' in the current scan."
    lines = [f"{len(matches)} sender(s) matching '{query}':"]
    for g in matches[:12]:
        size = f"{g.total_size_mb:.1f} MB" if g.total_size_mb >= 0.1 else f"{g.total_size_bytes // 1024} KB"
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
            "properties": {"query": {"type": "string", "description": "Name, email, or domain to search for."}},
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
                "recipient_context": {"type": "string", "description": "Who it's to and any relevant context."},
                "thread_snippet": {"type": "string", "description": "The message being replied to, if any."},
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
                "query": {"type": "string", "description": "Optionally match senders by name/email/domain substring instead of (or in addition to) explicit addresses."},
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
                    "enum": ["/", "/agent", "/stats", "/triage", "/agents", "/sync", "/accounts", "/watch", "/undo", "/settings"],
                },
                "label": {"type": "string", "description": "Short button label, e.g. 'Open Stats'."},
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

    blocked_set = BlocklistRepo(get_session()).blocked_emails(account_email) if account_email else set()
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
        size = f"{g.total_size_mb:.1f} MB" if g.total_size_mb >= 0.1 else f"{g.total_size_bytes // 1024} KB"
        out.append({
            "sender_email": g.sender_email,
            "sender_name": g.display_name,
            "count": g.count,
            "size_str": size,
            "sensitive": classify_sender_risk(g) == "sensitive",
        })
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
Active account: {account_email or 'none connected yet'}. AI mode: {ai_mode}.

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
        return {"reply": "Hi! Ask me about your inbox, have me draft an email, or tell me what you'd like to clean up.", "actions": []}

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
                agent = AgentRepo(get_session()).get_by_email(account_email) if account_email else None
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
                names = ", ".join(g.sender_email for g in matched[:5]) + ("…" if len(matched) > 5 else "")
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
    return _resp(request, "agent.html", ctx)


def _build_agent_system(account_email: str, mode: str) -> str:
    overview = _chat_overview_text(account_email)
    autopilot = "ON" if _autopilot_on() else "OFF"
    return f"""\
You are the postmind Super Agent — an autonomous but careful email assistant. The user \
describes an outcome in plain English and you use tools to achieve it: analyze storage, \
search senders, find large emails, clean up the inbox, and create automation (heartbeat \
agents and rules).

Operating rules:
- Use READ tools freely to gather facts before acting. Quote real numbers.
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
- AUTOPILOT is currently {autopilot}. When ON, stage_archive/stage_label/stage_mark_read \
execute immediately (no card) because they are fully reversible — tell the user what you \
did and that it's undoable. Trash, unsubscribe, and send ALWAYS require a confirm card even \
under autopilot.
- Never write a URL or route path (like `/sync` or `/stats`) in your reply — refer to \
pages by name ("the Sync page", "Stats"). Those are not commands the user types.

Active account: {account_email or 'none connected yet'}. AI mode: {mode}.

Live inbox snapshot:
{overview}"""


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
        if name == "get_inbox_overview":
            return _chat_overview_text(account_email)
        if name == "search_senders":
            return _chat_search_senders(tool_input.get("query", ""), account_email)
        if name == "analyze_storage":
            from postmind.core.sender_stats import fetch_sender_groups_from_db
            from postmind.core.storage import EmailRepo, get_session
            cached = _cache_get()
            if cached:
                groups = cached["groups"]
            elif account_email and EmailRepo(get_session()).get_inbox(account_email, limit=1):
                groups = fetch_sender_groups_from_db(account_email=account_email, scope="inbox", min_count=1, top_n=500, sort_by="size")
            else:
                return "No scan data available — ask the user to open Stats or run a Sync first."
            return agent_tools.summarize_storage(groups, tool_input.get("group_by", "sender"), int(tool_input.get("top_n", 10) or 10))
        if name == "find_largest_messages":
            try:
                provider = _build_provider()
                return agent_tools.find_largest_messages(provider, tool_input.get("query", ""), int(tool_input.get("limit", 10) or 10))
            except Exception as exc:
                return f"Couldn't fetch message sizes: {exc}"
        if name == "find_unopened_subscriptions":
            from postmind.core.storage import get_session
            if not account_email:
                return "No active account."
            rows = agent_tools.find_unopened_subscriptions(
                get_session(), account_email,
                int(tool_input.get("min_count", 3) or 3),
                int(tool_input.get("limit", 15) or 15),
            )
            return agent_tools.format_unopened(rows)
        if name == "list_automation":
            from postmind.core.storage import AgentRepo, RuleRepo, get_session
            session = get_session()
            agent = AgentRepo(session).get_by_email(account_email) if account_email else None
            rules = RuleRepo(session).list_active(account_email) if account_email else []
            parts = []
            if agent:
                parts.append(f"Heartbeat agent '{agent.name}' every {agent.interval_minutes}m (active={agent.is_active}, rules={agent.run_rules}).")
            else:
                parts.append("No heartbeat agent yet.")
            if rules:
                parts.append("Active rules: " + "; ".join(f"{r.name} → {r.action}" for r in rules[:5]))
            else:
                parts.append("No active rules.")
            return " ".join(parts)
        if name == "stage_trash":
            matched, err = _chat_resolve_senders(tool_input.get("senders") or [], tool_input.get("query", ""), account_email)
            if err:
                return err
            if not matched:
                return "No matching senders found in the current scan — nothing staged."
            total = sum(g.count for g in matched)
            mb = sum(g.total_size_bytes for g in matched) / (1024 * 1024)
            href = "/purge/preview?" + urlencode([("senders", g.sender_email) for g in matched])
            if not any(a.get("href") == href for a in actions):
                actions.append({"label": f"Review & confirm ({total} emails)", "href": href})
            names = ", ".join(g.sender_email for g in matched[:5]) + ("…" if len(matched) > 5 else "")
            return f"Staged {len(matched)} sender(s) — {total} emails, ~{mb:.0f} MB ({names}). The user must confirm in the preview before anything moves to Trash."
        if name in ("stage_archive", "stage_label", "stage_mark_read"):
            action = {"stage_archive": "archive", "stage_label": "label", "stage_mark_read": "mark_read"}[name]
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
            staged, blocked, sensitive, err = _resolve_action_targets(
                tool_input.get("senders") or [], tool_input.get("query", ""), account_email
            )
            if err:
                return err
            if not staged:
                extra = f" ({len(blocked)} protected sender(s) skipped)" if blocked else ""
                return f"No matching senders to {action.replace('_', ' ')}{extra} — nothing staged."
            total = sum(g.count for g in staged)
            verb = action.replace("_", " ")
            title = {"archive": "Archive emails", "label": f"Label emails “{label_name}”", "mark_read": "Mark emails as read"}[action]
            # Autopilot: auto-execute reversible actions without a confirm card
            # (opt-in, off by default; trash/unsubscribe/send never qualify). Even
            # under autopilot, SENSITIVE senders (bank/legal/health) keep the human
            # gate — they're routed to a confirm card, never auto-executed.
            if _autopilot_on() and action in _AUTOPILOT_ACTIONS:
                sset = set(sensitive)
                auto = [g for g in staged if g.sender_email not in sset]
                held = [g for g in staged if g.sender_email in sset]
                parts = []
                if auto:
                    try:
                        _undo_id, count = _execute_reversible_action(account_email, action, auto, label_name)
                    except Exception as exc:
                        return f"Couldn't {verb}: {exc}"
                    if not any(a.get("href") == "/undo" for a in actions):
                        actions.append({"label": "Undo", "href": "/undo"})
                    parts.append(f"Autopilot: {verb}d {count} emails from {len(auto)} sender(s); reversible from Undo for 30 days.")
                if held:
                    cards.append({
                        "type": "bulk_action",
                        "title": title,
                        "fields": {
                            "action": action, "label_name": label_name,
                            "targets": _enrich_targets(held), "total_count": sum(g.count for g in held),
                            "blocked": blocked, "sensitive": sensitive, "undoable": True,
                        },
                    })
                    parts.append(f"{len(held)} sensitive sender(s) (bank/legal/health) need your confirmation — shown as a card.")
                if blocked:
                    parts.append(f"{len(blocked)} protected sender(s) skipped.")
                return " ".join(parts) or "Nothing to do."
            cards.append({
                "type": "bulk_action",
                "title": title,
                "fields": {
                    "action": action,
                    "label_name": label_name,
                    "targets": _enrich_targets(staged),
                    "total_count": total,
                    "blocked": blocked,
                    "sensitive": sensitive,
                    "undoable": True,
                },
            })
            note = f" ({len(blocked)} protected skipped)" if blocked else ""
            return f"Staged a {action.replace('_', ' ')} of {len(staged)} sender(s), {total} emails{note}. Showed a confirmation card; nothing happens until the user confirms. Reversible for 30 days."
        if name == "stage_unsubscribe":
            staged, blocked, sensitive, err = _resolve_action_targets(
                tool_input.get("senders") or [], tool_input.get("query", ""), account_email
            )
            if err:
                return err
            if not staged:
                extra = f" ({len(blocked)} protected sender(s) skipped)" if blocked else ""
                return f"No matching senders to unsubscribe from{extra} — nothing staged."
            total = sum(g.count for g in staged)
            cards.append({
                "type": "unsubscribe",
                "title": "Unsubscribe from senders",
                "fields": {
                    "targets": _enrich_targets(staged),
                    "total_count": total,
                    "blocked": blocked,
                    "sensitive": sensitive,
                },
            })
            note = f" ({len(blocked)} protected skipped)" if blocked else ""
            return f"Staged unsubscribe from {len(staged)} sender(s){note}. Showed a confirmation card. Unsubscribe is external and NOT undoable; trashing the back-catalog is optional and undoable. Nothing happens until the user confirms."
        if name == "draft_email":
            from postmind.core.storage import AgentRepo, get_session
            soul = {}
            agent = AgentRepo(get_session()).get_by_email(account_email) if account_email else None
            if agent:
                soul = {"voice_style": agent.voice_style, "user_context": agent.user_context, "writing_guidelines": agent.writing_guidelines}
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
            cards.append({
                "type": "send_email",
                "title": "Review draft & send",
                "fields": {"to": (tool_input.get("to") or "").strip(), "subject": subject, "body": body},
            })
            return f"Drafted the email and showed an editable card. Subject: {subject}. The user can edit and must click Send — nothing is sent automatically."
        if name == "send_email":
            cards.append({
                "type": "send_email",
                "title": "Review & send",
                "fields": {
                    "to": (tool_input.get("to") or "").strip(),
                    "subject": (tool_input.get("subject") or "").strip(),
                    "body": (tool_input.get("body") or "").strip(),
                },
            })
            return "Showed an editable send-email card. Always-confirm: nothing is sent until the user clicks Send."
        if name == "create_agent":
            email = (tool_input.get("email") or account_email or "").strip()
            if not email:
                return "No account to attach the agent to — ask the user to connect an account first."
            card = {
                "type": "create_agent",
                "title": "Create heartbeat agent",
                "fields": {
                    "email": email,
                    "name": (tool_input.get("name") or email.split("@")[0].title()),
                    "interval_minutes": int(tool_input.get("interval_minutes", 30) or 30),
                    "voice_style": tool_input.get("voice_style", "") or "",
                    "user_context": tool_input.get("user_context", "") or "",
                    "run_rules": bool(tool_input.get("run_rules", True)),
                    "run_followups": bool(tool_input.get("run_followups", True)),
                    "run_avoidance": bool(tool_input.get("run_avoidance", False)),
                },
            }
            cards.append(card)
            return f"Staged a heartbeat agent for {email} (every {card['fields']['interval_minutes']}m). Showed the user a confirmation card."
        if name == "create_rule":
            nl = (tool_input.get("natural_language") or "").strip()
            if not nl:
                return "Need the rule in plain English."
            if not account_email:
                return "No active account — ask the user to connect one first."
            try:
                nl_rule = ai.translate_rule(nl)
            except Exception as exc:
                return f"Couldn't translate that rule: {exc}"
            cards.append({
                "type": "create_rule",
                "title": "Create rule",
                "fields": {
                    "natural_language": nl,
                    "gmail_query": nl_rule.gmail_query,
                    "action": nl_rule.action,
                    "explanation": nl_rule.explanation,
                    "warnings": nl_rule.warnings or [],
                },
            })
            return f"Staged a rule: {nl_rule.explanation} (query: {nl_rule.gmail_query}, action: {nl_rule.action}). Showed the user a confirmation card."
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
        return {"reply": "Tell me what you'd like to do — e.g. “what's eating my storage?”, “delete everything from blah.com”, or “create an agent that archives newsletters weekly.”", "actions": [], "cards": []}

    mode = _chat_mode()
    guidance = _agent_mode_guidance(mode)
    if guidance is not None:
        return guidance

    account_email = _get_web_account() or ""
    actions: list[dict] = []
    cards: list[dict] = []
    engine_kwargs = _chat_engine_kwargs()

    def _run():
        from postmind.core import agent_tools
        from postmind.core.ai_engine import AIEngine

        ai = AIEngine(**engine_kwargs)
        system = _build_agent_system(account_email, mode)
        executor_tool = _build_agent_tool_executor(account_email, ai, actions, cards)
        return ai.chat(
            messages,
            system=system,
            tools=agent_tools.ALL_TOOLS,
            tool_executor=executor_tool,
            max_tool_iterations=12,
        )

    try:
        loop = asyncio.get_event_loop()
        reply = await loop.run_in_executor(_executor, _run)
    except Exception as exc:
        return {"reply": f"Sorry — I hit an error: {exc}", "actions": [], "cards": []}

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
            _empty_stream({
                "reply": "Tell me what you'd like to do — e.g. “what's eating my storage?”, “delete everything from blah.com”, or “create an agent that archives newsletters weekly.”",
                "actions": [],
                "cards": [],
            }),
            media_type="text/event-stream",
        )

    guidance = _agent_mode_guidance(mode)
    if guidance is not None:
        return StreamingResponse(_empty_stream(guidance), media_type="text/event-stream")

    account_email = _get_web_account() or ""
    actions: list[dict] = []
    cards: list[dict] = []
    engine_kwargs = _chat_engine_kwargs()

    # Bridge the sync chat_stream generator (run in the thread pool) to the async
    # SSE response via a queue. A sentinel marks completion.
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()
    _SENTINEL = object()

    def _produce():
        from postmind.core import agent_tools
        from postmind.core.ai_engine import AIEngine

        def _put(item):
            loop.call_soon_threadsafe(queue.put_nowait, item)

        try:
            ai = AIEngine(**engine_kwargs)
            system = _build_agent_system(account_email, mode)
            executor_tool = _build_agent_tool_executor(account_email, ai, actions, cards)
            if mode == "cloud":
                for event in ai.chat_stream(
                    messages,
                    system=system,
                    tools=agent_tools.ALL_TOOLS,
                    tool_executor=executor_tool,
                    max_tool_iterations=12,
                ):
                    # tool_start carries raw tool input; the client only needs the name.
                    if event.get("type") == "tool_start":
                        _put({"type": "tool_start", "name": event.get("name", "")})
                    else:
                        _put(event)
            else:
                # Local: no token streaming (Ollama tool-use isn't reliably
                # streamable). Run the non-streaming loop and emit the reply once.
                reply = ai.chat(
                    messages,
                    system=system,
                    tools=agent_tools.ALL_TOOLS,
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
            # Final event with the accumulated actions/cards for confirm rendering.
            if not await request.is_disconnected():
                yield _sse({"type": "final", "actions": actions, "cards": cards})
                yield _sse({"type": "done"})
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
    repo.register(email, (form.get("name") or email.split("@")[0].title()).strip(), max(1, min(1440, int(form.get("interval_minutes") or 30))))
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
        from postmind.core.storage import get_session

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


def _render_action_preview(request: Request, action: str, senders: list[str], label_name: str = "") -> HTMLResponse:
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
        return _resp(request, "error.html", {"error": "None of those senders are in the current scan (or all are protected). Re-run Stats and try again."})

    total_count = sum(g.count for g in staged)
    total_mb = round(sum(g.total_size_bytes for g in staged) / (1024 * 1024), 1)
    ctx = _base()
    ctx.update({
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
    })
    return _resp(request, "agent_action_preview.html", ctx)


@app.get("/agent/action/preview", response_class=HTMLResponse)
async def agent_action_preview_get(request: Request):
    p = request.query_params
    return _render_action_preview(request, p.get("action", ""), p.getlist("senders"), p.get("label_name", ""))


@app.post("/agent/action/preview", response_class=HTMLResponse)
async def agent_action_preview_post(request: Request):
    form = await request.form()
    return _render_action_preview(request, form.get("action", ""), form.getlist("senders"), form.get("label_name", ""))


_AUTOPILOT_ACTIONS = ("archive", "label", "mark_read")  # reversible, undoable; never trash/unsubscribe/send


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
        return _resp(request, "error.html", {"error": "Scan data expired or all senders protected. Re-run Stats."})

    try:
        loop = asyncio.get_event_loop()
        undo_id, count = await loop.run_in_executor(
            _executor, _execute_reversible_action, account_email, action, staged, label_name
        )
        _scan_cache.pop(_get_web_account() or "default", None)
    except Exception as exc:
        return _resp(request, "error.html", {"error": str(exc)})

    return RedirectResponse(f"/undo?acted={count}&action={action}&undo_id={undo_id}", status_code=303)


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
        return _resp(request, "error.html", {"error": "Scan data expired or all senders protected. Re-run Stats."})

    def _do_unsub():
        from postmind.core.storage import UndoLogRepo, get_session
        from postmind.core.unsubscribe import UnsubscribeEngine

        client = _build_provider()
        if not client.supports("unsubscribe"):
            raise ValueError("This account's provider does not support unsubscribe — only Gmail does.")
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
        return RedirectResponse(f"/undo?unsubscribed={ok}&of={total}&purged={trashed}&undo_id={undo_id}", status_code=303)
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
        return _resp(request, "error.html", {"error": "A single valid recipient address is required."})
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
