# Newsletter & Promotions Digest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the Daily Brief with Newsletter and Promotions tabs that batch-summarize subscription and vendor emails from the last 24h, then auto-trash them 48h after the digest is generated; users can permanently exempt any sender via a per-card Keep toggle.

**Architecture:** DailyBriefGenerator gets two new private methods (_generate_newsletter_digest, _generate_promo_digest) that run after existing stats gathering and append two new JSON columns (newsletters_json, promotions_json) plus a digest_trash_after timestamp to the DailyBrief row. The heartbeat daemon checks digest_trash_after each cycle and executes trash when expired. A new DigestExemption table (account_email, sender_email) persists Keep decisions; exemptions are re-checked at trash time, not just at generation time.

**Tech Stack:** SQLAlchemy 2.0 (SQLite), FastAPI, HTMX, Jinja2, Tailwind, existing AIEngine/MockAIEngine pattern

---

## File Map

| File | Change |
|------|--------|
| `postmind/core/storage.py` | Add `DigestExemption` model, 3 new columns to `DailyBrief`, `DigestExemptionRepo`, update `_run_migrations`, update `DailyBriefRepo.save` |
| `postmind/core/ai_engine.py` | Add `summarize_newsletter_sender()` and `extract_promo_offer_line()` |
| `postmind/core/mock_ai.py` | Add mock versions of both new AI methods |
| `postmind/core/daily_brief.py` | Add `_generate_newsletter_digest()` and `_generate_promo_digest()`; update `get_or_generate()` to call them |
| `postmind/core/daemon.py` | Add `_run_digest_trash()` call in `_triage_account()` |
| `postmind/web/server.py` | Add `POST /digest/exempt` and `DELETE /digest/exempt`; add `_render_digest_tabs()`; update `brief_page()` and `brief_generate()` to pass digest context; add Newsletters/Promotions to tab rendering |
| `postmind/web/templates/daily_brief.html` | Add 4-tab layout (inbox, newsletters, promotions, deals→folds in), Keep toggle, countdown badge, Undo All banner |
| `tests/test_newsletter_digest.py` | New test file covering digest generation, exemptions, trash execution |

---

## Task 1: Storage — DigestExemption model + new DailyBrief columns

**Files:**
- Modify: `postmind/core/storage.py`

- [ ] **Step 1: Write failing test to verify DigestExemption model exists**

```python
# tests/test_newsletter_digest.py  (create new file)
"""Newsletter & Promotions Digest — storage, generation, exemption, trash tests."""
import json
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def _use_clean_db(clean_db):
    pass


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
        newsletters_json=json.dumps([{"sender": "Sub", "sender_email": "s@x.com", "email_ids": ["id1"], "summary_bullets": ["a", "b", "c"], "exempted": False}]),
        promotions_json=json.dumps([]),
        digest_trash_after=datetime.now(timezone.utc) + timedelta(hours=48),
    )
    session.add(brief)
    session.commit()
    fetched = session.query(DailyBrief).filter_by(account_email="me@example.com").first()
    assert fetched.newsletters_json is not None
    assert fetched.digest_trash_after is not None
```

- [ ] **Step 2: Run test — expect failure**

```bash
cd /Users/tashfeenekram/postmind && .venv/bin/python -m pytest tests/test_newsletter_digest.py::test_digest_exemption_model_exists tests/test_newsletter_digest.py::test_daily_brief_has_digest_columns -v 2>&1 | tail -20
```

Expected: `AttributeError` or `OperationalError` — columns/model don't exist yet.

- [ ] **Step 3: Add DigestExemption model and new DailyBrief columns to storage.py**

In `postmind/core/storage.py`, after the `DailyBrief` class (line ~951), add:

```python
class DigestExemption(Base):
    """Senders permanently exempted from digest auto-trash."""

    __tablename__ = "digest_exemptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_email = Column(String, nullable=False)
    sender_email = Column(String, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
```

In the `DailyBrief` class, add three columns after `deals_json`:

```python
    newsletters_json = Column(Text, nullable=True)   # JSON list of newsletter digest items
    promotions_json = Column(Text, nullable=True)    # JSON list of promo digest items
    digest_trash_after = Column(DateTime, nullable=True)  # UTC: trash non-exempted items after this
```

In `_run_migrations()`, add to `new_columns` dict:

```python
        "daily_briefs": [
            ("items_json", "TEXT"),
            ("deals_json", "TEXT"),
            ("newsletters_json", "TEXT"),
            ("promotions_json", "TEXT"),
            ("digest_trash_after", "DATETIME"),
        ],
```

- [ ] **Step 4: Update DailyBriefRepo.save() to persist new columns**

In `DailyBriefRepo.save()`, update the fields tuple:

```python
    def save(self, brief: DailyBrief) -> DailyBrief:
        existing = self.get_today(brief.account_email, brief.brief_date)
        if existing:
            for col in (
                "content",
                "ai_used",
                "unread_count",
                "new_since_yesterday",
                "high_priority_count",
                "overdue_followups_count",
                "avoided_count",
                "items_json",
                "deals_json",
                "newsletters_json",
                "promotions_json",
                "digest_trash_after",
            ):
                setattr(existing, col, getattr(brief, col))
            existing.generated_at = datetime.now(timezone.utc)
            self.s.commit()
            return existing
        self.s.add(brief)
        self.s.commit()
        return brief
```

- [ ] **Step 5: Add DigestExemptionRepo**

After `DailyBriefRepo`, add:

```python
class DigestExemptionRepo:
    def __init__(self, session: Session):
        self.s = session

    def add(self, account_email: str, sender_email: str) -> None:
        existing = self.s.query(DigestExemption).filter_by(
            account_email=account_email, sender_email=sender_email.lower()
        ).first()
        if not existing:
            self.s.add(DigestExemption(
                account_email=account_email,
                sender_email=sender_email.lower(),
            ))
            self.s.commit()

    def remove(self, account_email: str, sender_email: str) -> None:
        self.s.query(DigestExemption).filter_by(
            account_email=account_email, sender_email=sender_email.lower()
        ).delete()
        self.s.commit()

    def get_set(self, account_email: str) -> set[str]:
        rows = self.s.query(DigestExemption.sender_email).filter_by(
            account_email=account_email
        ).all()
        return {r.sender_email for r in rows}

    def is_exempted(self, account_email: str, sender_email: str) -> bool:
        return sender_email.lower() in self.get_set(account_email)
```

- [ ] **Step 6: Run tests — expect pass**

```bash
cd /Users/tashfeenekram/postmind && .venv/bin/python -m pytest tests/test_newsletter_digest.py::test_digest_exemption_model_exists tests/test_newsletter_digest.py::test_daily_brief_has_digest_columns -v 2>&1 | tail -20
```

Expected: both PASS.

- [ ] **Step 7: Commit**

```bash
git add postmind/core/storage.py tests/test_newsletter_digest.py && git commit -m "feat(storage): DigestExemption model + newsletter/promo digest columns on DailyBrief"
```

---

## Task 2: AI methods — newsletter summarization and promo offer extraction

**Files:**
- Modify: `postmind/core/ai_engine.py`
- Modify: `postmind/core/mock_ai.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_newsletter_digest.py`:

```python
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
```

- [ ] **Step 2: Run tests — expect failure**

