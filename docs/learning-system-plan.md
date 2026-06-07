# Behavioral Learning System — Implementation Plan

> As a user trashes, archives, and replies to emails, postmind learns from those signals
> to improve future AI classification, daily brief surfacing, and triage ordering — without
> requiring the user to explicitly configure anything.

---

## Codebase Observations

**`CleanupFeedbackRecord` / `CleanupFeedbackRepo`** (`storage.py`): Stores per-sender
approve/skip/drop decisions from the `/cleanup` batch flow only. Schema:
`account_email, sender_email, batch_key, action, decision, created_at`. `sender_priors()`
computes a ±15 confidence adjustment per sender based on approval rate. This table is
tightly coupled to batch-cleanup semantics (`batch_key`, `approved/skipped/dropped`) — **do
not extend it** for brief/triage signals. New table is the right call.

**`ClassificationCacheRecord`** (`storage.py`): Keyed by `gmail_id`. Stores
`category, priority, explanation, suggested_action, requires_reply, deadline_hint`. No
per-account partitioning (gmail_ids are naturally account-scoped). No `created_at` index.

**`classify_batch()`** (`ai_engine.py`): Builds prompt from `From:`, `Subject:`, `Snippet:`
only. **No prior signals are currently injected.** `SYSTEM_PROMPT` is static.

**`_gather_stats()`** (`daily_brief.py`): `recent_unclassified` ordered by `internal_date
DESC`, no behavioral filtering. `high_priority_items` ordered by fetch order, no reply-based
promotion. Neither list uses any behavioral signal today.

**`/triage/trash`** and **`/brief/action`** (`server.py`): Both record an `UndoLogEntry`
and call `batch_trash` / `batch_archive`. **Neither records a behavioral signal.**

**Triage page** (`triage.html`): Only trash and draft-reply buttons. No archive/keep/skip
swipe actions exist. Reply signal is available via `/drafts/create`.

**No `UserActionRecord` table exists yet.**

---

## Phase 1 — Signal Capture and Storage (No LLM Changes)

**Goal**: Every user action on an email is durably recorded with enough context to compute
per-sender behavioral signals. Brief ranking improves immediately using pure Python filters.

### 1A. New `UserActionRecord` Table

Add to `postmind/core/storage.py` (alongside `CleanupFeedbackRecord`):

```python
class UserActionRecord(Base):
    __tablename__ = "user_actions"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    account_email = Column(String, nullable=False, index=True)
    gmail_id      = Column(String, nullable=False)
    sender_email  = Column(String, nullable=False)
    sender_name   = Column(String, default="")
    subject       = Column(String, default="")
    action        = Column(String, nullable=False)   # trash | archive | reply | keep | skip
    source        = Column(String, nullable=False)   # brief | triage | bulk | agent
    ai_category   = Column(String, default="")       # from classification_cache at time of action
    ai_priority   = Column(String, default="")
    created_at    = Column(DateTime,
                       default=lambda: datetime.now(timezone.utc), index=True)
```

Design decisions:
- `gmail_id` is **not unique** — a message may be archived then trashed after an undo.
  Dedup at query time with `MAX(created_at)` per `gmail_id`.
- `sender_email/sender_name/subject` are **denormalized** — `EmailRecord` rows can become
  invisible after trash (is_inbox → False). Signals must outlive the email.
- `ai_category/ai_priority` capture the classification context at action time so you can
  later audit "was this a high-priority email the user trashed?" — useful for Phase 2
  signal quality analysis.

Add to `_run_migrations()` in `storage.py`:
```python
("user_actions", [
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("account_email", "TEXT NOT NULL"),
    ("gmail_id", "TEXT NOT NULL"),
    ("sender_email", "TEXT NOT NULL"),
    ("sender_name", "TEXT DEFAULT ''"),
    ("subject", "TEXT DEFAULT ''"),
    ("action", "TEXT NOT NULL"),
    ("source", "TEXT NOT NULL"),
    ("ai_category", "TEXT DEFAULT ''"),
    ("ai_priority", "TEXT DEFAULT ''"),
    ("created_at", "DATETIME"),
]),
```

