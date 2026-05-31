"""Phase 2 Super Agent backend — generalized actions, unsubscribe, send.

These tests exercise the new WRITE tools (stage_archive/label/mark_read/
unsubscribe/draft_email/send_email) and their confirm endpoints. They seed the
in-memory scan cache directly and drive the agent loop with a mocked AIEngine
so no network/AI is required.

Safety properties asserted:
- WRITE tools only STAGE — they emit a card, never execute in the loop.
- Confirm endpoints re-resolve targets server-side, record an UndoLogRepo entry
  BEFORE acting, and call the provider (mocked).
- Protected (blocklist) senders are filtered out before staging.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from postmind.core.sender_stats import SenderGroup


@pytest.fixture()
def shared_db(monkeypatch):
    """In-memory SQLite shared across connections/threads (StaticPool).

    The confirm endpoints run their work in a ThreadPoolExecutor, which opens a
    fresh DB connection on another thread. A normal :memory: engine gives each
    connection its own empty database, so we pin a single shared connection.
    """
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


@pytest.fixture(autouse=True)
def _cloud_mode(monkeypatch):
    # Force the agent into cloud mode so tools are wired.
    monkeypatch.setenv("POSTMIND_AI_MODE", "cloud")
    monkeypatch.setenv("POSTMIND_CHAT_AI_MODE", "cloud")
    import postmind.config as config
    config._settings = None
    yield
    config._settings = None


def _group(email, name="Sender", count=10, size=5_000_000, ids=None):
    now = datetime.now(timezone.utc)
    return SenderGroup(
        sender_email=email,
        sender_name=name,
        count=count,
        total_size_bytes=size,
        earliest_date=now,
        latest_date=now,
        sample_subjects=["hi"],
        message_ids=ids or [f"{email}-{i}" for i in range(count)],
        has_unsubscribe=True,
    )


@pytest.fixture()
def seeded(monkeypatch, shared_db):
    """App + TestClient with a seeded scan cache and a fixed active account."""
    from postmind.web import server

    account = "me@example.com"
    monkeypatch.setattr(server, "_get_web_account", lambda: account)
    groups = [
        _group("news@promo.com", "Promo News", count=12),
        _group("alerts@bank.com", "My Bank", count=3),  # sensitive
    ]
    server._scan_cache[account] = {
        "groups": groups,
        "profile": {},
        "account_email": account,
        "scanned_at": "now",
        "expires": time.time() + 300,
    }
    yield server, account, groups
    server._scan_cache.clear()


def _run_agent(server, account, monkeypatch, tool_name, tool_input):
    """Invoke POST /agent with a mocked AIEngine.chat that fires one tool call."""
    captured = {}

    class FakeAI:
        def __init__(self, *a, **k):
            pass

        def chat(self, messages, system=None, tools=None, tool_executor=None, **kw):
            captured["result"] = tool_executor(tool_name, tool_input)
            return "Staged for your confirmation."

        def compose_email(self, intent, recipient_context="", thread_snippet="", soul=None):
            return "Subject: Hello there\n\nThis is the drafted body."

        def translate_rule(self, nl):  # pragma: no cover - unused here
            raise AssertionError

    import postmind.core.ai_engine as ai_mod
    monkeypatch.setattr(ai_mod, "AIEngine", FakeAI)

    client = TestClient(server.app)
    resp = client.post("/agent", json={"messages": [{"role": "user", "content": "do it"}]})
    return resp.json(), captured.get("result", "")


def test_stage_archive_emits_card_and_skips_protected(seeded, monkeypatch):
    server, account, _ = seeded
    # Mock provider so supports('labels') is True.
    prov = MagicMock()
    prov.supports.return_value = True
    monkeypatch.setattr(server, "_build_provider", lambda: prov)

    # Protect the bank sender.
    from postmind.core.storage import BlocklistRepo, get_session
    BlocklistRepo(get_session()).add(account, "alerts@bank.com")

    data, _ = _run_agent(server, account, monkeypatch, "stage_archive", {"query": ".com"})
    cards = data["cards"]
    assert len(cards) == 1
    c = cards[0]
    assert c["type"] == "bulk_action"
    assert c["fields"]["action"] == "archive"
    targets = {t["sender_email"] for t in c["fields"]["targets"]}
    assert "news@promo.com" in targets
    assert "alerts@bank.com" not in targets  # protected -> skipped
    assert "alerts@bank.com" in c["fields"]["blocked"]


def test_stage_label_requires_label_name(seeded, monkeypatch):
    server, account, _ = seeded
    prov = MagicMock(); prov.supports.return_value = True
    monkeypatch.setattr(server, "_build_provider", lambda: prov)
    data, result = _run_agent(server, account, monkeypatch, "stage_label", {"query": "promo"})
    # No label_name -> tool returns an error string, no card emitted.
    assert data["cards"] == []
    assert "label name" in result.lower()


def test_action_confirm_records_undo_and_calls_provider(seeded, monkeypatch):
    server, account, _ = seeded
    prov = MagicMock()
    prov.supports.return_value = True
    prov.batch_archive.return_value = 12
    monkeypatch.setattr(server, "_build_provider", lambda: prov)

    client = TestClient(server.app)
    resp = client.post(
        "/agent/action/confirm",
        data={"action": "archive", "senders": ["news@promo.com"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/undo?" in resp.headers["location"]
    prov.batch_archive.assert_called_once()
    archived_ids = prov.batch_archive.call_args[0][0]
    assert len(archived_ids) == 12

    from postmind.core.storage import UndoLogRepo, get_session
    entries = UndoLogRepo(get_session()).list_recent(account)
    assert any(e.operation == "archive" and len(e.message_ids) == 12 for e in entries)


def test_action_confirm_gated_on_supports(seeded, monkeypatch):
    server, account, _ = seeded
    prov = MagicMock()
    prov.supports.return_value = False  # provider lacks labels
    monkeypatch.setattr(server, "_build_provider", lambda: prov)
    client = TestClient(server.app)
    resp = client.post(
        "/agent/action/confirm",
        data={"action": "archive", "senders": ["news@promo.com"]},
        follow_redirects=False,
    )
    # error page, provider archive never called
    assert resp.status_code == 200
    prov.batch_archive.assert_not_called()


def test_stage_unsubscribe_card(seeded, monkeypatch):
    server, account, _ = seeded
    data, result = _run_agent(server, account, monkeypatch, "stage_unsubscribe", {"query": "promo"})
    assert len(data["cards"]) == 1
    c = data["cards"][0]
    assert c["type"] == "unsubscribe"
    assert "not undoable" in result.lower()


def test_unsubscribe_confirm_calls_engine(seeded, monkeypatch):
    server, account, groups = seeded
    from postmind.core.gmail_client import Message, MessageHeader

    prov = MagicMock()
    prov.supports.return_value = True
    prov.get_email_address.return_value = account
    prov.gmail_client = MagicMock()
    msg = Message(
        id="m1", thread_id="t1", label_ids=[], snippet="",
        headers=MessageHeader(from_="news@promo.com"), size_estimate=10,
    )
    prov.get_messages_batch.return_value = [msg]
    monkeypatch.setattr(server, "_build_provider", lambda: prov)

    called = {}

    class FakeEngine:
        def __init__(self, client, acct):
            called["acct"] = acct

        def batch_unsubscribe(self, messages, use_headless=True):
            called["n"] = len(messages)
            from postmind.core.unsubscribe import UnsubscribeResult
            return [UnsubscribeResult("news@promo.com", "header_url", True, "ok")]

    import postmind.core.unsubscribe as unsub_mod
    monkeypatch.setattr(unsub_mod, "UnsubscribeEngine", FakeEngine)

    client = TestClient(server.app)
    resp = client.post(
        "/agent/unsubscribe/confirm",
        data={"senders": ["news@promo.com"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "unsubscribed=1" in resp.headers["location"]
    assert called["acct"] == account
    assert called["n"] == 1


def test_send_email_card_and_confirm(seeded, monkeypatch):
    server, account, _ = seeded
    data, _ = _run_agent(
        server, account, monkeypatch, "send_email",
        {"to": "boss@example.com", "subject": "Hi", "body": "Hello boss"},
    )
    assert len(data["cards"]) == 1
    assert data["cards"][0]["type"] == "send_email"
    assert data["cards"][0]["fields"]["to"] == "boss@example.com"

    # Confirm path actually calls client.send via gmail_client.
    prov = MagicMock()
    prov.gmail_client.send.return_value = "sent-1"
    monkeypatch.setattr(server, "_build_provider", lambda: prov)
    client = TestClient(server.app)
    resp = client.post(
        "/agent/send",
        data={"to": "boss@example.com", "subject": "Hi", "body": "Hello boss"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    prov.gmail_client.send.assert_called_once_with(to="boss@example.com", subject="Hi", body="Hello boss")


def test_send_email_rejects_bad_recipient(seeded, monkeypatch):
    server, account, _ = seeded
    client = TestClient(server.app)
    resp = client.post("/agent/send", data={"to": "notanemail", "subject": "x", "body": "y"})
    assert resp.status_code == 200  # error page, not a redirect


def test_draft_email_emits_send_card(seeded, monkeypatch):
    server, account, _ = seeded
    data, _ = _run_agent(server, account, monkeypatch, "draft_email", {"intent": "thank them", "to": "x@y.com"})
    assert len(data["cards"]) == 1
    c = data["cards"][0]
    assert c["type"] == "send_email"
    assert c["fields"]["subject"] == "Hello there"
    assert "drafted body" in c["fields"]["body"]
