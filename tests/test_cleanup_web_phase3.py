"""Phase 3 (web layer) of Smart Cleanup Batches: the learning loop.

Covers POST /cleanup/confirm recording CleanupFeedbackRecord rows from the
hidden ``feedback_json`` payload, and promoting a batch into a RuleDefinition
via ``create_rule``. Mirrors the conftest in-memory DB isolation and the
provider/account monkeypatch pattern used by the Super Agent web tests.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import postmind.web.server as s
import postmind.core.storage as st
from postmind.core.sender_stats import SenderGroup


@pytest.fixture(autouse=True)
def _shared_db(monkeypatch):
    """In-memory SQLite shared across threads (the confirm handler runs DB work
    in an executor thread). A StaticPool keeps the single in-memory connection
    alive so the schema created here is visible from every thread/session.
    """
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


def _seed_cache(monkeypatch, account="me@example.com", senders=("promo@deals.com",)):
    """Point the web UI at a known account, stub the provider, and prime the
    scan cache the confirm handler reads from."""
    monkeypatch.setattr(s, "_get_web_account", lambda: account)

    groups = [
        SenderGroup(
            sender_email=e,
            sender_name="",
            count=10,
            total_size_bytes=1024 * 1024,
            earliest_date=datetime(2020, 1, 1, tzinfo=timezone.utc),
            latest_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
            sample_subjects=["Deal!"],
            message_ids=[f"{e}-1", f"{e}-2"],
            has_unsubscribe=True,
        )
        for e in senders
    ]
    s._scan_cache.clear()
    s._cache_set(groups, {"emailAddress": account}, account)

    # Stub provider so batch_trash/batch_archive never hit the network.
    class _P:
        def batch_trash(self, ids):
            return None

        def batch_archive(self, ids):
            return None

    monkeypatch.setattr(s, "_build_provider", lambda: _P())
    return account


def test_confirm_records_feedback_rows(monkeypatch):
    account = _seed_cache(monkeypatch)
    feedback = [
        {"sender_email": "promo@deals.com", "batch_key": "promos-unopened",
         "action": "trash", "decision": "approved"},
        {"sender_email": "other@x.com", "batch_key": "promos-unopened",
         "action": "trash", "decision": "skipped"},
    ]

    resp = TestClient(s.app).post(
        "/cleanup/confirm",
        data={"trash_senders": ["promo@deals.com"], "feedback_json": json.dumps(feedback)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    rows = st.get_session().query(st.CleanupFeedbackRecord).filter_by(account_email=account).all()
    assert len(rows) == 2
    by_sender = {r.sender_email: r.decision for r in rows}
    assert by_sender["promo@deals.com"] == "approved"
    assert by_sender["other@x.com"] == "skipped"


def test_confirm_with_no_senders_still_records_feedback(monkeypatch):
    """A submit with only feedback (no approved senders) must not short-circuit
    before recording; it should redirect back to /cleanup."""
    account = _seed_cache(monkeypatch)
    feedback = [
        {"sender_email": "promo@deals.com", "batch_key": "promos-unopened",
         "action": "trash", "decision": "skipped"},
    ]

    resp = TestClient(s.app).post(
        "/cleanup/confirm",
        data={"feedback_json": json.dumps(feedback)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/cleanup"

    rows = st.get_session().query(st.CleanupFeedbackRecord).filter_by(account_email=account).all()
    assert len(rows) == 1
    assert rows[0].decision == "skipped"


def test_confirm_creates_rule_from_template(monkeypatch):
    account = _seed_cache(monkeypatch)

    resp = TestClient(s.app).post(
        "/cleanup/confirm",
        data={"create_rule": "promos-unopened", "feedback_json": "[]"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    rules = st.RuleRepo(st.get_session()).list_active(account)
    assert len(rules) == 1
    rule = rules[0]
    assert rule.name == "Auto-clear old promotions"
    assert rule.gmail_query == "category:promotions older_than:90d"
    assert rule.action == "trash"
    assert rule.natural_language == "Created from the Clean Up page"


def test_confirm_ignores_unknown_rule_key(monkeypatch):
    account = _seed_cache(monkeypatch)

    resp = TestClient(s.app).post(
        "/cleanup/confirm",
        data={"create_rule": "not-a-real-batch",
              "feedback_json": json.dumps([
                  {"sender_email": "promo@deals.com", "batch_key": "x",
                   "action": "trash", "decision": "skipped"}])},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert st.RuleRepo(st.get_session()).list_active(account) == []


def test_confirm_empty_submit_redirects_without_writes(monkeypatch):
    account = _seed_cache(monkeypatch)

    resp = TestClient(s.app).post("/cleanup/confirm", data={}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/cleanup"
    assert st.get_session().query(st.CleanupFeedbackRecord).count() == 0
    assert st.RuleRepo(st.get_session()).list_active(account) == []