### 1B. `UserActionRepo`

Add to `storage.py`:

```python
class UserActionRepo:
    def __init__(self, session: Session): self.s = session

    def record(self, account_email: str, gmail_id: str, sender_email: str,
               sender_name: str, subject: str, action: str, source: str,
               ai_category: str = "", ai_priority: str = "") -> None:
        """Best-effort — never raises, so callers don't need try/except."""
        try:
            self.s.add(UserActionRecord(
                account_email=account_email, gmail_id=gmail_id,
                sender_email=sender_email, sender_name=sender_name,
                subject=subject, action=action, source=source,
                ai_category=ai_category, ai_priority=ai_priority,
            ))
            self.s.commit()
        except Exception:
            self.s.rollback()

    def sender_action_counts(self, account_email: str,
                              lookback_days: int = 90) -> dict[str, dict[str, int]]:
        """{sender_email: {action: count}} for actions in the lookback window."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        rows = (
            self.s.query(
                UserActionRecord.sender_email,
                UserActionRecord.action,
                func.count().label("n"),
            )
            .filter(
                UserActionRecord.account_email == account_email,
                UserActionRecord.created_at >= cutoff,
            )
            .group_by(UserActionRecord.sender_email, UserActionRecord.action)
            .all()
        )
        result: dict[str, dict[str, int]] = {}
        for sender_email, action, n in rows:
            result.setdefault(sender_email, {})[action] = n
        return result

    def high_trash_senders(self, account_email: str,
                            min_actions: int = 3,
                            trash_rate: float = 0.80) -> set[str]:
        """Senders where trash / total_actions >= trash_rate with >= min_actions total."""
        counts = self.sender_action_counts(account_email)
        result = set()
        for sender, actions in counts.items():
            total = sum(actions.values())
            if total < min_actions:
                continue
            if actions.get("trash", 0) / total >= trash_rate:
                result.add(sender)
        return result

    def replied_senders(self, account_email: str,
                         lookback_days: int = 90) -> set[str]:
        """Senders the user has replied to at least once."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        rows = (
            self.s.query(UserActionRecord.sender_email)
            .filter(
                UserActionRecord.account_email == account_email,
                UserActionRecord.action == "reply",
                UserActionRecord.created_at >= cutoff,
            )
            .distinct().all()
        )
        return {r.sender_email for r in rows}
```

### 1C. Wire Signal Capture at Action Endpoints

**`/triage/trash`** (`server.py` ~line 3055, inside `_do()`)**:

```python
# After batch_trash succeeds, inside _do():
from postmind.core.storage import ClassificationCacheRepo, EmailRepo, UserActionRepo
session = get_session()
email_rec = EmailRepo(session).get_by_gmail_id(account_email, gmail_id)
cls = ClassificationCacheRepo(session).get_many([gmail_id]).get(gmail_id, {})
if email_rec:
    UserActionRepo(session).record(
        account_email=account_email, gmail_id=gmail_id,
        sender_email=email_rec.sender_email or "",
        sender_name=email_rec.sender_name or "",
        subject=email_rec.subject or "",
        action="trash", source="triage",
        ai_category=cls.get("category", ""),
        ai_priority=cls.get("priority", ""),
    )
```

**`/brief/action`** (`server.py` ~line 3139, inside `_do()`)**:

Same pattern. For bulk actions, batch-load `EmailRepo`:
```python
# Batch lookup to avoid N+1
email_recs = {r.gmail_id: r for r in
              session.query(EmailRecord)
              .filter(EmailRecord.gmail_id.in_(ids)).all()}
cls_map = ClassificationCacheRepo(session).get_many(ids)
for gid in ids:
    rec = email_recs.get(gid)
    cls = cls_map.get(gid, {})
    if rec:
        UserActionRepo(session).record(
            account_email=account_email, gmail_id=gid,
            sender_email=rec.sender_email or "",
            sender_name=rec.sender_name or "",
            subject=rec.subject or "",
            action=base_action, source="brief",
            ai_category=cls.get("category", ""),
            ai_priority=cls.get("priority", ""),
        )
```

