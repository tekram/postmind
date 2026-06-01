# Plan: Smart First-Run Cleanup — postmind greets a 10-year inbox and does the work

Status: **PROPOSED** (not yet implemented). Author: codebase + web research,
May 2026.

This document specifies what should happen the *first time* a user opens postmind
against a large, old, never-curated inbox — the "I have 60,000 emails going back a
decade, where do I even start?" case. Today the wizard connects an account and then
drops the user on a dashboard that needs a manual sync and a manual scan before it
shows anything useful. The goal is to turn that cold start into a guided, mostly-
automatic "here's your inbox, here's the plan, approve it" experience — and to use
the LLM to *narrate and prioritize* that plan, not just to classify emails.

It is written against the *actual* code in this repo and cites real files/functions.
It deliberately reuses the existing scoring, recommendation, bulk, and undo
machinery rather than inventing parallel systems.

---

## 1. Goals & non-goals

### Goals
- **Zero dead ends after onboarding.** The moment an account connects, postmind
  starts pulling mail and, when it has enough, shows concrete cleanup opportunities
  — never an empty dashboard with a "go run sync" instruction.
- **Lead with one big, safe win.** For a decade-old inbox the first impression
  should be "postmind found 23,000 newsletters from senders you've ignored for
  years — reclaim 3.1 GB?" with a single approve button, not a 50-row table the
  user has to study.
- **Be smart without being reckless.** Every proposed action is reversible (Trash
  or Archive, 30-day undo), sensitive senders (banks/health/legal) are never in a
  one-click batch, and nothing executes without explicit confirmation on first run.
- **Use the LLM where it actually adds value** — plain-language summarization,
  prioritization, and grouping of the cleanup plan — while keeping the *detection*
  on the existing deterministic signals so the feature works with **AI off** too.
- **Degrade gracefully across AI modes** (cloud / local / off), matching the
  posture established in [super-agent-plan.md](super-agent-plan.md) and
  [chat-assistant-plan.md](chat-assistant-plan.md).

