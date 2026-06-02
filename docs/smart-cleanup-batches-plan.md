# Plan: Smart Cleanup Batches — fast, AI-grouped bulk decisions

Status: **PROPOSED** (not started). Author: codebase research + design, June 2026.

This document specifies a new cleanup experience built for **speed of bulk
decisions**: AI groups inbox mail into a handful of semantically meaningful,
high-confidence *batches* ("expired deal alerts", "shipping notices for delivered
orders", "calendar invites for past meetings"), and the user clears them with a
fast keyboard-driven approve / skip flow — one decision per batch instead of one
per sender or per email. It is written against the *actual* code in this repo and
cites real files/functions.

---

## 1. The problem with cleanup today

Cleanup currently makes the user do too much per-decision work:

- **Grouping is by sender, not by meaning.** `build_cleanup_plan()`
  (`postmind/core/sender_stats.py:1181`) buckets by hard-coded heuristics —
  confidence ≥70 → trash, `inbox_days ≥ 365` → archive, `count ≥ 100` → flood.
  Useful, but a "newsletters" bucket can still mix a sender you love with three
  you forgot you subscribed to, so the user falls back to reviewing senders
  one at a time on `/purge/preview`.
- **AI classification is rich but disconnected.** `classify_emails()`
  (`postmind/core/ai_engine.py:153`) already tags each email with
  `category` / `priority` / `suggested_action` / `requires_reply` /
  `deadline_hint`, but those signals power the **Triage** tab only. They never
  feed the cleanup batching. So the cheapest, most semantic signal we have is
  thrown away at cleanup time.
- **The flow is point-and-click, not momentum-driven.** Every batch is a separate
  page load and confirm dialog. There's no "approve, next, approve, next" rhythm,
  which is exactly what makes inbox-zero tools (Sanebox, Clean Email, Superhuman)
  feel fast.

The bet: **a semantic batch + a keyboard flow** turns "30 minutes of sender
review" into "90 seconds of approve/skip."

### Non-goals
- Not changing the safety model: every WRITE still routes through confirm-first
  preview + `UndoLogRepo` (Trash-only, 30-day undo). See `super-agent-plan.md`.
- Not a per-email triage replacement — Triage stays for "decide each email." This
  is "decide whole batches at once."
- No new destructive action types. We reuse `archive` / `trash` / `mark_read` /
  `label` / `unsubscribe` from `BulkEngine._execute_action()`.

---

## 2. Core idea: the Batch

A **CleanupBatch** is a named, reviewable group of messages the user can accept or
reject as one unit. Unlike today's sender buckets, a batch is defined by *meaning*,
combining the deterministic sender signals we already have with the AI category
signals we already compute:

```
CleanupBatch
  key            stable id, e.g. "promos-unopened", "shipping-delivered"
  title          warm one-liner ("Deal alerts you never opened")
  rationale      one sentence on why it's safe
  action         "trash" | "archive" | "mark_read"  (reuses BulkEngine)
  message_ids    concrete ids (server-side; never sent to the LLM)
  sender_count / email_count / size_mb
  confidence     0–100, drives ordering + auto-select
  sample         3–5 redacted previews (subject + sender) for the card
  protected      bool — sensitive senders are split out, never in a batch
```

Batches are produced by a new **batcher** that runs in three layers, cheapest
first, so cost and latency scale with how much certainty we already have:

1. **Deterministic seed (free).** Reuse `fetch_sender_groups()` +
   `compute_impact_scores()` + `classify_sender_risk()` to get scored senders and
   strip sensitive ones (`postmind/core/sender_stats.py`). This is today's plan.
2. **Category overlay (already-cached AI).** Pull `ai_category` /
   `suggested_action` from `EmailRecord` / `ClassificationCacheRecord`
   (`postmind/core/storage.py`) where present. No new LLM call — just a join.
   Messages with `suggested_action in {archive, unsubscribe, delete}` and a
   coherent category collapse into semantic batches.
3. **Semantic naming + splitting (one cheap LLM call).** A single new
   `AIEngine.propose_batches()` call takes the **body-free digest** of the seed
   groups (same privacy contract as `cleanup_plan_digest()`,
   `sender_stats.py:1298`) and returns batch definitions: which sender-groups to
   merge, what to name them, suggested action, and a confidence. The model only
   sees aggregate signals + sender domains + category counts — never email bodies,
   never the concrete `message_ids`. The server maps batch keys back to ids.

This mirrors the existing `summarize_cleanup_plan()` privacy pattern: the LLM
shapes *labels and groupings*, the server owns *the data and the numbers*.

---

## 3. The fast-decision UI: `/cleanup`

A new page (and a refresh of the welcome/first-run flow) that presents batches as
a stack of cards, optimized for keyboard rhythm:

```
┌──────────────────────────────────────────────┐
│  Deal alerts you never opened        TRASH    │
│  142 emails · 6 senders · 38 MB · 96% safe    │
│  ───────────────────────────────────────────  │
│  • Groupon — "Your weekend deals are here"    │
│  • RetailMeNot — "50% off ends tonight"       │
│  • …3 more                          [expand]  │
│                                                │
│  [A] Approve   [S] Skip   [E] Edit   [↵] Next  │
└──────────────────────────────────────────────┘
        batch 1 of 7 · ~4 min of inbox cleared
```

Interaction model:
- **Keyboard-first:** `A` approve, `S` skip, `E` edit (drop senders / change
  action), `Z` undo last, `Enter` advance. Approvals **queue** and execute as a
  single confirm-first bulk op at the end (or per-batch with autopilot on).
- **Auto-selected by confidence:** batches ≥ a safe threshold are pre-marked
  "approve"; the user is really just *vetoing*, which is faster than selecting.
- **Momentum feedback:** a running "X emails / Y MB cleared" tally and progress
  count, the dopamine loop that makes these flows addictive.
- **One commit, fully undoable:** the queued approvals flow through the existing
  preview → confirm → `UndoLogRepo` machinery so the whole session is one undo
  entry (or batched entries). Reuses `/purge/confirm` / `/agent/action/confirm`.

This is additive — `/triage`, `/purge`, `/agent` are untouched.

---

## 4. Make it *learn* (so it gets faster every week)

The signal that turns this from "nice" to "much better" is feedback. We already
store the raw material; we just need to close the loop:

- **Record every accept / skip / edit** keyed by sender + batch-key + action in a
  new lightweight `cleanup_feedback` table (sibling to `undo_log` in
  `storage.py`). "Skipped sender X in the promos batch twice" → never auto-select
  X again. "Always approves shipping-delivered" → bump its confidence to the top.
- **Feed it back into batching.** `propose_batches()` and the deterministic
  confidence get a per-user prior: a small adjustment from observed accept-rate
  per sender and per category. No model training — just a stored multiplier.
- **Proactively offer a rule.** When the user approves the same kind of batch 3
  sessions running, surface "Always do this automatically?" → creates a recurring
  `rules` entry (reuses `translate_rule()` / the rules table). This is where
  cleanup work trends toward zero.

---

## 5. Phasing

**Phase 1 — Batcher + page (no new LLM).** Build `CleanupBatch` +
`build_cleanup_batches()` from layers 1–2 only (deterministic + cached
categories). Ship `/cleanup` with the card stack and keyboard flow, wired to the
existing preview/confirm/undo path. Delivers the speed win immediately, fully
offline-capable. *This is the milestone that proves the UX.*

**Phase 2 — Semantic layer.** Add `AIEngine.propose_batches()` (one cheap,
body-free call) for better names and smarter splits/merges; degrade gracefully to
Phase-1 batches when AI is off or local-only (mirror the chat fallback in
`super-agent-plan.md`).

**Phase 3 — Learning loop.** Add `cleanup_feedback`, per-user confidence priors,
and "approve, next time?" → recurring rule. Adds the compounding "easier over
time" payoff.

**Phase 4 — First-run integration.** Replace the welcome cleanup screen with the
batch flow so a new user's *first* experience is a 90-second inbox declutter.

---

## 6. Key files / touch points

| Area | File | What changes |
|---|---|---|
| Batch model + builder | `postmind/core/sender_stats.py` (new `build_cleanup_batches()` beside `build_cleanup_plan():1181`) | semantic grouping over scored senders + cached categories |
| Cached categories | `postmind/core/storage.py` (`EmailRecord`, `ClassificationCacheRecord`) | read `ai_category`/`suggested_action`; new `cleanup_feedback` table |
| Semantic naming | `postmind/core/ai_engine.py` (new `propose_batches()` near `summarize_cleanup_plan():302`) | one body-free LLM call, batch defs only |
| Execution / undo | `postmind/core/bulk_engine.py` (`_execute_action`), `UndoLogRepo` | reused unchanged |
| Page + endpoints | `postmind/web/server.py`, `postmind/web/templates/cleanup.html` (new) | `/cleanup`, `/cleanup/batches`, queue → existing confirm |
| CLI parity (optional) | `postmind/cli/main.py` | `postmind cleanup` interactive batch flow |

---

## 7. Safety & privacy (unchanged guarantees)

- **Body-free AI.** `propose_batches()` sees only the same aggregate digest as
  `cleanup_plan_digest()` — counts, domains, sizes, category tallies. Never bodies,
  never the concrete `message_ids`. Server maps keys → ids.
- **Sensitive senders never batched.** `classify_sender_risk()` splits banks /
  health / legal / personal into a protected note, exactly as today.
- **Confirm-first + 30-day undo.** Every approval commits through the existing
  preview/confirm/`UndoLogRepo` path; nothing executes silently unless the user
  opts into autopilot (and even then sensitive senders keep the human gate).
- **AI-off still works.** Phase 1 is fully deterministic; the page degrades to
  named-but-not-LLM batches when AI is disabled or local-only.

---

## 8. Decisions

1. **Default action bias — TRASH.** The headline promos batch defaults to *trash*
   (reclaim storage, matches today's `build_cleanup_plan` headline). Still fully
   recoverable for the 30-day undo window, so this is safe.
2. **Auto-select by confidence — YES, threshold ≥85.** Batches scoring ≥85 are
   pre-marked "approve" so the user is only *vetoing*. Below 85, the batch is shown
   unselected and requires an explicit `A`. (Threshold is a setting so we can tune
   it; learning-loop priors in Phase 3 adjust per-sender confidence into it.)

### Still open
- **Per-batch commit vs. one commit at the end** — execute each approved batch
  immediately (snappier, more undo entries) or queue and commit once at the end
  (one clean undo). *Decision for Phase 1: queue, then commit once per action type*
  (`UndoLogRepo.operation` is single-valued, so trash/archive become separate undo
  entries — one per action, not one per batch).

---

## 9. Phase 1 implementation contract (build now)

Phase 1 is deterministic-only (layers 1–2): no new LLM call. It reuses the
DB-backed scan, the scan cache, and the confirm/undo machinery verbatim.

### 9a. Backend — `postmind/core/sender_stats.py`

Add beside `build_cleanup_plan()` (line 1181), reusing `SenderGroup`,
`compute_confidence_score()`, `classify_sender_risk()`, `_bucket_from` patterns:

```python
AUTO_SELECT_THRESHOLD = 85  # batches at/above this are pre-checked "approve"

@dataclass
class CleanupBatch:
    key: str               # stable: "promos-unopened", "old-clutter", "flood-<domain>", "review"
    title: str
    rationale: str
    action: str            # "trash" | "archive"  (Phase 1: no mark_read)
    sender_emails: list[str]
    count: int
    size_mb: float
    confidence: int        # 0–100, email-count-weighted average across members
    category: str          # dominant ai_category among members, or ""
    sample: list[dict]     # 3–5 {"sender": str, "subject": str}, redacted previews
    @property
    def sender_count(self) -> int: return len(self.sender_emails)
    @property
    def auto_select(self) -> bool: return self.confidence >= AUTO_SELECT_THRESHOLD

@dataclass
class CleanupBatchPlan:
    batches: list[CleanupBatch]      # ordered by confidence desc, then size desc
    protected_note: str
    protected_count: int
    total_senders: int
    total_emails: int
    @property
    def cleanable_emails(self) -> int: return sum(b.count for b in self.batches)
    @property
    def cleanable_mb(self) -> float: ...

def build_cleanup_batches(
    groups: list[SenderGroup],
    categories: dict[str, dict] | None = None,   # gmail_id -> classification dict (category/suggested_action/...)
    auto_select_threshold: int = AUTO_SELECT_THRESHOLD,
) -> CleanupBatchPlan: ...
```

Grouping logic (deterministic):
- Strip `classify_sender_risk(g) == "sensitive"` into the protected note (same as
  `build_cleanup_plan`). Never batch them.
- Compute per-sender `compute_confidence_score`. When `categories` is provided,
  overlay the dominant `ai_category` / `suggested_action` per sender (join on the
  sender's `message_ids`) and use it to (a) name batches semantically and (b) nudge
  which batch a sender lands in. With no categories, fall back to today's buckets.
- Emit batches roughly: `promos-unopened` (conf ≥70, default **trash**),
  `old-clutter` (`inbox_days ≥ 365`, archive), one `flood-<domain>` per very-high-
  count domain (≥100, archive), `review` (conf 40–69, archive). Default action bias
  is **trash** for the promos headline (decision §8.1).
- `sample`: take up to 5 `sample_subjects` across the batch's senders.
- Each sender appears in exactly one batch (use the `used` set pattern from
  `build_cleanup_plan`).

Unit tests in `tests/` (mirror existing `build_cleanup_plan` tests): sensitive
exclusion, auto_select threshold boundary, trash-default on promos, category
overlay changes naming, empty input.

### 9b. Web — `postmind/web/server.py` + templates

- **`GET /cleanup`** (model on the `welcome` handler, server.py:594): in an executor,
  `fetch_sender_groups_from_db(account_email, scope="anywhere", min_count=1, top_n=1000)`,
  `compute_impact_scores(groups)`, then read cached categories with
  `ClassificationCacheRepo(get_session()).get_many([all gmail_ids])`
  (storage.py:609) keyed by each group's `message_ids`, build a
  `{gmail_id: classification}` map, call `build_cleanup_batches(groups, categories)`.
  `_cache_set(groups, {...}, account_email)` so confirm can resolve senders. Render
  `cleanup.html`. Set `ctx["active"] = "cleanup"`.
- **`POST /cleanup/confirm`**: form carries approved `senders` lists grouped by
  `action`. Reuse the `purge_confirm` body (server.py:901) almost verbatim —
  `_cache_get()`, `BlocklistRepo.blocked_emails` enforcement, record undo BEFORE via
  `UndoLogRepo.record(operation=action, ...)`, then `batch_trash`/`batch_archive`.
  Loop once per distinct action (≤2 undo entries). Redirect to
  `/undo?purged=<total>&...`.
- **`cleanup.html`** (new, extends base.html, Tailwind like `welcome.html`): render
  the batch stack as cards. A small vanilla-JS controller drives the keyboard flow
  (`A` approve / `S` skip / `E` edit / `Z` undo-select / `Enter` next), maintains a
  running cleared tally, pre-checks `batch.auto_select` batches, and on "Clean all"
  posts the approved sets to `/cleanup/confirm`. No build step — inline `<script>`
  consistent with the repo's templates.
- **Nav**: add a "Quick Clean" link in `base.html` (line ~255 block) with
  `active == 'cleanup'` highlighting. Keep `/stats` ("Clean Up", the sender
  explorer) as-is.

Safety guarantees from §7 hold unchanged: body-free, sensitive excluded,
confirm-first, 30-day undo, AI-off works (Phase 1 has no LLM call at all).
