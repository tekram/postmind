# Plan: Agent harness strategy — MCP-ify the tools, fix local tool-use, don't swap the loop

Status: **Phases 1 & 2 SHIPPED**; Phase 3 deliberately deferred (see §6). Author: codebase
+ web research, June 2026. Companion to `docs/super-agent-plan.md`.

Shipped in this branch:
- **Move #1 (MCP server).** `postmind/core/agent_service.py` — a harness-independent
  resolve→stage→confirm→execute engine — plus `postmind/core/agent_mcp.py`, a FastMCP
  server exposing the full tool catalog (6 READ + 8 stage + confirm/cancel/list). Run with
  `postmind mcp` (stdio); `pip install 'postmind[mcp]'`. The stage→confirm boundary and
  server-resolved targets live in the service, so the guarantee holds for *any* MCP host.
- **Move #2 (local tool-use).** `AIEngine._chat_local_tools` now drives the
  OpenAI-compatible `/v1/chat/completions` tools API (Ollama/llama.cpp/LM Studio/vLLM),
  which local tool-callers are actually tuned on, with the same graceful fallback.

This document answers a concrete question: *should postmind adopt a third-party agentic
harness (Claude Agent SDK, OpenAI Codex, OpenCode, Goose, …) — ideally driving a local
LLM — to make the Super Agent "super good"?* It is written against the actual code in this
repo and cites real files/functions.

**Bottom line up front:** No wholesale swap. The leverage is not in the loop (we already
have a good one). It is in **(1) exposing our tool catalog as an MCP server** so *any*
harness can drive postmind while our safety boundary stays harness-independent, and
**(2) fixing local tool-calling reliability** directly. A third option — swapping our
hand-rolled loop's *implementation* for a thin typed library (Pydantic AI) behind the
existing `AIEngine` seam — is reasonable but lower priority.

---

## 1. What we already have

postmind is **not** starting from zero. The agent loop exists and is solid:

- `AIEngine.chat(...)` (`core/ai_engine.py:557`) — Anthropic tool-use loop with prompt
  caching on the system prompt + last tool (`_chat_cloud`, `ai_engine.py:660`), capped at
  `max_tool_iterations`.
- `AIEngine.chat_stream(...)` (`ai_engine.py:716`) — streaming sibling that yields
  structured `text_delta` / `tool_start` / `tool_result` / `done` events for the SSE
  step-card UI (`POST /agent/stream`).
- `AIEngine._chat_local_tools(...)` (`ai_engine.py:603`) — local tool-use against Ollama's
  `/api/chat` `tools` API, with graceful fallback to plain conversation on any failure
  ("Ollama tool-use support varies by model").
- `core/agent_tools.py` — the **single source of truth for tool schemas**, deliberately
  free of web imports so any caller can reuse it. READ tools run immediately; WRITE tools
  only *stage*.
- `core/ai/mode.py` — `off` / `local` / `cloud` enforcement in one place.

**The crown jewel is the safety boundary, not the loop** (`agent_tools.py:9–16`,
`super-agent-plan.md` §5):

- READ tools run silently; **WRITE tools never execute inside the loop** — they stage an
  action and emit a confirm card.
- Confirm targets (sender emails, message IDs) are **always server-resolved by our code**,
  never free-form text from the model — this contains prompt injection from untrusted email
  bodies.
- Trash-only deletes, undo logs (`UndoLogRepo`), blocklist + risk-tier gating
  (`BlocklistRepo`, `classify_sender_risk`), same-origin CSRF guard.
- Two red-team passes done; all findings fixed.

Any plan that puts this boundary at risk is a regression, regardless of how shiny the
harness is.

---

## 2. The question, decomposed

"Make it super good with an agent harness + local LLM" bundles two *separable* goals:

- **(A) A better orchestration loop** — planning, multi-step chaining, subagents, context
  compaction, session persistence, hooks.