**`/drafts/create`** (`server.py` ~line 4651)**:

After `service.draft_reply(...)` succeeds, record `action="reply", source="triage"`.
Reply is the strongest positive signal — a user explicitly chose to engage.

### 1D. Daily Brief Ranking (Pure Python, No LLM)

Modify `DailyBriefGenerator._gather_stats()` in `daily_brief.py`:

**Filter high-trash senders from `recent_unclassified`**:
```python
from postmind.core.storage import UserActionRepo
trash_senders = UserActionRepo(session).high_trash_senders(self.account_email)
# In the recent_unclassified loop:
if (r.sender_email or "").lower() in trash_senders:
    continue  # skip — user consistently trashes this sender
```

**Promote replied-to senders in `high_priority_items`**:
```python
replied = UserActionRepo(session).replied_senders(self.account_email)
# Stable sort: replied senders float to top
high_priority_items.sort(
    key=lambda x: 0 if (x.get("sender_email") or "").lower() in replied else 1
)
```

> Note: `high_priority_items` is built from `EmailRecord` objects that have `sender_email`.
> Add `sender_email` to the dict at collection time (it's already on the record).

### Phase 1: What NOT to build yet

- No LLM prompt changes
- No triage archive/keep buttons
- No classification cache invalidation
- No rule synthesis
- No per-sender stats UI visible to the user

---

## Phase 2 — LLM-Assisted Re-classification via Prior Injection

**Why Option A (prompt-time injection) over Option B (periodic re-classification)**:

The user runs `qwen2.5:32b` locally at ~30s per 3-email batch. Option B would trigger
background inference after every N actions — a background latency storm. Option A adds
~100–200 tokens to an existing prompt that the user is already waiting on. On-demand,
latency is accepted. Option B can be added later as an idle-time background job.

### 2A. Prior Injection in `classify_batch()`

Modify `AIEngine.classify_batch()` in `ai_engine.py` to accept
`sender_priors: dict[str, dict[str, int]] | None = None`:

```python
def _prior_hint(self, sender_email: str, priors: dict) -> str:
    actions = priors.get(sender_email.lower(), {})
    parts = []
    if actions.get("trash", 0) >= 3:
        parts.append(f"trashed {actions['trash']}x by this user")
    if actions.get("archive", 0) >= 3:
        parts.append(f"archived {actions['archive']}x")
    if actions.get("reply", 0) >= 1:
        parts.append(f"user replied to {actions['reply']}x — engaged sender")
    return f" [User history: {'; '.join(parts)}]" if parts else ""
```

In `email_summaries` construction, append `_prior_hint(msg.headers.from_, sender_priors)`
to each `EMAIL N` block. The LLM already sees `From:` so it can match the hint.

Only inject when action count crosses minimum thresholds (trash/archive ≥ 3, reply ≥ 1) —
below that, signal is noise.

### 2B. Wire Priors into Classification Call Sites

Both call sites that invoke `classify_batch`:
1. `_gather_stats()` in `daily_brief.py` (line ~135)
2. `_triage_pending` in `server.py` (line ~2955)

Load priors once per request, pass to each batch:
```python
priors = UserActionRepo(get_session()).sender_action_counts(account_email)
# lower-case keys for case-insensitive matching
priors_lc = {k.lower(): v for k, v in priors.items()}
ai.classify_batch(chunk, sender_priors=priors_lc)
```

For the parallelized `classify_emails()` path, priors need to be captured in the
`pool.submit` closure — add a `classify_batch_with_priors` wrapper or thread-local.

### 2C. Triage Ordering Using Behavioral Signals

Extend the row sort in `triage_page()` to use a behavioral score as a secondary key:

```python
trash_senders = UserActionRepo(session).high_trash_senders(account_email)
replied_senders = UserActionRepo(session).replied_senders(account_email)

def _behavioral_score(row: dict) -> int:
    se = (row.get("sender_email") or "").lower()
    if se in replied_senders: return -1   # float to top
    if se in trash_senders:   return  1   # sink to bottom
    return 0

rows.sort(key=lambda r: (_PRIORITY_ORDER.get(r["priority"], 99), _behavioral_score(r)))
```

Load the two sets once before the sort — one SQL query each, no LLM.

### Phase 2: What NOT to build yet

- No background re-classification job
- No classification cache invalidation
- No rule synthesis or confirmation UX
- No per-sender explanations in the UI

---

## Phase 3 — Rule Synthesis and UX

Once signals have accumulated a clear pattern, use the LLM to synthesize a rule and
propose it to the user for one-click confirmation.

### 3A. Pattern Detection (No LLM)

```python
def candidates_for_rule_synthesis(account_email: str) -> list[str]:
    """Return sender_emails with strong trash signal and no existing rule."""
    counts = UserActionRepo(session).sender_action_counts(account_email)
    existing_rules = {r.gmail_query for r in RuleRepo(session).list(account_email)}
    candidates = []
    for sender, actions in counts.items():
        total = sum(actions.values())
        trash = actions.get("trash", 0)
        if total >= 5 and trash / total >= 0.85:
            if f"from:{sender}" not in str(existing_rules):
                candidates.append(sender)
    return candidates
```

Run this check after each `/triage/trash` action (non-blocking — add to background
executor). If a new candidate emerges, store a proposed rule.

### 3B. LLM Rule Synthesis

Add `AIEngine.synthesize_rule_from_actions(sender_email, sender_name, sample_subjects,
action_counts)` in `ai_engine.py`. Uses existing `translate_rule()` infrastructure:
generate a NL description, pass through `translate_rule()` to get `gmail_query + action`.
Input is sender metadata + 3–5 sample subjects only — no bodies, privacy-safe.

Store as `RuleDefinition(is_active=False)` with a new `proposed_at` column (migration).

### 3C. Confirmation UX

Banner on `/brief` and `/triage` pages for proposed rules:
> "You've trashed 8 emails from alerts@render.com. Create a rule to auto-archive these?"
> [Create rule] [Dismiss]

One-click confirm: `is_active = True`. Dismiss: delete row. Uses existing `RuleRepo`.

### Phase 3: What NOT to build yet

- No automatic rule execution without confirmation
- No "learning summary" page
- No export of behavioral data

---

## Privacy

All `user_actions` data lives in local SQLite (`~/.postmind/postmind.db`).

- **Local AI mode**: Prior hints are injected into Ollama prompts — nothing leaves the machine.
- **Cloud AI mode**: Prior hints (counts only, no email content) are sent to the Anthropic API as part of classification prompts. Same data posture as existing classification (subject + snippet already sent). Add a note to the settings page.

---

## The 3 Most Impactful Things to Ship First

1. **`UserActionRecord` table + signal capture** (Phase 1A + 1C) — prerequisite for
   everything. Best-effort inserts that never break existing endpoints. Low risk.

2. **Filter high-trash senders from `recent_unclassified`** (Phase 1D) — immediately
   visible on the next brief generation after a few trash actions. No LLM changes.
   This is the highest-perceived-value change: the brief stops surfacing junk senders.

3. **Prior injection into `classify_batch()`** (Phase 2A + 2B) — once signal accumulates
   (~1 week of normal use), triage starts demoting consistently-trashed senders and the
   brief `high_priority_items` becomes more accurate. This is the compound-interest step
   that makes the system feel like it "learns."

---

## Implementation Files

| File | Changes |
|------|---------|
| `postmind/core/storage.py` | `UserActionRecord`, `UserActionRepo`, migration |
| `postmind/core/ai_engine.py` | `classify_batch` prior injection, `synthesize_rule` (Ph3) |
| `postmind/core/daily_brief.py` | `_gather_stats` filtering + promotion |
| `postmind/web/server.py` | Signal capture at `/triage/trash`, `/brief/action`, `/drafts/create` |
| `postmind/core/mock_ai.py` | Update `classify_batch` signature to accept `sender_priors` |
| `postmind/web/templates/triage.html` | Behavioral sort in triage (Ph2) |