```bash
cd /Users/tashfeenekram/postmind && .venv/bin/python -m pytest tests/test_newsletter_digest.py::test_mock_summarize_newsletter_sender tests/test_newsletter_digest.py::test_mock_extract_promo_offer_line -v 2>&1 | tail -10
```

Expected: `AttributeError: 'MockAIEngine' object has no attribute 'summarize_newsletter_sender'`

- [ ] **Step 3: Add methods to ai_engine.py**

In `postmind/core/ai_engine.py`, after `generate_daily_brief()`, add:

```python
    def summarize_newsletter_sender(
        self,
        sender: str,
        emails: list[dict],
    ) -> list[str]:
        """Return exactly 3 bullet-point strings summarising a sender's last-24h emails.

        Each email dict has 'subject' and 'snippet'. Privacy-first: no full bodies.
        """
        email_lines = "\n".join(
            f'- Subject: {e.get("subject", "")} | Snippet: {e.get("snippet", "")[:200]}'
            for e in emails[:5]
        )
        prompt = f"""\
Summarise the following newsletter emails from "{sender}" in exactly 3 short bullet points.
Each bullet should be one sentence capturing a key topic, story, or insight.
Output ONLY a JSON array of 3 strings. No preamble, no markdown, just the array.

Emails:
{email_lines}

Output format: ["bullet 1", "bullet 2", "bullet 3"]
"""
        raw = self._complete(SYSTEM_PROMPT, prompt, max_tokens=200)
        import re as _re
        m = _re.search(r'\[.*?\]', raw, _re.DOTALL)
        if m:
            try:
                bullets = json.loads(m.group(0))
                if isinstance(bullets, list) and len(bullets) >= 3:
                    return [str(b) for b in bullets[:3]]
            except Exception:
                pass
        lines = [l.lstrip("•-123456789. ").strip() for l in raw.splitlines() if l.strip()]
        lines = [l for l in lines if l]
        while len(lines) < 3:
            lines.append("(no summary)")
        return lines[:3]

    def extract_promo_offer_line(
        self,
        sender: str,
        subject: str,
        snippet: str,
    ) -> str:
        """Extract a single offer line (≤ 12 words) from a promotional email.

        Returns a terse human-readable summary of the deal/offer.
        """
        prompt = f"""\
Extract the core offer or promotion from this vendor email in 12 words or fewer.
Be specific: include discount %, deadline, or product name if present.
Output ONLY the offer line. No preamble, no punctuation at end unless a date.

From: {sender}
Subject: {subject}
Snippet: {snippet[:300]}
"""
        raw = self._complete(SYSTEM_PROMPT, prompt, max_tokens=60)
        return raw.strip().strip('"').strip("'")
```

- [ ] **Step 4: Add mock implementations to mock_ai.py**

In `postmind/core/mock_ai.py`, after `generate_daily_brief()`, add:

```python
    def summarize_newsletter_sender(
        self,
        sender: str,
        emails: list[dict],
    ) -> list[str]:
        subjects = [e.get("subject", "")[:50] for e in emails[:3]]
        return [
            f"[mock] {sender} covered: {subjects[0] if subjects else 'recent topics'}",
            "[mock] Key industry updates and analysis included",
            "[mock] Actionable insights for this week",
        ]

    def extract_promo_offer_line(
        self,
        sender: str,
        subject: str,
        snippet: str,
    ) -> str:
        subject_lower = subject.lower()
        if "%" in subject:
            import re as _re
            m = _re.search(r'\d+%', subject)
            if m:
                return f"[mock] {m.group(0)} off — {sender}"
        return f"[mock] Special offer from {sender}"
```

- [ ] **Step 5: Run tests — expect pass**

```bash
cd /Users/tashfeenekram/postmind && .venv/bin/python -m pytest tests/test_newsletter_digest.py::test_mock_summarize_newsletter_sender tests/test_newsletter_digest.py::test_mock_extract_promo_offer_line -v 2>&1 | tail -10
```

Expected: both PASS.

- [ ] **Step 6: Commit**

```bash
git add postmind/core/ai_engine.py postmind/core/mock_ai.py tests/test_newsletter_digest.py && git commit -m "feat(ai): summarize_newsletter_sender + extract_promo_offer_line methods"
```

---

## Task 3: Daily Brief — digest generation

**Files:**
- Modify: `postmind/core/daily_brief.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_newsletter_digest.py`:

```python
@pytest.fixture(autouse=True)
def _mock_ai(monkeypatch):
    import postmind.core.daily_brief as db_mod
    from postmind.core.mock_ai import MockAIEngine

    class _S:
        ai_mode = "cloud"
        ai_max_classify_batch = 10
        ai_classify_parallelism = 1
        avoidance_view_threshold = 3

    monkeypatch.setattr(db_mod, "get_settings", lambda: _S())

    import postmind.core.ai_engine as ae_mod
    monkeypatch.setattr(ae_mod, "AIEngine", MockAIEngine)


def _add_email_record(session, gmail_id, *, list_unsubscribe="", ai_category="", sender_email=None, snippet="test snippet", internal_date=None):
    from postmind.core.storage import EmailRecord
    import time
    now_ms = int(time.time() * 1000)
    rec = EmailRecord(
        account_email="me@example.com",
        gmail_id=gmail_id,
        thread_id=f"t-{gmail_id}",
        subject=f"Subject for {gmail_id}",
        sender_email=sender_email or f"{gmail_id}@sender.com",
        sender_name=f"Sender {gmail_id}",
        snippet=snippet,
        label_ids_json='["INBOX","UNREAD"]',
        internal_date=internal_date or now_ms,
        is_unread=True,
        is_inbox=True,
        list_unsubscribe=list_unsubscribe,
        ai_category=ai_category,
    )
    from postmind.core.storage import EmailRepo
    EmailRepo(session).upsert(rec)


def test_newsletter_digest_generated_from_list_unsubscribe(clean_db):
    from postmind.core.storage import get_session
    from postmind.core.daily_brief import DailyBriefGenerator

    session = get_session()
    _add_email_record(session, "nl1", list_unsubscribe="<mailto:unsub@news.com>", sender_email="news@newsletter.com")
    _add_email_record(session, "nl2", list_unsubscribe="<https://unsub.example.com>", sender_email="digest@weekly.com")
    _add_email_record(session, "nopromo", list_unsubscribe="", sender_email="person@company.com")

    brief = DailyBriefGenerator("me@example.com").get_or_generate(force=True)

    assert brief.newsletters_json is not None
    newsletters = json.loads(brief.newsletters_json)
    newsletter_senders = {n["sender_email"] for n in newsletters}
    assert "news@newsletter.com" in newsletter_senders
    assert "digest@weekly.com" in newsletter_senders
    assert "person@company.com" not in newsletter_senders


def test_newsletter_digest_excludes_exempted_senders(clean_db):
    from postmind.core.storage import DigestExemptionRepo, get_session
    from postmind.core.daily_brief import DailyBriefGenerator

    session = get_session()
    _add_email_record(session, "nl1", list_unsubscribe="<unsub@x.com>", sender_email="news@x.com")
    DigestExemptionRepo(session).add("me@example.com", "news@x.com")

    brief = DailyBriefGenerator("me@example.com").get_or_generate(force=True)

    newsletters = json.loads(brief.newsletters_json or "[]")
    assert not any(n["sender_email"] == "news@x.com" for n in newsletters)


def test_digest_trash_after_set_when_items_exist(clean_db):
    from postmind.core.storage import get_session
    from postmind.core.daily_brief import DailyBriefGenerator

    session = get_session()
    _add_email_record(session, "nl1", list_unsubscribe="<unsub@x.com>", sender_email="news@x.com")

    brief = DailyBriefGenerator("me@example.com").get_or_generate(force=True)

    assert brief.digest_trash_after is not None
    delta = brief.digest_trash_after - datetime.now(timezone.utc)
    assert timedelta(hours=47) < delta < timedelta(hours=49)


def test_digest_trash_after_not_set_when_no_items(clean_db):
    from postmind.core.daily_brief import DailyBriefGenerator

    # No newsletter/promo emails at all
    brief = DailyBriefGenerator("me@example.com").get_or_generate(force=True)
    assert brief.digest_trash_after is None
```

