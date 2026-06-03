"""Autodraft — generate reply drafts in the user's voice and park them in Gmail.

Creating a draft is non-destructive: nothing reaches the recipient until a human
explicitly hits Send. That lets us generate drafts freely (on demand, or proactively
in the heartbeat daemon) while keeping a hard human gate on sending.

Design notes:
  - Recipient + thread targets are ALWAYS resolved by this code from the fetched
    message, never from model free-text — contains prompt injection from email bodies.
  - Sensitive senders (bank/health/legal/gov/school) are never autodrafted.
  - Composition requires cloud AI mode (AIEngine.compose_email gates on it); the
    MockAIEngine stub lets the full pipeline run in tests without a key.
  - Drafts only work on providers that support the 'drafts' capability (Gmail).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from postmind.core.gmail_client import Message
from postmind.core.sender_stats import _is_sensitive_domain
from postmind.core.storage import DraftRecord, DraftRepo, get_session

# Default minimum confidence for proactive (daemon) drafting. On-demand drafting
# is user-initiated and bypasses the threshold.
DEFAULT_CONFIDENCE_THRESHOLD = 60

# Phrase signals that an inbound message wants a reply from the user.
_REQUEST_PHRASES = (
    "can you",
    "could you",
    "would you",
    "please",
    "let me know",
    "thoughts?",
    "what do you think",
    "your thoughts",
    "any update",
    "circling back",
    "following up",
    "get back to me",
    "lmk",
    "wdyt",
)
_MEETING_PHRASES = (
    "meet",
    "meeting",
    "schedule",
    "calendar",
    "available",
    "availability",
    "find time",
    "hop on a call",
    "quick call",
    "sync up",
    "catch up",
)
# Senders we never draft replies to.
_NOREPLY_RE = re.compile(r"(no[-_.]?reply|do[-_.]?not[-_.]?reply|notifications?@|mailer@)", re.I)


@dataclass
class Draft:
    subject: str
    body: str


def _split_draft(text: str) -> tuple[str, str]:
    """Split a composed draft ('Subject: ...\\n\\n<body>') into (subject, body)."""
    text = (text or "").strip()
    subject, body = "", text
    if text.lower().startswith("subject:"):
        first, _, rest = text.partition("\n")
        subject = first.split(":", 1)[1].strip()
        body = rest.lstrip("\n")
    return subject, body


def _re_subject(subject: str) -> str:
    """Return a thread-safe reply subject (single 'Re:' prefix)."""
    s = (subject or "").strip()
    if not s:
        return "Re:"
    return s if s.lower().startswith("re:") else f"Re: {s}"


def _domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


class AutodraftService:
    """Stateless-ish orchestrator. Provider + AI + account are injected per request."""

    def __init__(self, provider, ai, account_email: str, soul: dict | None = None):
        self.provider = provider
        self.ai = ai
        self.account_email = account_email
        self.soul = soul or {}

    # ── Eligibility ────────────────────────────────────────────────────────────

    def supports_drafts(self) -> bool:
        return bool(self.provider.supports("drafts"))

    def is_sensitive(self, sender_email: str) -> bool:
        return _is_sensitive_domain(_domain(sender_email))

    def should_draft(self, msg: Message) -> tuple[bool, str, int]:
        """Return (eligible, trigger, confidence 0–100) for an inbound message.

        Heuristic — good enough to gate proactive drafting and to run keyless in
        tests. Bulk/automated senders and sensitive domains are excluded outright.
        """
        sender = msg.sender_email
        if not sender or "@" not in sender:
            return False, "", 0
        if _NOREPLY_RE.search(sender) or _NOREPLY_RE.search(msg.headers.from_ or ""):
            return False, "", 0
        if msg.headers.list_unsubscribe:  # newsletter / mass mail
            return False, "", 0
        if self.is_sensitive(sender):
            return False, "", 0

        hay = f"{msg.headers.subject or ''}\n{msg.snippet or ''}\n{msg.body_text or ''}".lower()
        if any(p in hay for p in _MEETING_PHRASES):
            return True, "meeting", 80
        if any(p in hay for p in _REQUEST_PHRASES):
            return True, "reply_needed", 75
        if "?" in hay:
            return True, "reply_needed", 60
        return False, "", 30

    # ── Context + composition ──────────────────────────────────────────────────

    def build_context(self, msg: Message) -> dict:
        """Server-resolved reply targets + grounding for composition."""
        sender_name = msg.sender_name or msg.sender_email
        snippet = (msg.body_text or msg.snippet or "").strip()
        return {
            "to_email": msg.sender_email,
            "thread_id": msg.thread_id or "",
            "in_reply_to_gmail_id": msg.id,
            "in_reply_to_rfc_id": msg.headers.message_id or "",
            "subject": _re_subject(msg.headers.subject or ""),
            "recipient_context": f"Replying to {sender_name} <{msg.sender_email}>.",
            "thread_snippet": snippet[:1500],
        }

    def compose(self, msg: Message, context: dict, instruction: str = "") -> Draft:
        """Generate the draft text via the AI engine and split it."""
        intent = instruction.strip() or (
            f"Write a concise, helpful reply to this email from "
            f"{msg.sender_name or msg.sender_email}. Address what they asked."
        )
        raw = self.ai.compose_email(
            intent=intent,
            recipient_context=context["recipient_context"],
            thread_snippet=context["thread_snippet"],
            soul=self.soul,
        )
        subject, body = _split_draft(raw)
        return Draft(subject=subject or context["subject"], body=body)

    # ── Persistence ────────────────────────────────────────────────────────────

    def persist(
        self,
        context: dict,
        draft: Draft,
        trigger: str,
        confidence: int,
        model: str = "",
    ) -> DraftRecord:
        """Create the Gmail draft (in-thread) and record it for review."""
        gc = getattr(self.provider, "gmail_client", None)
        if gc is None:
            raise ValueError("Drafting requires a Gmail account.")
        draft_id = gc.create_draft(
            to=context["to_email"],
            subject=draft.subject,
            body=draft.body,
            thread_id=context["thread_id"] or None,
            in_reply_to=context["in_reply_to_rfc_id"] or None,
        )
        rec = DraftRecord(
            account_email=self.account_email,
            gmail_draft_id=draft_id,
            thread_id=context["thread_id"],
            in_reply_to_gmail_id=context["in_reply_to_gmail_id"],
            in_reply_to_rfc_id=context["in_reply_to_rfc_id"],
            to_email=context["to_email"],
            subject=draft.subject,
            body=draft.body,
            trigger=trigger,
            confidence=confidence,
            model=model,
        )
        return DraftRepo(get_session()).upsert_for_thread(rec)

    # ── Entry points ───────────────────────────────────────────────────────────

    def draft_reply(
        self,
        gmail_id: str,
        instruction: str = "",
        trigger: str = "manual",
        model: str = "",
    ) -> DraftRecord:
        """On-demand: draft a reply to one message. User-initiated, no threshold."""
        if not self.supports_drafts():
            raise ValueError("This account doesn't support drafting (Gmail only).")
        msgs = self.provider.get_messages_batch([gmail_id])
        if not msgs:
            raise ValueError("Couldn't load that message.")
        msg = msgs[0]
        if self.is_sensitive(msg.sender_email):
            raise ValueError(
                "This sender looks sensitive (bank/health/legal). "
                "Draft a reply manually rather than with AI."
            )
        context = self.build_context(msg)
        draft = self.compose(msg, context, instruction=instruction)
        _, detected_trigger, confidence = self.should_draft(msg)
        return self.persist(
            context,
            draft,
            trigger=trigger if trigger != "manual" else (detected_trigger or "manual"),
            confidence=confidence if trigger == "manual" else confidence,
            model=model,
        )

    def run_for_inbox(
        self,
        limit: int = 20,
        threshold: int = DEFAULT_CONFIDENCE_THRESHOLD,
        model: str = "",
    ) -> list[DraftRecord]:
        """Proactive: scan recent unread inbox and pre-draft high-confidence replies.

        Skips threads that already have an open draft so the daemon is idempotent.
        """
        if not self.supports_drafts():
            return []
        ids = self.provider.list_message_ids(query="in:inbox is:unread", max_results=limit)
        if not ids:
            return []
        repo = DraftRepo(get_session())
        created: list[DraftRecord] = []
        for msg in self.provider.get_messages_batch(ids):
            eligible, trigger, confidence = self.should_draft(msg)
            if not eligible or confidence < threshold:
                continue
            if msg.thread_id and repo.open_for_thread(self.account_email, msg.thread_id):
                continue
            context = self.build_context(msg)
            try:
                draft = self.compose(msg, context)
                created.append(self.persist(context, draft, trigger, confidence, model=model))
            except Exception:
                continue  # one bad message shouldn't abort the batch
        return created
