# Plan: Floating Chat Assistant for postmind

## Goal

Make postmind easier to onboard and easier to use by adding an LLM-powered
conversational assistant that is available on every page. The assistant should
leverage the existing `AIEngine` (cloud Anthropic + local Ollama), answer
questions grounded in the user's real inbox, draft emails in the user's voice,
and guide the user to the right page — without ever performing destructive
actions directly.

## Constraints & principles

- **Reuse, don't reinvent.** The app already has a working `AIEngine` with cloud
  (Anthropic) and local (Ollama) backends, plus `compose_email`, `classify_emails`,
  `translate_rule`, etc. The assistant builds on these.
- **Privacy-first.** AI is off by default. When AI is off, the assistant must not
  call any model — it returns a canned message guiding the user to Settings.
- **Safe by design.** The assistant never deletes/archives/sends. It *proposes*
  and routes the user into the existing confirm-first flows (Stats → purge
  preview, which is Trash-only and undoable).
- **Match existing stack.** FastAPI + Jinja2 + htmx + Tailwind (CDN). The chat
  widget uses vanilla JS (chat needs dynamic append + history, which htmx fits
  poorly), consistent with how the rest of the app mixes htmx and small JS.
- **Graceful degradation.** Cloud mode gets full tool-use; local mode degrades to
  grounded conversation (Ollama tool-use is unreliable across models); off mode
  guides to Settings.

## Architecture — three pieces

### 1. `AIEngine.chat()` — agentic conversation method

File: `postmind/core/ai_engine.py`

```python
def chat(self, messages, system, tools=None, tool_executor=None,
         max_tokens=1024, max_tool_iterations=6) -> str
```

- `messages`: list of `{"role": "user"|"assistant", "content": str}`.
- **Cloud mode** (`_chat_cloud`): runs an Anthropic tool-use loop. On
  `stop_reason == "tool_use"`, appends the assistant turn, dispatches each
  `tool_use` block through `tool_executor(name, input) -> str`, appends
  `tool_result` blocks, and loops up to `max_tool_iterations`. Returns the final
  concatenated text.
- **Prompt caching**: system prompt passed as a list block with
  `cache_control: ephemeral`; the last tool definition also gets `cache_control`.
  This keeps multi-turn conversations cheap (system + tools are reused each turn).
- **Local mode**: flattens the conversation into a single prompt and calls the
  existing `_complete()` (Ollama `/api/chat`). No tools.
- **Off mode**: raises `ValueError` (the endpoint handles off mode before calling).
- Tool execution failures are caught and surfaced to the model as
  `Error running <tool>: <exc>` rather than crashing the request.

### 2. `/chat` endpoint + tools + grounding helpers

File: `postmind/web/server.py`

- **`_PAGES`**: dict mapping route paths → human descriptions, used both for the
  `navigate` tool enum and the system prompt.
- **`_chat_overview_text(account_email)`**: builds a compact, live inbox snapshot.
  Reads from the in-memory scan cache (`_cache_get()`) if present, else from the
  local DB via `fetch_sender_groups_from_db` (only when `EmailRepo.get_inbox`
  returns data). If neither exists, returns text telling the user to Sync / open
  Stats first. Includes sender count, total emails, reclaimable MB, and top 8
  senders.
- **`_chat_search_senders(query, account_email)`**: substring search over
  sender email/name/domain from the same cache-or-DB source. Returns up to 12
  matches.
- **`_CHAT_TOOLS`**: tool schemas for `get_inbox_overview`, `search_senders`,
  `draft_email` (intent/recipient_context/thread_snippet), and `navigate`
  (page enum + label).
- **`_build_chat_system(page, account_email, ai_mode)`**: assembles the system
  prompt — identity, brevity/safety rules ("never claim to have deleted/sent
  anything"), current page, account, AI mode, the page list, and the live inbox
  snapshot.
- **`POST /chat`**: 
  - Parses JSON `{messages, page}`. Sanitises messages (only valid roles,
    content cast to str and truncated to 4000 chars, capped to last 12). Empty →
    greeting.
  - If AI mode is `off`, returns a guiding reply + an "Open Settings" action
    (no model call).
  - Otherwise runs the chat in the thread pool executor (`_run`). The
    `tool_executor` closure dispatches the four tools; `navigate` appends to an
    `actions` list (deduped by href). `draft_email` pulls the agent's soul
    (voice/context/guidelines) via `AgentRepo.get_by_email` and calls
    `ai.compose_email`. Tools are only passed in cloud mode.
  - Returns `{"reply": str, "actions": [{"label","href"}]}`.
  - Note: `draft_email` → `compose_email` is cloud-only (raises `ValueError`
    otherwise), but tools are only passed in cloud mode, so the guard is a
    belt-and-suspenders safety net rather than a reachable local path.

### 3. Floating widget

File: `postmind/web/templates/base.html` (so it appears on every page that
extends base, including onboarding).

- Fixed bottom-right launcher button (teal, chat icon). Click opens a panel
  (`w-22rem`, `h-32rem`, responsive max-w/max-h).
- Header shows AI-mode-aware subtitle (cloud / local / off), plus clear and close
  buttons.
- Scrollable message list; composer with auto-growing `<textarea>`,
  Enter-to-send (Shift+Enter newline), send button.
- Vanilla JS: history persisted to `localStorage` key `postmind_chat_v1`
  (capped 24). Minimal markdown rendering (`**bold**`, `` `code` ``, newlines),
  HTML-escaped. Assistant action buttons rendered as teal link chips.
- `send()` POSTs `{messages, page: window.location.pathname}` to `/chat`, shows a
  pulsing typing bubble, then renders the reply + actions.

## What is intentionally NOT done

- The assistant does not execute destructive actions (purge/archive/send). It
  proposes and routes to confirm-first flows.
- No changes to the onboarding wizard structure (still Gmail-OAuth-first); the
  assistant simply appears there too.
- No streaming responses (single request/response per turn).

## Testing performed

- `app` imports cleanly; `/chat` route registered; `AIEngine.chat` present.
- `/chat` AI-off path returns the Settings-guidance reply + action.
- `/chat` empty-messages path returns the greeting.
- Cloud tool-use wiring verified with a mocked `AIEngine`: the executor's
  `navigate` + `search_senders` calls work and `navigate` populates the `actions`
  list returned to the client.
- Widget markers (`pm-assistant`, `pm-chat-panel`, `/chat`, input placeholder)
  confirmed present in rendered `/settings` HTML.
- Existing suite: 62 failures are **pre-existing** (old `mailtrim` package name +
  an IMAP password prompt); they fail identically on a clean tree with these
  changes stashed. No new regressions.

## Files changed

- `postmind/core/ai_engine.py` — `chat()` + `_chat_cloud()`.
- `postmind/web/server.py` — `_PAGES`, `_chat_overview_text`,
  `_chat_search_senders`, `_CHAT_TOOLS`, `_build_chat_system`, `POST /chat`.
- `postmind/web/templates/base.html` — floating widget markup + JS.

## Possible follow-ups

- Let the assistant execute confirmed cleanups with an in-chat confirm step.
- Add an IMAP path and an "enable AI" step to the onboarding wizard.
- Stream assistant responses for snappier UX.
