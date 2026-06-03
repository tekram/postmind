"""Harness-independent agent service — resolve → stage → confirm → execute.

This is the engine behind any agent *harness* that drives postmind: the web Super
Agent, the MCP server (``core/agent_mcp.py``), and any future CLI/SDK loop. It is
deliberately free of web/MCP imports so the safety boundary lives in one place,
independent of whichever harness is calling.

The contract mirrors the web executor (``web/server.py``) but with no request
scope — it resolves senders straight from the locally synced DB:

- **READ** helpers return text the model reasons over; they never change anything.
- **WRITE** actions never execute when *staged*. ``stage_*`` resolves the targets
  with *our* code (blocklist + sensitive-tier gating), binds the action to a
  server-resolved message-ID list, and returns a single-use **confirm token** plus
  a structured descriptor. Nothing changes until :meth:`confirm` is called with
  that token.
- Confirm tokens are bound to the resolved targets at stage time. A harness (or a
  prompt-injected email body) can never smuggle a different target into execution
  — it can only confirm or cancel what our code already resolved.
- Every reversible action records an undo log *before* the provider call, so
  ``postmind undo`` / the Undo page can reverse it for 30 days. Deletes go to
  Trash, never permanent.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from postmind.config import get_active_account, get_settings, load_account_config

# Reversible actions that record an undo log and are restorable from /undo.
_REVERSIBLE = ("archive", "label", "mark_read", "trash")
# Token lifetime — a staged action the user never confirms expires quietly.
_TOKEN_TTL_SECONDS = 60 * 60


@dataclass
class StagedAction:
    """A WRITE action resolved by our code, awaiting an explicit confirm.

    ``message_ids`` / ``senders`` are computed server-side at stage time and are
    the *only* thing :meth:`AgentService.confirm` will act on.
    """

    token: str
    kind: (
        str  # trash | archive | label | mark_read | unsubscribe | send | create_agent | create_rule
    )
    summary: str
    created_at: float = field(default_factory=time.time)
    message_ids: list[str] = field(default_factory=list)
    senders: list[str] = field(default_factory=list)
    params: dict = field(default_factory=dict)
    undoable: bool = False
    used: bool = False

    def descriptor(self) -> dict:
        """JSON-safe view a harness can render as a confirm card."""
        return {
            "token": self.token,
            "kind": self.kind,
            "summary": self.summary,
            "senders": self.senders,
            "email_count": len(self.message_ids),
            "params": self.params,
            "undoable": self.undoable,
        }


class AgentService:
    """Stateful per-session agent engine. One instance owns its staged-action store.

    Construct once per harness session (the MCP server keeps one for the life of
    the connection). ``account_email`` defaults to the active account.
    """

    def __init__(self, account_email: str | None = None, ai=None):
        self.account_email = account_email or get_active_account() or ""
        self._ai = ai
        self._provider = None
        self._groups_cache: list | None = None
        self._staged: dict[str, StagedAction] = {}

    # ── Lazy dependencies ────────────────────────────────────────────────────

    @property
    def ai(self):
        if self._ai is None:
            from postmind.core.ai_engine import AIEngine

            self._ai = AIEngine()
        return self._ai

    def provider(self):
        if self._provider is None:
            self._provider = _build_provider(self.account_email)
        return self._provider

    def _groups(self):
        """Sender groups from the locally synced DB (no provider calls)."""
        if self._groups_cache is None:
            from postmind.core.sender_stats import fetch_sender_groups_from_db
            from postmind.core.storage import EmailRepo, get_session

            if self.account_email and EmailRepo(get_session()).get_inbox(
                self.account_email, limit=1
            ):
                self._groups_cache = fetch_sender_groups_from_db(
                    account_email=self.account_email,
                    scope="inbox",
                    min_count=1,
                    top_n=500,
                    sort_by="score",
                )
            else:
                self._groups_cache = []
        return self._groups_cache

    # ── READ helpers (return text; never mutate) ─────────────────────────────

    def inbox_overview(self) -> str:
        from postmind.core.sender_stats import (
            generate_recommendations,
            group_by_domain,
            reclaimable_mb,
        )

        groups = self._groups()
        if not groups:
            return (
                "No inbox data yet — there's nothing to quote numbers from. Ask the user to "
                "run `postmind sync` (or the Sync page) to pull their mailbox in first."
            )
        domain_map = {d.domain: d for d in group_by_domain(groups)}
        recs = generate_recommendations(groups, top_n=5, domain_map=domain_map)
        total = sum(g.count for g in groups)
        lines = [
            f"Inbox snapshot for {self.account_email or 'active account'}:",
            f"- {len(groups)} senders, {total:,} emails in scope",
            f"- ~{reclaimable_mb(recs):.0f} MB reclaimable from the top cleanup suggestions",
            "- Top senders by impact:",
        ]
        for g in groups[:8]:
            lines.append(f"  • {g.display_name} <{g.sender_email}> — {g.count} emails, {_size(g)}")
        return "\n".join(lines)

    def analyze_storage(self, group_by: str = "sender", top_n: int = 10) -> str:
        from postmind.core import agent_tools

        groups = self._groups()
        if not groups:
            return "No scan data available — run a Sync first."
        return agent_tools.summarize_storage(groups, group_by, int(top_n or 10))

    def search_senders(self, query: str) -> str:
        groups = self._groups()
        if not groups:
            return "No scan data available — run a Sync first."
        q = (query or "").lower().strip()
        matches = [
            g
            for g in groups
            if q in (g.sender_email or "").lower()
            or q in (g.sender_name or "").lower()
            or q in (g.domain or "").lower()
        ]
        if not matches:
            return f"No senders matching '{query}' in the current scan."
        lines = [f"{len(matches)} sender(s) matching '{query}':"]
        for g in matches[:12]:
            lines.append(f"- {g.display_name} <{g.sender_email}> — {g.count} emails, {_size(g)}")
        return "\n".join(lines)

    def find_largest_messages(self, query: str = "", limit: int = 10) -> str:
        from postmind.core import agent_tools

        return agent_tools.find_largest_messages(self.provider(), query, int(limit or 10))

    def find_unopened_subscriptions(self, min_count: int = 3, limit: int = 15) -> str:
        from postmind.core import agent_tools
        from postmind.core.storage import get_session

        if not self.account_email:
            return "No active account."
        rows = agent_tools.find_unopened_subscriptions(
            get_session(), self.account_email, int(min_count or 3), int(limit or 15)
        )
        return agent_tools.format_unopened(rows)

    def list_automation(self) -> str:
        from postmind.core.storage import AgentRepo, RuleRepo, get_session

        if not self.account_email:
            return "No active account."
        session = get_session()
        agent = AgentRepo(session).get_by_email(self.account_email)
        rules = RuleRepo(session).list_active(self.account_email)
        parts = []
        if agent:
            parts.append(
                f"Heartbeat agent '{agent.name}' every {agent.interval_minutes}m "
                f"(active={agent.is_active}, rules={agent.run_rules})."
            )
        else:
            parts.append("No heartbeat agent yet.")
        if rules:
            parts.append("Active rules: " + "; ".join(f"{r.name} → {r.action}" for r in rules[:5]))
        else:
            parts.append("No active rules.")
        return " ".join(parts)

    def draft_email(
        self, intent: str, recipient_context: str = "", thread_snippet: str = ""
    ) -> str:
        """Compose a soul-aware draft. Returns text only — sends nothing."""
        from postmind.core.storage import AgentRepo, get_session

        soul = {}
        agent = (
            AgentRepo(get_session()).get_by_email(self.account_email)
            if self.account_email
            else None
        )
        if agent:
            soul = {
                "voice_style": agent.voice_style,
                "user_context": agent.user_context,
                "writing_guidelines": agent.writing_guidelines,
            }
        return self.ai.compose_email(
            intent=intent,
            recipient_context=recipient_context,
            thread_snippet=thread_snippet,
            soul=soul,
        )

    # ── Target resolution + safety gating ────────────────────────────────────

    def resolve_targets(self, senders: list[str] | None, query: str = ""):
        """Resolve explicit emails + a substring query to safe SenderGroups.

        Returns ``(staged, blocked, sensitive)``:
        - ``staged``    — groups safe to act on (blocklisted senders removed).
        - ``blocked``   — sender emails skipped because they are on the blocklist.
        - ``sensitive`` — staged senders flagged bank/legal/health (warn + opt-in).
        """
        from postmind.core.sender_stats import classify_sender_risk
        from postmind.core.storage import BlocklistRepo, get_session

        groups = self._groups()
        wanted = {e.strip().lower() for e in (senders or []) if e.strip()}
        q = (query or "").strip().lower()
        matched = []
        for g in groups:
            em = (g.sender_email or "").lower()
            if em in wanted:
                matched.append(g)
            elif q and (
                q in em or q in (g.sender_name or "").lower() or q in (g.domain or "").lower()
            ):
                matched.append(g)

        blocked_set = (
            BlocklistRepo(get_session()).blocked_emails(self.account_email)
            if self.account_email
            else set()
        )
        staged, blocked, sensitive = [], [], []
        for g in matched:
            if g.sender_email in blocked_set:
                blocked.append(g.sender_email)
                continue
            staged.append(g)
            if classify_sender_risk(g) == "sensitive":
                sensitive.append(g.sender_email)
        return staged, blocked, sensitive

    # ── Staging (returns a confirm token; nothing executes) ──────────────────

    def _new_token(self) -> str:
        self._gc_tokens()
        return secrets.token_urlsafe(16)

    def _gc_tokens(self) -> None:
        cutoff = time.time() - _TOKEN_TTL_SECONDS
        for tok in [t for t, a in self._staged.items() if a.created_at < cutoff or a.used]:
            self._staged.pop(tok, None)

    def _stage(self, action: StagedAction) -> dict:
        self._staged[action.token] = action
        return action.descriptor()

    def stage_cleanup(self, action: str, senders=None, query="", label_name="") -> dict:
        """Stage a bulk reversible action: trash | archive | label | mark_read."""
        if action not in _REVERSIBLE:
            return {"error": f"Unknown action '{action}'."}
        if action != "trash":
            prov = self.provider()
            if not prov.supports("labels"):
                return {
                    "error": f"This account's provider does not support {action.replace('_', ' ')}"
                    " — only Gmail does. Trash is supported."
                }
        if action == "label" and not (label_name or "").strip():
            return {"error": "A label name is required to stage a label action."}

        staged, blocked, sensitive = self.resolve_targets(senders, query)
        if not staged:
            extra = f" ({len(blocked)} protected sender(s) skipped)" if blocked else ""
            return {"error": f"No matching senders to {action.replace('_', ' ')}{extra}."}

        ids = [mid for g in staged for mid in g.message_ids]
        emails = [g.sender_email for g in staged]
        verb = action.replace("_", " ")
        summary = (
            f"{verb} {len(ids)} emails from {len(staged)} sender(s)"
            + (f" → label “{label_name}”" if action == "label" else "")
            + (f"; {len(blocked)} protected skipped" if blocked else "")
        )
        return self._stage(
            StagedAction(
                token=self._new_token(),
                kind=action,
                summary=summary,
                message_ids=ids,
                senders=emails,
                params={
                    "label_name": label_name,
                    "blocked": blocked,
                    "sensitive": sensitive,
                },
                undoable=True,
            )
        )

    def stage_unsubscribe(self, senders=None, query="", also_trash=False) -> dict:
        staged, blocked, sensitive = self.resolve_targets(senders, query)
        if not staged:
            extra = f" ({len(blocked)} protected sender(s) skipped)" if blocked else ""
            return {"error": f"No matching senders to unsubscribe from{extra}."}
        ids = [mid for g in staged for mid in g.message_ids]
        emails = [g.sender_email for g in staged]
        summary = (
            f"unsubscribe from {len(staged)} sender(s)"
            + ("; also trash the back-catalog (undoable)" if also_trash else "")
            + (f"; {len(blocked)} protected skipped" if blocked else "")
        )
        return self._stage(
            StagedAction(
                token=self._new_token(),
                kind="unsubscribe",
                summary=summary,
                message_ids=ids,
                senders=emails,
                params={
                    "also_trash": bool(also_trash),
                    "blocked": blocked,
                    "sensitive": sensitive,
                },
                undoable=False,  # unsubscribe is external; the optional trash IS undoable
            )
        )

    def stage_send(self, to: str, subject: str, body: str) -> dict:
        import re

        to = (to or "").strip()
        if not re.fullmatch(r"[^\s@,]+@[^\s@,]+\.[^\s@,]+", to):
            return {"error": "A single valid recipient address is required."}
        if not (body or "").strip():
            return {"error": "Email body is empty."}
        return self._stage(
            StagedAction(
                token=self._new_token(),
                kind="send",
                summary=f"send “{(subject or '(no subject)').strip()}” to {to}",
                params={"to": to, "subject": (subject or "").strip(), "body": body.strip()},
            )
        )

    def stage_create_agent(
        self,
        email: str = "",
        name: str = "",
        interval_minutes: int = 30,
        voice_style: str = "",
        user_context: str = "",
        run_rules: bool = True,
        run_followups: bool = True,
        run_avoidance: bool = False,
    ) -> dict:
        email = (email or self.account_email or "").strip()
        if not email:
            return {"error": "No account to attach the agent to — connect an account first."}
        params = {
            "email": email,
            "name": name or email.split("@")[0].title(),
            "interval_minutes": int(interval_minutes or 30),
            "voice_style": voice_style or "",
            "user_context": user_context or "",
            "run_rules": bool(run_rules),
            "run_followups": bool(run_followups),
            "run_avoidance": bool(run_avoidance),
        }
        return self._stage(
            StagedAction(
                token=self._new_token(),
                kind="create_agent",
                summary=f"create heartbeat agent for {email} (every {params['interval_minutes']}m)",
                params=params,
            )
        )

    def stage_create_rule(self, natural_language: str) -> dict:
        nl = (natural_language or "").strip()
        if not nl:
            return {"error": "Need the rule in plain English."}
        if not self.account_email:
            return {"error": "No active account — connect one first."}
        try:
            rule = self.ai.translate_rule(nl)
        except Exception as exc:
            return {"error": f"Couldn't translate that rule: {exc}"}
        return self._stage(
            StagedAction(
                token=self._new_token(),
                kind="create_rule",
                summary=f"{rule.explanation} (query: {rule.gmail_query}, action: {rule.action})",
                params={
                    "natural_language": nl,
                    "gmail_query": rule.gmail_query,
                    "action": rule.action,
                    "action_params": rule.action_params,
                    "explanation": rule.explanation,
                    "warnings": rule.warnings or [],
                },
            )
        )

    # ── Confirm / cancel ─────────────────────────────────────────────────────

    def list_staged(self) -> list[dict]:
        self._gc_tokens()
        return [a.descriptor() for a in self._staged.values() if not a.used]

    def cancel(self, token: str) -> dict:
        a = self._staged.pop(token, None)
        if a is None:
            return {"error": "No such staged action (it may have expired or been used)."}
        return {"ok": True, "cancelled": a.kind}

    def confirm(self, token: str) -> dict:
        """Execute a previously staged action, bound to its server-resolved targets."""
        a = self._staged.get(token)
        if a is None:
            return {"error": "No such staged action (it may have expired or been used)."}
        if a.used:
            return {"error": "That action was already confirmed."}
        try:
            result = self._execute(a)
        except Exception as exc:
            return {"error": f"Couldn't complete {a.kind}: {exc}"}
        a.used = True
        self._staged.pop(token, None)
        return result

    def _execute(self, a: StagedAction) -> dict:
        if a.kind in ("trash", "archive", "label", "mark_read"):
            return self._exec_reversible(a)
        if a.kind == "unsubscribe":
            return self._exec_unsubscribe(a)
        if a.kind == "send":
            return self._exec_send(a)
        if a.kind == "create_agent":
            return self._exec_create_agent(a)
        if a.kind == "create_rule":
            return self._exec_create_rule(a)
        return {"error": f"Unknown action kind '{a.kind}'."}

    def _exec_reversible(self, a: StagedAction) -> dict:
        from postmind.core.storage import UndoLogRepo, get_session

        client = self.provider()
        ids = a.message_ids
        label_name = a.params.get("label_name", "")
        action = a.kind
        params = {"label_name": label_name} if action == "label" else {}

        # Record undo BEFORE the provider call so the op is always reversible.
        entry = UndoLogRepo(get_session()).record(
            account_email=self.account_email,
            operation=action,
            message_ids=ids,
            description=(
                f"{action} {len(ids)} emails from {len(a.senders)} sender(s): "
                + ", ".join(a.senders[:3])
                + ("…" if len(a.senders) > 3 else "")
            ),
            metadata={"senders": a.senders, "action_params": params},
        )

        if action == "trash":
            client.batch_trash(ids)
        elif action == "archive":
            client.batch_archive(ids)
        elif action == "mark_read":
            client.batch_label(ids, remove=["UNREAD"])
        elif action == "label":
            gc = getattr(client, "gmail_client", None)
            if gc is None:
                raise ValueError("Labels require a Gmail account.")
            label_id = gc.get_or_create_label(label_name)
            client.batch_label(ids, add=[label_id])
        return {
            "ok": True,
            "action": action,
            "affected": len(ids),
            "undo_id": entry.id,
            "undoable": True,
            "message": f"{action.replace('_', ' ')} done for {len(ids)} emails — undoable for 30 days.",
        }

    def _exec_unsubscribe(self, a: StagedAction) -> dict:
        from postmind.core.storage import UndoLogRepo, get_session
        from postmind.core.unsubscribe import UnsubscribeEngine

        client = self.provider()
        if not client.supports("unsubscribe"):
            raise ValueError("This account's provider does not support unsubscribe — only Gmail.")
        gc = getattr(client, "gmail_client", None)
        if gc is None:
            raise ValueError("Unsubscribe requires a Gmail account.")
        acct = client.get_email_address()

        # One representative message per sender (carries List-Unsubscribe headers).
        by_sender: dict[str, str] = {}
        # message_ids are flattened across senders; re-fetch one per sender via search.
        messages = []
        seen: set[str] = set()
        for mid in a.message_ids:
            if mid in seen:
                continue
            seen.add(mid)
            msgs = client.get_messages_batch([mid])
            if msgs:
                m = msgs[0]
                if m.sender_email not in by_sender:
                    by_sender[m.sender_email] = mid
                    messages.append(m)
        engine = UnsubscribeEngine(gc, acct)
        results = engine.batch_unsubscribe(messages)
        ok = sum(1 for r in results if r.success)

        undo_id = None
        trashed = 0
        if a.params.get("also_trash") and a.message_ids:
            entry = UndoLogRepo(get_session()).record(
                account_email=acct,
                operation="trash",
                message_ids=a.message_ids,
                description=f"Trash back-catalog of {len(a.senders)} unsubscribed sender(s)",
                metadata={"senders": a.senders},
            )
            client.batch_trash(a.message_ids)
            undo_id = entry.id
            trashed = len(a.message_ids)
        return {
            "ok": True,
            "action": "unsubscribe",
            "unsubscribed": ok,
            "of": len(results),
            "trashed": trashed,
            "undo_id": undo_id,
            "message": f"Unsubscribed from {ok}/{len(results)} sender(s)"
            + (f"; trashed {trashed} back-catalog emails (undoable)." if trashed else "."),
        }

    def _exec_send(self, a: StagedAction) -> dict:
        client = self.provider()
        gc = getattr(client, "gmail_client", None)
        if gc is None:
            raise ValueError("Sending mail requires a Gmail account.")
        gc.send(
            to=a.params["to"], subject=a.params.get("subject", ""), body=a.params.get("body", "")
        )
        return {"ok": True, "action": "send", "to": a.params["to"], "message": "Email sent."}

    def _exec_create_agent(self, a: StagedAction) -> dict:
        from postmind.core.storage import AgentRepo, get_session

        p = a.params
        repo = AgentRepo(get_session())
        repo.register(p["email"], p["name"], p["interval_minutes"])
        repo.update_soul(
            p["email"], voice_style=p.get("voice_style"), user_context=p.get("user_context")
        )
        repo.update_features(
            p["email"],
            run_rules=p.get("run_rules", True),
            run_followups=p.get("run_followups", True),
            run_avoidance=p.get("run_avoidance", False),
        )
        repo.set_active(p["email"], True)
        return {
            "ok": True,
            "action": "create_agent",
            "email": p["email"],
            "message": f"Heartbeat agent for {p['email']} created (every {p['interval_minutes']}m).",
        }

    def _exec_create_rule(self, a: StagedAction) -> dict:
        from postmind.core.storage import RuleDefinition, RuleRepo, get_session

        p = a.params
        rule = RuleDefinition(
            account_email=self.account_email,
            name=p["natural_language"][:80],
            natural_language=p["natural_language"],
            gmail_query=p["gmail_query"],
            action=p["action"],
            ai_explanation=p.get("explanation", ""),
        )
        rule.action_params = p.get("action_params", {})
        created = RuleRepo(get_session()).create(rule)
        return {
            "ok": True,
            "action": "create_rule",
            "rule_id": created.id,
            "message": f"Rule created: {p.get('explanation', p['natural_language'])}",
        }


# ── Helpers ────────────────────────────────────────────────────────────────────


def _size(g) -> str:
    return (
        f"{g.total_size_mb:.1f} MB"
        if g.total_size_mb >= 0.1
        else f"{g.total_size_bytes // 1024} KB"
    )


def _build_provider(account_email: str):
    """Construct an EmailProvider for ``account_email`` outside any web request.

    Mirrors ``web/server.py::_build_provider`` (per-account config → provider),
    reading IMAP credentials from the environment.
    """
    import os

    from postmind.core.providers.factory import get_provider

    if account_email:
        cfg = load_account_config(account_email)
        provider_name = cfg.get("provider", "gmail")
    else:
        provider_name = get_settings().provider
        cfg = {}

    if provider_name == "imap":
        s = get_settings()
        return get_provider(
            "imap",
            imap_server=cfg.get("imap_server") or s.imap_server,
            imap_user=cfg.get("imap_user") or s.imap_user,
            imap_password=os.environ.get("POSTMIND_IMAP_PASSWORD", ""),
            imap_port=cfg.get("imap_port") or s.imap_port,
            imap_folder=cfg.get("imap_folder") or s.imap_folder,
        )
    return get_provider("gmail", account_email=account_email)
