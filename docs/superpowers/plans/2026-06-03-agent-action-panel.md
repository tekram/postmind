# Agent Action Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Super Agent an email-level "Action Panel" — a slide-out drawer on `/agent` that lists the actual emails it proposes to trash, grouped-by-sender (expandable to individual), for review and confirmation.

**Architecture:** A new WRITE tool `stage_trash_query` resolves a model-supplied Gmail query live, caches the resolved message set server-side under a token, and emits a `trash_review` card. Two endpoints serve the resolved emails (`GET /agent/review/{token}`) and execute the confirmed subset (`POST /agent/review/{token}/confirm`) through the existing undo-log + `batch_trash` safe path. A drawer in `agent.html` renders the review. The model only ever supplies a search string; message IDs are server-resolved and confirm enforces submitted ⊆ cached.

**Tech Stack:** Python 3.11/3.13, FastAPI + Jinja2, SQLAlchemy 2.0, pytest, ruff. Frontend is vanilla JS + Tailwind classes in `agent.html`. Provider abstraction via `EmailProvider`.

---

## Spec reference

`docs/superpowers/specs/2026-06-03-agent-action-panel-design.md`

## File map

- `postmind/core/agent_tools.py` — new `stage_trash_query` schema in `WRITE_TOOLS`; new stateless helper `resolve_trash_query()`.
- `postmind/web/server.py` — module-level `_REVIEW_CACHE` + `_review_*` helpers; `stage_trash_query` branch in `_build_agent_tool_executor`; `GET /agent/review/{token}`; `POST /agent/review/{token}/confirm`.
- `postmind/web/templates/agent.html` — `trash_review` branch in `cardHtml`; drawer markup + JS.
- `tests/test_agent_action_panel.py` — new test module (resolver, staging, both endpoints).

## Key existing facts (grounding)

- `Message` (`postmind/core/gmail_client.py`): `.id`, `.size_estimate`, `.internal_date` (ms since epoch), `.headers` (`MessageHeader` with `.subject`, `.list_unsubscribe`), `.sender_email` (property), `.sender_name` (property).
- Provider methods: `provider.list_message_ids(query=..., max_results=...) -> list[str]`, `provider.get_messages_metadata(ids) -> list[Message]`, `provider.batch_trash(ids) -> int`, `provider.supports("labels")`.
- Undo log: `UndoLogRepo(get_session()).record(account_email=, operation="trash", message_ids=, description=, metadata=)` — call BEFORE trashing (BulkEngine ordering).
- Sensitive flag: `from postmind.core.sender_stats import _is_sensitive_domain` — `_is_sensitive_domain(domain) -> bool`.
- Server helpers: `_build_provider()`, `_get_web_account()`, scan-cache pattern (`_scan_cache`, `_cache_get`).
- Test patterns (`tests/test_super_agent_phase2.py`): `shared_db` fixture (StaticPool in-memory DB shared across threads — required because confirm runs in a ThreadPoolExecutor), `_cloud_mode` autouse, monkeypatch `server._build_provider`, `server._build_agent_tool_executor(account, MagicMock(), actions, cards)` to test a tool branch directly, `TestClient(server.app)`.

---

### Task 1: `resolve_trash_query` helper + tool schema

**Files:**
- Modify: `postmind/core/agent_tools.py` (add to `WRITE_TOOLS`; add helper near `find_largest_messages`)
- Test: `tests/test_agent_action_panel.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_agent_action_panel.py`:

