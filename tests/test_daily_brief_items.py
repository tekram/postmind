"""Daily brief email-selection tests.

Covers which emails the brief surfaces as deep-linkable items (``items_json``).
The choice is "high-priority only": only classified high-priority/action_required
emails appear, and *all* of them do — recent unread is never used to pad the list.
"""

import json

import pytest


@pytest.fixture(autouse=True)
def _use_clean_db(clean_db):
    """Apply the shared in-memory DB fixture to every test in this module."""


@pytest.fixture(autouse=True)
def _ai_off(monkeypatch):
    """Force the stats fallback so no AI call is attempted."""
    import postmind.core.daily_brief as db

    class _S:
        ai_mode = "off"

    monkeypatch.setattr(db, "get_settings", lambda: _S())


def _add_email(repo, gmail_id, *, unread=True, internal_date=1700000000000):
    from postmind.core.storage import EmailRecord

    repo.upsert(
        EmailRecord(
            account_email="me@gmail.com",
            gmail_id=gmail_id,
            thread_id=f"t-{gmail_id}",
            subject=f"Subject {gmail_id}",
            sender_email=f"{gmail_id}@example.com",
            sender_name=f"Sender {gmail_id}",
            snippet="…",
            label_ids_json=json.dumps(["INBOX", "UNREAD"] if unread else ["INBOX"]),
            internal_date=internal_date,
            is_unread=unread,
            is_inbox=True,
        )
    )


def _gen():
    from postmind.core.daily_brief import DailyBriefGenerator

    return DailyBriefGenerator("me@gmail.com").get_or_generate(force=True)


def test_brief_surfaces_only_high_priority_items():
    """With several unread but one classified high, only that one is an item."""
    from postmind.core.storage import (
        ClassificationCacheRepo,
        EmailRepo,
        get_session,
    )

    session = get_session()
    repo = EmailRepo(session)
    for i in range(5):
        _add_email(repo, f"id{i}", internal_date=1700000000000 + i)

    ClassificationCacheRepo(session).upsert_many(
        [{"gmail_id": "id2", "priority": "high"}]
    )

    brief = _gen()
    items = json.loads(brief.items_json)
    assert [i["gmail_id"] for i in items] == ["id2"]


def test_brief_is_empty_when_nothing_high_priority():
    """No high-priority classification ⇒ no padded recent-unread items."""
    from postmind.core.storage import EmailRepo, get_session

    repo = EmailRepo(get_session())
    for i in range(3):
        _add_email(repo, f"id{i}")

    brief = _gen()
    assert brief.items_json is None


def test_brief_surfaces_all_high_priority_items():
    """Every high-priority email is surfaced (not truncated to a small cap)."""
    from postmind.core.storage import (
        ClassificationCacheRepo,
        EmailRepo,
        get_session,
    )

    session = get_session()
    repo = EmailRepo(session)
    for i in range(12):
        _add_email(repo, f"id{i:02d}", internal_date=1700000000000 + i)

    ClassificationCacheRepo(session).upsert_many(
        [{"gmail_id": f"id{i:02d}", "priority": "high"} for i in range(12)]
    )

    brief = _gen()
    items = json.loads(brief.items_json)
    assert len(items) == 12
