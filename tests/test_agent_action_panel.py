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
