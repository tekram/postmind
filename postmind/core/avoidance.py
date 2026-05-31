"""Avoidance detector — surfaces emails you keep seeing but never act on."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from postmind.core.ai_engine import AIEngine
from postmind.core.gmail_client import GmailClient, Message
from postmind.core.storage import EmailRecord, EmailRepo, get_session


@dataclass
class AvoidedEmail:
    record: EmailRecord
    message: Message | None
    ai_insight: str  # Why are you avoiding it + one suggested action
    view_count: int
    days_in_inbox: float


class AvoidanceDetector:
    """
    Identifies emails the user keeps viewing but never acts on — the "inbox anxiety"
    problem that no existing tool addresses.

    How it works:
    - Every time an email is surfaced in a triage list, its view_count is incremented
    - Once view_count >= threshold (default: 3), it's flagged as "avoided"
    - AI analyzes why the user might be avoiding it and suggests a concrete action
    """

    def __init__(self, client: GmailClient, account_email: str, ai: AIEngine | None = None):
        self.client = client
        self.account_email = account_email
        self.ai = ai or AIEngine()
        self.session = get_session()
        self.repo = EmailRepo(self.session)

    def record_view(self, gmail_id: str) -> None:
        """Call this whenever an email is shown to the user in a triage list."""
        self.repo.increment_view(gmail_id)

    def get_avoided_emails(self, with_insights: bool = True) -> list[AvoidedEmail]:
        """
        Return emails the user has been avoiding, enriched with AI insights.
        """
        records = self.repo.find_avoided(self.account_email)
        results = []

        # Fetch full messages for AI insight generation
        if records and with_insights:
            ids = [r.gmail_id for r in records]
            try:
                messages = self.client.get_messages_batch(ids)
                msg_map = {m.id: m for m in messages}
            except Exception:
                msg_map = {}
        else:
            msg_map = {}

        now = datetime.now(timezone.utc)

        for record in records:
            msg = msg_map.get(record.gmail_id)
            days_in_inbox = 0.0
            if record.internal_date:
                ts = datetime.fromtimestamp(record.internal_date / 1000, tz=timezone.utc)
                days_in_inbox = (now - ts).total_seconds() / 86400

            insight = ""
            if with_insights and msg:
                try:
                    insight = self.ai.analyze_avoided_email(msg)
                except Exception:
                    insight = "Unable to generate insight."

            results.append(
                AvoidedEmail(
                    record=record,
                    message=msg,
                    ai_insight=insight,
                    view_count=record.view_count,
                    days_in_inbox=round(days_in_inbox, 1),
                )
            )

        return results

    def process(self, gmail_id: str, action: str, client: GmailClient | None = None) -> None:
        """
        Mark an avoided email as acted on (so it stops appearing).
        action: "archive", "reply", "delete", "delegate", "snooze"
        """
        c = client or self.client
        record = self.repo.get(gmail_id)
        if not record:
            return

        if action == "archive":
            c.archive(gmail_id)
        elif action == "delete":
            c.trash(gmail_id)

        self.repo.mark_acted_on(gmail_id)

    def get_stats(self) -> dict:
        records = self.repo.find_avoided(self.account_email, threshold=1)
        return {
            "total_avoided": len(records),
            "avg_view_count": (
                round(sum(r.view_count for r in records) / len(records), 1) if records else 0
            ),
            "max_view_count": max((r.view_count for r in records), default=0),
        }
