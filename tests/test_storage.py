"""Tests for the storage layer."""

import json
from datetime import datetime, timedelta, timezone

import pytest

# ── DB isolation ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _use_clean_db(clean_db):
    """Apply the shared in-memory DB fixture to every test in this module."""


def test_email_record_upsert():
    from postmind.core.storage import EmailRecord, EmailRepo, get_session

    session = get_session()
    repo = EmailRepo(session)

    rec = EmailRecord(
        account_email="test@gmail.com",
        gmail_id="abc123",
        thread_id="thread1",
        subject="Test Subject",
        sender_email="sender@example.com",
        sender_name="Sender",
        snippet="Hello world",
        label_ids_json=json.dumps(["INBOX", "UNREAD"]),
        internal_date=1700000000000,
        is_unread=True,
        is_inbox=True,
    )
    repo.upsert(rec)

    fetched = repo.get("abc123")
    assert fetched is not None
    assert fetched.subject == "Test Subject"
    assert fetched.is_inbox is True

    # Upsert again with update
    rec.subject = "Updated Subject"
    repo.upsert(rec)
    fetched2 = repo.get("abc123")
    assert fetched2.subject == "Updated Subject"


def test_follow_up_lifecycle():
    from postmind.core.storage import FollowUp, FollowUpRepo, get_session

    session = get_session()
    repo = FollowUpRepo(session)

    now = datetime.now(timezone.utc)
    fu = FollowUp(
        account_email="test@gmail.com",
        sent_message_id="msg1",
        thread_id="thread1",
        to_email="recipient@example.com",
        subject="Follow up on proposal",
        sent_at=now,
        remind_at=now - timedelta(seconds=1),  # already due
        remind_only_if_no_reply=True,
    )
    repo.create(fu)

    due = repo.get_due("test@gmail.com")
    assert len(due) == 1
    assert due[0].subject == "Follow up on proposal"

    repo.mark_replied("thread1")
    # get_due filters replied=False, so replied threads no longer appear
    due_after = repo.get_due("test@gmail.com")
    assert len(due_after) == 0
    # But the record itself is updated
    record = session.query(FollowUp).filter_by(thread_id="thread1").first()
    assert record.replied is True


def test_undo_log_record_and_undo():
    from postmind.core.storage import UndoLogRepo, get_session

    session = get_session()
    repo = UndoLogRepo(session)

    entry = repo.record(
        account_email="test@gmail.com",
        operation="archive",
        message_ids=["id1", "id2", "id3"],
        description="Archived newsletters",
        metadata={"gmail_query": "label:newsletters"},
    )

    assert entry.id is not None
    assert entry.message_ids == ["id1", "id2", "id3"]
    assert entry.is_undone is False

    recent = repo.list_recent("test@gmail.com")
    assert len(recent) == 1

    repo.mark_undone(entry.id)
    entry_after = repo.get(entry.id)
    assert entry_after.is_undone is True
    assert entry_after.undone_at is not None


def test_rule_create_and_deactivate():
    from postmind.core.storage import RuleDefinition, RuleRepo, get_session

    session = get_session()
    repo = RuleRepo(session)

    rule = RuleDefinition(
        account_email="test@gmail.com",
        name="Archive old LinkedIn",
        natural_language="archive LinkedIn notifications older than 7 days",
        gmail_query="from:linkedin.com older_than:7d",
        action="archive",
    )
    rule.action_params = {}
    created = repo.create(rule)
    assert created.id is not None

    active = repo.list_active("test@gmail.com")
    assert len(active) == 1

    repo.deactivate(created.id)
    active_after = repo.list_active("test@gmail.com")
    assert len(active_after) == 0


