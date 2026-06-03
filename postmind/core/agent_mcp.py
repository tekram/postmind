"""MCP server exposing postmind's agent tools to any MCP host.

This makes postmind drivable by *any* agent harness that speaks MCP — Claude
Agent SDK, Claude Desktop, Codex(? no MCP yet), OpenCode, Goose, Cursor — and by
postmind's own loop, all over the *same* safety boundary. The boundary lives in
:class:`~postmind.core.agent_service.AgentService` (resolve → stage → confirm),
not in the harness, so the confirm-first guarantee holds regardless of who is
calling.

Design:
- READ / analysis tools run immediately and return text.
- WRITE tools only ``stage_*`` — they return a structured staged-action descriptor
  with a single-use **confirm token**. The host MUST present it for human approval
  and then call ``confirm_action(token)``. There is no auto-execute path. Targets
  are server-resolved by our code; the host can only confirm/cancel what we
  resolved, never name its own targets.
- Only postmind's domain tools are exposed — never filesystem/bash/web. Keeping the
  surface domain-only is the product.

Run it with ``postmind mcp`` (stdio). Requires the ``mcp`` extra:
``pip install 'postmind[mcp]'``.
"""

from __future__ import annotations

import json

from postmind.core.agent_service import AgentService

_INSTRUCTIONS = """\
postmind agent — drive a privacy-first Gmail/IMAP inbox.

Use the READ tools (get_inbox_overview, analyze_storage, search_senders,
find_largest_messages, find_unopened_subscriptions, list_automation) to gather
facts and quote real numbers before acting.

You cannot change the inbox directly. The stage_* tools (stage_trash,
stage_archive, stage_label, stage_mark_read, stage_unsubscribe, stage_send,
stage_create_agent, stage_create_rule) only PREPARE an action and return a confirm
token. Show the staged action to the user and call confirm_action(token) only after
they explicitly approve. Trash/archive/label/mark_read are undoable for 30 days;
unsubscribe and send are not. Never claim an action is done until you've called
confirm_action and seen "ok": true.
"""


def _dump(obj) -> str:
    """Serialize a tool result for the host (compact JSON for dicts, str otherwise)."""
    if isinstance(obj, (dict, list)):
        return json.dumps(obj, ensure_ascii=False)
    return str(obj)


def build_server(account_email: str | None = None):
    """Construct the FastMCP server bound to one :class:`AgentService` session."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise SystemExit(
            "The MCP server requires the 'mcp' package.\n"
            "Install it with:  pip install 'postmind[mcp]'"
        ) from exc

    svc = AgentService(account_email=account_email)
    mcp = FastMCP("postmind", instructions=_INSTRUCTIONS)

    # ── READ tools ───────────────────────────────────────────────────────────

    @mcp.tool()
    def get_inbox_overview() -> str:
        """Live snapshot: sender count, total emails, reclaimable storage, top senders."""
        return svc.inbox_overview()

    @mcp.tool()
    def analyze_storage(group_by: str = "sender", top_n: int = 10) -> str:
        """Largest storage consumers. group_by: 'sender' or 'domain'."""
        return svc.analyze_storage(group_by, top_n)

    @mcp.tool()
    def search_senders(query: str) -> str:
        """Search senders by name, email, or domain substring."""
        return svc.search_senders(query)

    @mcp.tool()
    def find_largest_messages(query: str = "", limit: int = 10) -> str:
        """Largest individual emails by message size. Optional Gmail-style scope."""
        return svc.find_largest_messages(query, limit)

    @mcp.tool()
    def find_unopened_subscriptions(min_count: int = 3, limit: int = 15) -> str:
        """Newsletters/subscriptions the user rarely opens (unsubscribe candidates)."""
        return svc.find_unopened_subscriptions(min_count, limit)

    @mcp.tool()
    def list_automation() -> str:
        """Show the user's heartbeat agent (if any) and active rules."""
        return svc.list_automation()

    @mcp.tool()
    def draft_email(intent: str, recipient_context: str = "", thread_snippet: str = "") -> str:
        """Draft an email in the user's voice (text only; sends nothing). To send,
        follow up with stage_send and confirm_action."""
        try:
            return svc.draft_email(intent, recipient_context, thread_snippet)
        except Exception as exc:
            return f"Couldn't draft: {exc}"

    # ── WRITE tools — stage only, return a confirm token ──────────────────────

    @mcp.tool()
    def stage_trash(senders: list[str] | None = None, query: str = "") -> str:
        """Stage moving emails from senders to Trash (undoable). Returns a confirm token."""
        return _dump(svc.stage_cleanup("trash", senders, query))

    @mcp.tool()
    def stage_archive(senders: list[str] | None = None, query: str = "") -> str:
        """Stage archiving emails from senders (undoable). Returns a confirm token."""
        return _dump(svc.stage_cleanup("archive", senders, query))

    @mcp.tool()
    def stage_label(label_name: str, senders: list[str] | None = None, query: str = "") -> str:
        """Stage labeling emails from senders (undoable). Returns a confirm token."""
        return _dump(svc.stage_cleanup("label", senders, query, label_name=label_name))

    @mcp.tool()
    def stage_mark_read(senders: list[str] | None = None, query: str = "") -> str:
        """Stage marking emails from senders as read (undoable). Returns a confirm token."""
        return _dump(svc.stage_cleanup("mark_read", senders, query))

    @mcp.tool()
    def stage_unsubscribe(
        senders: list[str] | None = None, query: str = "", also_trash: bool = False
    ) -> str:
        """Stage unsubscribing from senders (NOT undoable; optional back-catalog trash IS).
        Returns a confirm token."""
        return _dump(svc.stage_unsubscribe(senders, query, also_trash))

    @mcp.tool()
    def stage_send(to: str, subject: str, body: str) -> str:
        """Stage sending an email (always-confirm; no auto-send). Returns a confirm token."""
        return _dump(svc.stage_send(to, subject, body))

    @mcp.tool()
    def stage_create_agent(
        email: str = "",
        name: str = "",
        interval_minutes: int = 30,
        voice_style: str = "",
        user_context: str = "",
        run_rules: bool = True,
        run_followups: bool = True,
        run_avoidance: bool = False,
    ) -> str:
        """Stage creating a background heartbeat agent. Returns a confirm token."""
        return _dump(
            svc.stage_create_agent(
                email=email,
                name=name,
                interval_minutes=interval_minutes,
                voice_style=voice_style,
                user_context=user_context,
                run_rules=run_rules,
                run_followups=run_followups,
                run_avoidance=run_avoidance,
            )
        )

    @mcp.tool()
    def stage_create_rule(natural_language: str) -> str:
        """Stage a recurring rule from plain English. Returns a confirm token + warnings."""
        return _dump(svc.stage_create_rule(natural_language))

    # ── Confirm / cancel ──────────────────────────────────────────────────────

    @mcp.tool()
    def list_staged_actions() -> str:
        """List actions staged this session and awaiting confirmation."""
        return _dump(svc.list_staged())

    @mcp.tool()
    def confirm_action(token: str) -> str:
        """Execute a staged action by its confirm token. Call ONLY after the user
        explicitly approves. Binds to the targets our code resolved at stage time."""
        return _dump(svc.confirm(token))

    @mcp.tool()
    def cancel_action(token: str) -> str:
        """Discard a staged action without executing it."""
        return _dump(svc.cancel(token))

    return mcp


def main(account_email: str | None = None) -> None:
    """Entry point for ``postmind mcp`` — run the stdio MCP server."""
    build_server(account_email).run()
