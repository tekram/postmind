"""Newsletter & Promotions Digest — storage, generation, exemption, and auto-trash tests."""

import json
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def _use_clean_db(clean_db):
    pass


# ── Storage model tests ───────────────────────────────────────────────────────


def test_digest_exemption_model_exists():
    from postmind.core.storage import DigestExemption, get_session

    session = get_session()
    ex = DigestExemption(account_email="me@example.com", sender_email="news@sub.com")
    session.add(ex)
    session.commit()
    fetched = session.query(DigestExemption).filter_by(sender_email="news@sub.com").first()
    assert fetched is not None
    assert fetched.account_email == "me@example.com"


def test_daily_brief_has_digest_columns():
    from postmind.core.storage import DailyBrief, get_session

    session = get_session()
    brief = DailyBrief(
        account_email="me@example.com",
        brief_date="2026-06-08",
        content="test",
        newsletters_json=json.dumps([
            {
                "sender": "Sub",
                "sender_email": "s@x.com",
                "email_ids": ["id1"],
                "summary_bullets": ["a", "b", "c"],
                "exempted": False,
            }
        ]),
        promotions_json=json.dumps([]),
        digest_trash_after=datetime.now(timezone.utc) + timedelta(hours=48),
    )
    session.add(brief)
    session.commit()
    fetched = session.query(DailyBrief).filter_by(account_email="me@example.com").first()
    assert fetched.newsletters_json is not None
    assert fetched.promotions_json is not None
    assert fetched.digest_trash_after is not None


def test_digest_exemption_repo_add_get_remove():
    from postmind.core.storage import DigestExemptionRepo, get_session

    session = get_session()
    repo = DigestExemptionRepo(session)

    repo.add("me@example.com", "news@x.com")
    repo.add("me@example.com", "DEALS@vendor.COM")  # should lowercase

    s = repo.get_set("me@example.com")
    assert "news@x.com" in s
    assert "deals@vendor.com" in s

    assert repo.is_exempted("me@example.com", "News@X.com")  # case-insensitive

    repo.remove("me@example.com", "news@x.com")
    assert "news@x.com" not in repo.get_set("me@example.com")


def test_digest_exemption_repo_add_idempotent():
    from postmind.core.storage import DigestExemptionRepo, get_session

    session = get_session()
    repo = DigestExemptionRepo(session)
    repo.add("me@example.com", "dup@x.com")
    repo.add("me@example.com", "dup@x.com")  # no unique constraint violation
    assert len(repo.get_set("me@example.com")) == 1


# ── AI method tests ───────────────────────────────────────────────────────────


def test_mock_summarize_newsletter_sender():
    from postmind.core.mock_ai import MockAIEngine

    ai = MockAIEngine()
    bullets = ai.summarize_newsletter_sender(
        sender="The Rundown AI",
        emails=[{"subject": "AI News #42", "snippet": "Top 5 AI stories this week..."}],
    )
    assert isinstance(bullets, list)
    assert len(bullets) == 3
    assert all(isinstance(b, str) and b for b in bullets)


def test_mock_extract_promo_offer_line():
    from postmind.core.mock_ai import MockAIEngine

    ai = MockAIEngine()
    line = ai.extract_promo_offer_line(
        sender="Acme Shop",
        subject="30% off all orders this weekend",
        snippet="Use code SAVE30 at checkout. Expires Sunday.",
    )
    assert isinstance(line, str)
    assert len(line) > 0


# ── Daily brief digest generation tests ──────────────────────────────────────


def _add_email_record(
    session,
    gmail_id,
    *,
    list_unsubscribe="",
    ai_category="",
    sender_email=None,
    sender_name=None,
    snippet="test snippet",
    subject=None,
    internal_date=None,
    deal_score=0,
):
    import time

    from postmind.core.storage import ClassificationCacheRepo, EmailRecord, EmailRepo

    now_ms = int(time.time() * 1000)
    rec = EmailRecord(
        account_email="me@example.com",
        gmail_id=gmail_id,
        thread_id=f"t-{gmail_id}",
        subject=subject or f"Subject for {gmail_id}",
        sender_email=sender_email or f"{gmail_id}@sender.com",
        sender_name=sender_name or f"Sender {gmail_id}",
        snippet=snippet,
        label_ids_json='["INBOX","UNREAD"]',
        internal_date=internal_date or now_ms,
        is_unread=True,
        is_inbox=True,
        list_unsubscribe=list_unsubscribe,
        ai_category=ai_category,
    )
    EmailRepo(session).upsert(rec)
    if deal_score > 0:
        ClassificationCacheRepo(session).upsert_many([
            {
                "gmail_id": gmail_id,
                "category": "other",
                "priority": "low",
                "explanation": "promo",
                "suggested_action": "archive",
                "requires_reply": False,
                "deadline_hint": "",
                "deal_score": deal_score,
            }
        ])


