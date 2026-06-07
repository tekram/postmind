"""Tests for AgentService.stage_trash_query / _exec_trash_query.

Uses the same helpers as test_agent_service.py: a FakeProvider that records
calls, a _seed() that populates EmailRecords in the in-memory test DB, and
a _svc() factory that wires them together.

The FakeProvider here is extended with list_message_ids / get_messages_metadata
so agent_tools.resolve_trash_query can exercise its Gmail-query path.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import postmind.config as cfg
from postmind.core.agent_service import AgentService
from postmind.core.storage import (  # noqa: F401 (unused imports used by fixtures)
    EmailRecord,
    UndoLogRepo,
    get_session,
)

# ── FakeProvider ──────────────────────────────────────────────────────────────


class FakeProvider:
    """Minimal provider stub: supports Gmail-style capabilities and records calls."""

    def __init__(self, message_ids=None, message_meta=None):
        self.trashed: list[str] = []
        self._ids = list(message_ids or [])
        self._meta = list(message_meta or [])

    def supports(self, capability: str) -> bool:
        return capability in ("labels", "unsubscribe")

    def batch_trash(self, ids):
        self.trashed.extend(ids)
        return len(ids)

    def list_message_ids(self, query="", max_results=200):
        return self._ids[:max_results]

    def get_messages_metadata(self, ids):
        return [m for m in self._meta if m.id in ids]


def _make_meta(mid: str, sender_email: str = "news@example.com"):
    """Build a minimal message-metadata stub for resolve_trash_query."""
    m = MagicMock()
    m.id = mid
    m.sender_email = sender_email
    m.sender_name = sender_email.split("@")[0]
    m.size_estimate = 1024
    m.internal_date = 1_700_000_000_000  # some ms epoch
    m.headers = MagicMock()
    m.headers.subject = f"Subject for {mid}"
    m.headers.list_unsubscribe = ""
    return m


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _seed(account="me@x.com", senders=None):
    """Seed inbox EmailRecords."""
    senders = senders or {"news@promo.com": 5, "deals@shop.com": 3}
    session = get_session()
    n = 0
    for email, count in senders.items():
        for i in range(count):
            session.add(
                EmailRecord(
                    account_email=account,
                    gmail_id=f"g{n}",
                    thread_id=f"t{n}",
                    sender_email=email,
                    sender_name=email.split("@")[0],
                    is_inbox=True,
                    is_unread=True,
                    size_estimate=1000,
                )
            )
            n += 1
    session.commit()
    cfg.set_active_account(account)


def _svc(account="me@x.com", provider=None):
    svc = AgentService(account_email=account)
    svc._provider = provider or FakeProvider()
    return svc


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_stage_trash_query_resolves_emails(clean_db):
    """stage_trash_query returns a staged descriptor with kind=trash_query and
    the correct email_count, token, and undoable flag."""
    _seed()
    mids = ["m1", "m2", "m3"]
    meta = [_make_meta(mid, sender_email="news@promo.com") for mid in mids]
    provider = FakeProvider(message_ids=mids, message_meta=meta)
    svc = _svc(provider=provider)

    desc = svc.stage_trash_query("older_than:1y", "old emails")

    assert "error" not in desc
    assert desc["kind"] == "trash_query"
    assert desc["email_count"] == 3
    assert desc["undoable"] is True
    assert desc["token"]
    # No provider write yet — staging only.
    assert provider.trashed == []


def test_stage_trash_query_empty_query_returns_error(clean_db):
    """An empty gmail_query must return an error dict, not raise."""
    _seed()
    svc = _svc()

    result = svc.stage_trash_query("", "")

    assert "error" in result
    assert "query" in result["error"].lower() or "required" in result["error"].lower()


def test_stage_trash_query_no_results_returns_error(clean_db):
    """When the provider finds no emails for the query, return an error dict."""
    _seed()
    provider = FakeProvider(message_ids=[], message_meta=[])
    svc = _svc(provider=provider)

    result = svc.stage_trash_query("older_than:10y", "very old emails")

    assert "error" in result
    assert "matched" in result["error"].lower() or "No emails" in result["error"]


def test_confirm_trash_query_executes_and_records_undo(clean_db):
    """confirm() after stage_trash_query calls provider.batch_trash with the
    resolved IDs and records an UndoLogEntry."""
    _seed()
    mids = ["a1", "a2", "a3"]
    meta = [_make_meta(mid) for mid in mids]
    provider = FakeProvider(message_ids=mids, message_meta=meta)
    svc = _svc(provider=provider)

    desc = svc.stage_trash_query("category:promotions older_than:1y", "old promos")
    assert "token" in desc

    result = svc.confirm(desc["token"])

    assert result["ok"] is True
    assert result["action"] == "trash_query"
    assert result["affected"] == 3
    assert set(provider.trashed) == set(mids)

    entry = UndoLogRepo(get_session()).get(result["undo_id"])
    assert entry is not None
    assert entry.operation == "trash"


def test_confirm_trash_query_undoable(clean_db):
    """confirm() result must have undoable=True and a non-None undo_id."""
    _seed()
    mids = ["x1", "x2"]
    meta = [_make_meta(mid) for mid in mids]
    provider = FakeProvider(message_ids=mids, message_meta=meta)
    svc = _svc(provider=provider)

    desc = svc.stage_trash_query("has:list-unsubscribe older_than:2y", "old newsletters")
    result = svc.confirm(desc["token"])

    assert result.get("undoable") is True
    assert result.get("undo_id") is not None
