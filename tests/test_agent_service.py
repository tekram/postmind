"""Harness-independent AgentService: resolve → stage → confirm → execute.

Exercises the safety boundary the MCP server and web Super Agent share: WRITE
actions only stage (no execution), confirm tokens are single-use and bound to
server-resolved message IDs, the blocklist/sensitive gates apply, and confirm
executes against a provider while recording an undo log.
"""

from __future__ import annotations

import pytest

import postmind.config as cfg
from postmind.core.agent_service import AgentService
from postmind.core.storage import (
    BlocklistRepo,
    EmailRecord,
    RuleRepo,
    UndoLogRepo,
    get_session,
)


class FakeProvider:
    """Records calls; supports labels/unsubscribe like Gmail."""

    def __init__(self):
        self.trashed: list[str] = []
        self.archived: list[str] = []

    def supports(self, capability: str) -> bool:
        return capability in ("labels", "unsubscribe")

    def batch_trash(self, ids):
        self.trashed.extend(ids)
        return len(ids)

    def batch_archive(self, ids):
        self.archived.extend(ids)
        return len(ids)

    def batch_label(self, ids, add=None, remove=None):
        return len(ids)


def _seed(account="me@x.com", senders=None):
    """Seed inbox EmailRecords. ``senders`` maps email → count."""
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


def _svc(account="me@x.com", ai=None):
    svc = AgentService(account_email=account, ai=ai)
    svc._provider = FakeProvider()  # bypass real Gmail/IMAP construction
    return svc


# ── Resolution + safety gating ────────────────────────────────────────────────


def test_resolve_targets_matches_query_and_filters_blocklist(clean_db):
    _seed()
    BlocklistRepo(get_session()).add("me@x.com", "deals@shop.com")
    svc = _svc()
    staged, blocked, sensitive = svc.resolve_targets(senders=None, query="shop.com")
    # deals@shop.com is blocklisted → excluded and reported.
    assert [g.sender_email for g in staged] == []
    assert blocked == ["deals@shop.com"]


def test_resolve_targets_explicit_emails(clean_db):
    _seed()
    svc = _svc()
    staged, blocked, sensitive = svc.resolve_targets(senders=["news@promo.com"], query="")
    assert [g.sender_email for g in staged] == ["news@promo.com"]
    assert blocked == []


# ── Stage → confirm trash ───────────────────────────────────────────────────


def test_stage_trash_then_confirm_executes_once(clean_db):
    _seed()
    svc = _svc()
    desc = svc.stage_cleanup("trash", senders=["news@promo.com"])
    assert "token" in desc and desc["kind"] == "trash"
    assert desc["email_count"] == 5
    assert desc["undoable"] is True
    # Staging executes nothing.
    assert svc._provider.trashed == []

    token = desc["token"]
    result = svc.confirm(token)
    assert result["ok"] is True
    assert result["affected"] == 5
    assert len(svc._provider.trashed) == 5
    # Undo log recorded.
    assert UndoLogRepo(get_session()).get(result["undo_id"]).operation == "trash"
    # Token is single-use.
    again = svc.confirm(token)
    assert "error" in again


def test_confirm_unknown_token_errors(clean_db):
    _seed()
    svc = _svc()
    assert "error" in svc.confirm("nope")


def test_stage_trash_no_match_returns_error(clean_db):
    _seed()
    svc = _svc()
    out = svc.stage_cleanup("trash", query="nonexistent.example")
    assert "error" in out
    assert svc._provider.trashed == []


# ── Archive requires label support; cancel discards ───────────────────────────


def test_stage_archive_blocked_when_provider_lacks_labels(clean_db):
    _seed()
    svc = _svc()

    class NoLabels(FakeProvider):
        def supports(self, capability):
            return False

    svc._provider = NoLabels()
    out = svc.stage_cleanup("archive", senders=["news@promo.com"])
    assert "error" in out


def test_cancel_removes_staged_action(clean_db):
    _seed()
    svc = _svc()
    token = svc.stage_cleanup("archive", senders=["news@promo.com"])["token"]
    assert svc.list_staged()
    assert svc.cancel(token).get("ok") is True
    assert svc.list_staged() == []


# ── Send validation ───────────────────────────────────────────────────────────


def test_stage_send_rejects_bad_recipient(clean_db):
    _seed()
    svc = _svc()
    assert "error" in svc.stage_send("not-an-email", "Hi", "body")
    assert "error" in svc.stage_send("a@b.com, c@d.com", "Hi", "body")
    ok = svc.stage_send("a@b.com", "Hi", "body")
    assert ok["kind"] == "send"


# ── Create rule via mock AI ───────────────────────────────────────────────────


def test_stage_and_confirm_create_rule(clean_db):
    from postmind.core.mock_ai import MockAIEngine

    _seed()
    svc = _svc(ai=MockAIEngine())
    desc = svc.stage_create_rule("archive newsletters older than 30 days")
    assert desc["kind"] == "create_rule"
    res = svc.confirm(desc["token"])
    assert res["ok"] is True
    assert RuleRepo(get_session()).list_active("me@x.com")


# ── MCP server exposes the catalog (skipped if mcp not installed) ─────────────


def test_mcp_server_exposes_all_tools():
    pytest.importorskip("mcp")
    import asyncio

    from postmind.core.agent_mcp import build_server

    srv = build_server("me@x.com")
    names = {t.name for t in asyncio.run(srv.list_tools())}
    # READ + stage_* + confirm/cancel/list — the full safety-bounded surface.
    assert {"get_inbox_overview", "stage_trash", "confirm_action", "cancel_action"} <= names
    # No filesystem/bash/web tools leak in — domain surface only.
    assert not any(n in names for n in ("bash", "read_file", "write_file", "web_fetch"))