- **(B) A better local-model experience** — so cloud (Anthropic) isn't required for the
  powerful path.

These have different answers. Conflating them leads to "let's adopt the Claude Agent SDK,"
which helps (A) a little, **hurts** (B) a lot, and threatens the safety boundary.

---

## 3. Why a wholesale harness swap is the wrong move

### 3.1 Generic coding harnesses have the opposite design center
Claude Agent SDK, Codex, OpenCode, Goose are built for an **autonomous agent that runs
bash and edits files** under broad permissions. postmind's entire value prop is the
inverse: *nothing destructive happens without a human click, and the model can never name
its own targets.* Adopting one means re-implementing our confirm-first boundary on top of a
framework that wants to act on its own — strictly more code and more attack surface.

### 3.2 Feature map — most SDK wins are things we don't want here

| SDK feature (Claude Agent SDK) | Need it in postmind? |
|---|---|
| Built-in bash / file / web tools | **No** — this is exactly the attack surface we closed. |
| Permission system (`allowedTools`, modes) | We have a better, domain-specific one (stage→confirm + blocklist + risk tiers). |
| Context compaction / auto-summary | Marginal — turns cap at 12, system prompt is cached. |
| Subagents | *Maybe* nice later (a "research senders" subagent); not core. |
| Hooks / session persistence | Minor convenience over our request-scoped executor. |
| **Local model support** | **The thing we most want — and it has no first-class path.** |

### 3.3 The Claude Agent SDK can't do local first-class
Confirmed via docs research (June 2026): the Agent SDK is hardwired to Anthropic's API.
Local Ollama works only through an `ANTHROPIC_BASE_URL` proxy hack or third-party LiteLLM —
explicitly not a supported path. So the SDK fails goal (B) and locks us to Anthropic for
goal (A). Bad trade.

### 3.4 Codex CLI / coding-CLI harnesses
Codex CLI runs local models (`gpt-oss` via Ollama) but is a **standalone tool, not a
library**, and has **no MCP**. OpenCode/Goose/Aider are model-agnostic (Ollama +
OpenAI-compatible) and MCP-capable, but they are *harness binaries*, not embeddable loops —
useful as **clients** of postmind (see §5), not as a replacement for our in-app loop.

---

## 4. The open-source landscape (reference)

Two distinct categories — keep them separate:

**Coding CLIs / harness binaries** (model-agnostic, terminal-first, mostly MCP-capable):
- **OpenCode** — 2026 breakout; 75+ model endpoints; Ollama + OpenAI-compatible; MCP;
  plan (read-only) vs build agents.
- **Goose** (Linux Foundation / Agentic AI Foundation) — general-purpose agent; 15+
  providers incl. **Ollama**; strong MCP "extensions" model.
- **Aider** — Git-native; Ollama + OpenAI-compatible; simpler.
- **Pi / Crush** — minimal harnesses; Pi's "lazy skills" (load tool schemas only on use) is
  a clever context-saver worth borrowing conceptually.
- **Codex CLI** — native Ollama; open client; **no MCP**; standalone tool.

**Agent libraries** (embeddable, like our own loop):
- **Pydantic AI** — typed tools/outputs, FastAPI-style DX, multi-provider via LiteLLM,
  local-friendly. *The most relevant to us* if we ever stop hand-maintaining the loop.
- **LangGraph** — durable state, human-in-the-loop checkpoints, branching; powerful but
  heavy; overkill until flows get genuinely multi-stage.
- **smolagents** (HF) — code-execution agents; wrong shape (we don't want the model writing
  & executing code).
- **CrewAI** — multi-agent orchestration; not our problem yet.

**Local model reality:** the binding constraint is the *model*, not the framework.
Credible local tool-callers in 2026: Qwen3 / Qwen-72B, Llama 3.3, DeepSeek-V3.2, gpt-oss;
small native function-callers (Phi-4-mini, Falcon 3) run on Apple Silicon.

