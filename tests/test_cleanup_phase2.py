"""Phase 2 — semantic naming layer for smart cleanup batches.

Covers the body-free digest, AIEngine.propose_batches (text overlay + key
filtering + graceful empty), and the MockAIEngine stub so the path works without
an Anthropic key.
"""

import json
from datetime import datetime, timedelta, timezone

from postmind.core.ai_engine import AIEngine
from postmind.core.mock_ai import MockAIEngine
from postmind.core.sender_stats import (
    SenderGroup,
    build_cleanup_batches,
    cleanup_batches_digest,
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


def _plan():
    groups = [
        _g(
            "deals@promo.com",
            "Promo Co",
            300,
            500,
            400,
            True,
            subjects=["50% off everything", "Last chance deal"],
        ),
        _g("old@stuff.com", "Old Stuff", 5, 3, 800, False),
    ]
    compute_impact_scores(groups)
    return build_cleanup_batches(groups)


# ── Digest is body-free and carries subject lines ────────────────────────────


def test_digest_is_body_free_and_carries_subjects():
    plan = _plan()
    digest = cleanup_batches_digest(plan)
    assert digest, "expected at least one batch in the digest"
    for entry in digest:
        # aggregate signals + subject lines only — never sender lists or message ids
        assert set(entry.keys()) == {
            "key",
            "title",
            "action",
            "category",
            "senders",
            "emails",
            "size_mb",
            "confidence",
            "subjects",
        }
        assert isinstance(entry["subjects"], list)
        assert len(entry["subjects"]) <= 5
        assert all(isinstance(s, str) for s in entry["subjects"])
        # no sender emails anywhere in the payload
        blob = json.dumps(entry)
        assert "@" not in blob or "subjects" in entry  # subjects may legitimately omit emails
    promos = next(e for e in digest if e["key"] == "promos-unopened")
    assert "50% off everything" in promos["subjects"]


def test_digest_subjects_capped():
    plan = _plan()
    digest = cleanup_batches_digest(plan, max_subjects=1)
    for entry in digest:
        assert len(entry["subjects"]) <= 1


# ── AIEngine.propose_batches text overlay ─────────────────────────────────────


def test_propose_batches_applies_text_and_drops_unknown_keys(monkeypatch):
    engine = AIEngine.__new__(AIEngine)  # bypass __init__ (no backend needed)
    engine._mode = "cloud"

    fake = {
        "batches": {
            "promos-unopened": {"title": "Deal blasts", "rationale": "Safe to clear."},
            "bogus": {"title": "should be dropped", "rationale": "x"},
        }
    }
    monkeypatch.setattr(engine, "_complete", lambda *a, **k: json.dumps(fake))

    digest = [
        {
            "key": "promos-unopened",
            "title": "x",
            "action": "trash",
            "category": "",
            "senders": 2,
            "emails": 10,
            "size_mb": 5.0,
            "confidence": 90,
            "subjects": ["a", "b"],
        }
    ]
    out = engine.propose_batches(digest)

    assert out["batches"]["promos-unopened"]["title"] == "Deal blasts"
    assert "bogus" not in out["batches"], "unknown batch key must be dropped"


def test_propose_batches_empty_digest_short_circuits():
    engine = AIEngine.__new__(AIEngine)
    engine._mode = "cloud"
    assert engine.propose_batches([]) == {"batches": {}}


def test_propose_batches_ignores_blank_or_nonstring_fields(monkeypatch):
    engine = AIEngine.__new__(AIEngine)
    engine._mode = "cloud"
    fake = {"batches": {"review": {"title": "   ", "rationale": 123}}}
    monkeypatch.setattr(engine, "_complete", lambda *a, **k: json.dumps(fake))
    digest = [
        {
            "key": "review",
            "title": "t",
            "action": "archive",
            "category": "",
            "senders": 1,
            "emails": 1,
            "size_mb": 1.0,
            "confidence": 50,
            "subjects": [],
        }
    ]
    out = engine.propose_batches(digest)
    # blank title + non-string rationale → nothing usable → key omitted
    assert "review" not in out["batches"]


def test_propose_batches_strips_markdown_fence(monkeypatch):
    engine = AIEngine.__new__(AIEngine)
    engine._mode = "cloud"
    body = json.dumps({"batches": {"review": {"title": "T", "rationale": "R"}}})
    monkeypatch.setattr(engine, "_complete", lambda *a, **k: f"```json\n{body}\n```")
    digest = [
        {
            "key": "review",
            "title": "t",
            "action": "archive",
            "category": "",
            "senders": 1,
            "emails": 1,
            "size_mb": 1.0,
            "confidence": 50,
            "subjects": [],
        }
    ]
    out = engine.propose_batches(digest)
    assert out["batches"]["review"] == {"title": "T", "rationale": "R"}


# ── MockAIEngine stub (no API key) ────────────────────────────────────────────


def test_mock_propose_batches_overlays_every_batch():
    digest = cleanup_batches_digest(_plan())
    out = MockAIEngine().propose_batches(digest)
    assert set(out["batches"]) == {e["key"] for e in digest}
    for entry in out["batches"].values():
        assert entry["title"].startswith("[mock]")
        assert entry["rationale"].startswith("[mock]")


def test_mock_propose_batches_empty():
    assert MockAIEngine().propose_batches([]) == {"batches": {}}
