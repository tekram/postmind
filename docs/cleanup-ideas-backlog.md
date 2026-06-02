# Cleanup ideas backlog

Future AI-cleanup directions beyond the active plan
([smart-cleanup-batches-plan.md](smart-cleanup-batches-plan.md), which covers
**faster bulk decisions**). These three came up during planning and are worth
keeping. Each notes the signals/code we already have so they're cheap to start.

---

## 1. Less repetitive work — a learning cleanup loop

Stop making the user clean up the same thing twice. Largely overlaps with Phase 3
of the active plan, but stands alone as a theme.

- **Learn keep-vs-trash per sender/category.** We already capture the signals —
  `view_count`, `is_acted_on` (`EmailRecord`), `undo_log` (reversals = "you got
  this wrong"), `sender_blocklist`. None of it currently feeds `build_cleanup_plan()`
  (`sender_stats.py:1181`), which is purely deterministic. Store an accept-rate
  prior per sender and nudge confidence with it.
- **Proactive recurring rules.** After the user clears the same kind of mail ~3
  sessions running, offer "always do this automatically?" → create a `rules` entry
  (reuse `translate_rule()`, `ai_engine.py:235`). This is what trends repeat work
  toward zero.
- **Subscription lifecycle.** Detect "subscribed but never opened in 90 days" and
  bundle unsubscribe + back-catalog trash in one move (extends the existing
  `UnsubscribeEngine` + "never opened" detection from `super-agent-plan.md`).

---

## 2. More trust to act — "what you'd lose" safety summaries

The blocker to bulk action is fear of deleting something important. Make AI raise
confidence instead of just asserting it.

- **Pre-deletion digest per batch.** Before trashing, AI summarizes "what's in
  here and what you'd lose" from the body-free digest — reuses the
  `cleanup_plan_digest()` privacy contract (`sender_stats.py:1298`).
- **Hidden-gem flagging.** Scan a candidate batch for anything that looks
  action-required / personal / has a deadline (`classify_emails()` already emits
  `requires_reply` + `deadline_hint`, `ai_engine.py:153`) and pull it *out* of the
  batch with a warning rather than letting it get swept away.
- **Surface, don't hide, contradictions.** If a "safe to trash" sender has a
  recent reply from the user, flag it — that's a signal it matters.

---

## 3. A smarter chat agent for cleanup

Today the chat's `propose_cleanup` tool (`server.py`) is a thin wrapper that stages
one sender purge. Make it genuinely agentic.

- **Multi-step momentum.** "Keep going" continues clearing the next batch without
  re-prompting; the agent narrates progress as it goes.
- **In-chat preview + undo.** Show the preview inline and let the user undo the
  last action from the chat (the `UndoLogRepo` path already supports it).
- **Preference memory.** Remember "never touch mail from my lawyer" across sessions
  and apply it silently — same prior store as idea #1.

---

## Notes

- All three preserve the existing guarantees: body-free AI, sensitive senders never
  auto-batched (`classify_sender_risk()`), confirm-first + 30-day undo
  (`UndoLogRepo`), and graceful degradation when AI is off / local-only.
- Natural sequencing: ship the active batches plan first, then **#2 (trust)** since
  it directly de-risks bulk action, then **#1 (learning)** for the compounding
  payoff, then **#3 (chat)** as the conversational surface over all of it.
