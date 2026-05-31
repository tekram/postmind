"""Local web interface for postmind — runs on localhost, nothing leaves your machine."""

from __future__ import annotations

import asyncio
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from postmind import __version__
from postmind.config import CREDENTIALS_PATH, DATA_DIR, TOKEN_PATH, get_settings

_THIS_DIR = Path(__file__).parent
_TEMPLATES_DIR = _THIS_DIR / "templates"

app = FastAPI(title="postmind", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# In-memory scan cache — keyed by "latest", short TTL
_scan_cache: dict[str, dict] = {}
_CACHE_TTL = 300  # 5 minutes

# In-memory sync task state: task_id → state dict
_sync_tasks: dict[str, dict] = {}

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
    """Return the email address the web UI is currently scoped to."""
    from postmind.config import get_active_account
    return _active_web_account or get_active_account()


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

    from postmind.core.sender_stats import (
        best_next_step,
        fetch_sender_groups_from_db,
        generate_recommendations,
        group_by_domain,
        reclaimable_mb,
    )
    from postmind.core.storage import AccountRepo, EmailRecord, EmailRepo, get_session

    account_email = _get_web_account() or ""
    session = get_session()

    cached = _cache_get()
    if cached:
        groups = cached["groups"]
        scanned_at = cached["scanned_at"]
        account_email = cached["account_email"]
        profile = cached["profile"]
    elif account_email and EmailRepo(session).get_inbox(account_email, limit=1):
        # No in-memory cache but local DB has data — build from DB
        groups = fetch_sender_groups_from_db(
            account_email=account_email,
            scope="inbox",
            min_count=1,
            top_n=50,
            sort_by="score",
        )
        acct_row = AccountRepo(session).get(account_email)
        scanned_at = acct_row.last_synced_at.strftime("%-d %b %Y") if (acct_row and acct_row.last_synced_at) else "local cache"
        profile = {}
    else:
        groups = None
        scanned_at = None
        profile = {}

    if groups:
        domain_groups = group_by_domain(groups)
        domain_map = {d.domain: d for d in domain_groups}
        recs = generate_recommendations(groups, top_n=5, domain_map=domain_map)
        bns = best_next_step(recs)
        total_reclaimable = reclaimable_mb(recs)

        # Total emails in DB
        total_emails = (
            session.query(EmailRecord)
            .filter(EmailRecord.account_email == account_email, EmailRecord.is_inbox.is_(True))
            .count()
        )

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
        })
    else:
        ctx["has_scan"] = False

    return _resp(request, "dashboard.html", ctx)


# ── Stats ─────────────────────────────────────────────────────────────────────


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    ctx = _base()
    ctx["active"] = "stats"
    ctx["sort_by"] = request.query_params.get("sort", "score")
    ctx["scope"] = request.query_params.get("scope", "inbox")
    ctx["since"] = request.query_params.get("since", "")
    return _resp(request, "stats.html", ctx)