- [ ] **Step 2: Run tests — expect failure**

```bash
cd /Users/tashfeenekram/postmind && .venv/bin/python -m pytest tests/test_newsletter_digest.py::test_newsletter_digest_generated_from_list_unsubscribe tests/test_newsletter_digest.py::test_digest_trash_after_set_when_items_exist -v 2>&1 | tail -15
```

Expected: `AssertionError` — `brief.newsletters_json` is `None`.

- [ ] **Step 3: Add digest generation methods to daily_brief.py**

Add imports at top of `postmind/core/daily_brief.py` (after existing imports):

```python
import json
```

Add two new private methods to `DailyBriefGenerator`, before `_generate_content`:

```python
    def _generate_newsletter_digest(
        self,
        session,
        ai,
        account_email: str,
        last24h_records: list,
        exempted_senders: set,
    ) -> list[dict]:
        """Build newsletter digest items from emails with list_unsubscribe header."""
        from collections import defaultdict

        grouped: dict[str, list] = defaultdict(list)
        for r in last24h_records:
            if r.list_unsubscribe and r.list_unsubscribe.strip():
                key = (r.sender_email or "").lower()
                if key and key not in exempted_senders:
                    grouped[key].append(r)

        items = []
        for sender_email, records in grouped.items():
            records.sort(key=lambda r: r.internal_date or 0, reverse=True)
            sender_name = records[0].sender_name or records[0].sender_email or sender_email
            emails_info = [
                {"subject": r.subject or "", "snippet": r.snippet or ""}
                for r in records[:5]
            ]
            try:
                bullets = ai.summarize_newsletter_sender(sender_name, emails_info)
            except Exception:
                bullets = ["(Summary unavailable)"] * 3
            items.append({
                "sender": sender_name,
                "sender_email": sender_email,
                "email_ids": [r.gmail_id for r in records],
                "summary_bullets": bullets[:3],
                "exempted": False,
            })

        items.sort(key=lambda x: x["sender"].lower())
        return items

    def _generate_promo_digest(
        self,
        session,
        ai,
        account_email: str,
        deals_items: list[dict],
        exempted_senders: set,
        newsletter_senders: set,
    ) -> list[dict]:
        """Build promo digest items from deal-scored emails (excluding newsletters)."""
        from collections import defaultdict
        from postmind.core.storage import EmailRepo

        email_repo = EmailRepo(session)
        grouped: dict[str, list] = defaultdict(list)
        for item in deals_items:
            sender_email = (item.get("sender_email") or "").lower()
            if not sender_email:
                continue
            if sender_email in exempted_senders or sender_email in newsletter_senders:
                continue
            grouped[sender_email].append(item)

        items = []
        for sender_email, group_items in grouped.items():
            group_items.sort(key=lambda x: x.get("deal_score", 0), reverse=True)
            top = group_items[0]
            sender_name = top.get("sender") or sender_email
            gmail_id = top.get("gmail_id", "")
            record = email_repo.get(gmail_id) if gmail_id else None
            snippet = (record.snippet or "") if record else ""
            try:
                offer_line = ai.extract_promo_offer_line(
                    sender=sender_name,
                    subject=top.get("subject", ""),
                    snippet=snippet,
                )
            except Exception:
                offer_line = top.get("subject", "")[:80]
            items.append({
                "sender": sender_name,
                "sender_email": sender_email,
                "email_ids": [i.get("gmail_id") for i in group_items if i.get("gmail_id")],
                "offer_line": offer_line,
                "deal_score": top.get("deal_score", 1),
                "exempted": False,
            })

        items.sort(key=lambda x: x.get("deal_score", 0), reverse=True)
        return items
```

- [ ] **Step 4: Update get_or_generate() to call the new methods**

In `get_or_generate()`, replace the section that builds `brief = DailyBrief(...)` with digest-aware version. Add before the `brief = DailyBrief(...)` line:

```python
        import json
        deals = stats.get("deals_items", [])
        deals_json = json.dumps(deals[:50]) if deals else None
        identified = stats["high_priority_items"] + stats.get("recent_unclassified", [])
        items_json = json.dumps(identified[:50]) if identified else None

        # ── Newsletter & Promotions digest ─────────────────────────────────
        newsletters_json = None
        promotions_json = None
        digest_trash_after = None

        if settings.ai_mode in ("cloud", "local"):
            try:
                from postmind.core.ai_engine import AIEngine
                from postmind.core.storage import DigestExemptionRepo, EmailRepo

                ai_for_digest = AIEngine()
                now_ms = int((datetime.now(timezone.utc) - timedelta(hours=24)).timestamp() * 1000)
                all_records = EmailRepo(session).get_inbox(self.account_email, limit=500)
                last24h = [r for r in all_records if (r.internal_date or 0) >= now_ms]
                exempted = DigestExemptionRepo(session).get_set(self.account_email)

                nl_items = self._generate_newsletter_digest(
                    session, ai_for_digest, self.account_email, last24h, exempted
                )
                newsletter_senders = {item["sender_email"] for item in nl_items}
                pr_items = self._generate_promo_digest(
                    session, ai_for_digest, self.account_email,
                    deals, exempted, newsletter_senders
                )

                if nl_items:
                    newsletters_json = json.dumps(nl_items)
                if pr_items:
                    promotions_json = json.dumps(pr_items)
                if nl_items or pr_items:
                    digest_trash_after = datetime.now(timezone.utc) + timedelta(hours=48)
            except Exception as exc:
                logger.warning("Digest generation failed: %s", exc)
```

And update the `brief = DailyBrief(...)` constructor to include the new fields:

```python
        brief = DailyBrief(
            account_email=self.account_email,
            brief_date=today_str,
            content=content,
            ai_used=ai_used,
            unread_count=stats["unread_count"],
            new_since_yesterday=stats["new_since_yesterday"],
            high_priority_count=len(stats["high_priority_items"]),
            overdue_followups_count=len(stats["overdue_follow_ups"]),
            avoided_count=stats["avoided_count"],
            items_json=items_json,
            deals_json=deals_json,
            newsletters_json=newsletters_json,
            promotions_json=promotions_json,
            digest_trash_after=digest_trash_after,
        )
```

Also add the `settings` variable before the digest block — it's already computed in `_generate_content` but needs to be available here. Move `settings = get_settings()` to be called once before the `stats, content, ai_used` gathering. In `get_or_generate()` add `settings = get_settings()` before the `stats = self._gather_stats()` call and use it in `_generate_content()` by passing it through or keeping the existing call there.