---

## 5. Recommended direction

### 5.1 Move #1 — Expose `agent_tools.py` as an MCP server (the strategic win)
The `super-agent-plan.md` already notes the catalog is "MCP-shaped." Make it literal.

- Wrap the READ/WRITE tool schemas + handlers as an **MCP server** (stdio + HTTP),
  keeping **stage→confirm and server-resolved targets inside the server**. The WRITE tools
  return a *staged action descriptor with a server-issued confirm token*; execution stays
  behind a separate confirm call that re-resolves targets from our data — exactly today's
  boundary, just exposed over MCP.
- Payoff:
  - **Our own loop drives it unchanged** (tool_executor calls the same handlers).
  - **Every external harness can drive postmind** — Claude Agent SDK, OpenCode, Goose,
    Codex(? no MCP), Claude Desktop, Cursor — without us coupling to any one of them.
  - **The safety guarantee is harness-independent** because it lives in the tool layer, not
    the loop. This is the correct architecture and we are ~80% there.
- Scope guardrails: expose **only** the domain tools; never bash/file/web. Confirm tokens
  remain single-use and bound to a resolved target list. Mode enforcement (`require_local`
  / `require_cloud`) wraps the server entry. Local/off mode degrades exactly as the floating
  assistant does.

### 5.2 Move #2 — Make local "super good" by fixing tool-calling (not the harness)
Today `_chat_local_tools` raises on flaky models and degrades to plain chat. Attack the
reliability directly:

- Switch local tool-use to Ollama's **OpenAI-compatible `/v1/chat/completions` with
  `tools`** (more robust across models than `/api/chat`); default to a strong tool-caller
  (Qwen3 / Llama 3.3).
- Apply **grammar / constrained decoding** for tool arguments — we *already do this* for the
  llama.cpp classifier (`core/llm.py`, grammar-constrained `S/C/A` output). The same
  technique makes local tool-arg JSON reliable.
- **Lean on the safety net:** because every WRITE only *stages*, a local model that picks
  the wrong tool produces a card the user rejects — harmless. Local can be "good enough"
  *precisely because* of the boundary. Keep the graceful-fallback path as the floor.

### 5.3 Move #3 (optional, lower priority) — Pydantic AI as the loop implementation
If we tire of maintaining `_chat_cloud` / `chat_stream` by hand, swap the **implementation**
(not the interface) for Pydantic AI behind the existing `AIEngine` seam: typed tools,
multi-provider (cloud + local via LiteLLM), streaming. The rest of the app keeps calling
`AIEngine.chat(...)`. Do this only if maintenance cost actually bites; it's not required for
"super good."

### 5.4 What NOT to do
- Do **not** rebuild postmind on the Claude Agent SDK (Anthropic lock-in, no first-class
  local, brings unwanted bash/file tools).
- Do **not** embed a coding-CLI harness (OpenCode/Goose/Codex) as the in-app engine — wrong,
  dangerous design center. They are fine as *external MCP clients* of our server (§5.1).
- Do **not** let any harness resolve WRITE targets from model free-text. Ever.

---

## 6. Phased roadmap

**Phase 1 — MCP server extraction (≈3–5 days).**
- New `postmind/core/agent_mcp.py` (or `mcp/server.py`): expose READ tools + WRITE
  stage/confirm over MCP (stdio first, HTTP behind a flag). Reuse `agent_tools.py` handlers
  and the request-scoped resolution logic factored out of `server.py`.
- Confirm tokens: single-use, server-issued, bound to a resolved target list; honored by a
  `confirm` MCP tool *and* the existing `/agent/action/confirm` web endpoint (shared code).
- Mode gating + blocklist/risk filtering inside the server.
- Tests under `MockAIEngine`; verify an external MCP client (e.g. `claude` CLI) can list
  tools, run a READ, and stage—but not execute—a WRITE without the confirm step.
