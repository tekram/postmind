"""Tests for UserActionRepo — the behavioral signal store powering the learning loop."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from postmind.core.storage import (
    EmailRecord,
    EmailRepo,
    RuleDefinition,
    RuleRepo,
    UserActionRecord,
    UserActionRepo,
    get_session,
)

ACCOUNT = "me@example.com"
OTHER = "other@example.com"


@pytest.fixture(autouse=True)
def _use_clean_db(clean_db):
    pass


# ── record() ────────────────────────────────────────────────────────────────


def test_record_inserts_row():
    repo = UserActionRepo(get_session())
    repo.record(ACCOUNT, "msg1", "promo@deals.com", "Deals", "Sale!", "trash", "triage")
    rows = get_session().query(UserActionRecord).all()
    assert len(rows) == 1
    r = rows[0]
    assert r.account_email == ACCOUNT
    assert r.gmail_id == "msg1"
    assert r.sender_email == "promo@deals.com"
    assert r.action == "trash"
    assert r.source == "triage"


def test_record_stores_ai_classification_context():
    repo = UserActionRepo(get_session())
    repo.record(
        ACCOUNT, "msg2", "news@paper.com", "News", "Headline",
        "archive", "brief",
        ai_category="newsletter", ai_priority="low",
    )
    row = get_session().query(UserActionRecord).first()
    assert row.ai_category == "newsletter"
    assert row.ai_priority == "low"


def test_record_never_raises_on_bad_input():
    # Simulate a DB error by passing an excessively long string — should be swallowed.
    repo = UserActionRepo(get_session())
    # Should not raise even if something goes wrong internally.
    repo.record(ACCOUNT, "", "", "", "", "trash", "triage")


# ── sender_action_counts() ──────────────────────────────────────────────────


def test_sender_action_counts_aggregates_by_sender_and_action():
    repo = UserActionRepo(get_session())
    for _ in range(3):
        repo.record(ACCOUNT, "x", "promo@deals.com", "", "", "trash", "triage")
    repo.record(ACCOUNT, "y", "promo@deals.com", "", "", "archive", "triage")

    counts = repo.sender_action_counts(ACCOUNT)
    assert counts["promo@deals.com"]["trash"] == 3
    assert counts["promo@deals.com"]["archive"] == 1


def test_sender_action_counts_lowercases_sender_email():
    repo = UserActionRepo(get_session())
    repo.record(ACCOUNT, "a", "PROMO@Deals.COM", "", "", "trash", "triage")
    repo.record(ACCOUNT, "b", "promo@deals.com", "", "", "trash", "triage")

    counts = repo.sender_action_counts(ACCOUNT)
    assert counts.get("promo@deals.com", {}).get("trash", 0) == 2


def test_sender_action_counts_is_scoped_by_account():
    repo = UserActionRepo(get_session())
    repo.record(ACCOUNT, "a", "promo@deals.com", "", "", "trash", "triage")
    repo.record(OTHER, "b", "promo@deals.com", "", "", "archive", "triage")

    assert "trash" in repo.sender_action_counts(ACCOUNT).get("promo@deals.com", {})
    assert "archive" not in repo.sender_action_counts(ACCOUNT).get("promo@deals.com", {})


def test_sender_action_counts_respects_lookback_window():
    session = get_session()
    old = datetime.now(timezone.utc) - timedelta(days=100)
    session.add(UserActionRecord(
        account_email=ACCOUNT, gmail_id="old", sender_email="promo@deals.com",
        sender_name="", subject="", action="trash", source="triage", created_at=old,
    ))
    session.commit()

    counts = UserActionRepo(session).sender_action_counts(ACCOUNT, lookback_days=90)
    assert "promo@deals.com" not in counts


# ── high_trash_senders() ────────────────────────────────────────────────────


def test_high_trash_senders_returns_sender_with_high_trash_rate():
    repo = UserActionRepo(get_session())
    for _ in range(5):
        repo.record(ACCOUNT, "x", "spam@junk.com", "", "", "trash", "triage")
    result = repo.high_trash_senders(ACCOUNT)
    assert "spam@junk.com" in result


def test_high_trash_senders_excludes_below_min_actions():
    repo = UserActionRepo(get_session())
    repo.record(ACCOUNT, "a", "rare@junk.com", "", "", "trash", "triage")
    repo.record(ACCOUNT, "b", "rare@junk.com", "", "", "trash", "triage")
    # Only 2 actions, min_actions=3 by default
    assert "rare@junk.com" not in repo.high_trash_senders(ACCOUNT)


def test_high_trash_senders_excludes_mixed_sender():
    repo = UserActionRepo(get_session())
    for _ in range(3):
        repo.record(ACCOUNT, "x", "mixed@news.com", "", "", "trash", "triage")
    for _ in range(3):
        repo.record(ACCOUNT, "y", "mixed@news.com", "", "", "archive", "triage")
    # 3/6 = 50% trash rate, below 80% threshold
    assert "mixed@news.com" not in repo.high_trash_senders(ACCOUNT)


# ── replied_senders() ───────────────────────────────────────────────────────


def test_replied_senders_returns_senders_with_reply_action():
    repo = UserActionRepo(get_session())
    repo.record(ACCOUNT, "m1", "boss@work.com", "Boss", "Re: meeting", "reply", "triage")
    assert "boss@work.com" in repo.replied_senders(ACCOUNT)


def test_replied_senders_excludes_trash_only_senders():
    repo = UserActionRepo(get_session())
    repo.record(ACCOUNT, "m1", "promo@deals.com", "", "", "trash", "triage")
    assert "promo@deals.com" not in repo.replied_senders(ACCOUNT)


def test_replied_senders_is_scoped_by_account():
    repo = UserActionRepo(get_session())
    repo.record(OTHER, "m1", "boss@work.com", "", "", "reply", "triage")
    assert "boss@work.com" not in repo.replied_senders(ACCOUNT)


# ── candidates_for_rule_synthesis() ─────────────────────────────────────────


def test_candidates_for_rule_synthesis_finds_heavy_trash_sender():
    repo = UserActionRepo(get_session())
    for i in range(6):
        repo.record(
            ACCOUNT, f"msg{i}", "spam@promo.com", "Promos", f"Deal #{i}",
            "trash", "triage",
        )
    candidates = repo.candidates_for_rule_synthesis(ACCOUNT)
    assert any(c["sender_email"] == "spam@promo.com" for c in candidates)


def test_candidates_for_rule_synthesis_excludes_existing_rule():
    session = get_session()
    repo = UserActionRepo(session)
    for i in range(6):
        repo.record(ACCOUNT, f"m{i}", "spam@promo.com", "Promo", f"Subject {i}", "trash", "triage")

    # Create an existing rule covering this sender
    RuleRepo(session).create(RuleDefinition(
        account_email=ACCOUNT,
        name="Existing rule",
        natural_language="trash promos",
        gmail_query="from:spam@promo.com",
        action="trash",
        action_params_json="{}",
        ai_explanation="",
        is_active=True,
    ))

    candidates = repo.candidates_for_rule_synthesis(ACCOUNT)
    assert not any(c["sender_email"] == "spam@promo.com" for c in candidates)


def test_candidates_for_rule_synthesis_below_threshold_excluded():
    repo = UserActionRepo(get_session())
    # Only 3 trash + 3 archive = 50% trash rate, needs 85%
    for _ in range(3):
        repo.record(ACCOUNT, "x", "ok@news.com", "", "", "trash", "triage")
    for _ in range(3):
        repo.record(ACCOUNT, "y", "ok@news.com", "", "", "archive", "triage")
    assert not repo.candidates_for_rule_synthesis(ACCOUNT)


def test_candidates_include_sample_subjects():
    repo = UserActionRepo(get_session())
    subjects = [f"Deal #{i}" for i in range(6)]
    for i, subj in enumerate(subjects):
        repo.record(ACCOUNT, f"m{i}", "shop@store.com", "Store", subj, "trash", "triage")
    candidates = repo.candidates_for_rule_synthesis(ACCOUNT)
    assert candidates
    assert len(candidates[0]["sample_subjects"]) > 0