Actually the cleanest way: in `get_or_generate()`, after `stats = self._gather_stats()` add:
```python
        from postmind.config import get_settings
        settings = get_settings()
```

And update `_generate_content()` to accept an optional settings arg, or just call `get_settings()` internally as it already does.

- [ ] **Step 5: Run tests — expect pass**

```bash
cd /Users/tashfeenekram/postmind && .venv/bin/python -m pytest tests/test_newsletter_digest.py -k "newsletter_digest or trash_after or exempted" -v 2>&1 | tail -20
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add postmind/core/daily_brief.py tests/test_newsletter_digest.py && git commit -m "feat(brief): newsletter and promotions digest generation with 48h auto-trash timestamp"
```

---

## Task 4: Daemon — auto-trash execution

**Files:**
- Modify: `postmind/core/daemon.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_newsletter_digest.py`:

```python
def test_digest_trash_execution(clean_db, monkeypatch):
    """When digest_trash_after is in the past, daemon trashes non-exempted email IDs."""
    import json
    from datetime import datetime, timedelta, timezone
    from postmind.core.storage import DailyBrief, DailyBriefRepo, UndoLogRepo, get_session

    session = get_session()
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    brief = DailyBrief(
        account_email="me@example.com",
        brief_date=datetime.now(timezone.utc).date().isoformat(),
        content="test",
        newsletters_json=json.dumps([
            {"sender": "News", "sender_email": "news@x.com",
             "email_ids": ["id1", "id2"], "summary_bullets": ["a","b","c"], "exempted": False},
            {"sender": "Safe", "sender_email": "safe@x.com",
             "email_ids": ["id3"], "summary_bullets": ["a","b","c"], "exempted": True},
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

    from postmind.core import daemon
    daemon._run_digest_trash(session, FakeProvider(), "me@example.com")

    assert set(trashed) == {"id1", "id2"}  # id3 is exempted
    # digest_trash_after should be cleared
    refreshed = session.query(DailyBrief).filter_by(account_email="me@example.com").first()
    assert refreshed.digest_trash_after is None
    # Undo log should exist
    logs = UndoLogRepo(session).list_recent("me@example.com")
    assert any("digest_trash" in l.operation for l in logs)
```

- [ ] **Step 2: Run test — expect failure**

```bash
cd /Users/tashfeenekram/postmind && .venv/bin/python -m pytest tests/test_newsletter_digest.py::test_digest_trash_execution -v 2>&1 | tail -10
```

Expected: `AttributeError: module 'postmind.core.daemon' has no attribute '_run_digest_trash'`

- [ ] **Step 3: Add _run_digest_trash to daemon.py**

In `postmind/core/daemon.py`, add this function before `_triage_account`:

```python
def _run_digest_trash(session, provider, account_email: str) -> None:
    """Trash non-exempted digest emails if digest_trash_after has elapsed."""
    from datetime import datetime, timezone
    import json

    from postmind.core.storage import DailyBrief, DigestExemptionRepo, UndoLogRepo

    today_str = datetime.now(timezone.utc).date().isoformat()
    brief = session.query(DailyBrief).filter_by(
        account_email=account_email, brief_date=today_str
    ).first()
    if not brief:
        return
    if not brief.digest_trash_after:
        return

    trash_after = brief.digest_trash_after
    if trash_after.tzinfo is None:
        trash_after = trash_after.replace(tzinfo=timezone.utc)
    if trash_after > datetime.now(timezone.utc):
        return

    # Re-read exemptions at execution time (user may have toggled Keep after generation)
    exempted = DigestExemptionRepo(session).get_set(account_email)

    email_ids: list[str] = []
    for col in ("newsletters_json", "promotions_json"):
        raw = getattr(brief, col, None)
        if not raw:
            continue
        try:
            items = json.loads(raw)
            for item in items:
                if not item.get("exempted", False) and item.get("sender_email", "").lower() not in exempted:
                    email_ids.extend(item.get("email_ids", []))
        except Exception:
            pass

    email_ids = list(set(filter(None, email_ids)))
    if email_ids:
        # Batch trash in groups of 50
        for i in range(0, len(email_ids), 50):
            batch = email_ids[i:i+50]
            try:
                provider.trash_messages(batch)
            except Exception as exc:
                logger.warning("Digest trash batch failed: %s", exc)
        UndoLogRepo(session).record(
            account_email=account_email,
            operation="digest_trash",
            message_ids=email_ids,
            description=f"Auto-trash from digest: {len(email_ids)} emails",
        )

    brief.digest_trash_after = None
    session.commit()
```

- [ ] **Step 4: Wire _run_digest_trash into _triage_account**

In `_triage_account()`, after the `if run_daily_brief:` block, add:

```python
        # Auto-trash digest emails if 48h window has elapsed
        try:
            from postmind.core.storage import get_session
            from postmind.core.daemon import _run_digest_trash
            _run_digest_trash(get_session(), provider, email)
        except Exception as exc:
            logger.warning("Heartbeat %s: digest trash check failed: %s", email, exc)
```

Note: `_run_digest_trash` is in the same module so import it locally to avoid circular imports, or just call it directly as `_run_digest_trash(...)`.

Actually, since it's in the same file, just call it as `_run_digest_trash(get_session(), provider, email)` without the import.

- [ ] **Step 5: Run test — expect pass**

```bash
cd /Users/tashfeenekram/postmind && .venv/bin/python -m pytest tests/test_newsletter_digest.py::test_digest_trash_execution -v 2>&1 | tail -10
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add postmind/core/daemon.py tests/test_newsletter_digest.py && git commit -m "feat(daemon): auto-trash digest emails when 48h window expires"
```

---

## Task 5: API — /digest/exempt endpoints

**Files:**
- Modify: `postmind/web/server.py`

- [ ] **Step 1: Add POST and DELETE /digest/exempt routes to server.py**

Find the section around `/brief/action` route (~line 3791) and add after it:

```python
@app.post("/digest/exempt")
async def digest_exempt_add(request: Request):
    """Exempt a sender from digest auto-trash. Persists to DigestExemption table
    and updates today's brief JSON so the 48h trash won't include them."""
    from postmind.core.storage import DailyBrief, DigestExemptionRepo, get_session
    import json as _json

    account_email = _get_web_account() or ""
    if not account_email:
        return {"ok": False, "error": "no account"}

    body = await request.json()
    sender_email = (body.get("sender_email") or "").lower().strip()
    if not sender_email:
        return {"ok": False, "error": "missing sender_email"}

    session = get_session()
    DigestExemptionRepo(session).add(account_email, sender_email)

    # Also flip exempted=True in today's brief JSON so the trash countdown stops
    today_str = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date().isoformat()
    brief = session.query(DailyBrief).filter_by(account_email=account_email, brief_date=today_str).first()
    if brief:
        for col in ("newsletters_json", "promotions_json"):
            raw = getattr(brief, col, None)
            if not raw:
                continue
            try:
                items = _json.loads(raw)
                changed = False
                for item in items:
                    if item.get("sender_email", "").lower() == sender_email:
                        item["exempted"] = True
                        changed = True
                if changed:
                    setattr(brief, col, _json.dumps(items))
            except Exception:
                pass
        session.commit()

    return {"ok": True}


@app.delete("/digest/exempt")
async def digest_exempt_remove(request: Request):
    """Remove a sender exemption — they'll be included in future digest cleanups."""
    from postmind.core.storage import DailyBrief, DigestExemptionRepo, get_session
    import json as _json

    account_email = _get_web_account() or ""
    if not account_email:
        return {"ok": False, "error": "no account"}

    body = await request.json()
    sender_email = (body.get("sender_email") or "").lower().strip()
    if not sender_email:
        return {"ok": False, "error": "missing sender_email"}

    session = get_session()
    DigestExemptionRepo(session).remove(account_email, sender_email)

    today_str = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date().isoformat()
    brief = session.query(DailyBrief).filter_by(account_email=account_email, brief_date=today_str).first()
    if brief:
        for col in ("newsletters_json", "promotions_json"):
            raw = getattr(brief, col, None)
            if not raw:
                continue
            try:
                items = _json.loads(raw)
                changed = False
                for item in items:
                    if item.get("sender_email", "").lower() == sender_email:
                        item["exempted"] = False
                        changed = True
                if changed:
                    setattr(brief, col, _json.dumps(items))
            except Exception:
                pass
        session.commit()

    return {"ok": True}


@app.post("/digest/undo-all")
async def digest_undo_all(request: Request):
    """Exempt ALL senders in today's digest — effectively cancels auto-trash for today."""
    from postmind.core.storage import DailyBrief, DigestExemptionRepo, get_session
    import json as _json

    account_email = _get_web_account() or ""
    if not account_email:
        return {"ok": False, "error": "no account"}

    session = get_session()
    today_str = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date().isoformat()
    brief = session.query(DailyBrief).filter_by(account_email=account_email, brief_date=today_str).first()
    if not brief:
        return {"ok": False, "error": "no brief"}

    repo = DigestExemptionRepo(session)
    for col in ("newsletters_json", "promotions_json"):
        raw = getattr(brief, col, None)
        if not raw:
            continue
        try:
            items = _json.loads(raw)
            for item in items:
                item["exempted"] = True
                repo.add(account_email, item.get("sender_email", ""))
            setattr(brief, col, _json.dumps(items))
        except Exception:
            pass
    brief.digest_trash_after = None
    session.commit()
    return {"ok": True}
```

- [ ] **Step 2: Run existing tests to ensure no regressions**

```bash
cd /Users/tashfeenekram/postmind && .venv/bin/python -m pytest tests/ -q --tb=short 2>&1 | tail -20
```

Expected: all passing (or same pass/fail state as before this change).

- [ ] **Step 3: Commit**

```bash
git add postmind/web/server.py && git commit -m "feat(api): /digest/exempt POST/DELETE and /digest/undo-all endpoints"
```

---

## Task 6: Server — render digest tabs

**Files:**
- Modify: `postmind/web/server.py`

- [ ] **Step 1: Add _render_digest_tabs() function to server.py**

Find `_render_brief_links()` in `server.py` (~line 664) and add this new function after it:

```python
def _render_digest_tabs(brief, account_email: str) -> str:
    """Render the Newsletters and Promotions tab content HTML.

    Returns (newsletters_html, promotions_html) as a tuple.
    """
    import html as _html
    import json as _json
    from datetime import datetime, timezone

    if not brief:
        return "", ""

    trash_after_iso = ""
    if brief.digest_trash_after:
        ta = brief.digest_trash_after
        if ta.tzinfo is None:
            ta = ta.replace(tzinfo=timezone.utc)
        trash_after_iso = ta.isoformat()

    _icon_keep = (
        '<svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.75">'
        '<path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/></svg>'
    )
    _icon_trash = (
        '<svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.75">'
        '<path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21'
        'H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>'
    )

    def _countdown_badge(exempted: bool) -> str:
        if exempted or not trash_after_iso:
            return '<span class="pm-badge text-success border-success-border bg-success-subtle text-[10px]">Kept</span>'
        return (
            f'<span class="digest-countdown pm-badge text-ink-tertiary text-[10px]" '
            f'data-trash-after="{_html.escape(trash_after_iso)}">…</span>'
        )

    def _keep_toggle(sender_email: str, exempted: bool, tab: str) -> str:
        se = _html.escape(sender_email)
        if exempted:
            return (
                f'<button class="digest-keep-btn p-1 rounded text-success hover:bg-success-subtle transition-colors" '
                f'data-sender="{se}" data-exempted="1" data-tab="{tab}" '
                f'title="Click to un-keep (will be trashed next cycle)">'
                f'{_icon_keep}</button>'
            )
        return (
            f'<button class="digest-keep-btn p-1 rounded text-ink-tertiary hover:text-success hover:bg-success-subtle transition-colors" '
            f'data-sender="{se}" data-exempted="0" data-tab="{tab}" '
            f'title="Keep — never auto-trash this sender">'
            f'{_icon_keep}</button>'
        )

    # ── Newsletters tab ───────────────────────────────────────────────────────
    nl_items: list[dict] = []
    if brief.newsletters_json:
        try:
            nl_items = _json.loads(brief.newsletters_json)
        except Exception:
            pass

    if not nl_items:
        nl_html = (
            '<div class="text-center py-10 text-ink-tertiary">'
            '<p class="text-sm">No newsletters in the last 24 hours.</p>'
            '</div>'
        )
    else:
        cards = []
        for item in nl_items:
            se = (item.get("sender_email") or "").lower()
            sn = _html.escape(item.get("sender") or se)
            exempted = item.get("exempted", False)
            count = len(item.get("email_ids", []))
            bullets = item.get("summary_bullets") or []
            bullet_html = "".join(
                f'<li class="text-ink-subtle text-xs leading-relaxed">{_html.escape(str(b))}</li>'
                for b in bullets[:3]
            )
            cards.append(
                f'<div class="px-5 py-4 border-b border-hairline last:border-0" data-sender="{_html.escape(se)}">'
                f'<div class="flex items-start justify-between gap-3">'
                f'  <div class="min-w-0">'
                f'    <p class="text-sm font-semibold text-ink">{sn}</p>'
                f'    <p class="text-xs text-ink-tertiary">{count} email{"s" if count != 1 else ""}</p>'
                f'  </div>'
                f'  <div class="flex items-center gap-1.5 shrink-0">'
                f'    {_countdown_badge(exempted)}'
                f'    {_keep_toggle(se, exempted, "newsletters")}'
                f'  </div>'
                f'</div>'
                f'<ul class="list-disc pl-4 mt-2 space-y-1">{bullet_html}</ul>'
                f'</div>'
            )
        nl_html = "".join(cards)

    # ── Promotions tab ────────────────────────────────────────────────────────
    pr_items: list[dict] = []
    if brief.promotions_json:
        try:
            pr_items = _json.loads(brief.promotions_json)
        except Exception:
            pass

    if not pr_items:
        pr_html = (
            '<div class="text-center py-10 text-ink-tertiary">'
            '<p class="text-sm">No promotional emails in the last 24 hours.</p>'
            '</div>'
        )
    else:
        rows = []
        for item in pr_items:
            se = (item.get("sender_email") or "").lower()
            sn = _html.escape(item.get("sender") or se)
            exempted = item.get("exempted", False)
            count = len(item.get("email_ids", []))
            offer = _html.escape(item.get("offer_line") or "Promotional offer")
            rows.append(
                f'<div class="px-5 py-3 border-b border-hairline last:border-0 flex items-center gap-3" data-sender="{_html.escape(se)}">'
                f'  <div class="min-w-0 flex-1">'
                f'    <p class="text-sm font-semibold text-ink">{sn}</p>'
                f'    <p class="text-xs text-ink-subtle mt-0.5">{offer}</p>'
                f'    <p class="text-xs text-ink-tertiary">{count} email{"s" if count != 1 else ""}</p>'
                f'  </div>'
                f'  <div class="flex items-center gap-1.5 shrink-0">'
                f'    {_countdown_badge(exempted)}'
                f'    {_keep_toggle(se, exempted, "promotions")}'
                f'  </div>'
                f'</div>'
            )
        pr_html = "".join(rows)

    return nl_html, pr_html
```

