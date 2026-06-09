# Connecting postmind to Claude Code (MCP)

postmind exposes its Super Agent tools over the Model Context Protocol so you can drive your inbox directly from Claude Code, Claude Desktop, or any MCP-compatible host.

## The easy way — HTTP endpoint (recommended)

When `postmind serve` is running, it automatically exposes an MCP server at:

```
http://127.0.0.1:8484/mcp/sse
```

No subprocess, no extra install. If you're already using the web UI, you're already running the MCP server.

### Claude Code CLI

```bash
claude mcp add postmind --transport sse http://127.0.0.1:8484/mcp/sse -s user
```

This registers postmind globally — available in every Claude Code session as long as `postmind serve` is running.

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "postmind": {
      "url": "http://127.0.0.1:8484/mcp/sse"
    }
  }
}
```

Restart Claude Desktop after saving.

### Project-level (this repo)

The `.mcp.json` in this repo already has the HTTP config — just run `postmind serve` and open Claude Code in this directory.

---

## Alternative — stdio (no web server needed)

If you want MCP access without the web server running:

```bash
claude mcp add postmind /path/to/postmind/.venv/bin/postmind -- mcp -s user
```

This launches a subprocess on demand. Requires the `[mcp]` extra: `pip install 'postmind[mcp]'`.

---

## Verifying the connection

```bash
claude mcp get postmind
```

Should show `Status: ✔ Connected`.

Or in any Claude session: *"list the postmind tools"*

---

## What you get — 24 tools

### Read tools (run immediately, never mutate)

| Tool | What it does |
|---|---|
| `get_inbox_overview` | Sender count, total emails, reclaimable MB, top senders |
| `analyze_storage` | Largest storage consumers by sender or domain |
| `search_senders` | Search senders by name/email/domain |
| `find_largest_messages` | Largest individual emails by size |
| `read_email` | Full content of a specific email by message_id |
| `get_thread` | All messages in a thread, chronological |
| `find_emails_by_topic` | Full-text search across your inbox |
| `summarize_thread` | 3-bullet AI summary of a thread |
| `find_and_summarize_thread` | Search + pick thread + summarize in one call |
| `find_unopened_subscriptions` | Newsletters with ≥60% unread ratio |
| `list_automation` | Heartbeat agents and active rules |
| `run_sql` | Read-only SELECT over the local email cache |
| `draft_email` | Draft in your voice (sends nothing) |

### Write tools — stage only, always confirm-first

| Tool | What it stages |
|---|---|
| `stage_trash` | Move-to-trash by sender |
| `stage_trash_query` | Per-email review drawer from a Gmail search |
| `stage_archive` | Archive by sender |
| `stage_label` | Label by sender |
| `stage_mark_read` | Mark-as-read by sender |
| `stage_unsubscribe` | Unsubscribe via List-Unsubscribe header |
| `stage_send` | Send an email |
| `stage_create_agent` | Create a heartbeat background agent |
| `stage_create_rule` | Create an automation rule from plain English |

### Confirm / cancel

| Tool | What it does |
|---|---|
| `list_staged_actions` | Show pending actions and their tokens |
| `confirm_action(token)` | Execute a staged action |
| `cancel_action(token)` | Discard without executing |

---

## Safety model

All write operations go through a **stage → confirm** flow:

1. Call a `stage_*` tool → returns a confirm token and a summary of what will happen
2. Review via `list_staged_actions`
3. Call `confirm_action(token)` only after approving
4. Deletes go to Trash (undoable for 30 days), never permanent
5. Targets are **server-resolved** — Claude can only confirm/cancel what postmind's code resolved, never inject arbitrary addresses

---

## Multi-account

Bind to a specific account:

```bash
/path/to/.venv/bin/postmind mcp --account you@gmail.com
```

The HTTP endpoint uses whichever account is active in the web UI.

---

## Troubleshooting

**"Status: disconnected"** — run `postmind serve` first, then retry  
**"No active account"** — open http://127.0.0.1:8484 and connect a Gmail account  
**"No inbox data"** — run a Sync first from the web UI or `postmind sync`  
**stdio: "command not found"** — use the full absolute path to `.venv/bin/postmind`