@app.get("/stats/data", response_class=HTMLResponse)
async def stats_data(
    request: Request,
    sort: str = "score",
    scope: str = "inbox",
    since: str = "",
    top: int = 100,
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
        session = get_session()

        # Use local DB when synced data exists and no time filter (since) is applied
        has_local_data = bool(account_email and EmailRepo(session).get_inbox(account_email, limit=1))

        data_source = "Gmail API"
        total_emails_in_scope = 0

        if has_local_data and not since:
            db_scope = "inbox" if scope != "anywhere" else "anywhere"
            groups = fetch_sender_groups_from_db(
                account_email=account_email,
                scope=db_scope,
                min_count=1,
                top_n=top,
                sort_by=valid_sort,
            )
            profile = {"emailAddress": account_email}
            data_source = "local cache"
            from postmind.core.storage import EmailRecord
            db_q = session.query(EmailRecord).filter(EmailRecord.account_email == account_email)
            if db_scope == "inbox":
                db_q = db_q.filter(EmailRecord.is_inbox.is_(True))
            total_emails_in_scope = db_q.count()
        else:
            client = _build_provider()
            profile = client.get_profile()
            account_email = profile.get("emailAddress", "")
            query = "in:anywhere -in:trash -in:spam" if scope == "anywhere" else "in:inbox"
            if since:
                query += f" newer_than:{since}"
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
        if blocked:
            groups = [g for g in groups if g.sender_email not in blocked]

        _cache_set(groups, profile, account_email)

        domain_groups = group_by_domain(groups)
        domain_map = {d.domain: d for d in domain_groups}
        recs = generate_recommendations(groups, top_n=5, domain_map=domain_map)
        total_reclaimable = reclaimable_mb(recs)

        # Date range from groups
        all_dates = [g.earliest_date for g in groups if g.earliest_date]
        date_from = min(all_dates).strftime("%-d %b %Y") if all_dates else ""
        latest_dates = [g.latest_date for g in groups if g.latest_date]
        date_to = max(latest_dates).strftime("%-d %b %Y") if latest_dates else ""

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


def _render_purge_preview(request: Request, senders: list[str]) -> HTMLResponse:
    """Render the confirm-first purge preview for the given senders from the
    current scan cache. Shared by the POST form flow and the GET deep-link the
    chat assistant produces. Trashing still requires the explicit confirm button."""
    if not senders:
        return RedirectResponse("/stats", status_code=303)

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
        "undo_days": get_settings().undo_window_days,
    })
    return _resp(request, "purge_preview.html", ctx)


@app.post("/purge/preview", response_class=HTMLResponse)
async def purge_preview(request: Request):
    form = await request.form()
    return _render_purge_preview(request, form.getlist("senders"))


@app.get("/purge/preview", response_class=HTMLResponse)
async def purge_preview_get(request: Request):
    """Deep-link entrypoint (e.g. from the chat assistant): renders the same
    confirm-first preview. Read-only — nothing is trashed until the user confirms."""
    return _render_purge_preview(request, request.query_params.getlist("senders"))


@app.post("/purge/confirm", response_class=HTMLResponse)
async def purge_confirm(request: Request):
    form = await request.form()
    senders = form.getlist("senders")

    if not senders:
        return RedirectResponse("/stats", status_code=303)

    cached = _cache_get()
    if not cached:
        return _resp(request, "error.html", {"error": "Scan data expired. Please re-run Stats."})

    groups = cached["groups"]
    selected_groups = [g for g in groups if g.sender_email in senders]
    account_email = cached["account_email"]

    def _do_purge():
        from postmind.core.storage import UndoLogRepo, get_session

        client = _build_provider()
        all_ids = [mid for g in selected_groups for mid in g.message_ids]
        client.batch_trash(all_ids)

        entry = UndoLogRepo(get_session()).record(
            account_email=account_email,
            operation="trash",
            message_ids=all_ids,
            description=(
                f"Purged {len(all_ids)} emails from {len(selected_groups)} sender(s): "
                + ", ".join(g.sender_email for g in selected_groups[:3])
                + ("…" if len(selected_groups) > 3 else "")
            ),
            metadata={"senders": [g.sender_email for g in selected_groups]},
        )
        return entry.id, len(all_ids)

    try:
        loop = asyncio.get_event_loop()
        undo_id, count = await loop.run_in_executor(_executor, _do_purge)
        _scan_cache.pop(_get_web_account() or "default", None)
    except Exception as exc:
        return _resp(request, "error.html", {"error": str(exc)})

    return RedirectResponse(f"/undo?purged={count}&undo_id={undo_id}", status_code=303)


# ── Undo ─────────────────────────────────────────────────────────────────────


