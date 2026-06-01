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
        "name": "find_unopened_subscriptions",
        "description": "Find newsletters/subscriptions the user almost never opens — senders that have a List-Unsubscribe header and a high unread ratio. Use for 'unsubscribe me from newsletters I never open' / 'what subscriptions do I ignore'. Requires locally synced data.",
        "input_schema": {
            "type": "object",
            "properties": {
                "min_count": {"type": "integer", "description": "Minimum emails from a sender to consider (default 3)."},
                "limit": {"type": "integer", "description": "How many to return (default 15)."},
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
        "name": "stage_archive",
        "description": "Stage a bulk ARCHIVE (remove from inbox, keep searchable) of senders. You do NOT archive — you stage a confirmation card. Reversible (restores to inbox) and undoable for 30 days. Provide explicit sender emails and/or a substring query to match senders from the current scan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "senders": {"type": "array", "items": {"type": "string"}, "description": "Exact sender email addresses to stage."},
                "query": {"type": "string", "description": "Match senders by name/email/domain substring."},
            },
        },
    },
    {
        "name": "stage_label",
        "description": "Stage applying a LABEL to all email from senders. You do NOT label — you stage a confirmation card. Reversible (removes the label) and undoable for 30 days. Provide a label_name plus explicit sender emails and/or a substring query.",
        "input_schema": {
            "type": "object",
            "properties": {
                "senders": {"type": "array", "items": {"type": "string"}, "description": "Exact sender email addresses to stage."},
                "query": {"type": "string", "description": "Match senders by name/email/domain substring."},
                "label_name": {"type": "string", "description": "The label to apply (created if it doesn't exist)."},
            },
            "required": ["label_name"],
        },
    },
    {
        "name": "stage_mark_read",
        "description": "Stage marking all email from senders as READ. You do NOT mark — you stage a confirmation card. Reversible (marks unread again) and undoable for 30 days. Provide explicit sender emails and/or a substring query.",
        "input_schema": {
            "type": "object",
            "properties": {
                "senders": {"type": "array", "items": {"type": "string"}, "description": "Exact sender email addresses to stage."},
                "query": {"type": "string", "description": "Match senders by name/email/domain substring."},
            },
        },
    },
    {
        "name": "stage_unsubscribe",
        "description": "Stage UNSUBSCRIBING from senders (real List-Unsubscribe / one-click / headless). You do NOT unsubscribe — you stage a confirmation card listing each sender. Unsubscribe is an external, NOT-undoable action; optionally also trash the existing back-catalog (the trash IS undoable). Provide explicit sender emails and/or a substring query.",
        "input_schema": {
            "type": "object",
            "properties": {
                "senders": {"type": "array", "items": {"type": "string"}, "description": "Exact sender email addresses to stage."},
                "query": {"type": "string", "description": "Match senders by name/email/domain substring."},
            },
        },
    },
    {
        "name": "draft_email",
        "description": "Draft an email in the user's voice (soul-aware). Returns the Subject and body as text and shows the user an editable draft card. This produces text only — it sends nothing. To actually send, follow up with send_email once the user approves.",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "description": "What the email should accomplish."},
                "recipient_context": {"type": "string", "description": "Who it's to and any relevant context."},
                "thread_snippet": {"type": "string", "description": "The message being replied to, if any."},
                "to": {"type": "string", "description": "Recipient email address, if known."},
            },
            "required": ["intent"],
        },
    },
    {
        "name": "send_email",
        "description": "Stage SENDING an email. You do NOT send — you emit a card with editable to/subject/body that the user must confirm. Always-confirm; there is no auto-send. Use after draft_email or when the user gives explicit recipient/subject/body.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address."},
                "subject": {"type": "string", "description": "Email subject line."},
                "body": {"type": "string", "description": "Email body (plain text)."},
            },
            "required": ["to", "subject", "body"],
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


def find_unopened_subscriptions(session, account_email: str, min_count: int = 3, limit: int = 15):
    """Return senders the user rarely opens: have a List-Unsubscribe header and a
    high unread ratio in the local cache. Returns a list of dicts
    ``{sender_email, total, unread, unread_pct}`` sorted by volume, plus the count.

    Pure DB query (no provider call). Caller formats / stages the result.
    """
    from sqlalchemy import case, func

    from postmind.core.storage import EmailRecord

    rows = (
        session.query(
            EmailRecord.sender_email,
            func.count().label("total"),
            func.sum(case((EmailRecord.is_unread.is_(True), 1), else_=0)).label("unread"),
        )
        .filter(
            EmailRecord.account_email == account_email,
            EmailRecord.list_unsubscribe != "",
            EmailRecord.is_inbox.is_(True),
        )
        .group_by(EmailRecord.sender_email)
        .having(func.count() >= max(1, min_count))
        .all()
    )
    out = []
    for sender, total, unread in rows:
        unread = unread or 0
        if not total:
            continue
        pct = unread / total
        if pct >= 0.6:  # rarely opened
            out.append({
                "sender_email": sender,
                "total": int(total),
                "unread": int(unread),
                "unread_pct": round(pct * 100),
            })
    out.sort(key=lambda r: r["total"], reverse=True)
    return out[: max(1, limit)]


def format_unopened(rows: list[dict]) -> str:
    if not rows:
        return "No clearly-ignored subscriptions found (need locally synced data with unread/unsubscribe info — try a Sync first)."
    lines = [f"{len(rows)} subscription(s) you rarely open (unsubscribe candidates):"]
    for r in rows:
        lines.append(f"- {r['sender_email']} — {r['total']} emails, {r['unread_pct']}% unread")
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