def test_avoidance_view_tracking():
    from postmind.config import get_settings
    from postmind.core.storage import EmailRecord, EmailRepo, get_session

    session = get_session()
    repo = EmailRepo(session)

    rec = EmailRecord(
        account_email="test@gmail.com",
        gmail_id="avoid1",
        thread_id="t1",
        subject="That email I keep ignoring",
        sender_email="boss@company.com",
        is_inbox=True,
        is_acted_on=False,
        view_count=0,
        label_ids_json="[]",
    )
    repo.upsert(rec)

    threshold = get_settings().avoidance_view_threshold
    for _ in range(threshold):
        repo.increment_view("avoid1")

    avoided = repo.find_avoided("test@gmail.com")
    assert len(avoided) == 1
    assert avoided[0].view_count == threshold

    repo.mark_acted_on("avoid1")
    avoided_after = repo.find_avoided("test@gmail.com")
    assert len(avoided_after) == 0


# ── upsert_many bulk optimization ─────────────────────────────────────────────


def test_upsert_many_inserts_all():
    """upsert_many should insert all records in a single pass."""
    from postmind.core.storage import EmailRecord, EmailRepo, get_session

    session = get_session()
    repo = EmailRepo(session)

    records = [
        EmailRecord(
            account_email="bulk@test.com",
            gmail_id=f"bulk_{i}",
            thread_id=f"t{i}",
            subject=f"Subject {i}",
            sender_email="sender@example.com",
            sender_name="Sender",
            snippet="",
            label_ids_json="[]",
            internal_date=1_700_000_000_000 + i,
            size_estimate=1024 * i,
            is_unread=True,
            is_inbox=True,
        )
        for i in range(50)
    ]
    repo.upsert_many(records)

    for i in range(50):
        rec = repo.get(f"bulk_{i}")
        assert rec is not None, f"Record bulk_{i} missing after upsert_many"
        assert rec.subject == f"Subject {i}"


def test_upsert_many_updates_on_conflict():
    """upsert_many on an existing gmail_id should UPDATE the row, not insert a duplicate."""
    from postmind.core.storage import EmailRecord, EmailRepo, get_session

    session = get_session()
    repo = EmailRepo(session)

    original = EmailRecord(
        account_email="conflict@test.com",
        gmail_id="conflict_id",
        thread_id="t1",
        subject="Original Subject",
        sender_email="a@b.com",
        sender_name="A",
        snippet="",
        label_ids_json="[]",
        internal_date=1_700_000_000_000,
        size_estimate=100,
        is_unread=True,
        is_inbox=True,
    )
    repo.upsert_many([original])

    updated = EmailRecord(
        account_email="conflict@test.com",
        gmail_id="conflict_id",
        thread_id="t1",
        subject="Updated Subject",
        sender_email="a@b.com",
        sender_name="A",
        snippet="",
        label_ids_json="[]",
        internal_date=1_700_000_001_000,
        size_estimate=200,
        is_unread=False,
        is_inbox=True,
    )
    repo.upsert_many([updated])

    rec = repo.get("conflict_id")
    assert rec is not None
    assert rec.subject == "Updated Subject"
    # There should be exactly one row with this gmail_id
    from postmind.core.storage import EmailRecord as ER
    count = session.query(ER).filter_by(gmail_id="conflict_id").count()
    assert count == 1


def test_upsert_many_empty_list_no_crash():
    """upsert_many([]) must be a no-op — no exception raised."""
    from postmind.core.storage import EmailRepo, get_session

    repo = EmailRepo(get_session())
    repo.upsert_many([])  # should not raise


def test_existing_gmail_ids_returns_set():
    """existing_gmail_ids should return the set of gmail_ids for an account."""
    from postmind.core.storage import EmailRecord, EmailRepo, get_session

    session = get_session()
    repo = EmailRepo(session)

    for i in range(3):
        repo.upsert(
            EmailRecord(
                account_email="ids@test.com",
                gmail_id=f"exist_{i}",
                thread_id=f"t{i}",
                subject="",
                sender_email="x@y.com",
                sender_name="",
                snippet="",
                label_ids_json="[]",
                internal_date=0,
                size_estimate=0,
                is_unread=False,
                is_inbox=True,
            )
        )

    existing = repo.existing_gmail_ids("ids@test.com")
    assert isinstance(existing, set)
    assert existing == {"exist_0", "exist_1", "exist_2"}