@app.get("/undo", response_class=HTMLResponse)
async def undo_page(request: Request):
    purged = request.query_params.get("purged")
    restored = request.query_params.get("restored")
    undo_id = request.query_params.get("undo_id")

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
            "ollama_base_url": s.ollama_base_url,
            "ollama_model": s.ollama_model,
            "chat_ai_mode": s.chat_ai_mode,  # "" = inherit
            "chat_cloud_model": s.chat_cloud_model or s.ai_model,
            "chat_ollama_model": s.chat_ollama_model or s.ollama_model,
        })
    except Exception:
        ctx.update({
            "ai_mode": "off", "provider": "gmail",
            "imap_server": "", "imap_user": "",
            "undo_days": 30, "has_api_key": False,
            "ollama_base_url": "http://localhost:11434",
            "ollama_model": "llama3.2",
            "chat_ai_mode": "", "chat_cloud_model": "claude-sonnet-4-6",
            "chat_ollama_model": "qwen2.5:32b",
        })

    ctx.update({
        "data_dir": str(DATA_DIR),
        "credentials_exist": CREDENTIALS_PATH.exists(),
        "token_exists": _is_authed(),
    })
    return _resp(request, "settings.html", ctx)


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
    all_db = {r.email: r for r in AccountRepo(get_session()).list_all()}
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
    from postmind.config import token_path_for
    token = token_path_for(email)
    if token.exists():
        token.unlink()
    AccountRepo(get_session()).deactivate(email)
    if _active_web_account == email:
        _active_web_account = None
    _scan_cache.pop(email, None)
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
        email = state["email"]
        _scan_cache.clear()
        return HTMLResponse(f"""<div class="bg-green-50 border border-green-200 rounded-xl p-4">
  <p class="text-green-800 font-medium text-sm">&#10003; Account added: {email}</p>
  <a href="/accounts" class="text-teal-600 text-sm font-medium mt-2 inline-block">View accounts &rarr;</a>
</div>""")
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
        updates["MAILTRIM_OLLAMA_BASE_URL"] = url
        updates["MAILTRIM_OLLAMA_MODEL"] = model
    elif mode == "cloud":
        api_key = (form.get("anthropic_api_key") or "").strip()
        if api_key:
            updates["ANTHROPIC_API_KEY"] = api_key

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


@app.get("/sync", response_class=HTMLResponse)
async def sync_page(request: Request):
    ctx = _base()
    ctx["active"] = "sync"
    return _resp(request, "sync.html", ctx)


