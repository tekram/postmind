# Connecting postmind to Claude Code (MCP)

postmind exposes its Super Agent tools over the Model Context Protocol so you can drive your inbox directly from Claude Code or Claude Desktop.

## What you get

Once connected, any Claude session can:

- Analyze your inbox storage by sender, domain, or size
- Find and summarize email threads (AI-powered, 3-bullet summaries)
- Stage bulk actions (trash, archive, label, mark-read, unsubscribe) — always confirm-first
- Send emails via a stage → confirm draft flow
- Create automation rules and heartbeat background agents
- Query the local email cache with arbitrary read-only SQL

## Setup — Claude Code CLI

Add postmind to Claude Code's MCP server list. There are two ways:

### Option A: Project-level (only active when working in this repo)

Run from the postmind directory:

```bash
claude mcp add -s project postmind /Users/tashfeenekram/postmind/.venv/bin/postmind -- mcp
```

This writes to `.mcp.json` in the repo root. You can also edit that file directly:

```json
{
  "mcpServers": {
    "postmind": {
      "type": "stdio",
      "command": "/Users/tashfeenekram/postmind/.venv/bin/postmind",
      "args": ["mcp"]
    }
  }
}
```

### Option B: Global (active in every Claude Code session)

Run in your terminal:

```bash
claude mcp add -s user postmind /Users/tashfeenekram/postmind/.venv/bin/postmind -- mcp
```

This writes to `~/.claude.json` (the user-scoped MCP registry). The server becomes available in all Claude Code sessions.

## Setup — Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "postmind": {
      "command": "/Users/tashfeenekram/postmind/.venv/bin/postmind",
      "args": ["mcp"]
    }
  }
}
```

Restart Claude Desktop after saving.

## Verifying the connection

In any Claude session type: `list the postmind tools`

You should see tools like `get_inbox_overview`, `analyze_storage`, `find_emails_by_topic`, `summarize_thread`, `stage_trash`, etc.

## Available tools

### Read / analysis tools (run immediately, return text)

| Tool | Description |
|------|-------------|
| `get_inbox_overview` | Live snapshot: sender count, total emails, reclaimable storage, top senders. |
| `analyze_storage` | Largest storage consumers. Grouped by sender or domain. |
| `search_senders` | Search senders by name, email, or domain substring. |
| `find_largest_messages` | Largest individual emails by message size. Optional Gmail-style scope query. |
| `summarize_thread` | Fetch a thread and return a 3-bullet AI summary. Requires cloud AI mode. |
| `find_and_summarize_thread` | Search for emails matching a query, pick the most relevant thread, and summarize it in 3 bullets. |
| `find_unopened_subscriptions` | Newsletters/subscriptions the user rarely opens (unsubscribe candidates). |
| `list_automation` | Show the user's heartbeat agent (if any) and active rules. |
| `run_sql` | Run one read-only SELECT over a snapshot of the local email cache. |
| `read_email` | Fetch the full content of a specific email by its message_id. |
| `get_thread` | Fetch all messages in a thread in chronological order. |
| `find_emails_by_topic` | Search for emails matching a topic or keyword. |
| `draft_email` | Draft an email in the user's voice (text only; sends nothing). |

### Write tools — stage only, return a confirm token

| Tool | Description |
|------|-------------|
| `stage_trash` | Stage moving emails from senders to Trash (undoable). Returns a confirm token. |
| `stage_archive` | Stage archiving emails from senders (undoable). Returns a confirm token. |
| `stage_label` | Stage labeling emails from senders (undoable). Returns a confirm token. |
| `stage_mark_read` | Stage marking emails from senders as read (undoable). Returns a confirm token. |
| `stage_unsubscribe` | Stage unsubscribing from senders (NOT undoable; optional back-catalog trash IS). Returns a confirm token. |
| `stage_send` | Stage sending an email (always-confirm; no auto-send). Returns a confirm token. |
| `stage_create_agent` | Stage creating a background heartbeat agent. Returns a confirm token. |
| `stage_create_rule` | Stage a recurring rule from plain English. Returns a confirm token + warnings. |
| `stage_trash_query` | Stage a query-based trash review using a Gmail search query. Returns a confirm token. |

### Confirm / cancel

| Tool | Description |
|------|-------------|
| `list_staged_actions` | List actions staged this session and awaiting confirmation. |
| `confirm_action` | Execute a staged action by its confirm token. Call ONLY after the user explicitly approves. |
| `cancel_action` | Discard a staged action without executing it. |

## Safety model

All write operations (trash, archive, unsubscribe, send) go through a stage → confirm flow:

1. Call the `stage_*` tool — returns a confirm token and a summary of what will happen
2. Review the staged action via `list_staged_actions`
3. Call `confirm_action(token)` only after approving
4. Deletes go to Trash (undoable for 30 days via `postmind undo`), never permanent

Targets are always **server-resolved** by postmind's code. The MCP host (Claude) can only confirm or cancel what postmind resolved — it cannot name its own targets. This contains prompt injection from untrusted email bodies.

## Binding to a specific account

If you have multiple accounts configured, bind the server to one:

```json
{
  "mcpServers": {
    "postmind": {
      "command": "/Users/tashfeenekram/postmind/.venv/bin/postmind",
      "args": ["mcp", "--account", "you@example.com"]
    }
  }
}
```

## Troubleshooting

- **"command not found"**: use the full path to `.venv/bin/postmind` from the postmind directory
- **"No active account"**: run `postmind serve` first and connect a Gmail account at http://127.0.0.1:8484
- **No emails returned**: run a sync first (`postmind sync` or the Sync page in the web UI)
- **"The MCP server requires the 'mcp' package"**: run `pip install 'postmind[mcp]'` inside the venv
- **Thread summary fails**: `summarize_thread` requires cloud AI mode — run `postmind config set ai_mode cloud`
