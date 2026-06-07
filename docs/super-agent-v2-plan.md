# Super Agent v2 — MCP Consumer + Quality Improvements

**Status:** Planning. June 2026.
**Author:** Tashfeen Ekram + Claude Code

All Phase 1–4 features are shipped. This document covers the next layer: consuming external
MCP servers inside the agent loop, closing tool parity gaps, and targeted UX improvements.

---

## 1. MCP Consumer (highest impact, biggest ask)

### What this means

Today postmind *is* an MCP server (exposes its tools to Claude Desktop via `postmind mcp`).
The Super Agent does **not** consume external MCP servers. This would let the agent call tools
from Calendar, Slack, Linear, Notion, etc. during an agent turn — enabling cross-app workflows
driven by natural language over email.

**Example prompts enabled:**
- "Find emails from my 3pm meeting attendees and summarize the thread"
- "Create a Linear ticket from this support email"
- "Post a Slack message to #eng-ops about this outage email"
- "Check if I have a meeting with the sender of this email"

### Architecture

#### Config: `mcp_servers` section in `~/.postmind/accounts/<email>.json`

```json
{
  "mcp_servers": [
    {
      "name": "calendar",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-google-calendar"]
    },
    {
      "name": "slack",
      "command": "uvx",
      "args": ["mcp-slack"]
    },
    {
      "name": "linear",
      "url": "http://localhost:3333/mcp"
    }
  ]
}
```

Two transport modes:
- **stdio** (`command` + `args`): spawn subprocess, communicate over stdin/stdout using the MCP protocol
- **HTTP/SSE** (`url`): connect to a running MCP endpoint

#### New module: `postmind/core/mcp_client.py`

```python
class MCPClientSession:
    """Wraps a single MCP server connection (stdio or HTTP)."""
    name: str
    tools: list[dict]           # Anthropic-style tool schemas, prefixed with "mcp_{name}_"
    async def call_tool(self, name: str, input: dict) -> str: ...
    async def close(self): ...

class MCPClientPool:
    """Manages sessions for all configured MCP servers."""
    async def connect_all(self, server_configs: list[dict]) -> list[MCPClientSession]: ...
    async def get_tools(self) -> list[dict]: ...  # all tools from all servers, prefixed
    async def dispatch(self, tool_name: str, tool_input: dict) -> str: ...
```

Tool names are namespaced to avoid collisions: `mcp_calendar_list_events`, `mcp_slack_send_message`, etc.

#### Integration point: `web/server.py` — `_build_agent_tool_executor`

The tool executor already dispatches by name. Add a fall-through:

```python
# In _build_agent_tool_executor closure:
if tool_name.startswith("mcp_"):
    return await mcp_pool.dispatch(tool_name, tool_input)
```

The MCP tool list is appended to `agent_tools.ALL_TOOLS` at request build time:

```python
mcp_tools = await mcp_pool.get_tools()   # cached per session/request
tools = agent_tools.ALL_TOOLS + mcp_tools
ai.chat(messages, system=system, tools=tools, tool_executor=executor, ...)
```

#### MCP session lifecycle

- Pool is initialized once at server startup (or lazily on first request) and kept alive.
- On `postmind serve`, if `mcp_servers` is configured, `MCPClientPool.connect_all()` runs
  in the background startup event.
- Sessions are reconnected on failure with exponential backoff.
- Pool is injected into the request scope (FastAPI dependency or `app.state.mcp_pool`).

#### Settings UI: `/settings` page additions

- New "Connected MCP Servers" section.
- Lists configured servers with connection status (green/red pill).
- Add server form: name, transport (stdio/HTTP), command or URL.
- Test connection button: calls the server's `list_tools`, shows count.
- All stored in per-account config.

#### Safety model for MCP tools

MCP WRITE tools from external servers are treated as **READ** by default (returned as text
to the model). If the user wants the agent to execute them, they must mark the server as
`"allow_execute": true` in config. Without this, the agent can only read/query via MCP;
any write must be confirmed by the user in the chat (the model proposes, user approves in
a card, then postmind calls the MCP tool). This mirrors the existing stage→confirm pattern.

---

## 2. Tool Parity Gaps (web agent vs. MCP server)

### 2a. `read_email` — add to MCP server

`read_email(message_id)` exists in `agent_tools.py` and is wired in the web executor but
**not** in `agent_mcp.py` / `AgentService`. Any MCP host (Claude Desktop, Cursor) connecting
to postmind can't read individual emails.

**Fix:** Add `read_email` to `AgentService` and register `@mcp.tool()` in `agent_mcp.py`.

### 2b. `stage_trash_query` — add to MCP server

The per-email review drawer (query → list of emails → user selects → trash) only exists in
the web path. The MCP path can't do query-based review.

**Fix:** Add `stage_trash_query` to `AgentService` with a persistent (DB-backed) review
session instead of the in-memory `_review_put()` cache. Token TTL: 1 hour.

### 2c. `run_sql` — expose in web agent (power mode only)

`run_sql(query)` is MCP-only. It's a useful power tool for advanced users asking
"how many emails from Google do I have?" or "what's my oldest unread email?".

