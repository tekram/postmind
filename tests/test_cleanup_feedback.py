"""Tests for the Phase 3 learning loop: CleanupFeedbackRepo + sender priors."""

from datetime import datetime, timedelta, timezone

import pytest

from postmind.core.sender_stats import (
    AUTO_SELECT_THRESHOLD,
    build_cleanup_batches,
    compute_impact_scores,
)
from postmind.core.storage import (
    CleanupFeedbackRecord,
    CleanupFeedbackRepo,
    get_session,
)

from .test_cleanup_batches import _g

ACCOUNT = "me@example.com"


@pytest.fixture(autouse=True)
def _use_clean_db(clean_db):
    pass


def _items(sender, batch_key, action, decision, n):
    return [
        {"sender_email": sender, "batch_key": batch_key, "action": action, "decision": decision}
        for _ in range(n)
    ]


# ── sender_priors math ─────────────────────────────────────────────────────────


def test_always_approved_gives_near_max_positive_prior():
    repo = CleanupFeedbackRepo(get_session())
    repo.record_many(ACCOUNT, _items("a@promo.com", "promos-unopened", "trash", "approved", 5))
    priors = repo.sender_priors(ACCOUNT)
    # rate=1.0 -> round((1.0-0.5)*2*15)=15
    assert priors["a@promo.com"] == 15


def test_always_skipped_gives_near_max_negative_prior():
    repo = CleanupFeedbackRepo(get_session())
    repo.record_many(ACCOUNT, _items("b@promo.com", "promos-unopened", "trash", "skipped", 5))
    priors = repo.sender_priors(ACCOUNT)
    # rate=0.0 -> round((0-0.5)*2*15)=-15
    assert priors["b@promo.com"] == -15


def test_fifty_fifty_is_neutral_and_absent():
    repo = CleanupFeedbackRepo(get_session())
    repo.record_many(ACCOUNT, _items("c@promo.com", "promos-unopened", "trash", "approved", 3))
    repo.record_many(ACCOUNT, _items("c@promo.com", "promos-unopened", "trash", "skipped", 3))
    priors = repo.sender_priors(ACCOUNT)
    # rate=0.5 -> adjustment 0 -> only non-zero senders returned
    assert "c@promo.com" not in priors


def test_dropped_counts_in_denominator():
    repo = CleanupFeedbackRepo(get_session())
    repo.record_many(ACCOUNT, _items("d@promo.com", "promos-unopened", "trash", "approved", 1))
    repo.record_many(ACCOUNT, _items("d@promo.com", "promos-unopened", "trash", "dropped", 3))
    priors = repo.sender_priors(ACCOUNT)
    # rate=1/4=0.25 -> round((0.25-0.5)*2*15)=round(-7.5)=-8 (banker-safe: round(-7.5)==-8)
    assert priors["d@promo.com"] == round((0.25 - 0.5) * 2 * 15)


def test_clamping_respects_custom_max_adjust():
    repo = CleanupFeedbackRepo(get_session())
    repo.record_many(ACCOUNT, _items("e@promo.com", "promos-unopened", "trash", "approved", 10))
    priors = repo.sender_priors(ACCOUNT, max_adjust=8)
    assert priors["e@promo.com"] == 8  # clamped to +max_adjust


def test_priors_are_scoped_by_account():
    repo = CleanupFeedbackRepo(get_session())
    repo.record_many(ACCOUNT, _items("f@promo.com", "promos-unopened", "trash", "approved", 4))
    repo.record_many("other@example.com", _items("f@promo.com", "promos-unopened", "trash", "skipped", 4))
    assert repo.sender_priors(ACCOUNT)["f@promo.com"] == 15
    assert repo.sender_priors("other@example.com")["f@promo.com"] == -15


def test_record_many_empty_is_noop():
    repo = CleanupFeedbackRepo(get_session())
    repo.record_many(ACCOUNT, [])
    assert repo.sender_priors(ACCOUNT) == {}


# ── batch_session_counts ────────────────────────────────────────────────────────


def _insert_on_day(session, account, sender, batch_key, decision, day):
    """Insert a feedback row with an explicit created_at timestamp."""
    session.add(
        CleanupFeedbackRecord(
            account_email=account,
            sender_email=sender,
            batch_key=batch_key,
            action="trash",
            decision=decision,
            created_at=day,
        )
    )
    session.commit()