@pytest.fixture()
def _ai_cloud(monkeypatch):
    import postmind.core.daily_brief as db_mod
    from postmind.core.mock_ai import MockAIEngine

    class _S:
        ai_mode = "cloud"
        ai_max_classify_batch = 10
        ai_classify_parallelism = 1
        avoidance_view_threshold = 3
        undo_window_days = 30

    monkeypatch.setattr(db_mod, "get_settings", lambda: _S())

    import postmind.core.ai_engine as ae_mod

    monkeypatch.setattr(ae_mod, "AIEngine", MockAIEngine)
    # Also patch config.get_settings used inside daily_brief
    import postmind.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "get_settings", lambda: _S())


def test_newsletter_digest_generated_from_list_unsubscribe(_ai_cloud):
    from postmind.core.daily_brief import DailyBriefGenerator
    from postmind.core.storage import get_session

    session = get_session()
    _add_email_record(
        session, "nl1", list_unsubscribe="<mailto:unsub@news.com>", sender_email="news@newsletter.com"
    )
    _add_email_record(
        session, "nl2", list_unsubscribe="<https://unsub.example.com>", sender_email="digest@weekly.com"
    )
    _add_email_record(session, "nopromo", list_unsubscribe="", sender_email="person@company.com")

    brief = DailyBriefGenerator("me@example.com").get_or_generate(force=True)

    assert brief.newsletters_json is not None
    newsletters = json.loads(brief.newsletters_json)
    nl_senders = {n["sender_email"] for n in newsletters}
    assert "news@newsletter.com" in nl_senders
    assert "digest@weekly.com" in nl_senders
    assert "person@company.com" not in nl_senders


def test_newsletter_digest_excludes_exempted_senders(_ai_cloud):
    from postmind.core.daily_brief import DailyBriefGenerator
    from postmind.core.storage import DigestExemptionRepo, get_session

    session = get_session()
    _add_email_record(
        session, "nl1", list_unsubscribe="<unsub@x.com>", sender_email="news@x.com"
    )
    DigestExemptionRepo(session).add("me@example.com", "news@x.com")

    brief = DailyBriefGenerator("me@example.com").get_or_generate(force=True)

    newsletters = json.loads(brief.newsletters_json or "[]")
    assert not any(n["sender_email"] == "news@x.com" for n in newsletters)


def test_digest_trash_after_set_when_items_exist(_ai_cloud):
    from postmind.core.daily_brief import DailyBriefGenerator
    from postmind.core.storage import get_session

    session = get_session()
    _add_email_record(
        session, "nl1", list_unsubscribe="<unsub@x.com>", sender_email="news@x.com"
    )

    brief = DailyBriefGenerator("me@example.com").get_or_generate(force=True)

    assert brief.digest_trash_after is not None
    ta = brief.digest_trash_after
    if ta.tzinfo is None:
        ta = ta.replace(tzinfo=timezone.utc)
    delta = ta - datetime.now(timezone.utc)
    assert timedelta(hours=47) < delta < timedelta(hours=49)


def test_digest_trash_after_not_set_when_no_items(_ai_cloud):
    from postmind.core.daily_brief import DailyBriefGenerator

    # No newsletter/promo emails — trash window should not be set
    brief = DailyBriefGenerator("me@example.com").get_or_generate(force=True)
    assert brief.digest_trash_after is None


