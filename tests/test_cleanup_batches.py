"""Tests for the smart cleanup batcher (build_cleanup_batches)."""

from datetime import datetime, timedelta, timezone

from postmind.core.sender_stats import (
    AUTO_SELECT_THRESHOLD,
    CleanupBatch,
    SenderGroup,
    build_cleanup_batches,
    compute_impact_scores,
)


def _g(email, name, count, mb, age_days, unsub, subjects=None):
    now = datetime.now(timezone.utc)
    return SenderGroup(
        sender_email=email,
        sender_name=name,
        count=count,
        total_size_bytes=int(mb * 1024 * 1024),
        earliest_date=now - timedelta(days=age_days),
        latest_date=now,
        sample_subjects=subjects or ["Big sale inside", "Weekly digest"],
        message_ids=[f"{email}-{i}" for i in range(count)],
        has_unsubscribe=unsub,
    )


def _plan(groups, categories=None):
    compute_impact_scores(groups)
    return build_cleanup_batches(groups, categories=categories)


# ── Empty input ──────────────────────────────────────────────────────────────


def test_empty_groups_no_opportunity():
    plan = build_cleanup_batches([])
    assert plan.batches == []
    assert plan.has_opportunity is False
    assert plan.protected_note == ""
    assert plan.protected_count == 0
    assert plan.total_senders == 0
    assert plan.total_emails == 0
    assert plan.cleanable_emails == 0
    assert plan.cleanable_mb == 0.0


# ── Sensitive exclusion ────────────────────────────────────────────────────────


def test_sensitive_sender_never_in_a_batch():
    groups = [
        _g("deals@promo.com", "Promo Co", 300, 500, 400, True),
        _g("alerts@mybank.com", "My Bank", 200, 50, 400, True),  # sensitive
    ]
    plan = _plan(groups)
    all_targets: set[str] = set()
    for b in plan.batches:
        all_targets |= set(b.sender_emails)
    assert "alerts@mybank.com" not in all_targets
    assert plan.protected_count == 1
    assert "left 1 sender" in plan.protected_note.lower()
    assert "bank" in plan.protected_note.lower()
    # honest totals still count every scanned sender, batched or not
    assert plan.total_senders == 2
    assert plan.total_emails == 500


# ── Promos default action ──────────────────────────────────────────────────────


def test_promos_batch_defaults_to_trash():
    groups = [
        _g("deals@promo.com", "Promo Co", 300, 500, 400, True),
        _g("news@news.com", "Newsletter", 120, 80, 500, True),
    ]
    plan = _plan(groups)
    promos = next(b for b in plan.batches if b.key == "promos-unopened")
    assert promos.action == "trash"
    assert set(promos.sender_emails) == {"deals@promo.com", "news@news.com"}
    assert promos.count == 420
    assert promos.size_mb == 580.0


# ── Auto-select threshold boundary ──────────────────────────────────────────────


def test_auto_select_true_iff_confidence_at_or_above_threshold():
    assert AUTO_SELECT_THRESHOLD == 85
    below = CleanupBatch(
        key="x", title="t", rationale="r", action="trash", sender_emails=["a@b.com"],
        count=1, size_mb=1.0, confidence=84, category="", sample=[],
    )
    at = CleanupBatch(
        key="x", title="t", rationale="r", action="trash", sender_emails=["a@b.com"],
        count=1, size_mb=1.0, confidence=85, category="", sample=[],
    )
    above = CleanupBatch(
        key="x", title="t", rationale="r", action="trash", sender_emails=["a@b.com"],
        count=1, size_mb=1.0, confidence=86, category="", sample=[],
    )
    assert below.auto_select is False
    assert at.auto_select is True
    assert above.auto_select is True


def test_high_confidence_promos_batch_auto_selects():
    # Unsubscribe + old + very high count -> confidence pinned at 100, auto-selects.
    groups = [_g("deals@promo.com", "Promo Co", 300, 500, 400, True)]
    plan = _plan(groups)
    promos = next(b for b in plan.batches if b.key == "promos-unopened")
    assert promos.confidence >= AUTO_SELECT_THRESHOLD
    assert promos.auto_select is True


# ── Category overlay ───────────────────────────────────────────────────────────


def test_category_overlay_changes_title():
    groups = [_g("deals@promo.com", "Promo Co", 300, 500, 400, True)]
    # without categories -> generic default title
    plain = _plan([_g("deals@promo.com", "Promo Co", 300, 500, 400, True)])
    plain_promos = next(b for b in plain.batches if b.key == "promos-unopened")

    cats = {mid: {"category": "receipt"} for mid in groups[0].message_ids}
    overlaid = _plan(groups, categories=cats)
    promos = next(b for b in overlaid.batches if b.key == "promos-unopened")

    assert promos.category == "receipt"
    assert promos.title == "Old receipts & order confirmations"
    assert promos.title != plain_promos.title


# ── Disjoint batches ───────────────────────────────────────────────────────────


def test_each_sender_appears_in_at_most_one_batch():
    groups = [
        _g("deals@promo.com", "Promo Co", 300, 500, 400, True),   # promos
        _g("old@stuff.com", "Old Stuff", 5, 3, 800, False),       # old, low-conf
        _g("notify@app.com", "App", 250, 2, 50, False),           # flood, recent
        _g("misc@thing.com", "Thing", 30, 5, 200, True),          # review
    ]
    plan = _plan(groups)
    seen: list[str] = []
    for b in plan.batches:
        seen.extend(b.sender_emails)
    assert len(seen) == len(set(seen)), "a sender appeared in more than one batch"


def test_batches_ordered_by_confidence_then_size():
    groups = [
        _g("deals@promo.com", "Promo Co", 300, 500, 400, True),
        _g("old@stuff.com", "Old Stuff", 5, 3, 800, False),
    ]
    plan = _plan(groups)
    confs = [b.confidence for b in plan.batches]
    assert confs == sorted(confs, reverse=True)


def test_sample_is_body_free_and_capped():
    groups = [
        _g("deals@promo.com", "Promo Co", 300, 500, 400, True,
           subjects=["a", "b", "c", "d"]),
        _g("news@news.com", "Newsletter", 120, 80, 500, True,
           subjects=["e", "f", "g"]),
    ]
    plan = _plan(groups)
    promos = next(b for b in plan.batches if b.key == "promos-unopened")
    assert len(promos.sample) <= 5
    for item in promos.sample:
        assert set(item.keys()) == {"sender", "subject"}