```python
"""Agent Action Panel — email-level trash review drawer."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from postmind.core.gmail_client import Message, MessageHeader


def _msg(mid, sender, subject="Hi", size=1_000_000, internal_date=1_700_000_000_000, unsub=""):
    return Message(
        id=mid,
        thread_id="t-" + mid,
        label_ids=[],
        snippet="",
        headers=MessageHeader(from_=sender, subject=subject, list_unsubscribe=unsub),
        size_estimate=size,
        internal_date=internal_date,
    )


def test_resolve_trash_query_passes_query_and_maps_fields():
    from postmind.core import agent_tools

    prov = MagicMock()
    prov.list_message_ids.return_value = ["m1", "m2"]
    prov.get_messages_metadata.return_value = [
        _msg("m1", "News <news@promo.com>", subject="Weekly", size=2_000_000),
        _msg("m2", "Bank <alerts@bank.com>", subject="Statement", size=500_000),
    ]

    out = agent_tools.resolve_trash_query(prov, "older_than:2y", False, limit=100)

    prov.list_message_ids.assert_called_once()
    assert prov.list_message_ids.call_args.kwargs["query"] == "older_than:2y"
    assert [e["id"] for e in out] == ["m1", "m2"]
    first = out[0]
    assert first["sender_email"] == "news@promo.com"
    assert first["sender_name"] == "News"
    assert first["subject"] == "Weekly"
    assert first["size_estimate"] == 2_000_000
    assert "internal_date" in first and "date" in first


def test_resolve_trash_query_newsletters_only_filters_to_unsubscribe():
    from postmind.core import agent_tools

    prov = MagicMock()
    prov.list_message_ids.return_value = ["m1", "m2"]
    prov.get_messages_metadata.return_value = [
        _msg("m1", "news@promo.com", unsub="<https://promo.com/unsub>"),
        _msg("m2", "person@work.com", unsub=""),
    ]

    out = agent_tools.resolve_trash_query(prov, "older_than:2y", True, limit=100)
    assert [e["id"] for e in out] == ["m1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent_action_panel.py -q`
Expected: FAIL — `AttributeError: module 'postmind.core.agent_tools' has no attribute 'resolve_trash_query'`.

- [ ] **Step 3: Implement the helper**

In `postmind/core/agent_tools.py`, append after `find_largest_messages`:

```python
def resolve_trash_query(provider, gmail_query: str, newsletters_only: bool = False, limit: int = 200) -> list[dict]:
    """Resolve a Gmail query into individual messages for the trash review panel.

    The model supplies only a search string; we run it and shape the results.
    When ``newsletters_only`` is set, keep only messages that carry a
    List-Unsubscribe header. Returns dicts the panel renders directly.
    """
    from datetime import datetime, timezone

    limit = max(1, min(int(limit or 200), 500))
    scope = (gmail_query or "").strip() or "in:inbox"
    ids = provider.list_message_ids(query=scope, max_results=limit)
    if not ids:
        return []
    messages = provider.get_messages_metadata(ids)
    out: list[dict] = []
    for m in messages:
        if newsletters_only and not (m.headers.list_unsubscribe or "").strip():
            continue
        ms = m.internal_date or 0
        date_str = ""
        if ms:
            date_str = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        out.append(
            {
                "id": m.id,
                "subject": (m.headers.subject or "(no subject)"),
                "sender_email": m.sender_email,
                "sender_name": m.sender_name or m.sender_email,
                "size_estimate": int(m.size_estimate or 0),
                "internal_date": int(ms),
                "date": date_str,
            }
        )
    return out
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_agent_action_panel.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Add the tool schema**

In `postmind/core/agent_tools.py`, add to `WRITE_TOOLS` (after `stage_unsubscribe`, before `draft_email`):

```python
    {
        "name": "stage_trash_query",
        "description": "Stage an email-level trash REVIEW. Use when the user wants to delete a CLASS of mail described by criteria (e.g. 'newsletters older than 2 years', 'promotions from last year'). You do NOT delete — you compose a Gmail search query and the server resolves the matching emails into a review drawer the user approves message-by-message. Deletes go to Trash and are undoable for 30 days. Prefer this over stage_trash when the target is a query/time-range rather than named senders.",
        "input_schema": {
            "type": "object",
            "properties": {
                "gmail_query": {"type": "string", "description": "Gmail search operators that select the emails, e.g. 'older_than:2y', 'category:promotions older_than:1y'. A search string only — never message IDs."},
                "newsletters_only": {"type": "boolean", "description": "When true, keep only messages that have a List-Unsubscribe header (true newsletters/subscriptions). Default false."},
                "description": {"type": "string", "description": "Short human label for the review, e.g. 'newsletters older than 2 years'."},
            },
            "required": ["gmail_query", "description"],
        },
    },
