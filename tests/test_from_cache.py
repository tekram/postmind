"""Tests for fetch_sender_groups_from_db (the --from-cache pipeline).

These tests verify that the DB-backed sender-grouping function produces results
consistent with what the live Gmail pipeline would produce for the same data.
They use the shared ``clean_db`` fixture so they are fully isolated.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def _use_clean_db(clean_db):
    """Apply the shared in-memory DB to every test in this module."""


# ── helpers ────────────────────────────────────────────────────────────────────


def _insert_records(session, records_data: list[dict]) -> None:
    """Insert EmailRecord rows directly into the session for test setup."""
    from mailtrim.core.storage import EmailRecord

    for d in records_data:
        rec = EmailRecord(**d)
        session.add(rec)
    session.commit()


def _make_record(
    gmail_id: str,
    sender_email: str,
    sender_name: str = "",
    subject: str = "Test",
    account_email: str = "user@gmail.com",
    days_ago: int = 30,
    size_bytes: int = 1024,
    is_inbox: bool = True,
    has_unsubscribe: bool = False,
) -> dict:
    ts_ms = int(
        (datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp() * 1000
    )
    return dict(
        account_email=account_email,
        gmail_id=gmail_id,
        thread_id=f"t_{gmail_id}",
        subject=subject,
        sender_email=sender_email,
        sender_name=sender_name,
        snippet="",
        label_ids_json=json.dumps(["INBOX"] if is_inbox else []),
        internal_date=ts_ms,
        size_estimate=size_bytes,
        is_unread=True,
        is_inbox=is_inbox,
        list_unsubscribe="<mailto:unsub@example.com>" if has_unsubscribe else "",
    )


# ── tests ──────────────────────────────────────────────────────────────────────


def test_returns_empty_when_no_data():
    from mailtrim.core.sender_stats import fetch_sender_groups_from_db
    from mailtrim.core.storage import get_session

    session = get_session()
    groups = fetch_sender_groups_from_db("nobody@example.com")
    assert groups == []


def test_groups_by_sender_email():
    """Multiple messages from the same sender should be merged into one SenderGroup."""
    from mailtrim.core.sender_stats import fetch_sender_groups_from_db
    from mailtrim.core.storage import get_session

    session = get_session()
    _insert_records(
        session,
        [
            _make_record("id1", "news@example.com", "Example News", subject="Week 1"),
            _make_record("id2", "news@example.com", "Example News", subject="Week 2"),
            _make_record("id3", "news@example.com", "Example News", subject="Week 3"),
        ],
    )

    groups = fetch_sender_groups_from_db("user@gmail.com", min_count=1)
    assert len(groups) == 1
    assert groups[0].sender_email == "news@example.com"
    assert groups[0].count == 3


def test_separates_different_senders():
    """Messages from distinct senders should appear as separate SenderGroups."""
    from mailtrim.core.sender_stats import fetch_sender_groups_from_db
    from mailtrim.core.storage import get_session

    session = get_session()
    _insert_records(
        session,
        [
            _make_record("a1", "alice@alpha.com"),
            _make_record("b1", "bob@beta.com"),
            _make_record("b2", "bob@beta.com"),
        ],
    )

    groups = fetch_sender_groups_from_db("user@gmail.com", min_count=1)
    emails = {g.sender_email for g in groups}
    assert "alice@alpha.com" in emails
    assert "bob@beta.com" in emails


def test_min_count_filter():
    """Senders with fewer messages than min_count must be excluded."""
    from mailtrim.core.sender_stats import fetch_sender_groups_from_db
    from mailtrim.core.storage import get_session

    session = get_session()
    _insert_records(
        session,
        [
            _make_record("lone1", "loner@example.com"),  # only 1 message
            _make_record("m1", "many@example.com"),
            _make_record("m2", "many@example.com"),
        ],
    )

    groups = fetch_sender_groups_from_db("user@gmail.com", min_count=2)
    emails = {g.sender_email for g in groups}
    assert "many@example.com" in emails
    assert "loner@example.com" not in emails


def test_scope_inbox_only():
    """scope='inbox' must exclude non-inbox records."""
    from mailtrim.core.sender_stats import fetch_sender_groups_from_db
    from mailtrim.core.storage import get_session

    session = get_session()
    _insert_records(
        session,
        [
            _make_record("in1", "inbox@example.com", is_inbox=True),
            _make_record("in2", "inbox@example.com", is_inbox=True),
            _make_record("arch1", "archived@example.com", is_inbox=False),
            _make_record("arch2", "archived@example.com", is_inbox=False),
        ],
    )

    groups = fetch_sender_groups_from_db("user@gmail.com", scope="inbox", min_count=1)
    emails = {g.sender_email for g in groups}
    assert "inbox@example.com" in emails
    assert "archived@example.com" not in emails


def test_scope_anywhere_includes_archived():
    """scope='anywhere' (default) should include non-inbox records too."""
    from mailtrim.core.sender_stats import fetch_sender_groups_from_db
    from mailtrim.core.storage import get_session

    session = get_session()
    _insert_records(
        session,
        [
            _make_record("arch1", "archived@example.com", is_inbox=False),
            _make_record("arch2", "archived@example.com", is_inbox=False),
        ],
    )

    groups = fetch_sender_groups_from_db("user@gmail.com", scope="anywhere", min_count=1)
    emails = {g.sender_email for g in groups}
    assert "archived@example.com" in emails


def test_top_n_limits_results():
    """top_n must cap the number of returned SenderGroups."""
    from mailtrim.core.sender_stats import fetch_sender_groups_from_db
    from mailtrim.core.storage import get_session

    session = get_session()
    # 10 different senders, 2 messages each
    records = []
    for i in range(10):
        records.append(_make_record(f"s{i}_1", f"sender{i}@x.com"))
        records.append(_make_record(f"s{i}_2", f"sender{i}@x.com"))
    _insert_records(session, records)

    groups = fetch_sender_groups_from_db("user@gmail.com", top_n=3, min_count=1)
    assert len(groups) <= 3


def test_has_unsubscribe_propagated():
    """has_unsubscribe flag must be True when any message had a List-Unsubscribe header."""
    from mailtrim.core.sender_stats import fetch_sender_groups_from_db
    from mailtrim.core.storage import get_session

    session = get_session()
    _insert_records(
        session,
        [
            _make_record("u1", "news@promo.com", has_unsubscribe=True),
            _make_record("u2", "news@promo.com", has_unsubscribe=False),
        ],
    )

    groups = fetch_sender_groups_from_db("user@gmail.com", min_count=1)
    assert len(groups) == 1
    assert groups[0].has_unsubscribe is True


def test_size_bytes_summed():
    """Total size_bytes of the SenderGroup must equal the sum of its messages."""
    from mailtrim.core.sender_stats import fetch_sender_groups_from_db
    from mailtrim.core.storage import get_session

    session = get_session()
    _insert_records(
        session,
        [
            _make_record("sz1", "big@example.com", size_bytes=1_000_000),
            _make_record("sz2", "big@example.com", size_bytes=2_000_000),
            _make_record("sz3", "big@example.com", size_bytes=500_000),
        ],
    )

    groups = fetch_sender_groups_from_db("user@gmail.com", min_count=1)
    assert len(groups) == 1
    assert groups[0].total_size_bytes == 3_500_000


def test_impact_scores_assigned():
    """impact_score must be set (non-negative) after fetch_sender_groups_from_db."""
    from mailtrim.core.sender_stats import fetch_sender_groups_from_db
    from mailtrim.core.storage import get_session

    session = get_session()
    _insert_records(
        session,
        [
            _make_record("i1", "a@example.com", size_bytes=5_000_000),
            _make_record("i2", "a@example.com", size_bytes=5_000_000),
            _make_record("i3", "b@example.com", size_bytes=100),
            _make_record("i4", "b@example.com", size_bytes=100),
        ],
    )

    groups = fetch_sender_groups_from_db("user@gmail.com", min_count=1)
    for g in groups:
        assert 0 <= g.impact_score <= 100


def test_sort_by_size():
    """sort_by='size' should put the largest sender first."""
    from mailtrim.core.sender_stats import fetch_sender_groups_from_db
    from mailtrim.core.storage import get_session

    session = get_session()
    _insert_records(
        session,
        [
            _make_record("small1", "small@example.com", size_bytes=100),
            _make_record("small2", "small@example.com", size_bytes=100),
            _make_record("big1", "big@example.com", size_bytes=10_000_000),
            _make_record("big2", "big@example.com", size_bytes=10_000_000),
        ],
    )

    groups = fetch_sender_groups_from_db("user@gmail.com", sort_by="size", min_count=1)
    assert groups[0].sender_email == "big@example.com"


def test_sort_by_count():
    """sort_by='count' should put the sender with the most messages first."""
    from mailtrim.core.sender_stats import fetch_sender_groups_from_db
    from mailtrim.core.storage import get_session

    session = get_session()
    records = []
    # "frequent" has 5 messages, "rare" has 2
    for i in range(5):
        records.append(_make_record(f"freq_{i}", "frequent@x.com"))
    for i in range(2):
        records.append(_make_record(f"rare_{i}", "rare@x.com"))
    _insert_records(session, records)

    groups = fetch_sender_groups_from_db("user@gmail.com", sort_by="count", min_count=1)
    assert groups[0].sender_email == "frequent@x.com"


def test_isolates_accounts():
    """Records from a different account_email must not appear in results."""
    from mailtrim.core.sender_stats import fetch_sender_groups_from_db
    from mailtrim.core.storage import get_session

    session = get_session()
    _insert_records(
        session,
        [
            _make_record("own1", "mine@x.com", account_email="owner@gmail.com"),
            _make_record("own2", "mine@x.com", account_email="owner@gmail.com"),
            _make_record("other1", "theirs@x.com", account_email="other@gmail.com"),
            _make_record("other2", "theirs@x.com", account_email="other@gmail.com"),
        ],
    )

    groups = fetch_sender_groups_from_db("owner@gmail.com", min_count=1)
    emails = {g.sender_email for g in groups}
    assert "mine@x.com" in emails
    assert "theirs@x.com" not in emails


def test_sample_subjects_populated():
    """sample_subjects should be non-empty when the DB has subjects."""
    from mailtrim.core.sender_stats import fetch_sender_groups_from_db
    from mailtrim.core.storage import get_session

    session = get_session()
    _insert_records(
        session,
        [
            _make_record("sub1", "news@x.com", subject="Newsletter Week 1"),
            _make_record("sub2", "news@x.com", subject="Newsletter Week 2"),
        ],
    )

    groups = fetch_sender_groups_from_db("user@gmail.com", min_count=1)
    assert len(groups) == 1
    assert len(groups[0].sample_subjects) > 0
    assert any("Newsletter" in s for s in groups[0].sample_subjects)