- [ ] **Step 2: Update brief_page() to pass digest context**

In `brief_page()` (~line 906), after the `ctx.update({...})` block, add:

```python
    nl_html, pr_html = _render_digest_tabs(brief, account_email) if brief else ("", "")
    ctx["newsletters_html"] = nl_html
    ctx["promotions_html"] = pr_html
    ctx["has_newsletters"] = bool(brief and brief.newsletters_json and brief.newsletters_json != "[]")
    ctx["has_promotions"] = bool(brief and brief.promotions_json and brief.promotions_json != "[]")
    ctx["digest_trash_after_iso"] = (
        brief.digest_trash_after.isoformat() if (brief and brief.digest_trash_after) else ""
    )
```

- [ ] **Step 3: Update brief_generate() to return digest tabs**

In `brief_generate()` (~line 975), after the `links_html` and `content_html` lines, compute digest:

```python
    nl_html, pr_html = _render_digest_tabs(brief, account_email)
    has_nl = bool(brief.newsletters_json and brief.newsletters_json != "[]")
    has_pr = bool(brief.promotions_json and brief.promotions_json != "[]")
    trash_iso = brief.digest_trash_after.isoformat() if brief.digest_trash_after else ""
```

And update the returned HTML to include the HTMX-swapped digest tab content sections. The brief generate response should emit updated `#digest-nl-content` and `#digest-pr-content` divs alongside the existing `#brief-content` div. Add to the return HTMLResponse:

```python
    digest_tabs_html = (
        f'<div id="digest-nl-content">{nl_html}</div>'
        f'<div id="digest-pr-content">{pr_html}</div>'
        f'<script>_digestRefreshBadges("{trash_iso}"); _digestUpdateTabVisibility({str(has_nl).lower()},{str(has_pr).lower()});</script>'
    )
```

And append `digest_tabs_html` to the returned HTML string.

- [ ] **Step 4: Commit**

```bash
git add postmind/web/server.py && git commit -m "feat(server): digest tab rendering and brief page digest context"
```

---

## Task 7: UI — daily_brief.html newsletter/promotions tabs

**Files:**
- Modify: `postmind/web/templates/daily_brief.html`

- [ ] **Step 1: Add digest banner to brief header section**

In `daily_brief.html`, after the `{% if brief %}` stats mini-cards block (~line 38), add a digest trash banner:

```html
  {% if digest_trash_after_iso %}
  <div class="mb-5 flex items-center justify-between gap-3 text-xs bg-surface-2 border border-hairline rounded-button px-3 py-2.5">
    <div class="flex items-center gap-2 text-ink-subtle">
      <svg class="w-3.5 h-3.5 shrink-0 text-ink-tertiary" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.75">
        <path stroke-linecap="round" stroke-linejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/>
      </svg>
      <span id="digest-banner-text">Newsletters and promotions will be trashed in 48h</span>
    </div>
    <button onclick="_digestUndoAll(this)"
            class="text-accent font-medium hover:underline shrink-0">Undo All</button>
  </div>
  {% endif %}
```

- [ ] **Step 2: Replace the existing tab structure in the pm-card with a 4-tab layout**

The existing pm-card has `<div class="px-5 py-4 border-b border-hairline flex items-center gap-3">` header and an HTMX-swappable `#brief-content` div.

Replace the tab header section (the `<div class="px-5 py-4 border-b border-hairline flex items-center gap-3">` block and everything in the `#brief-content` div) with:

```html
  <!-- Brief content (HTMX swap target) -->
  <div class="pm-card overflow-hidden">
    <!-- Tab bar -->
    <div class="px-5 pt-4 pb-0 border-b border-hairline">
      <div class="flex items-center gap-1 -mb-px">
        <button id="tab-btn-inbox" onclick="_briefTab('inbox')"
          class="px-3 py-1.5 text-xs font-semibold border-b-2 border-accent text-accent -mb-px bg-transparent">
          Inbox
        </button>
        <button id="tab-btn-newsletters" onclick="_briefTab('newsletters')"
          class="px-3 py-1.5 text-xs font-semibold border-b-2 border-transparent text-ink-subtle -mb-px bg-transparent">
          Newsletters{% if has_newsletters %} <span class="ml-0.5 text-[10px] text-ink-tertiary">({{ newsletters_count }})</span>{% endif %}
        </button>
        <button id="tab-btn-promotions" onclick="_briefTab('promotions')"
          class="px-3 py-1.5 text-xs font-semibold border-b-2 border-transparent text-ink-subtle -mb-px bg-transparent">
          Promotions{% if has_promotions %} <span class="ml-0.5 text-[10px] text-ink-tertiary">({{ promotions_count }})</span>{% endif %}
        </button>
        <button id="tab-btn-deals" onclick="_briefTab('deals')"
          class="px-3 py-1.5 text-xs font-semibold border-b-2 border-transparent text-ink-subtle -mb-px bg-transparent">
          Deals
        </button>
        <div class="ml-auto flex items-center gap-2">
          {% if brief and brief.ai_used %}
          <span class="pm-badge text-accent border-accent-border bg-accent-subtle">AI</span>
          {% endif %}
          <span id="brief-spinner" class="htmx-indicator text-xs text-ink-tertiary">Generating…</span>
        </div>
      </div>
    </div>

    <div id="brief-content">
      {% if brief %}
      <!-- Inbox tab -->
      <div id="brief-tab-inbox" class="px-5 py-5">
        {{ brief_status_html | safe }}
        {{ brief_links_html | safe }}
        {{ brief_html | safe }}
        {% if brief.generated_at %}
        <p class="text-ink-tertiary text-xs mt-4 pt-3 border-t border-hairline">Generated {{ brief.generated_at.strftime("%H:%M UTC") }}</p>
        {% endif %}
      </div>

      <!-- Newsletters tab -->
      <div id="brief-tab-newsletters" style="display:none">
        <div id="digest-nl-content">{{ newsletters_html | safe }}</div>
      </div>

      <!-- Promotions tab -->
      <div id="brief-tab-promotions" style="display:none">
        <div id="digest-pr-content">{{ promotions_html | safe }}</div>
      </div>

      <!-- Deals tab -->
      <div id="brief-tab-deals" style="display:none">
        <!-- Deals content rendered by _render_brief_links deals_pane -->
        <div id="brief-deals-content">{{ deals_html | safe }}</div>
      </div>
      {% else %}
      <div class="px-5 py-5 text-center py-10">
        <svg class="w-8 h-8 mx-auto text-ink-tertiary/50 mb-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
          <path stroke-linecap="round" stroke-linejoin="round" d="M9 17v-2m3 2v-4m3 4v-6m2 10H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
        </svg>
        <p class="text-ink-tertiary text-sm">No brief for today yet.</p>
        <p class="text-ink-tertiary/70 text-xs mt-1">Hit "Generate Now" above to build one.</p>
      </div>
      {% endif %}
    </div>
  </div>
```