```

- [ ] **Step 6: Verify schema is well-formed**

Run: `python -c "from postmind.core import agent_tools as a; n=[t['name'] for t in a.WRITE_TOOLS]; print('stage_trash_query' in n); print(len(set(n))==len(n))"`
Expected: two lines, both `True`.

- [ ] **Step 7: Commit**

```bash
git add postmind/core/agent_tools.py tests/test_agent_action_panel.py
git commit -m "feat(agent): resolve_trash_query helper + stage_trash_query tool schema

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Review cache + `stage_trash_query` executor branch

**Files:**
- Modify: `postmind/web/server.py` (module-level cache near `_scan_cache`; helpers; new branch in `_build_agent_tool_executor`)
- Test: `tests/test_agent_action_panel.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_agent_action_panel.py`:

```python
def test_stage_trash_query_caches_and_emits_review_card(monkeypatch):
    from postmind.web import server

    account = "me@example.com"
    monkeypatch.setattr(server, "_get_web_account", lambda: account)
    server._REVIEW_CACHE.clear()

    prov = MagicMock()
    prov.supports.return_value = True
    prov.list_message_ids.return_value = ["m1", "m2", "m3"]
    prov.get_messages_metadata.return_value = [
        _msg("m1", "news@promo.com", subject="A", unsub="<u>"),
        _msg("m2", "news@promo.com", subject="B", unsub="<u>"),
        _msg("m3", "alerts@bank.com", subject="C", unsub="<u>"),
    ]
    monkeypatch.setattr(server, "_build_provider", lambda: prov)

    cards: list[dict] = []
    executor = server._build_agent_tool_executor(account, MagicMock(), [], cards)
    summary = executor(
        "stage_trash_query",
        {"gmail_query": "older_than:2y", "newsletters_only": True, "description": "old newsletters"},
    )

    assert len(cards) == 1
    card = cards[0]
    assert card["type"] == "trash_review"
    token = card["fields"]["token"]
    assert card["fields"]["total_count"] == 3
    assert card["fields"]["sender_count"] == 2
    assert card["fields"]["description"] == "old newsletters"
    # Resolved set cached under the token, scoped to the account.
    entry = server._REVIEW_CACHE[token]
    assert entry["account_email"] == account
    assert {e["id"] for e in entry["emails"]} == {"m1", "m2", "m3"}
    assert "3" in summary  # mentions the count for the model


def test_stage_trash_query_empty_match_stages_nothing(monkeypatch):
    from postmind.web import server

    account = "me@example.com"
    monkeypatch.setattr(server, "_get_web_account", lambda: account)
    prov = MagicMock()
    prov.supports.return_value = True
    prov.list_message_ids.return_value = []
    monkeypatch.setattr(server, "_build_provider", lambda: prov)

    cards: list[dict] = []
    executor = server._build_agent_tool_executor(account, MagicMock(), [], cards)
    summary = executor("stage_trash_query", {"gmail_query": "older_than:99y", "description": "x"})
    assert cards == []
    assert "nothing" in summary.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_agent_action_panel.py -k stage_trash_query -q`
Expected: FAIL — `AttributeError: module 'postmind.web.server' has no attribute '_REVIEW_CACHE'`.

- [ ] **Step 3: Add the review cache + helpers**

In `postmind/web/server.py`, near `_scan_cache` definition (top of the Helpers section, around line 60), add:

```python
_REVIEW_CACHE: dict[str, dict] = {}  # token -> {account_email, description, emails, expires}
_REVIEW_TTL = 1800  # seconds


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
```

- [ ] **Step 4: Add the executor branch**

In `_build_agent_tool_executor` (`postmind/web/server.py`), add a branch. Place it immediately after the `stage_trash` branch (after the `return f"Staged {len(matched)} sender(s) …"` line, ~line 3627):

```python
        if name == "stage_trash_query":
            gmail_query = (tool_input.get("gmail_query") or "").strip()
            description = (tool_input.get("description") or gmail_query or "matching emails").strip()
            newsletters_only = bool(tool_input.get("newsletters_only"))
            if not gmail_query:
                return "I need a search query (e.g. 'older_than:2y') to find emails to trash."
            try:
                provider = _build_provider()
            except Exception as exc:
                return f"Couldn't reach the mailbox to resolve that query: {exc}"
            if not provider.supports("labels"):
                return "Email-level trash review is Gmail-only right now. For other accounts, name the senders and I'll stage a sender-level trash instead."
            try:
                emails = agent_tools.resolve_trash_query(provider, gmail_query, newsletters_only, limit=200)
            except Exception as exc:
                return f"Couldn't resolve that query: {exc}"
            if not emails:
                return f"Nothing matched '{gmail_query}' — nothing staged."
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
```

