"""Local web interface for mailtrim — runs on localhost, nothing leaves your machine."""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from mailtrim import __version__
from mailtrim.config import CREDENTIALS_PATH, DATA_DIR, TOKEN_PATH, get_settings

_THIS_DIR = Path(__file__).parent
_TEMPLATES_DIR = _THIS_DIR / "templates"

app = FastAPI(title="mailtrim", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# In-memory scan cache — keyed by "latest", short TTL
_scan_cache: dict[str, dict] = {}
_CACHE_TTL = 300  # 5 minutes

_executor = ThreadPoolExecutor(max_workers=2)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _cache_set(groups, profile: dict, account_email: str) -> None:
    _scan_cache["latest"] = {
        "groups": groups,
        "profile": profile,
        "account_email": account_email,
        "scanned_at": datetime.now(timezone.utc).strftime("%H:%M"),
        "expires": time.time() + _CACHE_TTL,
    }


def _cache_get() -> dict | None:
    entry = _scan_cache.get("latest")
    if entry and time.time() < entry["expires"]:
        return entry
    return None


def _is_authed() -> bool:
    return TOKEN_PATH.exists()


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


def _base() -> dict:
    """Base template context — request passed separately to TemplateResponse."""
    return {
        "version": __version__,
        "ai_mode": _ai_mode(),
        "provider": _provider_name(),
        "is_authed": _is_authed(),
    }


def _resp(request: Request, name: str, ctx: dict, status: int = 200) -> HTMLResponse:
    """Render a template using Starlette 1.x API."""
    return templates.TemplateResponse(request, name, context=ctx, status_code=status)


def _build_provider():
    import os

    from mailtrim.core.providers.factory import get_provider

    s = get_settings()
    if s.provider == "imap":
        pw = os.environ.get("MAILTRIM_IMAP_PASSWORD", "")
        return get_provider(
            "imap",
            imap_server=s.imap_server,
            imap_user=s.imap_user,
            imap_password=pw,
            imap_port=s.imap_port,
            imap_folder=s.imap_folder,
        )
    return get_provider("gmail")


def _enrich_groups(groups) -> list[dict]:
    from mailtrim.core.sender_stats import (
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

    cached = _cache_get()
    if cached:
        from mailtrim.core.sender_stats import (
            best_next_step,
            generate_recommendations,
            group_by_domain,
            reclaimable_mb,
        )

        groups = cached["groups"]
        domain_groups = group_by_domain(groups)
        domain_map = {d.domain: d for d in domain_groups}
        recs = generate_recommendations(groups, top_n=5, domain_map=domain_map)
        bns = best_next_step(recs)
        total_reclaimable = reclaimable_mb(recs)

        ctx.update({
            "has_scan": True,
            "scanned_at": cached["scanned_at"],
            "account_email": cached["account_email"],
            "profile": cached["profile"],
            "top_senders": _enrich_groups(groups[:3]),
            "total_reclaimable": total_reclaimable,
            "sender_count": len(groups),
            "best_next": bns,
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
    top: int = 25,
):
    if not _is_authed():
        return _resp(
            request,
            "stats_error.html",
            {"error": "Not authenticated. Run mailtrim auth in your terminal first."},
        )

    def _scan():
        from mailtrim.core.sender_stats import (
            fetch_sender_groups,
            generate_recommendations,
            group_by_domain,
            reclaimable_mb,
        )
        from mailtrim.core.storage import BlocklistRepo, get_session

        client = _build_provider()
        profile = client.get_profile()
        account_email = profile.get("emailAddress", "")

        query = "in:anywhere -in:trash -in:spam" if scope == "anywhere" else "in:inbox"
        if since:
            query += f" newer_than:{since}"

        valid_sort = sort if sort in ("score", "count", "size", "oldest") else "score"

        groups = fetch_sender_groups(
            client,
            query=query,
            max_messages=1000,
            min_count=1,
            top_n=top,
            sort_by=valid_sort,
        )

        blocked = BlocklistRepo(get_session()).blocked_emails(account_email)
        if blocked:
            groups = [g for g in groups if g.sender_email not in blocked]

        _cache_set(groups, profile, account_email)

        domain_groups = group_by_domain(groups)
        domain_map = {d.domain: d for d in domain_groups}
        recs = generate_recommendations(groups, top_n=5, domain_map=domain_map)
        total_reclaimable = reclaimable_mb(recs)

        return {
            "senders": _enrich_groups(groups),
            "total_reclaimable": total_reclaimable,
            "account_email": account_email,
            "total_scanned": sum(g.count for g in groups),
            "scanned_at": datetime.now(timezone.utc).strftime("%H:%M"),
        }

    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(_executor, _scan)
    except Exception as exc:
        return _resp(request, "stats_error.html", {"error": str(exc)})

    return _resp(request, "stats_table.html", data)


# ── Purge ─────────────────────────────────────────────────────────────────────


@app.post("/purge/preview", response_class=HTMLResponse)
async def purge_preview(request: Request):
    form = await request.form()
    senders = form.getlist("senders")

    if not senders:
        return RedirectResponse("/stats", status_code=303)

    cached = _cache_get()
    if not cached:
        return _resp(request, "error.html", {"error": "Scan data expired. Please re-run Stats."})

    groups = cached["groups"]
    selected_groups = [g for g in groups if g.sender_email in senders]
    total_count = sum(g.count for g in selected_groups)
    total_mb = round(sum(g.total_size_bytes for g in selected_groups) / (1024 * 1024), 1)

    ctx = _base()
    ctx.update({
        "active": "stats",
        "selected": _enrich_groups(selected_groups),
        "senders": senders,
        "total_count": total_count,
        "total_mb": total_mb,
        "undo_days": get_settings().undo_window_days,
    })
    return _resp(request, "purge_preview.html", ctx)


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
        from mailtrim.core.storage import UndoLogRepo, get_session

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
        _scan_cache.clear()
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
        from mailtrim.core.storage import UndoLogRepo, get_session

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
        from mailtrim.core.bulk_engine import BulkEngine
        from mailtrim.core.gmail_client import GmailClient

        client = GmailClient()
        account_email = client.get_email_address()
        engine = BulkEngine(client, account_email)
        return engine.undo(entry_id)

    try:
        loop = asyncio.get_event_loop()
        count = await loop.run_in_executor(_executor, _do_undo)
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
        s = get_settings()
        ctx.update({
            "ai_mode": s.ai_mode,
            "provider": s.provider,
            "imap_server": s.imap_server,
            "imap_user": s.imap_user,
            "undo_days": s.undo_window_days,
            "has_api_key": bool(s.anthropic_api_key),
        })
    except Exception:
        ctx.update({
            "ai_mode": "off", "provider": "gmail",
            "imap_server": "", "imap_user": "",
            "undo_days": 30, "has_api_key": False,
        })

    ctx.update({
        "data_dir": str(DATA_DIR),
        "credentials_exist": CREDENTIALS_PATH.exists(),
        "token_exists": TOKEN_PATH.exists(),
    })
    return _resp(request, "settings.html", ctx)


@app.post("/settings/ai-mode")
async def update_ai_mode(request: Request):
    form = await request.form()
    mode = form.get("mode", "off")

    if mode not in ("off", "local", "cloud"):
        raise HTTPException(status_code=400, detail="Invalid AI mode")

    env_file = DATA_DIR / ".env"
    lines: list[str] = env_file.read_text().splitlines(keepends=True) if env_file.exists() else []

    key = "MAILTRIM_AI_MODE"
    new_line = f"{key}={mode}\n"
    updated = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = new_line
            updated = True
            break
    if not updated:
        lines.append(new_line)

    env_file.write_text("".join(lines))

    import mailtrim.config as _cfg
    _cfg._settings = None

    return RedirectResponse("/settings?success=ai_mode", status_code=303)