def test_newsletter_digest_items_have_bullets(_ai_cloud):
    from postmind.core.daily_brief import DailyBriefGenerator
    from postmind.core.storage import get_session

    session = get_session()
    _add_email_record(
        session,
        "nl1",
        list_unsubscribe="<unsub@news.com>",
        sender_email="news@news.com",
        sender_name="News Daily",
    )

    brief = DailyBriefGenerator("me@example.com").get_or_generate(force=True)

    newsletters = json.loads(brief.newsletters_json)
    assert len(newsletters) == 1
    item = newsletters[0]
    assert item["sender"] == "News Daily"
    assert item["sender_email"] == "news@news.com"
    assert len(item["summary_bullets"]) == 3
    assert item["exempted"] is False
    assert "nl1" in item["email_ids"]


# ── Daemon auto-trash tests ───────────────────────────────────────────────────


def test_digest_trash_execution():
    from postmind.core.storage import DailyBrief, UndoLogRepo, get_session

    session = get_session()
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    brief = DailyBrief(
        account_email="me@example.com",
        brief_date=datetime.now(timezone.utc).date().isoformat(),
        content="test",
        newsletters_json=json.dumps([
            {
                "sender": "News",
                "sender_email": "news@x.com",
                "email_ids": ["id1", "id2"],
                "summary_bullets": ["a", "b", "c"],
                "exempted": False,
            },
            {
                "sender": "Safe",
                "sender_email": "safe@x.com",
                "email_ids": ["id3"],
                "summary_bullets": ["a", "b", "c"],
                "exempted": True,
            },
        ]),
        promotions_json=json.dumps([]),
        digest_trash_after=past,
    )
    session.add(brief)
    session.commit()

    trashed = []

    class FakeProvider:
        def trash_messages(self, ids):
            trashed.extend(ids)

    from postmind.core.daemon import _run_digest_trash

    _run_digest_trash(session, FakeProvider(), "me@example.com")

    assert set(trashed) == {"id1", "id2"}  # id3 is exempted
    refreshed = session.query(DailyBrief).filter_by(account_email="me@example.com").first()
    assert refreshed.digest_trash_after is None
    logs = UndoLogRepo(session).list_recent("me@example.com")
    assert any("digest_trash" in l.operation for l in logs)


def test_digest_trash_skips_when_not_yet_due():
    from postmind.core.storage import DailyBrief, get_session

    session = get_session()
    future = datetime.now(timezone.utc) + timedelta(hours=24)
    brief = DailyBrief(
        account_email="me@example.com",
        brief_date=datetime.now(timezone.utc).date().isoformat(),
        content="test",
        newsletters_json=json.dumps([
            {
                "sender": "News",
                "sender_email": "news@x.com",
                "email_ids": ["id1"],
                "summary_bullets": ["a", "b", "c"],
                "exempted": False,
            }
        ]),
        promotions_json=json.dumps([]),
        digest_trash_after=future,
    )
    session.add(brief)
    session.commit()

    trashed = []

    class FakeProvider:
        def trash_messages(self, ids):
            trashed.extend(ids)

    from postmind.core.daemon import _run_digest_trash

    _run_digest_trash(session, FakeProvider(), "me@example.com")

    assert trashed == []  # should not trash yet
    refreshed = session.query(DailyBrief).filter_by(account_email="me@example.com").first()
    assert refreshed.digest_trash_after is not None  # not cleared


def test_digest_trash_respects_runtime_exemptions():
    """Exemptions added after generation (but before trash) are respected at trash time."""
    from postmind.core.storage import DailyBrief, DigestExemptionRepo, get_session

    session = get_session()
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    brief = DailyBrief(
        account_email="me@example.com",
        brief_date=datetime.now(timezone.utc).date().isoformat(),
        content="test",
        newsletters_json=json.dumps([
            {
                "sender": "News",
                "sender_email": "lateexempt@x.com",
                "email_ids": ["id1"],
                "summary_bullets": ["a", "b", "c"],
                "exempted": False,  # NOT marked exempted in JSON
            }
        ]),
        promotions_json=json.dumps([]),
        digest_trash_after=past,
    )
    session.add(brief)
    session.commit()
    # But user added a DigestExemption row after generation
    DigestExemptionRepo(session).add("me@example.com", "lateexempt@x.com")

    trashed = []

    class FakeProvider:
        def trash_messages(self, ids):
            trashed.extend(ids)

    from postmind.core.daemon import _run_digest_trash

    _run_digest_trash(session, FakeProvider(), "me@example.com")

    assert trashed == []  # runtime exemption was respected
