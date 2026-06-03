# Plan: Autodraft — proactively draft replies in the user's voice

Status: **PHASE 1 SHIPPED** (branch `feat/autodraft-replies`). On-demand drafting,
in-thread Gmail draft creation, the `/drafts` review UI, the `drafts` provider
capability, `DraftRecord`/`DraftRepo`, the `MockAIEngine.compose_email` stub, the
proactive `run_autodraft` daemon hook + agent toggle, and 19 tests are implemented and
passing. Phases 2–4 (learned voice, classifier tuning, opt-in auto-send) remain.
Author: codebase + web research, June 2026.

This document specifies an **autodraft** feature: postmind detects emails that need a
reply, generates a draft *in the user's voice and tone for that specific recipient*,
and parks it as a real **Gmail draft** for the user to review, edit, and send. It is
written against the *actual* code in this repo and cites real files/functions.

The central design bet — and the thing that makes this safe — is that **creating a
draft is not a destructive or outward-facing action**. Nothing reaches the recipient
until a human hits Send. That lets us be aggressive about *generating* drafts
(even autonomously in the heartbeat daemon) while keeping a hard human gate on
*sending*, exactly the "human-in-the-loop" pattern the industry converged on.

---

## 1. Goals & non-goals

### Goals
- When the user opens an email that warrants a reply, **a draft is already waiting**,
  written in their voice and matched to their relationship with that sender
  (formal to the CEO, casual to a peer) — the Superhuman auto-draft experience.
- Cover the three highest-value triggers:
  1. **Reply-needed**: someone asked a question or made a request directed at the user.
  2. **Meeting requests**: "can we find time?" → a draft that proposes/accepts.
  3. **Follow-ups**: the user sent something, got no reply, and a nudge is due
     (we already track these in `FollowUp`, `storage.py:81`).
- Drafts read like the user. Learn voice from their **Sent** mail (greeting style,
  sign-off, formality, sentence length, vocabulary) rather than a generic template.
- **Safe by construction**: autodraft only ever *writes a Gmail draft*. Sending stays
  behind the existing confirm-first `/agent/send` gate (`server.py:3895`). No message
  leaves the machine to a recipient without an explicit human click.
- Privacy-first posture preserved: AI is off by default; autodraft is **opt-in** and
  requires `cloud` mode (composition needs a capable model). The feature degrades
  gracefully — disabled and clearly labeled — under `off`/`local`.
- Everything reversible: a draft is trivially deletable, and we log draft creation so
  the user can find/purge autodrafts in bulk.

### Non-goals (for now)
- **No auto-send.** Autodraft never sends. A separate, explicitly-gated "auto-send for
  high-confidence routine replies" is deferred (see §7, Phase 4) and would reuse the
  same confidence machinery.
- **No IMAP support initially.** Draft creation rides on `GmailClient.create_draft`
  (`gmail_client.py:512`); IMAP has no draft/send (`providers/imap.py`). Gmail-only,
  gated by `provider.supports("drafts")` (a new capability, §4).
