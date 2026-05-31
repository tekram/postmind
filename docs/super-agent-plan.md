# Plan: Super Agent — postmind's natural-language email command center

Status: **Phases 1–2 SHIPPED** (branch `super-agent`). Phase 1 (NL command center,
READ tools, stage_trash, create_agent/create_rule), Phase 2 backend (generalized
archive/label/mark_read confirm flow, real unsubscribe, confirm-and-send), and
streaming (SSE step-cards) are implemented and tested; a security red-team review
was completed and its findings fixed (record-before-trash, blocklist-at-confirm,
same-origin CSRF guard, single-recipient send validation). Phase 3 (local tool-use
via Ollama, "never open" detection) and Phase 4 (convergence, opt-in auto-execute)
remain as future work. Author: codebase research + web research, May 2026.

This document specifies a dedicated "Super Agent" page where the user types
natural language and postmind plans + executes a wide range of inbox operations —
storage analysis, bulk trash/archive/label, unsubscribe, drafting/sending, and
**creating the heartbeat watcher agents conversationally** — with a confirm-first,
undoable safety model. It is written against the *actual* code in this repo and
cites real files/functions.

---

## 1. Goals & non-goals

### Goals
- One page, one input box: the user describes an outcome in plain English and the
  agent figures out which tools to call, in what order, to achieve it.
- Cover the five vision scenarios end to end:
  1. "find my largest email sizes" / "what's eating my storage"
  2. "delete everything from blah.com"
  3. "unsubscribe me from all newsletters I never open"
  4. "create an email agent that archives newsletters every week"
  5. "draft a reply to my boss's last email"
- Be **genuinely powerful but safe**: every destructive (WRITE) action routes
  through a confirm-first preview that reuses the existing `purge_preview` /
  `UndoLogRepo` machinery (Trash-only, 30-day undo).
- Stream the agent's steps (which tool is running, what it found) so the user sees
  reasoning, not a spinner — the "stream of thought" pattern.
- Preserve postmind's privacy-first posture: AI off by default; local mode keeps
  everything on device; the page must degrade gracefully when tools aren't
  available (local) or AI is off.

### Non-goals (for now)
- No third-party MCP integrations (Notion/Slack/Asana) like Shortwave. We scope to
  the user's mailbox only. (Noted as a future direction; the tool catalog is
  MCP-shaped so it could be exposed later.)
- No permanent delete from the agent. `batch_delete_permanent` exists on the
  provider (`providers/base.py:55`) but is intentionally **never** exposed as a
  tool — only Trash, which is undoable.
- No fully autonomous "auto-send" without confirmation in Phase 1 (Superhuman
  ships this as opt-in; we defer it behind an explicit per-account setting).
- Not a full email *client* (no thread reading UI). The agent operates on the
  scan/stats data model (`SenderGroup`) and provider batch ops.

### Relationship to the existing floating chat assistant — RECOMMENDATION: keep both, share a core, do NOT replace yet
The floating assistant (`POST /chat`, `_CHAT_TOOLS`, `_build_chat_system` in
`server.py:1629–1844`) is a deliberately *read-only/propose-only* helper available
on every page. It already has: a working agentic loop (`AIEngine.chat`), grounding
helpers (`_chat_overview_text`, `_chat_search_senders`, `_chat_resolve_senders`),
and the `propose_cleanup` → `/purge/preview` deep-link pattern.

The Super Agent is a *superset*: it adds WRITE tools (archive, label, unsubscribe,
send, create-agent) and a richer full-page streaming UI. Recommendation:

- **Phase 1:** Build the Super Agent as a new page that *reuses the same
  `AIEngine.chat` loop and the same tool-executor pattern*, with an **expanded tool
  registry**. Keep the floating assistant as-is (it stays read-only and is the
  "quick question from anywhere" surface).
- **Refactor:** Extract the tool registry + executor into a new module
  `postmind/core/agent_tools.py` so both the floating chat (read-only subset) and
  the Super Agent (full set) import from one source of truth. The floating
  assistant gets the read-only tools; the Super Agent gets all of them.
- **Later (Phase 4):** Once the Super Agent is proven, the floating widget can
  become a launcher that hands off to the Super Agent page for anything requiring
  a WRITE action. Do not delete the floating chat — it is genuinely lower-friction
  for "what's in my inbox" questions.

---

## 2. Architecture