Note: `agent_tools` is already imported at the top of `_build_agent_tool_executor` — do not re-import it inside the branch (that would shadow it and reintroduce the UnboundLocalError fixed in commit 044ef73).

- [ ] **Step 5: Run to verify it passes**

Run: `python -m pytest tests/test_agent_action_panel.py -k stage_trash_query -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add postmind/web/server.py tests/test_agent_action_panel.py
git commit -m "feat(agent): stage_trash_query executor branch + review cache

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `GET /agent/review/{token}` endpoint

**Files:**
- Modify: `postmind/web/server.py` (add route; place near the other `/agent/...` routes, e.g. after `/agent/action/confirm`)
- Test: `tests/test_agent_action_panel.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_agent_action_panel.py`:

```python
@pytest.fixture()
def shared_db(monkeypatch):
    import postmind.core.storage as storage

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    storage.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(storage, "_engine", engine)
    monkeypatch.setattr(storage, "_SessionLocal", factory)
    yield engine
    engine.dispose()


def test_get_review_groups_and_flags_sensitive(monkeypatch, shared_db):
    from postmind.web import server

    account = "me@example.com"
    monkeypatch.setattr(server, "_get_web_account", lambda: account)
    emails = [
        {"id": "m1", "subject": "A", "sender_email": "news@promo.com", "sender_name": "Promo", "size_estimate": 2_000_000, "internal_date": 1, "date": "2022-01-01"},
        {"id": "m2", "subject": "B", "sender_email": "news@promo.com", "sender_name": "Promo", "size_estimate": 1_000_000, "internal_date": 1, "date": "2022-01-02"},
        {"id": "m3", "subject": "C", "sender_email": "alerts@bank.com", "sender_name": "Bank", "size_estimate": 500_000, "internal_date": 1, "date": "2022-01-03"},
    ]
    token = server._review_put(account, "old stuff", emails)

    client = TestClient(server.app)
    resp = client.get(f"/agent/review/{token}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_count"] == 3
    assert data["description"] == "old stuff"
    groups = {g["sender_email"]: g for g in data["groups"]}
    assert groups["news@promo.com"]["count"] == 2
    assert len(groups["news@promo.com"]["emails"]) == 2
    assert groups["news@promo.com"]["sensitive"] is False
    assert groups["alerts@bank.com"]["sensitive"] is True  # bank domain


