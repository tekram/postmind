"""Tests for the Daily Brief Super Agent integration.

Covers:
- GET /brief/context endpoint
- _build_agent_system with and without brief_context
- /agent/stream route registration
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _use_clean_db(clean_db):
    """Apply the shared in-memory DB fixture to every test in this module."""


# ── /brief/context endpoint ────────────────────────────────────────────────────


def test_brief_context_endpoint_no_account(monkeypatch):
    """When no account is active, /brief/context returns empty items/summary."""
    from fastapi.testclient import TestClient

    from postmind.web import server

    monkeypatch.setattr(server, "_get_web_account", lambda: "")

    client = TestClient(server.app)
    resp = client.get("/brief/context")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["summary"] == ""


def test_brief_context_endpoint_with_brief(monkeypatch, clean_db):
    """When a brief exists, /brief/context returns the items from it."""
    import datetime

    from postmind.web import server

    account = "user@example.com"
    monkeypatch.setattr(server, "_get_web_account", lambda: account)

    # Build a fake brief object to return from the generator
    items = [
        {
            "sender": "Alice",
            "subject": "Q2 review",
            "priority": "high",
            "gmail_id": "abc123",
            "thread_id": "thread1",
            "suggested_action": "reply",
            "category": "action_required",
        },
        {
            "sender": "Bob",
            "subject": "Budget update",
            "priority": "high",
            "gmail_id": "def456",
            "thread_id": "thread2",
            "suggested_action": "archive",
            "category": "informational",
        },
    ]

    class _FakeBrief:
        items_json = json.dumps(items)
        content = "Test brief content"
        unread_count = 5
        high_priority_count = 2

    class _FakeGenerator:
        def __init__(self, email):
            pass

        def get_or_generate(self, force=False):
            return _FakeBrief()

    import postmind.core.daily_brief as db_mod

    monkeypatch.setattr(db_mod, "DailyBriefGenerator", _FakeGenerator)

    # Also patch the import inside the endpoint
    import postmind.web.server as server_mod

    monkeypatch.setattr(
        server_mod,
        "brief_context",
        server_mod.brief_context,  # keep the same function, generator is patched via module
    )

    from fastapi.testclient import TestClient

    client = TestClient(server.app)
    resp = client.get("/brief/context")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 2
    assert data["items"][0]["sender"] == "Alice"
    assert data["items"][1]["gmail_id"] == "def456"
    assert data["summary"] == "Test brief content"
    assert data["unread_count"] == 5
    assert data["high_priority_count"] == 2


# ── _build_agent_system ────────────────────────────────────────────────────────


def test_build_agent_system_with_brief_context():
    """Brief context is injected at the top of the system prompt."""
    from postmind.web.server import _build_agent_system

    result = _build_agent_system("test@x.com", "cloud", brief_context="test context")
    assert "test context" in result
    assert "Daily Brief" in result


def test_build_agent_system_without_brief_context():
    """Without brief_context, the Daily Brief preamble is absent."""
    from postmind.web.server import _build_agent_system

    result = _build_agent_system("test@x.com", "cloud")
    assert "Daily Brief page" not in result


# ── Route registration ─────────────────────────────────────────────────────────


def test_brief_stream_route_registered():
    """/agent/stream route exists (used by brief chat panel)."""
    from postmind.web import server

    paths = {getattr(r, "path", None) for r in server.app.routes}
    assert "/agent/stream" in paths


def test_brief_context_route_registered():
    """/brief/context route is registered."""
    from postmind.web import server

    paths = {getattr(r, "path", None) for r in server.app.routes}
    assert "/brief/context" in paths
