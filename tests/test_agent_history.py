"""Tests for Feature 1: persistent server-side conversation history.

Covers:
- AgentConversationRepo CRUD
- /agent/history GET endpoint
- /agent/history DELETE endpoint
- /agent/suggestions GET endpoint
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import postmind.core.storage as storage
from postmind.core.storage import AgentConversation, AgentConversationRepo, Base


# ── Repo unit tests ───────────────────────────────────────────────────────────


def test_save_and_retrieve_turns(clean_db):
    """Save 3 turns and get_recent returns all 3 in chronological order."""
    session = storage.get_session()
    repo = AgentConversationRepo(session)
    account = "me@x.com"
    sid = "sess1"

    repo.save_turn(account, sid, "user", "Hello")
    repo.save_turn(account, sid, "assistant", "Hi there!")
    repo.save_turn(account, sid, "user", "What's my storage?")

    rows = repo.get_recent(account, hours=24, limit=50)
    assert len(rows) == 3
    assert rows[0].role == "user"
    assert rows[0].content == "Hello"
    assert rows[1].role == "assistant"
    assert rows[1].content == "Hi there!"
    assert rows[2].content == "What's my storage?"


def test_get_recent_only_last_24h(clean_db):
    """Turns older than 24 hours are excluded from get_recent."""
    session = storage.get_session()
    repo = AgentConversationRepo(session)
    account = "me@x.com"
    sid = "sess2"

    # Save a turn with a timestamp 2 days in the past
    old_turn = AgentConversation(
        account_email=account,
        session_id=sid,
        role="user",
        content="Old message",
        created_at=time.time() - 48 * 3600,
    )
    session.add(old_turn)
    session.commit()

    # Save a fresh turn
    repo.save_turn(account, sid, "user", "Fresh message")

    rows = repo.get_recent(account, hours=24, limit=50)
    assert len(rows) == 1
    assert rows[0].content == "Fresh message"


def test_clear_removes_all_turns(clean_db):
    """clear() deletes all turns for the account and returns the count."""
    session = storage.get_session()
    repo = AgentConversationRepo(session)
    account = "me@x.com"
    sid = "sess3"

    repo.save_turn(account, sid, "user", "A")
    repo.save_turn(account, sid, "assistant", "B")
    repo.save_turn(account, sid, "user", "C")

    cleared = repo.clear(account)
    assert cleared == 3

    rows = repo.get_recent(account, hours=24, limit=50)
    assert rows == []


def test_clear_is_account_scoped(clean_db):
    """clear() only removes turns for the specified account."""
    session = storage.get_session()
    repo = AgentConversationRepo(session)

    repo.save_turn("a@x.com", "s1", "user", "From A")
    repo.save_turn("b@x.com", "s2", "user", "From B")

    repo.clear("a@x.com")

    assert repo.get_recent("a@x.com") == []
    assert len(repo.get_recent("b@x.com")) == 1


def test_save_turn_with_actions_and_cards(clean_db):
    """Actions and cards are serialised as JSON and round-trip correctly."""
    import json

    session = storage.get_session()
    repo = AgentConversationRepo(session)
    account = "me@x.com"
    actions = [{"label": "Undo", "href": "/undo"}]
    cards = [{"type": "bulk_action", "title": "Archive 5 emails"}]

    row = repo.save_turn(account, "s", "assistant", "Done!", actions=actions, cards=cards)

    assert json.loads(row.actions_json) == actions
    assert json.loads(row.cards_json) == cards


# ── HTTP endpoint tests ───────────────────────────────────────────────────────


@pytest.fixture()
def _static_db(monkeypatch):
    """In-memory SQLite with StaticPool so all connections share the same DB.

    Required for TestClient tests: without StaticPool each new connection
    gets its own empty in-memory database, causing "no such table" errors
    when the route opens a fresh session.
    """
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


def test_history_endpoint_returns_correct_shape(monkeypatch, _static_db):
    """GET /agent/history returns 200 with {turns: [...]} even when empty."""
    from postmind.web import server

    monkeypatch.setattr(server, "_get_web_account", lambda: "me@x.com")
    client = TestClient(server.app)

    resp = client.get("/agent/history")
    assert resp.status_code == 200
    data = resp.json()
    assert "turns" in data
    assert isinstance(data["turns"], list)


def test_history_endpoint_returns_saved_turns(monkeypatch, _static_db):
    """GET /agent/history returns turns saved via repo."""
    from postmind.web import server

    monkeypatch.setattr(server, "_get_web_account", lambda: "me@x.com")
    client = TestClient(server.app)

    # Seed a turn directly via repo
    session = storage.get_session()
    repo = AgentConversationRepo(session)
    repo.save_turn("me@x.com", "s1", "user", "What's my storage?")

    resp = client.get("/agent/history")
    assert resp.status_code == 200
    turns = resp.json()["turns"]
    assert len(turns) == 1
    assert turns[0]["role"] == "user"
    assert turns[0]["content"] == "What's my storage?"
    assert "actions" in turns[0]
    assert "cards" in turns[0]
    assert "ts" in turns[0]


def test_history_clear_endpoint(monkeypatch, _static_db):
    """DELETE /agent/history clears turns and returns count."""
    from postmind.web import server

    monkeypatch.setattr(server, "_get_web_account", lambda: "me@x.com")
    client = TestClient(server.app)

    session = storage.get_session()
    repo = AgentConversationRepo(session)
    repo.save_turn("me@x.com", "s1", "user", "Turn 1")
    repo.save_turn("me@x.com", "s1", "assistant", "Turn 2")

    resp = client.delete("/agent/history")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["cleared"] == 2

    # Verify they're gone
    rows = repo.get_recent("me@x.com")
    assert rows == []


def test_history_endpoint_no_account(monkeypatch, _static_db):
    """GET /agent/history with no active account returns empty turns."""
    from postmind.web import server

    monkeypatch.setattr(server, "_get_web_account", lambda: None)
    client = TestClient(server.app)

    resp = client.get("/agent/history")
    assert resp.status_code == 200
    assert resp.json() == {"turns": []}


def test_suggestions_endpoint_returns_5_chips(monkeypatch, _static_db):
    """GET /agent/suggestions returns 200 with a list of exactly 5 chips."""
    from postmind.web import server

    # No DB data — should return defaults
    monkeypatch.setattr(server, "_get_web_account", lambda: "me@x.com")
    client = TestClient(server.app)

    resp = client.get("/agent/suggestions")
    assert resp.status_code == 200
    data = resp.json()
    assert "chips" in data
    assert isinstance(data["chips"], list)
    assert len(data["chips"]) == 5


def test_suggestions_endpoint_no_account(monkeypatch, _static_db):
    """GET /agent/suggestions with no account returns default chips."""
    from postmind.web import server

    monkeypatch.setattr(server, "_get_web_account", lambda: None)
    client = TestClient(server.app)

    resp = client.get("/agent/suggestions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["chips"]) == 5
    assert "storage" in data["chips"][0].lower()
