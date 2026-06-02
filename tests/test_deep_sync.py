"""Tests for deep sync improvements and from-cache stats/purge."""

import json
import pytest


@pytest.fixture(autouse=True)
def _use_clean_db(clean_db):
    pass


# ── fetch_sender_groups_from_db ───────────────────────────────────────────────


def _make_record(
    gmail_id,
    sender_email,
    sender_name="Sender",
    subject="Hello",
    size=10000,
    internal_date=1700000000000,
    is_inbox=True,
    list_unsubscribe="",
    account_email="user@gmail.com",
):
    from postmind.core.storage import EmailRecord

    return EmailRecord(
        account_email=account_email,
        gmail_id=gmail_id,
        thread_id=f"thread-{gmail_id}",
        subject=subject,
        sender_email=sender_email,
        sender_name=sender_name,
        snippet="snippet",
        label_ids_json=json.dumps(["INBOX"]),
        internal_date=internal_date,
        size_estimate=size,
        is_unread=False,
        is_inbox=is_inbox,
        list_unsubscribe=list_unsubscribe,
    )


def _populate_db(records):
    from postmind.core.storage import EmailRepo, get_session

    repo = EmailRepo(get_session())
    repo.upsert_many(records)


def test_fetch_sender_groups_from_db_basic():
    from postmind.core.sender_stats import fetch_sender_groups_from_db

    records = [
        _make_record(f"id{i}", "news@example.com", size=5000, internal_date=1700000000000 + i)
        for i in range(5)
    ]
    _populate_db(records)

    groups = fetch_sender_groups_from_db("user@gmail.com", scope="anywhere", min_count=2)

    assert len(groups) == 1
    assert groups[0].sender_email == "news@example.com"
    assert groups[0].count == 5
    assert groups[0].total_size_bytes == 25000


def test_fetch_sender_groups_from_db_scope_inbox_only():
    from postmind.core.sender_stats import fetch_sender_groups_from_db

    records = [
        _make_record("in1", "a@x.com", is_inbox=True),
        _make_record("in2", "a@x.com", is_inbox=True),
        _make_record("arc1", "a@x.com", is_inbox=False),
        _make_record("arc2", "a@x.com", is_inbox=False),
    ]
    _populate_db(records)

    inbox_groups = fetch_sender_groups_from_db("user@gmail.com", scope="inbox", min_count=1)
    assert inbox_groups[0].count == 2

    all_groups = fetch_sender_groups_from_db("user@gmail.com", scope="anywhere", min_count=1)
    assert all_groups[0].count == 4


def test_fetch_sender_groups_from_db_sort_size():
    from postmind.core.sender_stats import fetch_sender_groups_from_db

    _populate_db([
        _make_record("s1", "big@x.com", size=100_000),
        _make_record("s2", "big@x.com", size=100_000),
        _make_record("s3", "small@x.com", size=1_000),
        _make_record("s4", "small@x.com", size=1_000),
    ])

    groups = fetch_sender_groups_from_db("user@gmail.com", scope="anywhere", min_count=1, sort_by="size")
    assert groups[0].sender_email == "big@x.com"
    assert groups[1].sender_email == "small@x.com"


def test_fetch_sender_groups_from_db_sort_count():
    from postmind.core.sender_stats import fetch_sender_groups_from_db

    _populate_db([
        _make_record("c1", "frequent@x.com"),
        _make_record("c2", "frequent@x.com"),
        _make_record("c3", "frequent@x.com"),
        _make_record("c4", "rare@x.com"),
        _make_record("c5", "rare@x.com"),
    ])

    groups = fetch_sender_groups_from_db("user@gmail.com", scope="anywhere", min_count=1, sort_by="count")
    assert groups[0].sender_email == "frequent@x.com"
    assert groups[0].count == 3


def test_fetch_sender_groups_from_db_empty():
    from postmind.core.sender_stats import fetch_sender_groups_from_db

    groups = fetch_sender_groups_from_db("nobody@gmail.com", scope="anywhere")
    assert groups == []


def test_fetch_sender_groups_from_db_min_count_filter():
    from postmind.core.sender_stats import fetch_sender_groups_from_db

    _populate_db([
        _make_record("x1", "solo@x.com"),  # only 1 — below default min_count=2
        _make_record("x2", "duo@x.com"),
        _make_record("x3", "duo@x.com"),
    ])

    groups = fetch_sender_groups_from_db("user@gmail.com", scope="anywhere", min_count=2)
    assert len(groups) == 1
    assert groups[0].sender_email == "duo@x.com"


def test_fetch_sender_groups_from_db_top_n():
    from postmind.core.sender_stats import fetch_sender_groups_from_db

    for i in range(10):
        _populate_db([
            _make_record(f"m{i}a", f"sender{i}@x.com"),
            _make_record(f"m{i}b", f"sender{i}@x.com"),
        ])

    groups = fetch_sender_groups_from_db("user@gmail.com", scope="anywhere", min_count=1, top_n=5)
    assert len(groups) == 5