Note: Pass `newsletters_count` and `promotions_count` from the server context (count items in each JSON list), and `deals_html` (extract the deals pane from `_render_brief_links`). Update `brief_page()` to compute these and pass them.

Also update `_render_brief_links()` to return separately addressable inbox and deals content so they can be placed in distinct tab divs. The simplest approach: return the full `links_html` as before for the inbox tab, and expose the deals pane via a new `_render_deals_tab()` function. Or pass `brief.deals_json` to the template and render it there.

Simplest implementation: add a `_render_deals_tab(brief, account_email)` helper in server.py that uses the existing `_build_rows` inner function. Actually, the `_build_rows` function is defined inside `_render_brief_links` — refactor it to be reachable from outside, or just inline the deals rendering.

For simplicity, in `brief_page()`, add:
```python
ctx["deals_html"] = _render_deals_tab(brief, account_email) if brief else ""
ctx["newsletters_count"] = len(json.loads(brief.newsletters_json)) if (brief and brief.newsletters_json) else 0
ctx["promotions_count"] = len(json.loads(brief.promotions_json)) if (brief and brief.promotions_json) else 0
```

And add `_render_deals_tab()` that extracts just the deals pane from the existing logic.

- [ ] **Step 3: Update _briefTab() JS in daily_brief.html**

Replace the existing `_briefTab()` function with:

```javascript
function _briefTab(tab) {
  const tabs = ['inbox', 'newsletters', 'promotions', 'deals'];
  tabs.forEach(t => {
    const pane = document.getElementById('brief-tab-' + t);
    const btn  = document.getElementById('tab-btn-' + t);
    if (!pane || !btn) return;
    const active = 'px-3 py-1.5 text-xs font-semibold border-b-2 border-accent text-accent -mb-px bg-transparent';
    const inactive = 'px-3 py-1.5 text-xs font-semibold border-b-2 border-transparent text-ink-subtle -mb-px bg-transparent';
    pane.style.display = t === tab ? '' : 'none';
    btn.className = t === tab ? active : inactive;
  });
}
```

- [ ] **Step 4: Add digest JS utilities to daily_brief.html extra_js block**

Add to the `{% block extra_js %}` section:

```javascript
// ── Digest keep/unkeep toggle ─────────────────────────────────────────────
document.addEventListener('click', async function(e) {
  const btn = e.target.closest('.digest-keep-btn');
  if (!btn) return;
  const sender = btn.dataset.sender;
  const exempted = btn.dataset.exempted === '1';
  const method = exempted ? 'DELETE' : 'POST';
  btn.disabled = true;
  try {
    const res = await fetch('/digest/exempt', {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sender_email: sender }),
    });
    const data = await res.json();
    if (data.ok) {
      // Flip UI state
      const newExempted = !exempted;
      btn.dataset.exempted = newExempted ? '1' : '0';
      btn.title = newExempted ? 'Click to un-keep (will be trashed next cycle)' : 'Keep — never auto-trash this sender';
      btn.className = btn.className.replace(
        newExempted ? 'text-ink-tertiary' : 'text-success',
        newExempted ? 'text-success' : 'text-ink-tertiary'
      );
      // Update countdown badge in same row
      const row = btn.closest('[data-sender]');
      if (row) {
        const badge = row.querySelector('.digest-countdown');
        if (badge) {
          if (newExempted) {
            badge.outerHTML = '<span class="pm-badge text-success border-success-border bg-success-subtle text-[10px]">Kept</span>';
          }
        }
      }
    }
  } finally {
    btn.disabled = false;
  }
});

// ── Countdown badges ──────────────────────────────────────────────────────
function _digestRefreshBadges(trashAfterIso) {
  if (!trashAfterIso) return;
  const target = new Date(trashAfterIso);
  document.querySelectorAll('.digest-countdown').forEach(el => {
    const now = new Date();
    const diffMs = target - now;
    if (diffMs <= 0) {
      el.textContent = 'Trashing soon';
    } else {
      const hrs = Math.floor(diffMs / 3600000);
      const mins = Math.floor((diffMs % 3600000) / 60000);
      el.textContent = hrs > 0 ? `Trashing in ${hrs}h` : `Trashing in ${mins}m`;
    }
  });
}

function _digestUpdateTabVisibility(hasNl, hasPr) {
  const nlBtn = document.getElementById('tab-btn-newsletters');
  const prBtn = document.getElementById('tab-btn-promotions');
  // Tabs always visible; just update visual weight if empty
}

async function _digestUndoAll(btn) {
  btn.disabled = true;
  try {
    const res = await fetch('/digest/undo-all', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    const data = await res.json();
    if (data.ok) {
      // Remove the banner
      btn.closest('[id]')?.remove() || btn.closest('div')?.remove();
      // Flip all countdown badges to Kept
      document.querySelectorAll('.digest-countdown').forEach(el => {
        el.outerHTML = '<span class="pm-badge text-success border-success-border bg-success-subtle text-[10px]">Kept</span>';
      });
      // Flip all keep buttons to active
      document.querySelectorAll('.digest-keep-btn').forEach(b => {
        b.dataset.exempted = '1';
        b.className = b.className.replace('text-ink-tertiary', 'text-success');
      });
    }
  } finally {
    btn.disabled = false;
  }
}

// Initialise countdown badges on page load
(function() {
  const iso = {{ ('"' + digest_trash_after_iso + '"') | safe if digest_trash_after_iso else 'null' }};
  if (iso) {
    _digestRefreshBadges(iso);
    setInterval(() => _digestRefreshBadges(iso), 60000);
  }
})();
```

- [ ] **Step 5: Run all tests**

```bash
cd /Users/tashfeenekram/postmind && .venv/bin/python -m pytest tests/ -q --tb=short 2>&1 | tail -20
```

Expected: all passing.

- [ ] **Step 6: Commit**

```bash
git add postmind/web/templates/daily_brief.html postmind/web/server.py && git commit -m "feat(ui): Newsletter and Promotions tabs in Daily Brief with Keep toggle and countdown"
```

---

## Task 8: Full integration — wire server context, fix _render_brief_links tab split, run + browser test

**Files:**
- Modify: `postmind/web/server.py`

- [ ] **Step 1: Extract deals pane from _render_brief_links into _render_deals_tab**

In `server.py`, refactor `_render_brief_links()` so that the deals pane HTML is also available standalone. Add a new `_render_deals_tab(brief, account_email) -> str` function that builds the deals rows using the same row logic. This is the simplest extraction:

