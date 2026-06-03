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