def test_same_day_approvals_count_as_one_session():
    session = get_session()
    day = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    _insert_on_day(session, ACCOUNT, "a@promo.com", "promos-unopened", "approved", day)
    _insert_on_day(session, ACCOUNT, "b@promo.com", "promos-unopened", "approved", day + timedelta(hours=5))
    counts = CleanupFeedbackRepo(session).batch_session_counts(ACCOUNT)
    assert counts["promos-unopened"] == 1


def test_different_days_count_separately():
    session = get_session()
    base = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    _insert_on_day(session, ACCOUNT, "a@promo.com", "promos-unopened", "approved", base)
    _insert_on_day(session, ACCOUNT, "a@promo.com", "promos-unopened", "approved", base + timedelta(days=1))
    _insert_on_day(session, ACCOUNT, "a@promo.com", "promos-unopened", "approved", base + timedelta(days=2))
    counts = CleanupFeedbackRepo(session).batch_session_counts(ACCOUNT)
    assert counts["promos-unopened"] == 3


def test_skipped_only_batch_not_counted():
    session = get_session()
    day = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    _insert_on_day(session, ACCOUNT, "a@promo.com", "old-clutter", "skipped", day)
    _insert_on_day(session, ACCOUNT, "a@promo.com", "old-clutter", "dropped", day)
    counts = CleanupFeedbackRepo(session).batch_session_counts(ACCOUNT)
    assert "old-clutter" not in counts


# ── build_cleanup_batches with sender_priors ───────────────────────────────────


def test_negative_prior_pushes_sender_out_of_promos():
    # A borderline promos sender (conf 72, just over the >=70 promos cutoff) would
    # normally land in promos; a -15 prior drops it to 57, into the review band.
    def make():
        return _g("edge@promo.com", "Edge", 35, 1, 90, True)  # conf 72

    baseline = make()
    compute_impact_scores([baseline])
    plain = build_cleanup_batches([baseline])
    promos = [b for b in plain.batches if b.key == "promos-unopened"]
    assert promos and "edge@promo.com" in promos[0].sender_emails

    nudged = make()
    compute_impact_scores([nudged])
    plan = build_cleanup_batches([nudged], sender_priors={"edge@promo.com": -15})
    promos_after = [b for b in plan.batches if b.key == "promos-unopened"]
    # -15 prior drops conf below 70, so it no longer lands in promos
    assert not any("edge@promo.com" in b.sender_emails for b in promos_after)
    review = next(b for b in plan.batches if b.key == "review")
    assert "edge@promo.com" in review.sender_emails


def test_negative_prior_drops_batch_below_auto_select():
    # A sender that auto-selects on its own (conf 86); a -15 prior should knock the
    # batch's count-weighted confidence below the auto-select threshold.
    def make():
        return _g("borderline@promo.com", "Borderline", 30, 5, 200, True)  # conf 86

    baseline = make()
    compute_impact_scores([baseline])
    plain = build_cleanup_batches([baseline])
    promos = next(b for b in plain.batches if b.key == "promos-unopened")
    assert promos.confidence >= AUTO_SELECT_THRESHOLD
    assert promos.auto_select is True

    nudged = make()
    compute_impact_scores([nudged])
    plan = build_cleanup_batches([nudged], sender_priors={"borderline@promo.com": -15})
    promos_after = next(b for b in plan.batches if b.key == "promos-unopened")
    assert promos_after.confidence < AUTO_SELECT_THRESHOLD
    assert promos_after.auto_select is False


def test_positive_prior_raises_confidence():
    def make():
        return _g("ok@promo.com", "OK", 35, 1, 90, True)  # conf 72

    base = make()
    compute_impact_scores([base])
    plain = build_cleanup_batches([base])
    base_batch = next(b for b in plain.batches if "ok@promo.com" in b.sender_emails)

    nudged = make()
    compute_impact_scores([nudged])
    plan = build_cleanup_batches([nudged], sender_priors={"ok@promo.com": 15})
    promos = next(b for b in plan.batches if b.key == "promos-unopened")
    assert "ok@promo.com" in promos.sender_emails
    assert promos.confidence > base_batch.confidence  # 72 -> 87