```python
def _render_deals_tab(brief, account_email: str) -> str:
    """Render just the deals pane for the Deals tab."""
    import html as _html
    import json as _json
    from urllib.parse import quote as _quote

    if not brief or not brief.deals_json:
        return '<div class="text-center py-10 text-ink-tertiary text-sm">No deals today.</div>'
    try:
        deals = _json.loads(brief.deals_json)
    except Exception:
        return ""
    if not deals:
        return '<div class="text-center py-10 text-ink-tertiary text-sm">No deals today.</div>'

    # Reuse the brief action buttons pattern
    is_gmail = not account_email.endswith((".imap",))  # heuristic; use provider check if available
    rows = []
    for item in deals[:50]:
        gid = _html.escape(str(item.get("gmail_id") or ""))
        sender = _html.escape(str(item.get("sender") or "Unknown"))
        subject = _html.escape(str(item.get("subject") or "(no subject)")[:80])
        score = item.get("deal_score", 0)
        score_label = {1: "offer", 2: "deal", 3: "hot deal"}.get(score, "")
        badge = f'<span class="pm-badge text-[10px] text-warning border-warning-border bg-warning-bg">{score_label}</span>' if score_label else ""
        open_btn = ""
        if is_gmail and gid:
            open_url = f"/brief/deal-open?gid={_quote(str(item.get('gmail_id') or ''), safe='')}"
            open_btn = (
                f'<a href="{open_url}" target="_blank" rel="noopener noreferrer" '
                f'class="p-1 rounded text-ink-tertiary hover:text-accent hover:bg-accent-subtle transition-colors">'
                f'<svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.75">'
                f'<path stroke-linecap="round" stroke-linejoin="round" d="M3 10h10a8 8 0 018 8v2M3 10l6 6M3 10l6-6"/></svg>'
                f'</a>'
            )
        rows.append(
            f'<div class="px-5 py-3 border-b border-hairline last:border-0 flex items-center gap-3" data-gmail-id="{gid}">'
            f'  <div class="min-w-0 flex-1">'
            f'    <p class="text-sm font-medium text-ink truncate">{subject}</p>'
            f'    <p class="text-xs text-ink-tertiary mt-0.5 truncate">{sender}</p>'
            f'  </div>'
            f'  <div class="flex items-center gap-1">{badge}{open_btn}</div>'
            f'</div>'
        )
    return "".join(rows)
```

- [ ] **Step 2: Update brief_page() with all new context vars**

In `brief_page()`, replace the `ctx.update({...})` section and additions with the full updated version:

```python
    import json as _json

    nl_html, pr_html = _render_digest_tabs(brief, account_email) if brief else ("", "")
    deals_html = _render_deals_tab(brief, account_email) if brief else ""

    newsletters_count = 0
    promotions_count = 0
    if brief and brief.newsletters_json:
        try:
            newsletters_count = len(_json.loads(brief.newsletters_json))
        except Exception:
            pass
    if brief and brief.promotions_json:
        try:
            promotions_count = len(_json.loads(brief.promotions_json))
        except Exception:
            pass

    trash_iso = ""
    if brief and brief.digest_trash_after:
        ta = brief.digest_trash_after
        if ta.tzinfo is None:
            from datetime import timezone as _tz
            ta = ta.replace(tzinfo=_tz.utc)
        trash_iso = ta.isoformat()

    ctx.update(
        {
            "brief": brief,
            "brief_status_html": _render_brief_status(brief) if brief else "",
            "brief_links_html": _render_brief_links(brief, account_email) if brief else "",
            "brief_html": _render_brief_html(brief.content) if brief else "",
            "newsletters_html": nl_html,
            "promotions_html": pr_html,
            "deals_html": deals_html,
            "newsletters_count": newsletters_count,
            "promotions_count": promotions_count,
            "has_newsletters": newsletters_count > 0,
            "has_promotions": promotions_count > 0,
            "digest_trash_after_iso": trash_iso,
            "recent": recent,
            "today_str": today_str,
            "account_email": account_email,
            "ai_mode": _ai_mode(),
        }
    )
```

- [ ] **Step 3: Update brief_generate() to also emit updated digest divs**

In `brief_generate()`, compute and include digest content in the HTMX swap response:

```python
    nl_html, pr_html = _render_digest_tabs(brief, account_email)
    deals_html = _render_deals_tab(brief, account_email)
    trash_iso = ""
    if brief.digest_trash_after:
        ta = brief.digest_trash_after
        if ta.tzinfo is None:
            from datetime import timezone as _tz
            ta = ta.replace(tzinfo=_tz.utc)
        trash_iso = ta.isoformat()

    return HTMLResponse(
        f'<div id="brief-content">'
        f'<div id="brief-tab-inbox" class="px-5 py-5">'
        f'<div class="flex items-center gap-2 mb-4">{ai_badge}'
        f'<span class="text-ink-tertiary text-xs">Generated at {gen_time}</span></div>'
        f"{status_html}{links_html}{content_html}"
        f'</div>'
        f'<div id="brief-tab-newsletters" style="display:none">'
        f'<div id="digest-nl-content">{nl_html}</div>'
        f'</div>'
        f'<div id="brief-tab-promotions" style="display:none">'
        f'<div id="digest-pr-content">{pr_html}</div>'
        f'</div>'
        f'<div id="brief-tab-deals" style="display:none">'
        f'<div id="brief-deals-content">{deals_html}</div>'
        f'</div>'
        f'</div>'
        f'<script>_digestRefreshBadges("{trash_iso}");</script>'
    )
```

- [ ] **Step 4: Run all tests**

```bash
cd /Users/tashfeenekram/postmind && .venv/bin/python -m pytest tests/ -q --tb=short 2>&1 | tail -25
```

Expected: all passing. Fix any failures before continuing.

- [ ] **Step 5: Start the server and test in the browser**

```bash
cd /Users/tashfeenekram/postmind && .venv/bin/postmind serve &
sleep 2
open http://127.0.0.1:8484/brief
```

Verify:
1. Daily Brief page loads without errors
2. 4 tabs appear: Inbox, Newsletters, Promotions, Deals
3. Clicking each tab shows/hides the correct pane
4. Newsletters/Promotions tabs show appropriate empty state if no qualifying emails
5. If emails exist: cards render with sender name, count, bullets/offer line
6. "Generate Now" button works and refreshes tab content
7. Keep toggle responds to clicks (check Network tab for POST /digest/exempt)
8. Undo All banner appears when digest_trash_after is set

- [ ] **Step 6: Run make check for full CI validation**

```bash
cd /Users/tashfeenekram/postmind && make check 2>&1 | tail -30
```

Fix any lint errors (`make fix` to auto-fix), then re-run.

- [ ] **Step 7: Final commit**

```bash
git add -p && git commit -m "$(cat <<'EOF'
feat: newsletter & promotions digest tabs in Daily Brief

- Newsletters tab: batch-summarizes subscription emails (list_unsubscribe
  header) from last 24h into per-sender 3-bullet AI summaries
- Promotions tab: extracts one-line offer from deal-scored vendor emails
- Keep toggle: permanently exempts a sender from auto-trash (DigestExemption)
- Auto-trash: heartbeat daemon trashes non-exempted emails 48h after digest
  generation; undo log written for 30-day reversal window
- Undo All: one-click exempts every sender in today's digest
- Deals tab: existing deals content relocated from inline to its own tab
- DigestExemption model + newsletters_json/promotions_json/digest_trash_after
  columns on DailyBrief

Co-Authored-By: Claude Sonnet 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```
