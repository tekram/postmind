"""Phase 2 (web layer): the semantic naming call wired into GET /cleanup.

Verifies that when AI is on, the batch titles/rationales are overlaid from
AIEngine.propose_batches, and that any AI failure degrades cleanly to the
deterministic Phase-1 names. Uses a shared in-memory DB (the route does its
DB work in an executor thread).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import postmind.core.ai_engine as ae
import postmind.core.storage as st
import postmind.web.server as s


@pytest.fixture(autouse=True)
def _shared_db(monkeypatch):
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


def _seed(monkeypatch, account="me@example.com"):
    monkeypatch.setattr(s, "_get_web_account", lambda: account)
    # An old, high-volume promo sender with unsubscribe → a confident promos batch.
    records = [
        st.EmailRecord(
            account_email=account,
            gmail_id=f"p{i}",
            thread_id=f"t{i}",
            subject="50% off everything",
            sender_email="deals@promo.com",
            sender_name="Promo Co",
            snippet="snippet",
            label_ids_json=json.dumps(["INBOX"]),
            internal_date=1_500_000_000_000,  # ~2017, comfortably old
            size_estimate=200_000,
            is_unread=False,
            is_inbox=True,
            list_unsubscribe="<mailto:unsub@promo.com>",
        )
        for i in range(120)
    ]
    st.EmailRepo(st.get_session()).upsert_many(records)
    return account


def test_cleanup_overlays_titles_when_ai_on(monkeypatch):
    _seed(monkeypatch)
    monkeypatch.setattr(s, "_ai_mode", lambda: "cloud")

    class FakeAI:
        def __init__(self, *a, **k):
            pass

        def propose_batches(self, digest):
            return {
                "batches": {
                    b["key"]: {"title": f"AI {b['key']}", "rationale": "AI says safe."}
                    for b in digest
                }
            }

    monkeypatch.setattr(ae, "AIEngine", FakeAI)

    resp = TestClient(s.app).get("/cleanup")
    assert resp.status_code == 200
    assert "AI promos-unopened" in resp.text
    assert "AI says safe." in resp.text


def test_cleanup_degrades_when_ai_raises(monkeypatch):
    _seed(monkeypatch)
    monkeypatch.setattr(s, "_ai_mode", lambda: "cloud")

    class BoomAI:
        def __init__(self, *a, **k):
            pass

        def propose_batches(self, digest):
            raise RuntimeError("model down")

    monkeypatch.setattr(ae, "AIEngine", BoomAI)

    resp = TestClient(s.app).get("/cleanup")
    # The page still renders the deterministic Phase-1 batch (no 500).
    assert resp.status_code == 200
    assert "Newsletters &amp; promotions you never opened" in resp.text


def test_cleanup_skips_ai_when_off(monkeypatch):
    _seed(monkeypatch)
    monkeypatch.setattr(s, "_ai_mode", lambda: "off")

    class Tripwire:
        def __init__(self, *a, **k):
            raise AssertionError("AIEngine must not be constructed when AI is off")

    monkeypatch.setattr(ae, "AIEngine", Tripwire)

    resp = TestClient(s.app).get("/cleanup")
    assert resp.status_code == 200
    assert "Newsletters &amp; promotions you never opened" in resp.text
