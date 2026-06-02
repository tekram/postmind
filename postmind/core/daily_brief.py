"""Daily Brief generator — gathers local DB stats and optionally calls the AI."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from postmind.config import get_settings

logger = logging.getLogger(__name__)


class DailyBriefGenerator:
    """Gather inbox stats from local DB and produce a morning brief.

    No Gmail API calls — everything comes from the local cache.
    """

    def __init__(self, account_email: str):
        self.account_email = account_email

    def get_or_generate(self, force: bool = False) -> "DailyBrief":
        """Return today's cached brief or generate it.

        force=True always regenerates (used by the "Generate Now" button).
        """
        from postmind.core.storage import DailyBrief, DailyBriefRepo, get_session

        session = get_session()
        repo = DailyBriefRepo(session)
        today_str = datetime.now(timezone.utc).date().isoformat()

        if not force:
            existing = repo.get_today(self.account_email, today_str)
            if existing:
                return existing

        stats = self._gather_stats()
        content, ai_used = self._generate_content(stats)

        # Persist the emails the brief is actually about (high-priority first,
        # else the recent unread it surfaced) so the UI can render deep links
        # to each one. Mirrors the "attention" set the AI narrative describes.
        import json
        identified = stats["high_priority_items"] or stats["recent_unread"]
        items_json = json.dumps(identified[:8]) if identified else None

        brief = DailyBrief(
            account_email=self.account_email,
            brief_date=today_str,
            content=content,
            ai_used=ai_used,
            unread_count=stats["unread_count"],
            new_since_yesterday=stats["new_since_yesterday"],
            high_priority_count=len(stats["high_priority_items"]),
            overdue_followups_count=len(stats["overdue_follow_ups"]),
            avoided_count=stats["avoided_count"],
            items_json=items_json,
        )
        return repo.save(brief)

    def _gather_stats(self) -> dict:
        """Pull all brief data from local DB. Zero API calls."""
        from postmind.core.storage import (
            ClassificationCacheRepo,
            EmailRepo,
            FollowUpRepo,
            get_session,
        )

        session = get_session()
        email_repo = EmailRepo(session)
        now = datetime.now(timezone.utc)
        yesterday_ms = int((now - timedelta(days=1)).timestamp() * 1000)

        records = email_repo.get_inbox(self.account_email, limit=500)

        unread_count = sum(1 for r in records if r.is_unread)
        new_since_yesterday = sum(
            1 for r in records if r.is_unread and r.internal_date >= yesterday_ms
        )

        unread_ids = [r.gmail_id for r in records if r.is_unread]
        cached_cls = ClassificationCacheRepo(session).get_many(unread_ids) if unread_ids else {}

        high_priority_items = []
        for r in records:
            if not r.is_unread:
                continue
            cls = cached_cls.get(r.gmail_id, {})
            if cls.get("priority") == "high" or cls.get("category") == "action_required":
                high_priority_items.append({
                    "gmail_id": r.gmail_id,
                    "sender": r.sender_name or r.sender_email,
                    "subject": r.subject or "(no subject)",
                })

        # Most-recent unread, so the brief can name concrete emails even when
        # nothing has been classified yet (classification cache may be empty).
        recent_unread = [
            {
                "gmail_id": r.gmail_id,
                "sender": r.sender_name or r.sender_email,
                "subject": r.subject or "(no subject)",
            }
            for r in records
            if r.is_unread
        ][:8]

        due_fus = FollowUpRepo(session).get_due(self.account_email)
        overdue_follow_ups = [
            {
                "to": fu.to_email,
                "subject": fu.subject or "(no subject)",
                "days_overdue": max(0, (now - fu.remind_at.replace(tzinfo=timezone.utc)).days)
                if fu.remind_at.tzinfo is None
                else max(0, (now - fu.remind_at).days),
            }
            for fu in due_fus
        ]

        avoided = email_repo.find_avoided(self.account_email)

        return {
            "unread_count": unread_count,
            "new_since_yesterday": new_since_yesterday,
            "high_priority_items": high_priority_items,
            "recent_unread": recent_unread,
            "overdue_follow_ups": overdue_follow_ups,
            "avoided_count": len(avoided),
        }

    def _generate_content(self, stats: dict) -> tuple[str, bool]:
        """Return (content_text, ai_used). Falls back to plain stats if AI is off."""
        settings = get_settings()
        today_formatted = datetime.now(timezone.utc).strftime("%A, %-d %B %Y")

        if settings.ai_mode in ("cloud", "local"):
            try:
                from postmind.core.ai_engine import AIEngine
                ai = AIEngine()
                content = ai.generate_daily_brief(
                    today=today_formatted,
                    unread_count=stats["unread_count"],
                    new_since_yesterday=stats["new_since_yesterday"],
                    high_priority_items=stats["high_priority_items"],
                    recent_unread=stats["recent_unread"],
                    overdue_follow_ups=stats["overdue_follow_ups"],
                    avoided_count=stats["avoided_count"],
                )
                return content, True
            except Exception as exc:
                logger.warning("Daily brief AI generation failed: %s", exc)

        return self._stats_fallback(stats, today_formatted), False

    def _stats_fallback(self, stats: dict, today_formatted: str) -> str:
        lines = [f"Daily Brief — {today_formatted}", ""]
        u = stats["unread_count"]
        n = stats["new_since_yesterday"]
        hp = len(stats["high_priority_items"])
        fu = len(stats["overdue_follow_ups"])
        av = stats["avoided_count"]

        if u == 0:
            lines.append("Your inbox is clear. Nothing unread.")
        else:
            lines.append(
                f"You have {u} unread email{'s' if u != 1 else ''}. "
                f"{n} arrived since yesterday."
            )

        if hp:
            lines.append(f"\nAction items ({hp}):")
            for item in stats["high_priority_items"][:5]:
                lines.append(f"  • {item['sender']}: {item['subject'][:70]}")
        elif stats.get("recent_unread"):
            # Nothing classified yet — show the latest unread so the brief is
            # never empty and the user has something concrete to act on.
            lines.append("\nLatest unread:")
            for item in stats["recent_unread"][:5]:
                lines.append(f"  • {item['sender']}: {item['subject'][:70]}")

        if fu:
            lines.append(f"\nOverdue follow-ups ({fu}):")
            for item in stats["overdue_follow_ups"][:3]:
                lines.append(
                    f"  • {item['to']}: {item['subject'][:60]} ({item['days_overdue']}d overdue)"
                )

        if av:
            lines.append(f"\nAvoided: {av} email{'s' if av != 1 else ''} you keep skipping.")

        if not hp and not fu and not av:
            lines.append("\nNo urgent action items. Good time to catch up on low-priority mail.")

        return "\n".join(lines)