### Non-goals (for now)
- No auto-execution of destructive actions on first run. Smart ≠ silent. The first
  run *proposes*; the user approves. (Opt-in autopilot already exists for recurring
  rules via the Super Agent — we link to it, we don't front-load it.)
- No permanent delete, ever — Trash and Archive only (consistent with the rest of
  the app; `batch_delete_permanent` stays unexposed).
- No new email-client UI (no thread reading). We operate on the existing
  `SenderGroup` / `EmailRecord` model.
- No new ML model or open-tracking pixel. "Never opened" stays approximated by the
  existing unread-ratio + `List-Unsubscribe` heuristic
  (`find_unopened_subscriptions` in `core/agent_tools.py`).

---

## 2. The problem with the current cold start

Traced through the code, today's first run is:

1. `GET /` → if no accounts, redirect to `/onboarding`
   (`web/server.py:231–238`).
2. Onboarding wizard: **connect account** → **AI mode** → **done**
   (`web/server.py:884–976`, `templates/onboarding.html`). The "done" step links to
   the dashboard or `/stats`.
3. Dashboard with **no scan data** shows a "Ready to trim your inbox / Scan inbox"
   CTA (`templates/dashboard.html:114–131`). Nothing is synced yet.
4. The user must **manually** visit `/sync`, pick a scope/limit, and click Start
   (`web/server.py:1368–1528`). Only after that does `/stats` (and the dashboard's
   `best_next_step` card) have data.

So the wizard ends precisely where the value hasn't started. There are **three
gaps**:

- **G1 — No first sync.** Sync is never auto-triggered; the wizard hands off to an
  empty app.
- **G2 — No "here's what I found" moment.** The dashboard's `best_next_step`
  (`sender_stats.py:1070`) is good but understated, and only appears after a manual
  scan. There's no welcoming summary of the *whole* opportunity.
- **G3 — No narration.** All the smart signals exist (impact, confidence, risk,
  promotional, age) but nothing turns them into a human sentence like "most of your
  storage is old marketing mail — safe to clear."

This plan closes G1–G3.

---

## 3. What already exists to build on (reuse, don't rebuild)

The detection and execution stack is essentially complete. The first-run feature is
mostly *sequencing and presentation* on top of it.

**Detection / scoring** (`core/sender_stats.py`):
- `fetch_sender_groups_from_db(account_email, scope, …, newer_than_days, older_than_days)`
  — full-history aggregation straight from the local DB, no API calls. (The age
  params were just added for the Stats "older than" filter — directly reusable.)
- `compute_impact_scores` (60% storage / 40% count), `compute_confidence_score`
  (unsubscribe header + age + frequency − transactional penalty), `classify_sender_risk`
  (sensitive / safe / review), `is_promotional` (List-Unsubscribe + ESP domain +
  sender prefix + Gmail category label).
- `generate_recommendations`, `best_next_step` (tier-priority: never points at a
  bank when a newsletter exists), `quick_win`, `InboxInsights`.

**Execution / safety**:
- `/purge/preview` + `/purge/confirm` (now action-aware: **trash or archive**),
  `BulkEngine`, `UndoLogRepo` (30-day undo), `BlocklistRepo` (protected senders).

**Automation**:
- `BulkEngine.create_rule` / `run_rules`, the heartbeat daemon (`core/daemon.py`),
  and opt-in autopilot — for "keep it clean going forward."

**AI** (`core/ai_engine.py`):
- `classify_emails`, `translate_rule`, `parse_bulk_intent`, `chat` / `chat_stream`,
  with cloud / local / off modes and graceful degradation.

**The single missing AI capability** is a summarization/prioritization method that
takes the *already-computed* sender groups and returns a short, structured cleanup
plan in plain language. That's the one new AI method this plan introduces
(§5, `summarize_cleanup_plan`).

---

## 4. The first-run experience (UX flow)

```
Onboarding step 1: Connect account
        │  (account registered)
        ▼
Onboarding step 2: AI mode (unchanged)
        │
        ▼
Onboarding step 3: "Scanning your inbox…"   ← NEW: kicks off background sync here
        │   live progress (reuse /sync/start + poll), runs while user reads
        │   "While we scan: postmind only moves mail to Trash/Archive, never
        │    deletes. Everything is undoable for 30 days."
        ▼
First-run summary  (/welcome)               ← NEW: the "here's what I found" moment
        │
        │   ┌─────────────────────────────────────────────────────────┐
        │   │  We scanned 58,420 emails from 1,204 senders.            │
        │   │                                                          │
        │   │  💡 Big win: 23,180 newsletters & promos you haven't     │
        │   │     opened in years — about 3.1 GB.   [Review & clean →] │
        │   │                                                          │
        │   │  Also worth a look:                                      │
        │   │   • 4,900 old notifications (GitHub, LinkedIn) · 1.2 GB  │
        │   │   • 12 senders with 500+ emails each                     │
        │   │                                                          │
        │   │  🔒 We left your banks, health, and personal mail alone. │
        │   └─────────────────────────────────────────────────────────┘
        │
        ▼
[Review & clean] → existing /purge/preview (action=trash|archive)
                   pre-populated with the safe, high-confidence senders
        │
        ▼
After first cleanup → offer "Keep it clean automatically?"
                      → links to Super Agent create_rule / autopilot (opt-in)
```

Key UX principles:
- **The summary is the product.** One headline win + 2–3 secondary callouts + an
  explicit "what we protected" line. Not a table — the table lives at `/stats` for
  users who want it.
- **Progress, not a blank wait.** Sync runs in the background during the summary
  build; the summary page can render partial results and refine as more sync
  completes (the scan reads the DB, which fills incrementally).
- **One click to value.** "Review & clean" deep-links into the *existing* confirm
  flow with senders pre-selected — no new destructive path.

---

## 5. Where the LLM comes in (and where it deliberately doesn't)

The user asked specifically about leveraging AI/LLM here. The right division of
labor:

### Detection stays deterministic (works with AI **off**)
"Which senders are safe bulk mail, how old, how big, how risky" is already answered
by `compute_confidence_score` + `classify_sender_risk` + `is_promotional`. These are
fast, free, private, and don't hallucinate. The first-run feature must produce a
useful plan **even in AI-off mode**, so detection cannot depend on the LLM.

### The LLM summarizes, prioritizes, and narrates (the value-add)
New method, cloud-first with local fallback:

```python
# core/ai_engine.py
def summarize_cleanup_plan(self, groups_digest: list[dict], insights: dict) -> CleanupPlan:
    """Given the ALREADY-SCORED sender groups + InboxInsights, return a short,
    structured, plain-language cleanup plan: one headline opportunity, 2–3 secondary
    buckets, and a one-line reassurance about what was left untouched.

    Input is a compact digest (sender, count, MB, age_days, confidence, risk,
    is_promotional) — NOT raw email bodies — so it's cheap, privacy-preserving, and
    cacheable. The model only ranks/labels/phrases; it never invents senders or
    numbers (those are passed through verbatim from the digest)."""
```

`CleanupPlan` (structured, so the template renders it, not free text):
```python
@dataclass
class CleanupBucket:
    title: str            # "Newsletters you've ignored for years"
    sender_emails: list[str]   # server-resolved, bound to the confirm flow
    count: int
    size_mb: float
    suggested_action: str  # "archive" | "trash"
    rationale: str         # one short LLM sentence

@dataclass
class CleanupPlan:
    headline: CleanupBucket
    secondary: list[CleanupBucket]
    protected_note: str    # "Left your banks, health, and personal mail alone."
```

**Safety against prompt injection / hallucination** (same posture as the Super
Agent): the LLM receives a digest and returns *groupings + prose*; the actual
`sender_emails` and numbers are re-resolved server-side from the scan cache before
anything reaches `/purge/preview`. The model can phrase the plan; it cannot choose
new targets or alter counts. Subjects/snippets fed in are treated as untrusted data.

### Mode degradation
- **Cloud** — full `summarize_cleanup_plan`, prompt-cached digest, best phrasing.
- **Local (Ollama)** — attempt the same; if the structured output is unreliable,
  fall back to a **template-rendered** plan (next bullet).
- **Off** — no model call. Render the plan from a **deterministic template** built
  directly from `best_next_step` + `InboxInsights` + the promotional/age buckets
  (e.g. "23,180 promotional emails from senders with unsubscribe links, none opened
  recently — about 3.1 GB"). Slightly less fluent, identically actionable.

So AI makes it *nicer and smarter-sounding*; the deterministic core makes it
*always work*.

### Optional deeper AI (later phases)
- Use `classify_emails` on a *sample* per bucket to confirm category labels and
  raise confidence ("we spot-checked 50 — all newsletters").
- Use `translate_rule` to turn the headline bucket into a standing rule in one click
  ("…and archive these automatically from now on").

---

## 6. Implementation phases

### Phase 1 — Auto-sync + first-run summary (no AI required)
Closes G1 + G2. Highest value, lowest risk.

- **Onboarding step 3 kicks off sync.** Reuse `POST /sync/start` (background task,
  `web/server.py:1375`) right after AI-mode is set; show the existing poll UI inline
  in the wizard's final step instead of a static "Done" page.
  - Scope default: `anywhere` (the whole back-catalogue is the point on first run).
  - Limit default: unlimited for first run, but render the summary as soon as a
    threshold (say 2,000 messages) is in the DB so the user isn't blocked on a 60k
    sync; keep syncing in the background.
- **New `GET /welcome` route + `welcome.html`.** Builds the plan from
  `fetch_sender_groups_from_db(scope="anywhere")` → scores → `best_next_step` +
  promotional/age bucketing → deterministic `CleanupPlan` template.
- **First-run detection.** Add a per-account flag (e.g. `AccountRepo` /
  `first_run_completed_at`, or infer "DB has data but user hasn't visited
  `/welcome`"). Route `/` to `/welcome` once, then to the normal dashboard.
- **"Review & clean" button** → existing `/purge/preview` with the headline bucket's
  senders pre-selected and `action` defaulted (archive for "ignored" buckets, trash
  for true junk). Reuses everything from the work just shipped.

### Phase 2 — LLM narration (cloud, with off/local fallback)
Closes G3.

- Add `summarize_cleanup_plan` + `CleanupPlan`/`CleanupBucket` to `ai_engine.py`.
- `/welcome` calls it when AI mode ≠ off; otherwise uses the Phase-1 template.
- Server re-resolves sender lists and numbers from the scan cache before rendering /
  before any confirm — model output is presentation-only.
- Prompt-cache the system prompt + digest framing (per the `claude-api` skill
  guidance) since the digest is large but stable within a session.

### Phase 3 — "Keep it clean" handoff
After the first cleanup completes, offer a single opt-in: "Archive mail from these
senders automatically from now on?" → `BulkEngine.create_rule` (via the Super Agent's
existing `create_rule` path) + optionally enable autopilot. Pure reuse of shipped
automation; no new execution code.

### Phase 4 (optional) — AI spot-check & confidence boost
Sample-classify each bucket with `classify_emails` to add a "we checked N, all were
newsletters" reassurance and to down-rank any bucket the classifier flags as mixed.

---

## 7. Risks & mitigations

- **Big-inbox sync latency.** A 60k-message metadata sync isn't instant. Mitigation:
  render the summary from a partial DB (threshold-gated) and refine; never block the
  UI on a full sync.
- **LLM mis-grouping / hallucinated numbers.** Mitigation: model gets a digest and
  returns prose+groupings only; all targets and figures are server-resolved from the
  scan cache before display and before confirm (same containment as the Super Agent).
- **Over-eager first impression.** Mitigation: nothing executes without the existing
  confirm-first preview; sensitive/protected senders are excluded from one-click
  buckets by `classify_sender_risk` + `BlocklistRepo`; archive (not trash) is the
  default for "ignored but not junk" buckets.
- **AI-off users feel second-class.** Mitigation: the deterministic template plan is
  the baseline everyone gets; AI only upgrades the phrasing/prioritization.
- **Privacy.** Mitigation: only a scored digest (sender, counts, sizes, age,
  flags) is ever sent to a model — never email bodies — and only when AI mode is
  cloud; local keeps it on device; off makes no call.

---

## 8. Summary

The detection and execution engines are already built; the cold start just doesn't
*use* them at the one moment they'd impress a new user most. This plan (1) auto-starts
the sync at the end of onboarding, (2) replaces the empty dashboard with a single
"here's your biggest safe win" summary built from the existing scoring, and (3) uses
the LLM to narrate and prioritize that summary — with a deterministic fallback so it
works with AI off. Every action routes through the confirm-first, undoable Trash/
Archive flow already shipped, and the natural next step ("keep it clean
automatically") hands off to the existing rules/autopilot system.
