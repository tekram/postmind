"""Tests for the smart first-run cleanup plan (build_cleanup_plan + narration)."""

from datetime import datetime, timedelta, timezone

import pytest

from postmind.core.sender_stats import (
    SenderGroup,
    build_cleanup_plan,
    cleanup_plan_digest,
    compute_impact_scores,
)


def _g(email, name, count, mb, age_days, unsub):
    now = datetime.now(timezone.utc)
    return SenderGroup(
        sender_email=email,
        sender_name=name,
        count=count,
        total_size_bytes=int(mb * 1024 * 1024),
        earliest_date=now - timedelta(days=age_days),
        latest_date=now,
        sample_subjects=["Big sale inside", "Weekly digest"],
        message_ids=[f"{email}-{i}" for i in range(count)],
        has_unsubscribe=unsub,
    )


def _plan(groups):
    compute_impact_scores(groups)
    return build_cleanup_plan(groups)


# ── Bucketing ──────────────────────────────────────────────────────────────


def test_empty_groups_no_opportunity():
    plan = build_cleanup_plan([])
    assert plan.headline is None
    assert plan.has_opportunity is False
    assert plan.secondary == []
    assert plan.total_emails == 0


def test_headline_is_safe_bulk_mail():
    groups = [
        _g("deals@promo.com", "Promo Co", 300, 500, 400, True),
        _g("news@news.com", "Newsletter", 120, 80, 500, True),
    ]
    plan = _plan(groups)
    assert plan.has_opportunity
    assert plan.headline.suggested_action == "trash"
    assert set(plan.headline.sender_emails) == {"deals@promo.com", "news@news.com"}
    # honest aggregate figures
    assert plan.headline.count == 420
    assert plan.headline.size_mb == pytest.approx(580.0, abs=1.0)


def test_sensitive_sender_never_in_a_bucket():
    groups = [
        _g("deals@promo.com", "Promo Co", 300, 500, 400, True),
        _g("alerts@mybank.com", "My Bank", 200, 50, 400, True),  # sensitive
    ]
    plan = _plan(groups)
    all_targets = set(plan.headline.sender_emails)
    for b in plan.secondary:
        all_targets |= set(b.sender_emails)
    assert "alerts@mybank.com" not in all_targets
    assert plan.protected_count == 1
    assert "bank" in plan.protected_note.lower() or "left" in plan.protected_note.lower()


def test_buckets_do_not_overlap():
    groups = [
        _g("deals@promo.com", "Promo Co", 300, 500, 400, True),   # headline
        _g("old@stuff.com", "Old Stuff", 5, 3, 800, False),       # old, low-conf
        _g("notify@app.com", "App", 250, 2, 50, False),           # frequent, recent
    ]
    plan = _plan(groups)
    seen = []
    for b in ([plan.headline] + plan.secondary):
        seen.extend(b.sender_emails)
    assert len(seen) == len(set(seen)), "a sender appeared in more than one bucket"


def test_headline_promoted_when_nothing_clears_safe_bar():
    # All senders are below the safe (>=70) confidence bar, but there's still old
    # clutter — the strongest secondary bucket should be promoted to headline.
    groups = [
        _g("old@stuff.com", "Old Stuff", 5, 40, 800, False),
        _g("misc@thing.com", "Thing", 3, 5, 700, False),
    ]
    plan = _plan(groups)
    assert plan.has_opportunity
    assert plan.headline is not None


# ── Digest ───────────────────────────────────────────────────────────────────


def test_digest_is_body_free_and_matches_buckets():
    groups = [
        _g("deals@promo.com", "Promo Co", 300, 500, 400, True),
        _g("old@stuff.com", "Old Stuff", 5, 3, 800, False),
    ]
    plan = _plan(groups)
    digest = cleanup_plan_digest(plan)
    assert len(digest) == 1 + len(plan.secondary)
    for entry in digest:
        # only aggregate signals — never sender lists or subjects
        assert set(entry.keys()) == {"key", "title", "senders", "emails", "size_mb", "action"}


# ── LLM narration application ─────────────────────────────────────────────────


def test_summarize_cleanup_plan_applies_text_and_drops_unknown_keys(monkeypatch):
    import json

    from postmind.core.ai_engine import AIEngine

    engine = AIEngine.__new__(AIEngine)  # bypass __init__ (no backend needed)
    engine._mode = "cloud"

    fake = {
        "intro": "Your inbox is mostly old marketing mail.",
        "buckets": {
            "headline": {"title": "Ignored newsletters", "rationale": "Safe to clear."},
            "bogus": {"title": "should be dropped", "rationale": "x"},
        },
    }
    monkeypatch.setattr(engine, "_complete", lambda *a, **k: json.dumps(fake))

    digest = [{"key": "headline", "title": "x", "senders": 2, "emails": 10,
               "size_mb": 5.0, "action": "trash"}]
    out = engine.summarize_cleanup_plan(digest, total_emails=10, total_senders=2)

    assert out["intro"] == "Your inbox is mostly old marketing mail."
    assert "headline" in out["buckets"]
    assert "bogus" not in out["buckets"], "unknown bucket key must be dropped"


def test_summarize_cleanup_plan_empty_digest_short_circuits():
    from postmind.core.ai_engine import AIEngine

    engine = AIEngine.__new__(AIEngine)
    engine._mode = "cloud"
    out = engine.summarize_cleanup_plan([], total_emails=0, total_senders=0)
    assert out == {"intro": "", "buckets": {}}


# ── First-run flag ─────────────────────────────────────────────────────────


def test_mark_welcomed_sets_timestamp_once(clean_db):
    from postmind.core.storage import AccountRepo, get_session

    s = get_session()
    repo = AccountRepo(s)
    repo.register("me@example.com", display_name="Me")
    assert repo.get("me@example.com").welcomed_at is None
    repo.mark_welcomed("me@example.com")
    first = repo.get("me@example.com").welcomed_at
    assert first is not None
    # idempotent — second call does not overwrite
    repo.mark_welcomed("me@example.com")
    assert repo.get("me@example.com").welcomed_at == first