**Fix:** Add `run_sql` to `agent_tools.ALL_TOOLS` gated behind
`settings.agent_power_mode = true`. The same read-only SQLite snapshot authorizer from
`AgentService` is reused. Disabled by default to keep the agent focused.

---

## 3. New Email Tools

### 3a. `get_thread(thread_id)` — READ

Returns all messages in a Gmail thread in chronological order (subject, from, date, snippet
per message). Enables: "summarize this thread", "who last replied?", "draft a follow-up".

**Implementation:** `provider.get_thread_messages(thread_id)` → format as numbered list.

### 3b. `summarize_thread(thread_id)` — READ

Calls `get_thread`, then passes to `ai.summarize()` (new small `_complete` call: "Summarize
this email thread in 3 bullet points"). Returns bullets to the model which relays to user.

### 3c. `find_emails_by_topic(topic, limit)` — READ

Full-text search using Gmail's native `q` parameter. Wraps `provider.list_message_ids(query=topic)`,
fetches top-N metadata, returns sender/subject/date table. Enables: "find emails about
the Q2 audit" without the user knowing Gmail query syntax.

### 3d. `snooze_sender(senders, days)` — WRITE (stage)

Creates a Gmail label `Snoozed/<date>` and moves emails from those senders there.
Creates a heartbeat-agent rule to un-snooze on that date. Stage → confirm card.

---

## 4. UI / UX Improvements

### 4a. Persistent conversation history (server-side)

Today history lives in `localStorage` (`postmind_agent_v1`). On a new device or cleared
storage, the conversation is gone. Per-session context is also lost on page refresh.

**Fix:** Store agent conversations in a new `AgentConversation` table in `postmind.db`
(session_id, account_email, messages JSON, created_at). The `/agent` GET endpoint loads
the last 24 hours of history for the active account. Clear button writes a tombstone.

### 4b. Tool metadata panel

When a tool step is shown in the timeline (e.g., "analyze_storage"), clicking it expands
to show the raw tool input and output. Useful for power users and debugging unexpected
responses. Already partially supported by the `toolResult` events — just needs a click
handler + expand animation.

### 4c. Example prompt refresh

Current 5 example chips are static. Replace with dynamic chips that:
- Show `count` from the last inbox overview (e.g., "You have 12 unseen subscriptions — review?")
- Rotate based on account state (large inbox → storage prompt, many newsletters → unsubscribe prompt)
- Loaded from `GET /agent/suggestions` which calls `_chat_overview_text` + simple heuristics.

### 4d. MCP server status in agent header

When MCP servers are configured, show connection status pills in the agent page header
("Calendar: connected", "Slack: offline"). Clicking opens the settings MCP section.

### 4e. `/agent/history` endpoint

`GET /agent/history?format=json` exports the full conversation log. Useful for debugging
and for users who want to audit what the agent did.

---

## 5. Implementation Order (Recommended)

| Priority | Item | Effort | Value |
|---|---|---|---|
| P0 | `read_email` in MCP server | Small | Closes obvious gap |
| P0 | `get_thread` web tool | Small | Unlocks thread workflows |
| P1 | MCP consumer: `MCPClientPool` + stdio transport | Medium | Core feature |
| P1 | MCP consumer: settings UI | Medium | Required for usability |
| P1 | `find_emails_by_topic` web tool | Small | High everyday value |
| P2 | MCP consumer: HTTP transport | Small | Needed for hosted MCP servers |
| P2 | `stage_trash_query` in MCP server | Medium | Parity |
| P2 | Persistent conversation history | Medium | Nice to have |
| P2 | `summarize_thread` web tool | Small | High value |
| P3 | `run_sql` in web agent (power mode) | Small | Niche |
| P3 | `snooze_sender` tool | Medium | New capability |
| P3 | Dynamic example chips | Small | Polish |
| P3 | Tool metadata expand panel | Small | Power user polish |

---

## 6. Open Questions

1. **MCP stdio lifecycle:** Who reaps the subprocess when `postmind serve` exits? Use
   `asyncio` process group + SIGTERM, or a supervisor? Prefer `asyncio.create_subprocess_exec`
   with a shutdown hook in FastAPI's `lifespan`.

2. **MCP WRITE gating:** Should the confirm card show the raw MCP tool arguments, or
   a human-readable summary? The latter requires the model to generate a description
   (already does this for stage_archive etc.), the former is simpler and safer.

3. **MCP auth:** Many MCP servers (Google Calendar, Slack) need OAuth tokens. Should
   postmind store these tokens in `~/.postmind/mcp_tokens/<server_name>.json`, or does
   each MCP server manage its own auth? Recommend: delegate auth entirely to the MCP
   server subprocess (it handles its own `~/.config/<server>`), postmind only manages
   connection config.

4. **Which MCP servers to ship first?** Recommend: Google Calendar (same OAuth infra as
   Gmail), then Slack (most common workplace tool). Linear and Notion are lower priority.

5. **`get_thread` provider support:** IMAP does not expose thread grouping the same way
   as Gmail's Thread API. Add a `supports(Capability.THREADS)` gate and fall back to
   `read_email` on the single message if threads not supported.