- Deliverable: postmind is drivable from any MCP host with the safety boundary intact.

**Phase 2 — local tool-use reliability (≈3–4 days).**
- `_chat_local_tools` → OpenAI-compatible `/v1` tools path; grammar-constrained args;
  strong-model default; keep graceful fallback.
- Bench a handful of local models on the five Super Agent scenarios; document which work.
- Deliverable: local mode runs the real tool loop (not just degrade-to-propose) on a
  recommended model.

**Phase 3 — optional loop implementation swap (DEFERRED — intentionally not shipped).**
- Prototype Pydantic AI behind `AIEngine.chat` / `chat_stream`; keep the public interface
  and streaming event shape identical. Gate behind a setting; ship only if it reduces
  maintenance without regressing caching/streaming.
- **Why deferred:** this swaps an *implementation* behind a seam for zero new user-facing
  capability, while risking the working Anthropic prompt-caching + SSE streaming paths and
  adding a heavy dependency. The plan gated it on "maintenance cost actually biting"; it
  hasn't. Phases 1 & 2 already deliver the goal ("super good without the cloud, drivable by
  any harness"). Revisit only if the hand-rolled loop becomes a maintenance burden.

**New modules/endpoints summary:**
`postmind/core/agent_mcp.py` (MCP server over the existing tool catalog);
shared confirm-token resolution extracted from `server.py`;
local tool-use path rework in `ai_engine.py`; (optional) Pydantic AI adapter.

---

## 7. Risks & open questions

- **Confirm-token model over MCP.** External MCP hosts won't render our HTML cards. The MCP
  WRITE tool must return a *structured* staged-action (counts, sample, targets, token) the
  host can present, and the host calls `confirm(token)` only after human approval. We must
  not auto-confirm. Worth a red-team pass before exposing WRITE over MCP to third-party
  hosts.
- **Local tool-call reliability is still model-dependent.** Mitigation: grammar constraints
  + strong-model default + the stage→confirm net + retained fallback.
- **MCP transport auth.** HTTP MCP needs auth/SSRF care; default to stdio (local) and gate
  HTTP behind explicit opt-in, mirroring the existing same-origin/CSRF posture.
- **Scope creep into a coding agent.** Keep the tool surface domain-only; never expose
  bash/file/web. The boundary is the product.

### Recommendations
1. Ship Move #1 (MCP server) first — it future-proofs against *every* harness and keeps the
   safety boundary where it belongs (the tool layer).
2. Then Move #2 (local tool-use reliability) — this is what actually delivers "super good
   without the cloud," and the harness was never the blocker.
3. Treat the Claude Agent SDK / OpenCode / Goose as **clients** of our MCP server, not as a
   replacement engine.
4. Defer the Pydantic AI loop swap until maintenance cost justifies it.

---

## Sources
- [Claude Agent SDK — overview](https://code.claude.com/docs/en/agent-sdk/overview)
- [Claude Agent SDK — MCP](https://code.claude.com/docs/en/agent-sdk/mcp)
- [Claude Agent SDK — custom tools](https://code.claude.com/docs/en/agent-sdk/custom-tools)
- [Coding CLI tools comparison (Tembo)](https://www.tembo.io/blog/coding-cli-tools-comparison)
- [awesome-cli-coding-agents](https://github.com/bradAGI/awesome-cli-coding-agents)
- [Goose (Agentic AI Foundation)](https://github.com/aaif-goose/goose)
- [AI agent frameworks 2026 (morphllm)](https://www.morphllm.com/ai-agent-framework)
- [Best open-source agent frameworks (Firecrawl)](https://www.firecrawl.dev/blog/best-open-source-agent-frameworks)
- [Codex + Ollama](https://ollama.com/blog/codex)
- [Best open-source LLMs for agentic coding 2026 (MindStudio)](https://www.mindstudio.ai/blog/best-open-source-llms-agentic-coding-2026)
- [Anthropic — Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