### 2.1 The agent loop — reuse `AIEngine.chat`
`AIEngine.chat(messages, system, tools, tool_executor, ...)`
(`ai_engine.py:349–437`) is already a correct Anthropic tool-use loop with prompt
caching: it loops on `stop_reason == "tool_use"`, dispatches each block through
`tool_executor(name, input) -> str`, appends `tool_result` blocks, and caps at
`max_tool_iterations`. We **reuse it unchanged** for the non-streaming path, and
add a streaming variant (below).

This matches current best practice for agentic loops: a single master loop that
thinks → acts → observes → repeats until done, with reversible actions and human
review checkpoints
([Anthropic — effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)).

Changes needed to the engine:
- Raise `max_tool_iterations` for the Super Agent (e.g. 10–12) since multi-step
  plans (search → resolve → preview) chain several calls. Shortwave/Superhuman both
  rely on automatic multi-step follow-up search
  ([Shortwave AI Assistant docs](https://www.shortwave.com/docs/guides/ai-assistant/)).
- Add `chat_stream(...)` — a generator that yields structured events
  (`text_delta`, `tool_start`, `tool_result`, `done`) for the streaming UI
  (§2.3). The existing non-streaming `chat` stays for the floating assistant.

### 2.2 Cloud vs local handling
Tool use is **cloud-only today**: `AIEngine.chat` ignores tools in local mode
(`ai_engine.py:370–380`, "Ollama tool-use is unreliable across models"). For the
Super Agent:

- **Cloud (Anthropic):** full tool catalog, full power. This is the primary target.
- **Local (Ollama):** two options, pick based on effort budget:
  - **Phase 1 (recommended, low effort): degrade to "plan, don't execute."** In
    local mode the page runs the existing local conversational path and uses the
    NL→structured parsers we already have (`parse_bulk_intent`, `translate_rule`)
    to *propose* an action card that deep-links into `/purge/preview` or the agent
    create form — i.e. the same confirm-first surfaces, just without an autonomous
    loop. The user still gets value ("here's what I'd do — confirm it") without
    relying on flaky local tool-use.
  - **Phase 3 (optional): local tool-use via Ollama's native tools API.** Newer
    models (e.g. `qwen2.5:32b`, already the default in `_persist_ai_mode`) support
    `/api/chat` `tools`. Gate behind a capability flag; fall back to the Phase-1
    degrade path on parse failure.
- **Off:** the page shows the same Settings-guidance card the floating assistant
  returns (`server.py:1766–1770`) — no model call, data stays local.

### 2.3 Streaming the loop (new)
The floating assistant is request/response. The Super Agent should stream so the
user sees each tool fire — this is the single biggest UX win and builds trust
([ShapeofAI — Stream of Thought](https://www.shapeof.ai/patterns/stream-of-thought)).

- Endpoint `POST /agent/stream` returns `text/event-stream` (SSE). FastAPI supports
  this via `StreamingResponse` over a sync generator run in the executor.
- The engine's `chat_stream` consumes Anthropic's SSE
  (`messages.stream`): accumulate `input_json_delta` partial JSON for tool inputs,
  finalize on `content_block_stop`, run the tool, emit a `tool_result` SSE event,
  continue the loop
  ([Anthropic streaming docs](https://docs.anthropic.com/en/docs/build-with-claude/streaming)).
- The client renders each tool call as a collapsible step (search_senders → "12
  matches", analyze_storage → table) and the final text token-by-token. WRITE tools
  emit an **action card** event instead of auto-running (see §4).

---

## 3. Capability map — what exists vs. gaps

| Capability | Exists today | Where | Wired to web? |
|---|---|---|---|
| List/scan senders, sizes, reclaimable MB | yes | `sender_stats.fetch_sender_groups(_from_db)`, `SenderGroup.total_size_mb/count/message_ids` | yes (stats, chat overview) |
| Confidence / risk scoring | yes | `compute_confidence_score`, `classify_sender_risk`, `sender_risk_tier_from_conf` | yes |
| Domain grouping | yes | `group_by_domain` / `DomainGroup` | yes (stats) |
| Recommendations | yes | `generate_recommendations` | yes |
| Trash (bulk) | yes | provider `batch_trash`, `/purge/confirm` + `UndoLogRepo` | **yes** (purge flow) |
| Archive (bulk) | yes (provider) | `batch_archive`, `BulkEngine._execute_action` | **NO web endpoint** — gap |
| Label (bulk) | yes (provider) | `batch_label`, `get_or_create_label` | **NO web endpoint** — gap |
| Mark read | yes (provider/bulk) | `batch_label` remove UNREAD | gap |
| Undo | yes | `UndoLogRepo.record/list_recent`, `BulkEngine.undo`, `/undo` | yes (trash/archive/label/mark_read all reversible per `BulkEngine.undo`) |
| Unsubscribe (real) | yes | `UnsubscribeEngine` (List-Unsubscribe + RFC 8058 + headless), SSRF-guarded `_is_safe_url` | **CLI-only** — not in web; `BulkEngine` *falls back to archive* (`bulk_engine.py:256`) — gap |
| Draft email (soul-aware) | yes | `AIEngine.compose_email`, `/agents/compose` | yes (draft only, no send) |
| **Send** email | partial | `client.send` exists (used by unsubscribe mailto) | **no web send path for user mail** — gap |
| NL → bulk op | yes | `AIEngine.parse_bulk_intent` → `BulkEngine.preview/execute` | partially (CLI/bulk) |
| NL → recurring rule | yes | `AIEngine.translate_rule`, `BulkEngine.create_rule`, `RuleRepo` | rules run in daemon |
| Create/config heartbeat agent | yes | `AgentRepo.register/update_soul/update_features/set_active`, daemon | yes (forms on /agents) |
| Protected senders (blocklist) | yes | `BlocklistRepo` | yes (settings/blocked) |
| Provider capability gate | yes | `EmailProvider.supports('labels'|'unsubscribe'|...)` | partial |

**Net gaps the Super Agent must close (these become small new endpoints/modules):**
1. A confirm-first **archive/label/mark_read** flow (mirror `/purge/preview` +
   `/purge/confirm`, generalized to any reversible action).
2. A web **unsubscribe** path that calls the real `UnsubscribeEngine` (not the
   archive fallback) and records to `UnsubscribeRecord`.
3. A web **send-draft** path (compose already exists; add a confirm-and-send step).
4. A conversational **create-agent** tool that drives `AgentRepo`.

---

## 4. Tool catalog

All tools live in a new `postmind/core/agent_tools.py`: each as an Anthropic
tool schema + a Python handler. The web `tool_executor` closure (mirroring
`server.py:1782–1833`) dispatches by name and has the request-scoped
`account_email`, provider, and an `actions`/`cards` accumulator in closure.

**Classification rule:** READ tools run immediately and silently. WRITE tools
**never execute inside the loop** — they emit a *staged action card* with a token
the user must confirm via a separate endpoint. This is the spotlighting /
least-privilege / human-checkpoint pattern recommended for agents handling
untrusted content
([Anthropic prompt-injection defenses](https://www.anthropic.com/research/prompt-injection-defenses);
[OWASP LLM01 Prompt Injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)).

### READ tools (auto-run, no confirmation)
| Tool | Inputs | Reads / does |
|---|---|---|
| `get_inbox_overview` | — | Reuse `_chat_overview_text`. Sender count, totals, reclaimable MB, top senders. |
| `analyze_storage` | `top_n`, `group_by` ("sender"\|"domain") | Largest consumers by `total_size_bytes`; reuse `fetch_sender_groups`/`group_by_domain` + `total_size_mb`. Answers "what's eating my storage". |
| `search_senders` | `query` | Reuse `_chat_search_senders` / `_chat_resolve_senders` → matched `SenderGroup`s with counts/size/risk. |
| `find_largest_messages` | `limit`, `query` | `list_message_ids` + `get_messages_metadata` sorted by `size_estimate`. Answers "find my largest email sizes". |
| `classify_recent` | `limit` | `AIEngine.classify_emails` over recent metadata for "which newsletters do I never open" (combine with avoidance/last-opened signals already tracked by `EmailRepo`/avoidance). |
| `get_rules` / `get_agents` | — | `RuleRepo.list_active`, `AgentRepo.get_by_email` — current automation state. |
| `preview_bulk_action` | `gmail_query`, `action` | `BulkEngine.preview`-style dry run: resolves message IDs + sample + estimated MB **without** changing anything. Feeds the action card. |

### WRITE tools (stage → confirm card → execute) — all reversible only
| Tool | Inputs | Does | Confirm + undo |
|---|---|---|---|
| `stage_trash` | `senders[]` \| `query` \| `gmail_query` | Resolve targets; emit card. On confirm → `batch_trash` + `UndoLogRepo.record(operation="trash")`. | Reuse `/purge/preview` + `/purge/confirm` verbatim. Undo = `batch_untrash`. |
| `stage_archive` | same | New generalized preview/confirm. `batch_archive`. | New `/agent/action/preview` + `/confirm`. Undo = re-add INBOX (`BulkEngine.undo`). |
| `stage_label` | same + `label_name` | `get_or_create_label` + `batch_label`. | Same generalized flow. Undo = remove label. |
| `stage_mark_read` | same | `batch_label(remove=["UNREAD"])`. | Undo = re-add UNREAD. |
| `stage_unsubscribe` | `senders[]` \| `query` | Resolve senders; emit card listing each. On confirm → real `UnsubscribeEngine.batch_unsubscribe` (header → one-click → headless), record `UnsubscribeRecord`. Optionally also `stage_trash` the back catalog. | Confirm card lists method per sender; SSRF-guarded by `_is_safe_url`. Unsubscribe itself isn't undoable (external) — card states this explicitly; the *trash* of existing mail is. |
| `draft_email` | `intent`, `recipient_context`, `thread_snippet` | Reuse `compose_email` (soul-aware via `AgentRepo`). Returns draft into an editable card. READ-ish (produces text, sends nothing). | — |
| `send_email` | `to`, `subject`, `body`, `reply_to_msg_id?` | Emit a card with the editable draft + recipient. On confirm → `client.send`. | Always-confirm. No auto-send in Phase 1. |
| `create_agent` | `email`, `name`, `interval_minutes`, `voice_style?`, `user_context?`, `writing_guidelines?`, `run_rules?`, `run_followups?`, `run_avoidance?` | Emit a card summarizing the agent. On confirm → `AgentRepo.register` + `update_soul` + `update_features`. Answers "create an email agent that archives newsletters every week". | Confirm card; fully reversible via `/agents/delete`. |
| `create_rule` | `natural_language` | `AIEngine.translate_rule`; emit card showing the generated `gmail_query` + action + warnings. On confirm → `BulkEngine.create_rule` (`RuleRepo`). Pairs with `create_agent` (rules run when the agent's `run_rules` is on). | Confirm card surfaces `NLRule.warnings`. Reversible (delete rule). |

**Protected-sender guard:** before staging any WRITE, the executor filters targets
through `BlocklistRepo` and shows skipped senders on the card. `classify_sender_risk`
"sensitive" tier (banks, government, legal — `sender_stats.py:384`) is surfaced as a
warning and pre-unchecked on the confirm card.

---

## 5. Safety model

1. **Auto-execute vs. always-confirm.** READ auto-runs. *Every* WRITE requires an
   explicit human confirm on a card — no exceptions in Phase 1. This is the human
   checkpoint + least-privilege guardrail from the agent-safety literature
   ([OpenAI — designing agents to resist prompt injection](https://openai.com/index/designing-agents-to-resist-prompt-injection/)).
2. **Trash-only deletes, 30-day undo.** No `batch_delete_permanent` tool. All
   destructive ops record to `UndoLogRepo` *before* acting (the pattern in
   `bulk_engine.py:111` and `/purge/confirm`), reversible from `/undo`.
3. **Dry-run / preview everywhere.** `preview_bulk_action` and the staged cards show
   exact counts + estimated MB + sample subjects before anything happens — reusing
   `BulkPreview`.
4. **Protected senders + risk tiers.** `BlocklistRepo` senders are never staged;
   sensitive-risk senders are flagged and opt-in only.
5. **Prompt-injection containment.** Email bodies/snippets are *untrusted input*.
   The model reads them, but they must not be able to trigger a WRITE:
   - WRITE tools only *stage* — they cannot complete without the user clicking a
     confirm card. An injected "delete everything" in an email body can at most
     produce a card the user will reject.
   - Confirm tokens are server-side and bound to a resolved, explicit target list
     (sender emails / message IDs) computed by *our* code, not free-form text from
     the model — so the model can't smuggle a different target into execution.
   - **Spotlighting:** untrusted email content is wrapped in clearly delimited
     blocks in the system/tool-result text with an instruction that content inside
     is data, never instructions
     ([OWASP Prompt Injection Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)).
   - **Intent re-check:** before rendering a confirm card, a cheap guard compares the
     staged action against the *original user request* (not the email content); a
     drift (e.g. user asked to unsubscribe, agent staged a trash of a different
     domain) downgrades to a warning. Keep the trusted:untrusted context ratio
     sane — never let a single huge email dominate the window.
6. **Local-only privacy guarantee.** In local/off mode, no inbox content leaves the
   device. The page banner states the active mode (mirrors floating assistant
   `_build_chat_system` line "AI mode: {mode}").
7. **SSRF** on unsubscribe is already handled by `unsubscribe._is_safe_url` (blocks
   private/link-local/metadata ranges, no redirect-follow) — we reuse it unchanged.

---

## 6. UI / UX design

**Page:** `GET /agent` → `templates/agent.html` (extends `base.html`, adds nav item).

**Layout (full page, three zones):**
- **Top — command input.** Large textarea with placeholder cycling the five example
  prompts; Enter to run, Shift+Enter newline. Chip row of one-tap examples
  ("What's eating my storage?", "Unsubscribe from newsletters I never open",
  "Archive newsletters weekly"). A small mode badge (cloud/local/off).
- **Middle — streamed run.** Each turn renders as a vertical timeline of
  **step cards** (collapsible accordion, the chain-of-thought UI pattern):
  - tool-call steps: icon + tool name + one-line result ("analyze_storage → top 10
    senders, 2.1 GB reclaimable"), expandable to the table/detail.
  - assistant text streamed token-by-token below the steps.
  - **action cards** for WRITE tools: a bordered card with the resolved target list
    (checkboxes, protected/sensitive ones flagged), counts + estimated MB, and a
    primary **Confirm** button + **Cancel**. Confirm posts the server token; on
    success it collapses into a result row with an **Undo** link to `/undo`.
- **Bottom — sticky composer** for follow-ups (conversation persists in the run).

**htmx vs richer JS — RECOMMENDATION: vanilla JS for the stream, htmx for cards.**
The existing app mixes both, and the floating widget already uses vanilla JS for
dynamic append/history (per `chat-assistant-plan.md`). Streaming SSE + token append
fits htmx poorly, so:
- Vanilla JS owns the EventSource/SSE connection, step-card rendering, and history
  (localStorage, like `postmind_chat_v1`).
- The confirm/cancel buttons inside action cards are plain `hx-post` to the
  confirm endpoint (htmx swaps the card to a result row) — reuses the
  `purge_preview.html` form idiom.

### Example end-to-end interactions
1. **"what's eating my storage?"** → `analyze_storage(group_by=domain)` step shows a
   table; assistant summarizes "linkedin.com 1.2 GB across 480 emails, you haven't
   opened any since March." Offers a `stage_trash`/`stage_archive` card. No deletion
   until Confirm.
2. **"delete everything from blah.com"** → `search_senders("blah.com")` →
   `stage_trash(query="blah.com")` → card lists matched senders + 312 emails, ~90 MB,
   `support@blah.com` flagged (looks transactional). User unchecks it, Confirms →
   trashed, Undo link appears.
3. **"unsubscribe me from newsletters I never open"** → `classify_recent` +
   open-rate signal → list of N newsletters → `stage_unsubscribe` card (per-sender
   method shown) + optional "also trash the back catalog" toggle → Confirm runs
   `UnsubscribeEngine.batch_unsubscribe`, records results.
4. **"draft a reply to my boss's last email"** → `search_senders(boss)` →
   `find_largest_messages`/latest → `draft_email` returns an editable draft card in
   the user's soul voice → user edits → `send_email` card → Confirm → `client.send`.
5. **"create an email agent that archives newsletters every week"** (the agent-build
   flow) → agent asks 1–2 clarifying Qs (which account? voice?) →
   `create_rule("archive newsletters")` shows generated `gmail_query`
   (`category:promotions OR label:newsletters`) + action=archive + warnings →
   `create_agent(interval, run_rules=on)` card summarizing → Confirm → `AgentRepo`
   rows created; assistant links to `/agents` and `/watch` to start the daemon.

---

## 7. Phased roadmap

**Phase 1 — thin slice, maximum reuse (≈3–5 days).** Cloud-only, non-streaming.
- New `agent_tools.py`: READ tools (`get_inbox_overview`, `analyze_storage`,
  `search_senders`, `find_largest_messages`) + `stage_trash` reusing the *existing*
  `propose_cleanup` → `/purge/preview` deep-link. `create_agent` + `create_rule`
  tools.
- New `GET /agent` page + `POST /agent` (clone of `chat_endpoint`, expanded tools,
  bigger `max_tool_iterations`). Reuse `_chat_*` helpers and the executor pattern.
- Confirm-first via existing purge flow; agent creation via existing `/agents/*`.
- Deliverable: type NL → analyze storage, stage trash (confirm in existing
  preview), and create a watcher agent + rule conversationally.
- Dependencies: none new beyond Anthropic (already present).

**Phase 2 — generalized actions + send + streaming (≈4–6 days).**
- New `POST /agent/action/preview` + `/agent/action/confirm` generalizing
  `/purge/*` to archive/label/mark_read (drive `BulkEngine._execute_action`,
  record undo). Wire `stage_archive/label/mark_read`.
- Web unsubscribe path → `UnsubscribeEngine` (close the archive-fallback gap);
  `stage_unsubscribe`.
- `send_email` confirm-and-send endpoint; editable draft card.
- `AIEngine.chat_stream` + `POST /agent/stream` (SSE) + step-card UI.

**Phase 3 — local tool-use + smarter targeting (≈3–5 days).**
- Optional Ollama native tool-use behind a capability flag; degrade path retained.
- "Never open" detection using `EmailRepo`/avoidance open signals for better
  unsubscribe/cleanup recommendations.
- Intent-drift guard + spotlighting hardening.

**Phase 4 — convergence + power (optional).**
- Floating assistant hands off WRITE intents to `/agent`.
- Opt-in auto-execute for low-risk recurring actions (per-account setting),
  Superhuman-style autopilot, still undoable.

**New endpoints/modules summary:**
`postmind/core/agent_tools.py` (registry + handlers); `templates/agent.html`;
`server.py`: `GET/POST /agent`, `POST /agent/stream`,
`POST /agent/action/preview`, `POST /agent/action/confirm`,
`POST /agent/unsubscribe/confirm`, `POST /agent/send`, `POST /agent/create-agent`;
`AIEngine.chat_stream`.

---

## 8. Risks & open questions

- **Local tool-use reliability.** Biggest unknown. Mitigation: Phase-1 degrade to
  parse-and-propose; treat local autonomous loop as optional.
- **Estimated-MB accuracy.** `BulkPreview` extrapolates from a 5-message sample
  (`bulk_engine.py:74`); for "largest" queries pull real `size_estimate` per message
  instead of extrapolating.
- **Prompt injection via email bodies.** Contained by stage-then-confirm + tokens
  bound to server-resolved targets, but worth a red-team pass before exposing
  `send_email`/`stage_unsubscribe`.
- **Provider parity.** IMAP `batch_untrash`/labels are best-effort
  (`providers/base.py:72`); gate label/archive tools on `supports('labels')` and
  show a clear message otherwise.
- **Cost.** Multi-step cloud loops cost more tokens; prompt caching (already in
  `_chat_cloud`) helps. Cap iterations and message history (12, as `/chat` does).

### Recommendations
1. Ship Phase 1 cloud-only, reusing the purge preview and `/agents/*` forms — it
   proves the loop with near-zero new safety surface.
2. Extract `agent_tools.py` first so the floating chat and Super Agent share one
   registry.
3. Make stage-then-confirm the *only* path to any WRITE; bind confirm tokens to
   server-resolved target lists.
4. Defer auto-send and local autonomous tool-use; both are opt-in, later phases.

---

## Sources
- [Anthropic — Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- [Anthropic — Advanced tool use](https://www.anthropic.com/engineering/advanced-tool-use)
- [Anthropic — Streaming messages](https://docs.anthropic.com/en/docs/build-with-claude/streaming)
- [Anthropic — Mitigating prompt injection in browser use](https://www.anthropic.com/research/prompt-injection-defenses)
- [OpenAI — Designing agents to resist prompt injection](https://openai.com/index/designing-agents-to-resist-prompt-injection/)
- [OWASP — LLM Prompt Injection Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)
- [OWASP GenAI — LLM01:2025 Prompt Injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)
- [ShapeofAI — Stream of Thought pattern](https://www.shapeof.ai/patterns/stream-of-thought)
- [assistant-ui — Chain of Thought UI](https://www.assistant-ui.com/docs/guides/chain-of-thought)
- [Shortwave — AI Assistant docs](https://www.shortwave.com/docs/guides/ai-assistant/)
- [Superhuman — Ask AI](https://help.superhuman.com/hc/en-us/articles/38458628979091-Ask-AI)
- [Superhuman — AI-native mail (Autopilot)](https://superhuman.com/products/mail/ai)