def test_existing_gmail_ids_ignores_other_accounts():
    """existing_gmail_ids must only return IDs for the requested account."""
    from postmind.core.storage import EmailRecord, EmailRepo, get_session

    session = get_session()
    repo = EmailRepo(session)

    repo.upsert(
        EmailRecord(
            account_email="account_a@test.com",
            gmail_id="id_a",
            thread_id="t1",
            subject="",
            sender_email="x@y.com",
            sender_name="",
            snippet="",
            label_ids_json="[]",
            internal_date=0,
            size_estimate=0,
            is_unread=False,
            is_inbox=True,
        )
    )
    repo.upsert(
        EmailRecord(
            account_email="account_b@test.com",
            gmail_id="id_b",
            thread_id="t2",
            subject="",
            sender_email="x@y.com",
            sender_name="",
            snippet="",
            label_ids_json="[]",
            internal_date=0,
            size_estimate=0,
            is_unread=False,
            is_inbox=True,
        )
    )

    assert repo.existing_gmail_ids("account_a@test.com") == {"id_a"}
    assert repo.existing_gmail_ids("account_b@test.com") == {"id_b"}


# ── synced_at population & last-sync backfill ─────────────────────────────────


def test_upsert_many_populates_synced_at():
    """Bulk upsert must stamp synced_at — the bulk path used to insert NULL,
    leaving the cache with no record of when rows were fetched."""
    from postmind.core.storage import EmailRecord, EmailRepo, get_session

    session = get_session()
    repo = EmailRepo(session)

    repo.upsert_many([
        EmailRecord(
            account_email="stamp@test.com",
            gmail_id="stamp_1",
            thread_id="t1",
            subject="",
            sender_email="x@y.com",
            sender_name="",
            snippet="",
            label_ids_json="[]",
            internal_date=0,
            size_estimate=0,
            is_unread=False,
            is_inbox=True,
        )
    ])

    rec = repo.get("stamp_1")
    assert rec.synced_at is not None, "upsert_many left synced_at NULL"


def test_backfill_last_synced_when_null_but_cached():
    """If an account has cached emails but no last_synced_at (e.g. an
    interrupted big sync), backfill should set a timestamp so the UI stops
    claiming the mailbox was 'Never' synced."""
    from postmind.core.storage import (
        AccountRepo,
        EmailRecord,
        EmailRepo,
        get_session,
    )

    session = get_session()
    AccountRepo(session).register("back@test.com")
    EmailRepo(session).upsert_many([
        EmailRecord(
            account_email="back@test.com",
            gmail_id="b1",
            thread_id="t1",
            subject="",
            sender_email="x@y.com",
            sender_name="",
            snippet="",
            label_ids_json="[]",
            internal_date=0,
            size_estimate=0,
            is_unread=False,
            is_inbox=True,
        )
    ])

    acct = AccountRepo(session).get("back@test.com")
    assert acct.last_synced_at is None  # precondition

    AccountRepo(session).backfill_last_synced("back@test.com")

    acct = AccountRepo(session).get("back@test.com")
    assert acct.last_synced_at is not None


def test_backfill_last_synced_preserves_existing():
    """Backfill must not clobber a real last_synced_at timestamp."""
    from postmind.core.storage import AccountRepo, get_session

    session = get_session()
    AccountRepo(session).register("keep@test.com")
    AccountRepo(session).update_last_synced("keep@test.com")
    before = AccountRepo(session).get("keep@test.com").last_synced_at

    AccountRepo(session).backfill_last_synced("keep@test.com")
    after = AccountRepo(session).get("keep@test.com").last_synced_at
    assert after == before


def test_backfill_last_synced_noop_without_cache():
    """No cached emails → nothing to backfill, stays None/'Never'."""
    from postmind.core.storage import AccountRepo, get_session

    session = get_session()
    AccountRepo(session).register("empty@test.com")
    AccountRepo(session).backfill_last_synced("empty@test.com")
    assert AccountRepo(session).get("empty@test.com").last_synced_at is None
