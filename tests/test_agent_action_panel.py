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
        {
            "id": "m1",
            "subject": "A",
            "sender_email": "news@promo.com",
            "sender_name": "Promo",
            "size_estimate": 2_000_000,
            "internal_date": 1,
            "date": "2022-01-01",
        },
        {
            "id": "m2",
            "subject": "B",
            "sender_email": "news@promo.com",
            "sender_name": "Promo",
            "size_estimate": 1_000_000,
            "internal_date": 1,
            "date": "2022-01-02",
        },
        {
            "id": "m3",
            "subject": "C",
            "sender_email": "alerts@bank.com",
            "sender_name": "Bank",
            "size_estimate": 500_000,
            "internal_date": 1,
            "date": "2022-01-03",
        },
    ]
    token = server._review_put(account, "old stuff", emails)

    client = TestClient(server.app)
    resp = client.get(f"/agent/review/{token}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_count"] == 3
    assert data["description"] == "old stuff"
    assert data["groups"][0]["sender_email"] == "news@promo.com"
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


def test_is_sensitive_sender_uses_authoritative_keywords():
    from postmind.core.sender_stats import is_sensitive_sender

    assert is_sensitive_sender("alerts@chase.com") is True
    assert is_sensitive_sender("news@promo.com") is False


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

    # The un-submitted id survives in the cache (so it isn't silently lost),
    # while the trashed id is consumed to prevent a double-trash on re-submit.
    remaining = {e["id"] for e in server._REVIEW_CACHE[token]["emails"]}
    assert remaining == {"m2"}


def test_confirm_unknown_token_404(monkeypatch, shared_db):
    from postmind.web import server

    monkeypatch.setattr(server, "_get_web_account", lambda: "me@example.com")
    client = TestClient(server.app)
    assert client.post("/agent/review/nope/confirm", data={"ids": ["m1"]}).status_code == 404
