"""Tool registry for the Super Agent (and the floating chat assistant).

This module is the single source of truth for the agent's tool *schemas* and the
stateless analysis helpers behind them. Request-scoped execution (provider,
account, the in-memory scan cache, and the actions/cards accumulators) lives in
``postmind/web/server.py``, which builds the ``tool_executor`` closure that
dispatches by name — mirroring the existing ``/chat`` executor.

Design notes:
- READ tools run immediately and return text the model can reason over.
- WRITE tools NEVER execute inside the loop. They *stage* an action and emit a
  card/action the user must confirm through a separate endpoint. Confirm targets
  are always server-resolved (sender emails / message IDs computed by our code),
  never free-form text from the model — this contains prompt injection from
  untrusted email bodies.
- Kept free of web imports so both the web layer and (future) CLI can reuse it.
"""

from __future__ import annotations

# ── Tool schemas ───────────────────────────────────────────────────────────────

READ_TOOLS: list[dict] = [
    {
        "name": "get_inbox_overview",
        "description": "Live snapshot of the inbox: sender count, total emails, reclaimable storage, and top senders by impact. Call when the user asks about the overall state of their inbox.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "analyze_storage",
        "description": "Find what is consuming the most storage. Returns the largest senders or domains by total size. Use for 'what's eating my storage' / 'biggest space hogs'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "group_by": {"type": "string", "enum": ["sender", "domain"], "description": "Aggregate by individual sender or by domain. Default sender."},
                "top_n": {"type": "integer", "description": "How many to return (default 10)."},
            },
        },
    },
    {
        "name": "search_senders",
        "description": "Search senders by name, email, or domain substring. Returns matching senders with counts, size, and risk. Use to find email from a person, company, or domain.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Name, email, or domain to search for."}},
            "required": ["query"],
        },
    },
    {
        "name": "find_largest_messages",
        "description": "Find the single largest individual emails (by attachment/message size). Use for 'find my largest email sizes' / 'biggest emails'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "How many messages to return (default 10, max 25)."},
                "query": {"type": "string", "description": "Optional Gmail-style scope, e.g. 'has:attachment'. Default the inbox."},
            },
        },
    },
    {
        "name": "list_automation",
        "description": "Show the user's current automation: their heartbeat agent (if any) and active rules. Use before creating new ones or when asked 'what automations do I have'.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

WRITE_TOOLS: list[dict] = [
    {
        "name": "stage_trash",
        "description": "Stage a bulk move-to-Trash of specific senders and give the user a button into the confirm-first preview. You do NOT delete — you stage and link. Deletes go to Trash and are undoable for 30 days. Provide explicit sender emails and/or a substring query to match senders from the current scan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "senders": {"type": "array", "items": {"type": "string"}, "description": "Exact sender email addresses to stage."},
                "query": {"type": "string", "description": "Match senders by name/email/domain substring."},
            },
        },
    },
    {
        "name": "create_agent",
        "description": "Stage creation of a heartbeat agent (a background watcher) for an account. Emits a confirmation card; the agent is only created when the user confirms. Use for 'create an email agent that …'. Pair with create_rule for actions like archiving newsletters.",
        "input_schema": {
            "type": "object",
            "properties": {
                "email": {"type": "string", "description": "Account email the agent watches. Defaults to the active account."},
                "name": {"type": "string", "description": "Display name for the agent."},
                "interval_minutes": {"type": "integer", "description": "How often it runs, in minutes (default 30)."},
                "voice_style": {"type": "string", "description": "Optional writing voice for drafts (e.g. 'concise, friendly')."},
                "user_context": {"type": "string", "description": "Optional context about the user for better drafts."},
                "run_rules": {"type": "boolean", "description": "Run the user's rules each cycle (default true)."},
                "run_followups": {"type": "boolean", "description": "Surface overdue follow-ups (default true)."},
                "run_avoidance": {"type": "boolean", "description": "Surface avoided emails (default false)."},
            },
        },
    },
    {
        "name": "create_rule",
        "description": "Stage a recurring rule from natural language (e.g. 'archive newsletters older than 30 days'). Translates to a Gmail query + action and emits a confirmation card showing exactly what it will do plus any warnings. The rule is only created when the user confirms. Rules run inside a heartbeat agent that has 'run rules' enabled.",
        "input_schema": {
            "type": "object",
            "properties": {
                "natural_language": {"type": "string", "description": "The rule in plain English."},
            },
            "required": ["natural_language"],
        },
    },
]

ALL_TOOLS: list[dict] = READ_TOOLS + WRITE_TOOLS


def read_tool_names() -> set[str]:
    return {t["name"] for t in READ_TOOLS}


# ── Stateless analysis helpers ───────────────────────────────────────────────


def summarize_storage(groups, group_by: str = "sender", top_n: int = 10) -> str:
    """Format the largest storage consumers from already-fetched SenderGroups."""
    from postmind.core.sender_stats import group_by_domain

    if not groups:
        return "No scan data available — ask the user to open Stats or run a Sync first."

    if group_by == "domain":
        domains = sorted(group_by_domain(groups), key=lambda d: d.total_size_mb, reverse=True)[:top_n]
        lines = [f"Largest storage by domain (top {len(domains)}):"]
        for d in domains:
            lines.append(f"- {d.domain} — {d.total_size_mb:.1f} MB across {d.count} emails")
        return "\n".join(lines)

    top = sorted(groups, key=lambda g: g.total_size_bytes, reverse=True)[:top_n]
    lines = [f"Largest storage by sender (top {len(top)}):"]
    for g in top:
        size = f"{g.total_size_mb:.1f} MB" if g.total_size_mb >= 0.1 else f"{g.total_size_bytes // 1024} KB"
        lines.append(f"- {g.display_name} <{g.sender_email}> — {size} across {g.count} emails")
    return "\n".join(lines)


def find_largest_messages(provider, query: str = "", limit: int = 10) -> str:
    """Fetch real per-message sizes and return the largest individual emails.

    Uses real ``size_estimate`` per message (not the sampled extrapolation that
    BulkPreview uses), so it answers 'largest emails' accurately.
    """
    limit = max(1, min(int(limit or 10), 25))
    scope = query.strip() or "in:inbox"
    ids = provider.list_message_ids(query=scope, max_results=400)
    if not ids:
        return f"No messages found for scope '{scope}'."
    messages = provider.get_messages_metadata(ids)
    messages.sort(key=lambda m: (m.size_estimate or 0), reverse=True)
    lines = [f"Largest {min(limit, len(messages))} emails in '{scope}':"]
    for m in messages[:limit]:
        mb = (m.size_estimate or 0) / (1024 * 1024)
        subj = (m.headers.subject or "(no subject)")[:60]
        lines.append(f"- {mb:.1f} MB — {subj} — from {m.sender_email}")
    return "\n".join(lines)