def test_get_review_unknown_token_404(monkeypatch, shared_db):
    from postmind.web import server

    monkeypatch.setattr(server, "_get_web_account", lambda: "me@example.com")
    client = TestClient(server.app)
    assert client.get("/agent/review/nope").status_code == 404
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_agent_action_panel.py -k get_review -q`
Expected: FAIL — 404 for the valid token (route not defined) / assertion errors.

- [ ] **Step 3: Implement the route**

In `postmind/web/server.py`, after the `/agent/action/confirm` route, add:

```python
@app.get("/agent/review/{token}")
async def agent_review_get(token: str):
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    from postmind.core.sender_stats import _is_sensitive_domain

    entry = _review_get(token)
    if not entry:
        raise HTTPException(status_code=404, detail="This review expired or was not found.")

    by_sender: dict[str, dict] = {}
    for e in entry["emails"]:
        sender = e["sender_email"]
        g = by_sender.get(sender)
        if g is None:
            domain = sender.split("@")[-1] if "@" in sender else sender
            g = by_sender[sender] = {
                "sender_email": sender,
                "sender_name": e.get("sender_name") or sender,
                "count": 0,
                "size_bytes": 0,
                "sensitive": _is_sensitive_domain(domain),
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
```

- [ ] **Step 4: Add the `_fmt_size` helper if it does not already exist**

Run: `grep -n "def _fmt_size" postmind/web/server.py`
If absent, add near the other helpers (after `_review_get`):

```python
def _fmt_size(num_bytes: int) -> str:
    mb = (num_bytes or 0) / (1024 * 1024)
    if mb >= 0.1:
        return f"{mb:.1f} MB"
    return f"{(num_bytes or 0) // 1024} KB"
```

If a size formatter already exists with a different name, use that one in Step 3 instead and skip this step.

- [ ] **Step 5: Run to verify it passes**

Run: `python -m pytest tests/test_agent_action_panel.py -k get_review -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add postmind/web/server.py tests/test_agent_action_panel.py
git commit -m "feat(agent): GET /agent/review/{token} returns grouped review JSON

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `POST /agent/review/{token}/confirm` endpoint

**Files:**
- Modify: `postmind/web/server.py` (add route after the GET route)
- Test: `tests/test_agent_action_panel.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_agent_action_panel.py`:

```python
def test_confirm_trashes_subset_and_writes_undo(monkeypatch, shared_db):
    from postmind.web import server
    from postmind.core.storage import UndoLogRepo, get_session

    account = "me@example.com"
    monkeypatch.setattr(server, "_get_web_account", lambda: account)
    emails = [
        {"id": "m1", "subject": "A", "sender_email": "news@promo.com", "sender_name": "Promo", "size_estimate": 1, "internal_date": 1, "date": "x"},
        {"id": "m2", "subject": "B", "sender_email": "news@promo.com", "sender_name": "Promo", "size_estimate": 1, "internal_date": 1, "date": "x"},
    ]
    token = server._review_put(account, "old", emails)

    trashed_ids = {}
    prov = MagicMock()
    prov.batch_trash.side_effect = lambda ids: trashed_ids.setdefault("ids", list(ids)) or len(ids)
    monkeypatch.setattr(server, "_build_provider", lambda: prov)

    client = TestClient(server.app)
    # Submit one real id plus one foreign id that must be rejected.
    resp = client.post(f"/agent/review/{token}/confirm", data={"ids": ["m1", "evil-id"]})
    assert resp.status_code == 200
    data = resp.json()
    assert data["trashed"] == 1
    assert data["undo_href"] == "/undo"
    assert trashed_ids["ids"] == ["m1"]  # foreign id dropped

    logs = UndoLogRepo(get_session()).list_recent(account, limit=5)
    assert any(set(l.message_ids) == {"m1"} for l in logs)


def test_confirm_unknown_token_404(monkeypatch, shared_db):
    from postmind.web import server

    monkeypatch.setattr(server, "_get_web_account", lambda: "me@example.com")
    client = TestClient(server.app)
    assert client.post("/agent/review/nope/confirm", data={"ids": ["m1"]}).status_code == 404
```

- [ ] **Step 2: Confirm the UndoLogRepo read API name**

Run: `grep -n "def list_recent\|def record\|class UndoLogRepo" postmind/core/storage.py`
Expected: shows `record(...)` and a recent-list method. If the listing method is not named `list_recent`, update the test's final assertion to use the actual method name (e.g. `list_for_account`). Keep the assertion semantics (an entry exists whose `message_ids == {"m1"}`).

- [ ] **Step 3: Run to verify it fails**

Run: `python -m pytest tests/test_agent_action_panel.py -k confirm -q`
Expected: FAIL — 404 for the valid token (route not defined).

- [ ] **Step 4: Implement the route**

In `postmind/web/server.py`, after the GET route, add:

```python
@app.post("/agent/review/{token}/confirm")
async def agent_review_confirm(token: str, ids: list[str] = Form(default=[])):
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    from postmind.core.storage import UndoLogRepo, get_session

    entry = _review_get(token)
    if not entry:
        raise HTTPException(status_code=404, detail="This review expired or was not found.")

    account_email = entry["account_email"]
    cached_ids = {e["id"] for e in entry["emails"]}
    # Trust boundary: only ids that were server-resolved into this token may run.
    selected = [i for i in (ids or []) if i in cached_ids]
    if not selected:
        return JSONResponse({"trashed": 0, "undo_href": "/undo"})

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

    count = await asyncio.get_event_loop().run_in_executor(_executor, _work)
    # Drop the consumed ids so a re-submit can't double-trash.
    entry["emails"] = [e for e in entry["emails"] if e["id"] not in set(selected)]
    return JSONResponse({"trashed": count, "undo_href": "/undo"})
```

Note: confirm `Form`, `asyncio`, and `_executor` are imported/defined in `server.py` (they are used by existing confirm endpoints). If `Form` is not already imported, add it to the existing `from fastapi import ...` line. Verify with: `grep -n "^import asyncio\|from fastapi import\|_executor = ThreadPoolExecutor" postmind/web/server.py`.

- [ ] **Step 5: Run to verify it passes**

Run: `python -m pytest tests/test_agent_action_panel.py -k confirm -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Run the whole new module + lint**

Run: `python -m pytest tests/test_agent_action_panel.py -q && make fix && make lint`
Expected: all tests pass; lint clean.

- [ ] **Step 7: Commit**

```bash
git add postmind/web/server.py tests/test_agent_action_panel.py
git commit -m "feat(agent): POST /agent/review/{token}/confirm trashes approved subset

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Frontend — review card + slide-out drawer

**Files:**
- Modify: `postmind/web/templates/agent.html`

No unit test (vanilla-JS template). Verified manually in Task 6.

- [ ] **Step 1: Add the `trash_review` card branch**

In `agent.html`, inside `function cardHtml(card)`, before the final `return "";`, add:

```javascript
    if (card.type === "trash_review") {
      const f = card.fields || {};
      return '<div class="mt-2 border border-accent-border bg-surface rounded-card p-4 shadow-whisper max-w-md">' +
        '<p class="flex items-center gap-1.5 text-sm font-semibold text-ink mb-1">' + CARD_SPARK + esc(card.title || "Review emails") + '</p>' +
        '<p class="text-xs text-ink-subtle mb-3">' + esc(f.total_count) + ' emails · ' + esc(f.sender_count) + ' sender(s) · reversible, undoable for 30 days.</p>' +
        '<button type="button" class="pm-btn px-4 py-1.5 open-review" data-token="' + esc(f.token) + '" data-desc="' + esc(f.description || "") + '">Open review &rarr;</button>' +
      '</div>';
    }
```

- [ ] **Step 2: Add the drawer markup**

In `agent.html`, immediately before the closing `</div>` of the top-level `<div class="flex flex-col h-screen">` (i.e. just before line `<script>`'s preceding `</div>`), add the drawer + scrim:

```html
  <!-- Action Panel drawer -->
  <div id="review-scrim" class="fixed inset-0 bg-black/30 z-40 hidden"></div>
  <aside id="review-drawer" class="fixed top-0 right-0 h-full w-full max-w-md bg-surface border-l border-hairline shadow-xl z-50 translate-x-full transition-transform duration-200 flex flex-col">
    <div class="px-5 py-4 border-b border-hairline flex items-center justify-between">
      <div>
        <h2 class="text-sm font-semibold text-ink">Review &amp; trash</h2>
        <p id="review-desc" class="text-xs text-ink-subtle"></p>
      </div>
      <div class="flex items-center gap-1 text-xs">
        <button type="button" class="review-view pm-pill border border-hairline" data-view="grouped">Grouped</button>
        <button type="button" class="review-view pm-pill border border-hairline" data-view="individual">Individual</button>
        <button id="review-close" type="button" class="ml-2 text-ink-subtle hover:text-ink p-1" aria-label="Close">&times;</button>
      </div>
    </div>
    <div id="review-body" class="flex-1 overflow-y-auto px-5 py-3 text-sm"></div>
    <div class="px-5 py-3 border-t border-hairline flex items-center justify-between gap-3">
      <span id="review-summary" class="text-xs text-ink-subtle tabular"></span>
      <button id="review-confirm" type="button" class="pm-btn px-4 py-1.5" disabled>Move to Trash</button>
    </div>
  </aside>
```

- [ ] **Step 3: Add the drawer JS**

In `agent.html`, inside the IIFE `(function () { … })();`, before the closing `})();`, add the drawer controller:

```javascript
  // ── Action Panel (review drawer) ──────────────────────────────────────────
  const drawer = document.getElementById("review-drawer");
  const scrim = document.getElementById("review-scrim");
  const reviewBody = document.getElementById("review-body");
  const reviewDesc = document.getElementById("review-desc");
  const reviewSummary = document.getElementById("review-summary");
  const reviewConfirm = document.getElementById("review-confirm");
  let reviewState = { token: null, groups: [], view: "grouped" };

  function openDrawer() { drawer.classList.remove("translate-x-full"); scrim.classList.remove("hidden"); }
  function closeDrawer() { drawer.classList.add("translate-x-full"); scrim.classList.add("hidden"); }
  document.getElementById("review-close").addEventListener("click", closeDrawer);
  scrim.addEventListener("click", closeDrawer);

  function selectedIds() {
    return Array.from(reviewBody.querySelectorAll('input.review-email:checked')).map(c => c.value);
  }
  function refreshSummary() {
    const n = selectedIds().length;
    reviewSummary.textContent = n + " selected";
    reviewConfirm.disabled = n === 0;
  }

  function renderReview() {
    const g = reviewState.groups;
    let h = "";
    if (reviewState.view === "grouped") {
      for (const grp of g) {
        const badge = grp.sensitive ? ' <span class="text-[10px] text-warning bg-warning-bg border border-warning-border rounded px-1">sensitive</span>' : "";
        h += '<details class="border-b border-hairline py-2" ' + (grp.sensitive ? "" : "open") + '>' +
          '<summary class="flex items-center justify-between gap-2 cursor-pointer list-none">' +
            '<span class="flex items-center gap-2 min-w-0"><input type="checkbox" class="review-master rounded text-accent" ' + (grp.sensitive ? "" : "checked") + '>' +
              '<span class="text-xs text-ink-muted truncate">' + esc(grp.sender_email) + badge + '</span></span>' +
            '<span class="text-[11px] text-ink-tertiary shrink-0 tabular">' + esc(grp.count) + ' · ' + esc(grp.size_str) + '</span>' +
          '</summary><div class="pl-6 pt-1">';
        for (const e of grp.emails) {
          h += '<label class="flex items-center justify-between gap-2 py-1 cursor-pointer">' +
            '<span class="flex items-center gap-2 min-w-0"><input type="checkbox" class="review-email rounded text-accent" value="' + esc(e.id) + '" ' + (grp.sensitive ? "" : "checked") + '>' +
            '<span class="text-xs text-ink-muted truncate">' + esc(e.subject) + '</span></span>' +
            '<span class="text-[11px] text-ink-tertiary shrink-0 tabular">' + esc(e.date) + ' · ' + esc(e.size_str) + '</span></label>';
        }
        h += '</div></details>';
      }
    } else {
      for (const grp of g) {
        for (const e of grp.emails) {
          h += '<label class="flex items-center justify-between gap-2 py-1.5 border-b border-hairline cursor-pointer">' +
            '<span class="flex items-center gap-2 min-w-0"><input type="checkbox" class="review-email rounded text-accent" value="' + esc(e.id) + '" ' + (grp.sensitive ? "" : "checked") + '>' +
            '<span class="min-w-0"><span class="text-xs text-ink-muted truncate block">' + esc(e.subject) + '</span><span class="text-[10px] text-ink-tertiary">' + esc(grp.sender_email) + '</span></span></span>' +
            '<span class="text-[11px] text-ink-tertiary shrink-0 tabular">' + esc(e.date) + ' · ' + esc(e.size_str) + '</span></label>';
        }
      }
    }
    reviewBody.innerHTML = h || '<p class="text-ink-subtle text-sm py-6 text-center">No emails to review.</p>';
    // master checkbox toggles its group
    reviewBody.querySelectorAll(".review-master").forEach(m => m.addEventListener("change", () => {
      m.closest("details").querySelectorAll(".review-email").forEach(c => { c.checked = m.checked; });
      refreshSummary();
    }));
    reviewBody.querySelectorAll(".review-email").forEach(c => c.addEventListener("change", refreshSummary));
    refreshSummary();
  }

  document.querySelectorAll(".review-view").forEach(b => b.addEventListener("click", () => {
    reviewState.view = b.dataset.view; renderReview();
  }));

  async function loadReview(token, desc) {
    reviewState = { token, groups: [], view: "grouped" };
    reviewDesc.textContent = desc || "";
    reviewBody.innerHTML = '<p class="text-ink-subtle text-sm py-6 text-center">Loading…</p>';
    openDrawer();
    try {
      const res = await fetch("/agent/review/" + encodeURIComponent(token));
      if (!res.ok) throw new Error("not found");
      const data = await res.json();
      reviewState.groups = data.groups || [];
      reviewDesc.textContent = (data.description || desc || "") + " · " + (data.total_count || 0) + " emails";
      renderReview();
    } catch (e) {
      reviewBody.innerHTML = '<p class="text-warning text-sm py-6 text-center">This review expired. Ask the agent again.</p>';
    }
  }

  reviewConfirm.addEventListener("click", async () => {
    const ids = selectedIds();
    if (!ids.length) return;
    reviewConfirm.disabled = true;
    const body = new URLSearchParams();
    ids.forEach(i => body.append("ids", i));
    try {
      const res = await fetch("/agent/review/" + encodeURIComponent(reviewState.token) + "/confirm", {
        method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body,
      });
      const data = await res.json();
      closeDrawer();
      const msg = "Moved " + (data.trashed || 0) + " email(s) to Trash — undoable for 30 days.";
      bubble("assistant", msg, [{ label: "Undo", href: data.undo_href || "/undo" }]);
      history.push({ role: "assistant", content: msg, actions: [{ label: "Undo", href: data.undo_href || "/undo" }], cards: [] });
      save();
    } catch (e) {
      reviewConfirm.disabled = false;
      bubble("assistant", "Couldn't move those to Trash — please try again.");
    }
  });

  // Delegate the "Open review" button (cards are injected dynamically).
  msgs.addEventListener("click", (e) => {
    const btn = e.target.closest(".open-review");
    if (btn) loadReview(btn.dataset.token, btn.dataset.desc);
  });
```

- [ ] **Step 4: Add an example prompt**

In `agent.html`, in the `#agent-examples` list, add to the Jinja list:

```
        "Find newsletters older than 2 years and let me review them",
```

- [ ] **Step 5: Commit**

```bash
git add postmind/web/templates/agent.html
git commit -m "feat(agent): Action Panel drawer — email-level trash review UI

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Full check + manual verification

- [ ] **Step 1: Run the full local CI**

Run: `make check`
Expected: lint + format + security + tests all pass. Fix any failures before continuing.

- [ ] **Step 2: Manual smoke (server)**

Run: `postmind serve` then open `http://localhost:8000/agent`. With a Gmail account synced and chat mode set to cloud/local, ask: "Find newsletters older than 2 years and let me review them." Confirm:
- A "Review: …" card appears with an "Open review →" button.
- Clicking opens the right drawer; emails are grouped by sender; bank/health senders show a "sensitive" badge and are pre-unchecked.
- Toggling Grouped/Individual re-renders; master checkbox toggles a group.
- "Move to Trash" trashes only checked emails; the chat shows a confirmation with a working Undo link; `/undo` lists the operation.

- [ ] **Step 3: Final commit if any fixes were needed**

```bash
git add -A
git commit -m "chore(agent): action panel polish + check fixes

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-review notes

- **Spec coverage:** tool schema (T1), live resolution + newsletter filter (T1), token cache (T2), `trash_review` card (T2), GET grouped JSON + sensitive flag + 404 (T3), confirm with undo log + subset enforcement + 404 (T4), drawer with grouped/individual toggle + sensitive pre-uncheck + confirmation/undo (T5), full check + manual verify (T6). All spec sections mapped.
- **Trust boundary:** model supplies only `gmail_query`; IDs resolved server-side (T1/T2) and confirm enforces submitted ⊆ cached (T4).
- **Safety:** undo log written before `batch_trash` (T4); consumed ids dropped to prevent double-trash.
- **Type consistency:** card type `trash_review`; cache shape `{account_email, description, emails, expires}`; email dict keys (`id, subject, sender_email, sender_name, size_estimate, internal_date, date`) consistent across T1→T4; group dict keys (`sender_email, sender_name, count, size_bytes, size_str, sensitive, emails`) consistent T3→T5.