- **Not a thread-reading UI.** We surface drafts where the user already reads mail
  (in Gmail itself, since they're real Gmail drafts) plus a lightweight review list
  in postmind. We do not rebuild a mail client.
- **No multi-recipient / reply-all drafting** in Phase 1. Single-recipient replies
  only, matching the existing single-recipient send validation (`server.py:3895`).

### Relationship to existing code — RECOMMENDATION: extend, don't fork
We already have most of the parts:
- `AIEngine.compose_email(intent, recipient_context, thread_snippet, soul)`
  (`ai_engine.py:509`) generates a `Subject: …\n\n<body>` draft, cloud-only, using a
  per-agent "soul" (`voice_style`, `user_context`, `writing_guidelines` on the `Agent`
  model, `storage.py:788`).
- `GmailClient.create_draft(to, subject, body, thread_id)` (`gmail_client.py:512`) and
  `.send(...)` (`gmail_client.py:460`) already exist.
- `draft_email` / `send_email` agent tools already stage editable/confirm cards
  (`agent_tools.py:137`), and the Super Agent already wires `compose_email` →
  `_split_draft` → confirm card (`server.py:3383`).
- The daemon already runs a per-account heartbeat with feature toggles and follow-up
  reply detection (`daemon.py:11`, `FollowUpTracker`).

Autodraft is the **proactive, voice-aware orchestration layer** over these: detect →
compose → persist as a Gmail draft → surface for review. We add (1) a reply-trigger
classifier, (2) a voice profile learned from Sent mail, (3) proper in-thread draft
threading, (4) a `DraftRecord` to track autodrafts, and (5) a daemon toggle + review UI.

---

## 2. Architecture

### 2.1 Pipeline
```
heartbeat / on-open / manual
        │
        ▼
[1] should_draft?   reply-trigger classifier (cloud classify, batched)
        │  yes
        ▼
[2] gather context  thread snippet + recipient relationship + voice profile
        │
        ▼
[3] compose         AIEngine.compose_email(intent, recipient_context, thread, soul)
        │
        ▼
[4] persist         GmailClient.create_draft(... thread_id, In-Reply-To, References)
        │            + DraftRecord row (status=ready, confidence, model)
        ▼
[5] surface         Gmail Drafts (native) + postmind review list (/drafts)
        │
        ▼
[6] human           review → edit → Send  (existing /agent/send confirm gate)
                    or dismiss → delete draft
```

### 2.2 Two entry points, one core
- **On-demand (Phase 1):** a "Draft a reply" button on an email / the Super Agent
  scenario "draft a reply to my boss's last email" (already listed as a goal in
  `super-agent-plan.md`). Synchronous, user is watching.
- **Proactive (Phase 3):** a new `run_autodraft` toggle on the `Agent` record (sibling
  of `run_rules`/`run_followups`, `storage.py:793`). The heartbeat (`daemon.py`) scans
  recent unread inbox, classifies for reply-need, and pre-creates drafts in the
  background so they're waiting when the user arrives.

Both call the same `AutodraftService` (new, `core/autodraft.py`) so behavior is
identical whether triggered by a click or the daemon.

### 2.3 Cloud vs local
`compose_email` raises `ValueError` unless `mode == "cloud"` (`ai_engine.py:521`).
Autodraft therefore requires cloud mode and is gated with `require_cloud()`
(`core/ai/mode.py:46`), which already emits the privacy warning that subjects +
snippets + **thread context** will be sent to Anthropic. Under `off`/`local` the
feature is visibly disabled with a one-line explanation, never silently degraded.

### 2.4 Voice profile (the "draft sounds like me" problem)
The single biggest quality lever per the research is voice matching. Two tiers:
- **Tier 1 (manual soul, exists today):** `Agent.voice_style` / `user_context` /
  `writing_guidelines`. Used as-is by `compose_email`. Ship Phase 1 on this.
- **Tier 2 (learned voice, Phase 2):** a `VoiceProfile` derived from the user's Sent
  mail — greeting pattern, sign-off, average reply length, formality, contraction
  use, recurring phrases — and a **per-recipient** override (formal vs casual based on
  prior history with that address). Stored as a new `VoiceProfile` row keyed by
  account (+ optional recipient bucket) and folded into the `soul` dict passed to
  `compose_email`. This is the Superhuman differentiator.

Hard constraints baked into the compose prompt (research-backed, see Sources):
- A **word-count ceiling** (models default ~2× too long without one).
- Style/contractions/formality knobs from the voice profile.
- A **blocklist** of filler phrases (the existing system prompt already forbids filler,
  `ai_engine.py:538` — extend it).
- A rule to **reference something specific** from the incoming thread, never generic.

---

## 3. Capability map (what exists vs. gaps)

| Need | Status | Where |
|---|---|---|
| Generate a draft body in voice | ✅ exists | `ai_engine.py:509` `compose_email` |
| Per-agent voice "soul" fields | ✅ exists | `storage.py:788` |
| Create a Gmail draft | ✅ exists | `gmail_client.py:512` `create_draft` |
| Send with confirm gate | ✅ exists | `server.py:3895` `/agent/send` |
| Stage/confirm card pattern | ✅ exists | `agent_tools.py:137`, `server.py:3383` |
| Heartbeat loop + toggles | ✅ exists | `daemon.py:11`, `storage.py:793` |
| Follow-up + reply detection | ✅ exists | `storage.py:81`, `FollowUpTracker` |
| **Reply-need classifier** | ❌ gap | new — `AutodraftService.should_draft` |
| **In-thread draft headers** (In-Reply-To/References) | ⚠️ partial | `create_draft` takes `thread_id` but must also set RFC-2822 headers + matching Subject |
| **Learned voice profile** | ❌ gap | new — `VoiceProfile`, learn from Sent |
| **DraftRecord tracking** | ❌ gap | new storage model |
| **`drafts` provider capability** | ❌ gap | extend `supports()` (`base.py`), Gmail=True / IMAP=False |
| **Review UI (`/drafts`)** | ❌ gap | new route/page |
| **MockAIEngine compose stub** | ❌ gap | tests need it (`mock_ai.py` has none) |

### 3.1 The threading gap (important)
Per the Gmail API docs, a draft only nests into the conversation if the message sets
`threadId` **and** the `In-Reply-To` + `References` headers to the original message-id
(`<id>` form) **and** keeps a matching `Subject` (`Re: …`). `create_draft`
(`gmail_client.py:512`) currently takes `thread_id` but we must verify/extend it to set
those headers, or the draft appears as a detached message. This is a concrete
implementation task, not a design choice.

---

## 4. Data model & API additions

### 4.1 `DraftRecord` (new, `storage.py`)
Tracks every autodraft so we can list, dedupe, expire, and report.
```
account_email, gmail_draft_id, thread_id, in_reply_to_msg_id,
to_email, subject, body_preview,
trigger ("reply_needed"|"meeting"|"followup"|"manual"),
confidence (0–1), model, voice_profile_version,
status ("ready"|"edited"|"sent"|"dismissed"|"stale"),
created_at, reviewed_at, expires_at
```
Repo: `DraftRepo` (sibling of `EmailRepo`/`FollowUpRepo`). Dedupe key:
`(account_email, thread_id)` — one open autodraft per thread, regenerated if the thread
gets a new inbound message.

### 4.2 `VoiceProfile` (new, Phase 2)
```
account_email, recipient_bucket ("default"|<domain>|<email>),
greeting, signoff, formality (0–1), avg_words, contractions (bool),
common_phrases_json, sample_count, learned_at
```

### 4.3 Provider capability
Add `"drafts"` to the known capabilities in `providers/base.py::supports`; `GmailProvider`
returns `True` (`gmail.py:75`), `IMAPProvider` returns `False`. All autodraft entry
points gate on `provider.supports("drafts")`.

### 4.4 Agent toggle
Add `run_autodraft: bool = False` to the `Agent` model (`storage.py:793`) and the daemon
cycle (`daemon.py`), mirroring `run_followups`. Off by default.

### 4.5 New service: `core/autodraft.py::AutodraftService`
```python
class AutodraftService:
    def should_draft(self, msg) -> tuple[bool, str, float]   # (yes, trigger, confidence)
    def build_context(self, thread) -> dict                  # snippet + recipient + voice
    def compose(self, msg, context) -> Draft                 # → AIEngine.compose_email
    def persist(self, draft, thread_id) -> DraftRecord       # → create_draft + DraftRecord
    def run_for_inbox(self, limit) -> list[DraftRecord]       # daemon + on-demand entry
```
Stateless analysis where possible (matches `agent_tools.py` philosophy); provider +
account injected per call (matches `server.py` request-scoped execution).

---

## 5. Safety & trust model

This is where autodraft earns the right to be proactive.

1. **Draft ≠ send.** Autodraft's only outward effect is a Gmail draft, which is private
   to the user's mailbox. No recipient is contacted. This is categorically safer than
   the destructive ops postmind already automates (trash/archive), so autonomous
   generation in the daemon is acceptable.
2. **Hard human gate on send.** Sending stays on the existing `/agent/send` path with
   single-recipient regex validation (`server.py:3895`). The model never sends; it
   only proposes. (Same trust boundary as `send_email` staging, `agent_tools.py:151`.)
3. **Server-resolved recipients.** The reply `to`/`thread_id` are resolved by *our*
   code from the cached/fetched thread, never from model free-text — the existing
   prompt-injection containment (`server.py:_resolve_action_targets`). An email body
   that says "reply to attacker@evil.com" cannot redirect the draft.
4. **Sensitive senders.** Reuse `sender_stats` risk flags: never autodraft for
   bank/legal/health/protected senders (mirrors the autopilot human-gate decision in
   `super-agent-plan.md`). Surface but require fully manual composition.
5. **Confidence threshold.** `should_draft` returns a confidence; only ≥ threshold
   become drafts proactively (research suggests calibrating so ~10–15% of borderline
   cases are skipped rather than over-drafting). Below threshold → no draft, optionally
   a "needs your attention" flag.
6. **Reversible & auditable.** Every autodraft is a `DraftRecord`; `/drafts` offers
   "dismiss all", and stale drafts (thread advanced, or > N days) auto-expire.
7. **Clear labeling.** Drafts are labeled/marked as postmind-generated so the user
   never mistakes an AI draft for something they wrote. A visible "✨ drafted by
   postmind — review before sending" banner.
8. **Privacy disclosure.** First use shows exactly what leaves the device (subjects +
   snippets + thread context → Anthropic) via `require_cloud()` (`core/ai/mode.py:46`).

---

## 6. UX

### 6.1 On-demand
- An email row / detail gains **"Draft a reply"**. Click → spinner → editable draft
  card (reuse the `draft_email` card shape, `agent_tools.py:137`) → **Save to Gmail
  drafts** or **Send** (confirm).
- Super Agent: "draft a reply to my boss's last email" resolves the boss's latest
  thread, calls `AutodraftService`, returns the editable card.

### 6.2 Proactive review list (`/drafts`)
A new page listing `DraftRecord`s with status `ready`:
- Per row: sender, subject, the draft body (inline-editable), confidence, trigger,
  "✨ drafted by postmind" badge.
- Actions: **Send** (confirm gate), **Edit & send**, **Dismiss** (delete Gmail draft +
  mark dismissed), **Regenerate** (different tone/length).
- Drafts also appear natively in Gmail's Drafts folder — the user can finish them on
  mobile. The review list is the postmind-side convenience, not the only surface.

### 6.3 Settings
- `/settings` and `/watch`: "Autodraft replies" toggle (per-agent `run_autodraft`),
  with confidence-threshold and word-ceiling knobs, plus the voice-profile editor
  (Tier 1 soul fields today; learned profile in Phase 2).

---

## 7. Phased roadmap

**Phase 1 — On-demand drafting (ship first).**
- `AutodraftService.compose` + `persist`; fix in-thread headers in `create_draft`.
- `drafts` capability gate; `DraftRecord` + `DraftRepo`.
- "Draft a reply" on-demand UI + Super Agent scenario.
- `MockAIEngine.compose_email` stub so tests run without a key.
- Manual voice via existing soul fields.
- *Outcome:* one-click, in-thread, voice-aware draft saved to Gmail.

**Phase 2 — Learned voice.**
- `VoiceProfile` learned from Sent mail; per-recipient formality bucketing.
- Word-ceiling + blocklist + "reference something specific" prompt hardening.
- *Outcome:* drafts that measurably sound like the user, tuned per relationship.

**Phase 3 — Proactive autodraft (daemon).**
- Reply-need classifier (`should_draft`) + confidence threshold.
- `run_autodraft` toggle in `Agent` + heartbeat integration (`daemon.py`).
- `/drafts` review list; dedupe + stale expiry.
- *Outcome:* drafts waiting before the user opens the inbox.

**Phase 4 — Opt-in auto-send (deferred, explicit).**
- For narrow, high-confidence, low-risk replies (e.g., meeting confirmations), an
  opt-in per-account setting that *sends* without the click — reusing the confidence
  machinery and never for sensitive senders. Separate red-team review before ship.

---

## 8. Testing

- `MockAIEngine.compose_email` returns a deterministic `Subject: …\n\n<body>` so the
  full pipeline is testable without an API key (autouse isolation per `conftest.py`).
- `should_draft` heuristic path in mock (regex for "?", "can you", meeting phrases),
  mirroring `mock_ai.py::_heuristic_parse` (`mock_ai.py:193`).
- Unit: in-thread header construction (In-Reply-To/References/Subject) — pure function,
  no network.
- Unit: sensitive-sender skip; confidence threshold; per-thread dedupe.
- Provider gating: autodraft refuses on IMAP (`supports("drafts") is False`).
- Use the `clean_db` fixture for `DraftRecord`/`VoiceProfile` tests.

---

## 9. Risks & open questions

- **Voice quality is the make-or-break.** A bland or wrong-tone draft is worse than no
  draft (deletion friction). Mitigation: word ceiling, learned voice, easy dismiss,
  start on-demand (Phase 1) before proactive so we tune quality before volume.
- **Threading correctness.** If In-Reply-To/References aren't right, drafts detach from
  the conversation — verify against a real Gmail account early.
- **Cost/latency of proactive drafting.** Composing for every reply-needed email is
  more expensive than classification. Mitigation: confidence gate + only recent unread
  + dedupe per thread + a daily cap per account.
- **Sarcasm/nuance misread** (noted across the research). Mitigation: human-in-the-loop
  by default; never auto-send in Phases 1–3.
- **Where does review happen — Gmail vs postmind?** RECOMMENDATION: both. Write a real
  Gmail draft (so it's there on mobile/native) *and* show it in `/drafts`. Open
  question only on which is the "primary" surface in the UI copy.
- **Stale drafts.** If the user replies in Gmail directly, our `DraftRecord` is stale.
  Mitigation: reconcile against thread state on heartbeat; expire on new inbound.

---

## Sources
- Superhuman Auto-Drafts (per-recipient voice, auto follow-up/meeting drafts):
  https://help.superhuman.com/hc/en-us/articles/40144492186515-Auto-Reminders-Auto-Drafts
  , https://help.superhuman.com/hc/en-us/articles/38456855116307-Write-with-AI
- Human-in-the-loop autonomy levels & confidence thresholds (target ~10–15% deferred):
  https://zapier.com/blog/human-in-the-loop/ ,
  https://parseur.com/blog/human-in-the-loop-ai
- Voice/tone matching & prompt structure (word-count ceiling, blocklist, specificity):
  https://wealthtechtoday.com/2026/04/22/ai-prompts-for-financial-advisors-client-emails/
  , https://www.newmail.ai/feeds/blog/ai-driven-email-personalization-scale
- Gmail API in-thread drafts (threadId + In-Reply-To/References + matching Subject):
  https://developers.google.com/workspace/gmail/api/guides/threads ,
  https://googleapis.github.io/google-api-python-client/docs/dyn/gmail_v1.users.drafts.html
- Gemini "Help Me Write" vs. proactive drafting (reactive vs. waiting-for-you):
  https://workspace.google.com/blog/product-announcements/new-ways-to-do-your-best-work