def test_fetch_sender_groups_from_db_account_isolation():
    from postmind.core.sender_stats import fetch_sender_groups_from_db

    _populate_db([
        _make_record("u1a", "news@x.com", account_email="alice@gmail.com"),
        _make_record("u1b", "news@x.com", account_email="alice@gmail.com"),
        _make_record("u2a", "news@x.com", account_email="bob@gmail.com"),
        _make_record("u2b", "news@x.com", account_email="bob@gmail.com"),
    ])

    alice_groups = fetch_sender_groups_from_db("alice@gmail.com", scope="anywhere", min_count=1)
    assert alice_groups[0].count == 2

    bob_groups = fetch_sender_groups_from_db("bob@gmail.com", scope="anywhere", min_count=1)
    assert bob_groups[0].count == 2


# ── get_messages_metadata_batch ───────────────────────────────────────────────


def test_get_messages_metadata_batch_calls_fetch_batch(monkeypatch):
    """Verify metadata batch uses format='metadata', not 'full'."""
    from postmind.core.gmail_client import GmailClient

    calls = []

    def fake_fetch_batch(self, ids, format="full", metadata_headers=None):
        calls.append({"format": format, "headers": metadata_headers, "ids": ids})
        return []

    monkeypatch.setattr(GmailClient, "_fetch_batch", fake_fetch_batch)
    monkeypatch.setattr(GmailClient, "__init__", lambda self, creds=None: None)

    client = GmailClient.__new__(GmailClient)
    client.get_messages_metadata_batch(["id1", "id2"])

    assert len(calls) == 1
    assert calls[0]["format"] == "metadata"
    assert "List-Unsubscribe" in calls[0]["headers"]


# ── rate-limit / quota retry ──────────────────────────────────────────────────


def _http_error(status, reason_text):
    """Build a googleapiclient HttpError with a given status and reason body."""
    from googleapiclient.errors import HttpError

    class _Resp:
        def __init__(self, s):
            self.status = s
            self.reason = "err"

        def get(self, key, default=None):
            return {"content-type": "application/json"}.get(key, default)

    content = (
        b'{"error": {"code": %d, "message": "%s", "errors": [{"reason": "%s"}]}}'
        % (status, reason_text.encode(), reason_text.encode())
    )
    return HttpError(_Resp(status), content, uri="https://gmail/test")


def test_is_rate_limit_detects_403_quota():
    """Gmail returns per-user rate limiting as HTTP 403 — must be recognized."""
    from postmind.core.gmail_client import _is_rate_limit

    assert _is_rate_limit(_http_error(403, "rateLimitExceeded")) is True
    assert _is_rate_limit(_http_error(429, "userRateLimitExceeded")) is True
    assert _is_rate_limit(_http_error(403, "Quota exceeded for quota metric")) is True
    # A genuine permission/auth 403 is NOT a rate limit and must not be retried.
    assert _is_rate_limit(_http_error(403, "insufficientPermissions")) is False


def test_with_retry_retries_403_rate_limit(monkeypatch):
    """A 403 rate-limit error is transient and must be retried, not raised."""
    import postmind.core.gmail_client as gc

    monkeypatch.setattr(gc.time, "sleep", lambda *_: None)  # no real waiting

    calls = {"n": 0}

    @gc._with_retry(max_attempts=4, base_delay=0.01)
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(403, "rateLimitExceeded")
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3  # failed twice, succeeded on the third


def test_with_retry_does_not_retry_plain_403(monkeypatch):
    """A non-rate-limit 403 (auth/permission) must raise immediately."""
    import postmind.core.gmail_client as gc
    from googleapiclient.errors import HttpError

    monkeypatch.setattr(gc.time, "sleep", lambda *_: None)

    calls = {"n": 0}

    @gc._with_retry(max_attempts=4, base_delay=0.01)
    def forbidden():
        calls["n"] += 1
        raise _http_error(403, "insufficientPermissions")

    with pytest.raises(HttpError):
        forbidden()
    assert calls["n"] == 1  # raised on first attempt, no retries


def test_list_message_ids_paginates_without_cap(monkeypatch):
    """With max_results=None, list_message_ids must follow every page token."""
    from postmind.core.gmail_client import GmailClient

    pages = [
        {"messages": [{"id": f"a{i}"} for i in range(500)], "nextPageToken": "p2"},
        {"messages": [{"id": f"b{i}"} for i in range(500)], "nextPageToken": "p3"},
        {"messages": [{"id": f"c{i}"} for i in range(123)]},  # last page, no token
    ]
    seq = iter(pages)

    class _List:
        def execute(self):
            return next(seq)

    class _Messages:
        def list(self, **kwargs):
            return _List()

    class _Users:
        def messages(self):
            return _Messages()

    class _Service:
        def users(self):
            return _Users()

    monkeypatch.setattr(GmailClient, "__init__", lambda self, creds=None: None)
    client = GmailClient.__new__(GmailClient)
    client._service = _Service()
    client._user = "me"

    ids = client.list_message_ids(query="in:anywhere", max_results=None)
    assert len(ids) == 1123  # all three pages, nothing truncated
