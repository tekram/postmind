"""Tests for the web-layer learning loop.

Covers:
- /triage/trash recording a UserActionRecord
- /brief/action recording UserActionRecords (single + bulk)
- /rules/proposals returning empty or proposal cards
- /rules/proposals/{id}/confirm activating a rule
- /rules/proposals/{id}/dismiss deleting a rule
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import postmind.core.storage as st
import postmind.web.server as s
from postmind.core.storage import (
    EmailRecord,
    RuleDefinition,
    RuleRepo,
    UserActionRecord,
    UserActionRepo,
)

ACCOUNT = "me@example.com"


@pytest.fixture(autouse=True)
def _shared_db(monkeypatch):
    """StaticPool so the executor thread sees the same in-memory DB."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    st.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(st, "_engine", engine)
    monkeypatch.setattr(st, "_SessionLocal", factory)
    yield engine
    engine.dispose()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(s, "_get_web_account", lambda: ACCOUNT)

    class _FakeProvider:
        def batch_trash(self, ids):
            pass
        def batch_archive(self, ids):
            pass
        def get_email_address(self):
            return ACCOUNT

    monkeypatch.setattr(s, "_build_provider", lambda: _FakeProvider())
    return TestClient(s.app, raise_server_exceptions=True)


def _seed_email(gmail_id: str, sender_email: str = "promo@deals.com",
                sender_name: str = "Deals", subject: str = "Big sale") -> None:
    session = st.get_session()
    session.add(EmailRecord(
        gmail_id=gmail_id,
        account_email=ACCOUNT,
        thread_id=gmail_id,
        sender_email=sender_email,
        sender_name=sender_name,
        subject=subject,
        snippet="",
        internal_date=0,
        size_estimate=1024,
        is_inbox=True,
        is_unread=True,
    ))
    session.commit()


# ── /triage/trash signal capture ─────────────────────────────────────────────


def test_triage_trash_records_user_action(client):
    _seed_email("msg-1")
    resp = client.post("/triage/trash", data={"gmail_id": "msg-1"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    rows = st.get_session().query(UserActionRecord).all()
    assert len(rows) == 1
    r = rows[0]
    assert r.gmail_id == "msg-1"
    assert r.action == "trash"
    assert r.source == "triage"
    assert r.account_email == ACCOUNT
    assert r.sender_email == "promo@deals.com"


def test_triage_trash_missing_id_returns_error(client):
    resp = client.post("/triage/trash", data={})
    assert resp.status_code == 400


def test_triage_trash_no_email_record_still_succeeds(client):
    # Email not in DB — should trash fine without recording a signal.
    resp = client.post("/triage/trash", data={"gmail_id": "unknown-msg"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # No signal recorded since there's no EmailRecord to look up
    assert st.get_session().query(UserActionRecord).count() == 0


# ── /brief/action signal capture ─────────────────────────────────────────────


def test_brief_action_trash_records_signal(client):
    _seed_email("brief-1")
    resp = client.post("/brief/action", data={"action": "trash", "gmail_id": "brief-1"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    rows = st.get_session().query(UserActionRecord).all()
    assert len(rows) == 1
    assert rows[0].action == "trash"
    assert rows[0].source == "brief"


def test_brief_action_archive_records_signal(client):
    _seed_email("brief-2")
    resp = client.post("/brief/action", data={"action": "archive", "gmail_id": "brief-2"})
    assert resp.status_code == 200

    row = st.get_session().query(UserActionRecord).first()
    assert row.action == "archive"


def test_brief_action_bulk_records_one_signal_per_email(client):
    for i in range(3):
        _seed_email(f"bulk-{i}")

    resp = client.post(
        "/brief/action",
        data={"action": "bulk_trash", "gmail_ids[]": ["bulk-0", "bulk-1", "bulk-2"]},
    )
    assert resp.status_code == 200

    rows = st.get_session().query(UserActionRecord).all()
    assert len(rows) == 3
    assert all(r.action == "trash" for r in rows)
    assert all(r.source == "brief" for r in rows)


# ── /rules/proposals ─────────────────────────────────────────────────────────


def test_rules_proposals_returns_empty_when_none(client):
    resp = client.get("/rules/proposals")
    assert resp.status_code == 200
    assert resp.text.strip() == ""


def test_rules_proposals_returns_card_for_each_proposal(client):
    session = st.get_session()
    for i in range(2):
        RuleRepo(session).create(RuleDefinition(
            account_email=ACCOUNT,
            name=f"Auto-trash: Spam Co {i}",
            natural_language=f"trash spam{i}",
            gmail_query=f"from:spam{i}@junk.com",
            action="trash",
            action_params_json="{}",
            ai_explanation=f"Trash all emails from spam{i}",
            is_active=False,
            proposed_at=datetime.now(timezone.utc),
        ))

    resp = client.get("/rules/proposals")
    assert resp.status_code == 200
    assert "Auto-trash: Spam Co 0" in resp.text
    assert "Auto-trash: Spam Co 1" in resp.text
    assert "Create rule" in resp.text
    assert "Dismiss" in resp.text


def test_rules_proposals_only_shows_inactive_proposed_rules(client):
    session = st.get_session()
    # Active rule (not a proposal)
    RuleRepo(session).create(RuleDefinition(
        account_email=ACCOUNT,
        name="Active rule",
        natural_language="",
        gmail_query="from:active@example.com",
        action="trash",
        action_params_json="{}",
        ai_explanation="",
        is_active=True,
        proposed_at=None,
    ))

    resp = client.get("/rules/proposals")
    assert resp.text.strip() == ""


# ── /rules/proposals/{id}/confirm ────────────────────────────────────────────


def test_confirm_proposal_activates_rule(client):
    session = st.get_session()
    repo = RuleRepo(session)
    repo.create(RuleDefinition(
        account_email=ACCOUNT,
        name="Auto-trash: Promo",
        natural_language="",
        gmail_query="from:promo@deals.com",
        action="trash",
        action_params_json="{}",
        ai_explanation="",
        is_active=False,
        proposed_at=datetime.now(timezone.utc),
    ))
    rule_id = repo.list_proposed(ACCOUNT)[0].id

    resp = client.post(f"/rules/proposals/{rule_id}/confirm")
    assert resp.status_code == 200

    rule = session.get(RuleDefinition, rule_id)
    assert rule.is_active is True
    assert rule.proposed_at is None


# ── /rules/proposals/{id}/dismiss ────────────────────────────────────────────


def test_dismiss_proposal_removes_rule(client):
    session = st.get_session()
    repo = RuleRepo(session)
    repo.create(RuleDefinition(
        account_email=ACCOUNT,
        name="Auto-trash: Junk",
        natural_language="",
        gmail_query="from:junk@spam.com",
        action="trash",
        action_params_json="{}",
        ai_explanation="",
        is_active=False,
        proposed_at=datetime.now(timezone.utc),
    ))
    rule_id = repo.list_proposed(ACCOUNT)[0].id

    resp = client.post(f"/rules/proposals/{rule_id}/dismiss")
    assert resp.status_code == 200

    assert repo.list_proposed(ACCOUNT) == []