@app.post("/sync/start", response_class=HTMLResponse)
async def sync_start(request: Request):
    form = await request.form()
    scope = form.get("scope", "inbox")
    raw_limit = int(form.get("limit", "1000"))
    limit = None if raw_limit == 0 else raw_limit

    task_id = uuid.uuid4().hex[:8]
    _sync_tasks[task_id] = {
        "status": "running",
        "step": 0,
        "message": "Connecting…",
        "count": 0,
        "total": 0,
        "error": None,
        "started_at": time.time(),
    }

    def _run():
        state = _sync_tasks[task_id]
        try:
            import json as _json

            from postmind.core.gmail_client import GmailClient
            from postmind.core.storage import EmailRecord, EmailRepo, UndoLogRepo, get_session

            client = GmailClient()
            profile = client.get_profile()
            account_email = profile.get("emailAddress", "")

            state["message"] = f"Connected to {account_email}"
            state["step"] = 1

            query = "in:anywhere -in:trash -in:spam" if scope == "anywhere" else "in:inbox"
            ids = client.list_message_ids(query=query, max_results=limit)
            total = len(ids)
            state["total"] = total
            state["message"] = f"Found {total:,} emails — fetching metadata…"
            state["step"] = 2

            session = get_session()
            repo = EmailRepo(session)
            chunk_size = 50
            saved = 0

            for i in range(0, total, chunk_size):
                chunk_ids = ids[i : i + chunk_size]
                messages = client.get_messages_metadata_batch(chunk_ids)
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
                saved += len(records)
                state["count"] = saved
                state["message"] = f"Synced {saved:,} / {total:,} emails…"
                state["step"] = 3

            # Housekeeping
            UndoLogRepo(session).purge_expired()

            elapsed = int(time.time() - state["started_at"])
            state["status"] = "done"
            state["message"] = f"Synced {saved:,} emails in {elapsed}s"
            state["count"] = saved

        except Exception as exc:
            state["status"] = "error"
            state["error"] = str(exc)
            state["message"] = str(exc)

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
        return HTMLResponse(f"""
<div id="sync-result" class="bg-green-50 border border-green-200 rounded-xl p-4">
  <div class="flex items-center justify-between">
    <div>
      <p class="text-green-800 font-medium text-sm">✓ {msg}</p>
      <p class="text-green-600 text-xs mt-0.5">Local cache is up to date</p>
    </div>
    <a href="/stats" class="bg-teal-600 hover:bg-teal-700 text-white text-xs font-medium px-4 py-2 rounded-lg transition-colors">
      View Stats →
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
        from postmind.core.ai_engine import AIEngine
        from postmind.core.gmail_client import GmailClient, Message, MessageHeader

        account_email = _get_web_account() or ""

        if scope == "all":
            # Read from local synced DB — no Gmail API calls needed
            from postmind.core.storage import EmailRepo, get_session
            session = get_session()
            repo = EmailRepo(session)
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
        else:
            client = GmailClient()
            profile = client.get_profile()
            account_email = profile.get("emailAddress", "")
            ids = client.list_message_ids(query="in:inbox is:unread", max_results=limit)
            if not ids:
                return [], account_email
            messages = client.get_messages_batch(ids)

        if not messages:
            return [], account_email

        ai = AIEngine()
        classified = ai.classify_emails(messages)

        msg_map = {m.id: m for m in messages}
        PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
        CATEGORY_ICONS = {
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

        results = []
        for c in sorted(classified, key=lambda x: PRIORITY_ORDER.get(x.priority, 3)):
            msg = msg_map.get(c.gmail_id)
            if not msg:
                continue
            results.append({
                "id": c.gmail_id,
                "priority": c.priority,
                "category": c.category,
                "category_icon": CATEGORY_ICONS.get(c.category, "📧"),
                "explanation": c.explanation,
                "suggested_action": c.suggested_action,
                "requires_reply": c.requires_reply,
                "deadline_hint": c.deadline_hint,
                "subject": msg.headers.subject or "(no subject)",
                "sender_name": msg.sender_name or msg.sender_email,
                "sender_email": msg.sender_email,
                "snippet": (msg.snippet or "")[:200],
            })

        return results, account_email

    try:
        loop = asyncio.get_event_loop()
        results, account_email = await loop.run_in_executor(_executor, _run)
    except Exception as exc:
        ctx["error"] = str(exc)
        return _resp(request, "triage.html", {**ctx, "ai_off": False, "results": []})

    ctx.update({
        "ai_off": False,
        "auth_error": False,
        "results": results,
        "account_email": account_email,
        "limit": limit,
        "scope": scope,
        "scanned_at": datetime.now(timezone.utc).strftime("%H:%M"),
    })
    return _resp(request, "triage.html", ctx)


# ── Assistant (floating chat) ──────────────────────────────────────────────────

_PAGES = {
    "/": "Dashboard — inbox overview at a glance",
    "/stats": "Stats — senders ranked by storage impact, with a Purge button",
    "/triage": "Triage — AI-classified unread inbox (priority, category, action)",
    "/agents": "Agents — per-account heartbeat watchers and their voice/soul config",
    "/sync": "Sync — pull the mailbox into the local cache",
    "/accounts": "Accounts — add / switch / remove Gmail and IMAP accounts",
    "/watch": "Watch — start/stop the heartbeat daemon that runs agents",
    "/undo": "Undo History — reverse recent operations within the undo window",
    "/settings": "Settings — AI mode, protected senders, data location",
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
            "No inbox scan data is available yet. The user should open Stats or run a "
            "Sync first so concrete numbers can be quoted."
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
                    "enum": ["/", "/stats", "/triage", "/agents", "/sync", "/accounts", "/watch", "/undo", "/settings"],
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


def _build_chat_system(page: str, account_email: str, ai_mode: str) -> str:
    here = _PAGES.get(page, "the app")
    overview = _chat_overview_text(account_email)
    pages = "\n".join(f"  {p} — {desc}" for p, desc in _PAGES.items())
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
                    return f"Added a '{label}' button linking to {page_path}."
                return "Unknown page."
            return f"Unknown tool: {name}"

        # Tools only work in cloud mode; local mode answers conversationally.
        tools = _CHAT_TOOLS if mode == "cloud" else None
        return ai.chat(messages, system=system, tools=tools, tool_executor=_executor_tool)

    try:
        loop = asyncio.get_event_loop()
        reply = await loop.run_in_executor(_executor, _run)
    except Exception as exc:
        return {"reply": f"Sorry — I hit an error: {exc}", "actions": []}

    return {"reply": reply, "actions": actions}
