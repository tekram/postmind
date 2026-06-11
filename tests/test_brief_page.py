"""Tests for the /brief page fast-load + auto-generate skeleton behaviour."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import postmind.core.storage as st
import postmind.web.server as s
from postmind.core.storage import DailyBrief

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
    return TestClient(s.app, raise_server_exceptions=True)


def _seed_brief() -> None:
    session = st.get_session()
    session.add(DailyBrief(
        account_email=ACCOUNT,
        brief_date=datetime.now(timezone.utc).date().isoformat(),
        content="**Test brief** content here.",
        ai_used=True,
        unread_count=5,
        new_since_yesterday=2,
        high_priority_count=1,
        overdue_followups_count=0,
        generated_at=datetime.now(timezone.utc),
    ))
    session.commit()
    session.close()


def test_brief_page_no_brief_renders_skeleton_without_generating(client, monkeypatch):
    """With no brief today, /brief must NOT generate inline — it renders a
    skeleton that auto-fires POST /brief/generate via HTMX."""
    import postmind.core.daily_brief as db_mod

    def _boom(*a, **kw):
        raise AssertionError("DailyBriefGenerator must not be constructed on GET /brief")

    monkeypatch.setattr(db_mod, "DailyBriefGenerator", _boom)

    resp = client.get("/brief")
    assert resp.status_code == 200
    html = resp.text
    # auto-trigger wiring on the swap target
    assert 'hx-post="/brief/generate"' in html
    assert 'hx-trigger="load"' in html
    # skeleton copy
    assert "Analyzing your inbox" in html
    # stat-cards OOB target exists even before generation
    assert 'id="brief-stat-cards"' in html


def test_brief_page_existing_brief_renders_content_no_autotrigger(client, monkeypatch):
    _seed_brief()
    import postmind.core.daily_brief as db_mod

    def _boom(*a, **kw):
        raise AssertionError("DailyBriefGenerator must not be constructed on GET /brief")

    monkeypatch.setattr(db_mod, "DailyBriefGenerator", _boom)

    resp = client.get("/brief")
    assert resp.status_code == 200
    html = resp.text
    assert "Test brief" in html
    # No auto-fire when brief exists. NOTE: must be this exact string — the page
    # also contains an unrelated `hx-get="/rules/proposals" hx-trigger="load"`.
    assert 'hx-post="/brief/generate" hx-trigger="load"' not in html
    assert 'id="brief-stat-cards"' in html
    assert ">5</p>" in html  # unread count rendered in stat card
