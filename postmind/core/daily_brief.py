"""Daily Brief generator — gathers local DB stats and optionally calls the AI."""

from __future__ import annotations

import json
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
                # Auto-refresh if the cached brief is older than 1 hour so new
                # emails that arrived since last generation are picked up.
                age = datetime.now(timezone.utc) - existing.generated_at.replace(
                    tzinfo=timezone.utc
                ) if existing.generated_at else timedelta(days=1)
                if age < timedelta(hours=1):
                    return existing

        stats = self._gather_stats()
        content, ai_used = self._generate_content(stats)

        # Persist the emails the brief is about so the UI can render deep links.
        # Deals (is_deal=True) are split into a separate tab; the inbox tab shows
        # AI-classified high-priority items plus unclassified recent arrivals.
        deals = stats.get("deals_items", [])
        deals_json = json.dumps(deals[:50]) if deals else None
        identified = stats["high_priority_items"] + stats.get("recent_unclassified", [])
        items_json = json.dumps(identified[:50]) if identified else None

        # ── Newsletter & Promotions digest ─────────────────────────────────
        newsletters_json = None
        promotions_json = None
        digest_trash_after = None

        settings = get_settings()
        if settings.ai_mode in ("cloud", "local"):
            try:
                from postmind.core.ai_engine import AIEngine
                from postmind.core.storage import DigestExemptionRepo, EmailRepo

                ai_for_digest = AIEngine()
                cutoff_ms = int(
                    (datetime.now(timezone.utc) - timedelta(hours=24)).timestamp() * 1000
                )
                all_records = EmailRepo(session).get_inbox(self.account_email, limit=500)
                last24h = [r for r in all_records if (r.internal_date or 0) >= cutoff_ms]
                exempted = DigestExemptionRepo(session).get_set(self.account_email)

                nl_items = self._generate_newsletter_digest(
                    session, ai_for_digest, self.account_email, last24h, exempted
                )
                newsletter_senders = {item["sender_email"] for item in nl_items}
                pr_items = self._generate_promo_digest(
                    session,
                    ai_for_digest,
                    self.account_email,
                    deals,
                    exempted,
                    newsletter_senders,
                )

                if nl_items:
                    newsletters_json = json.dumps(nl_items)
                if pr_items:
                    promotions_json = json.dumps(pr_items)
                if nl_items or pr_items:
                    digest_trash_after = datetime.now(timezone.utc) + timedelta(hours=48)
            except Exception as exc:
                logger.warning("Digest generation failed: %s", exc)

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
            deals_json=deals_json,
            newsletters_json=newsletters_json,
            promotions_json=promotions_json,
            digest_trash_after=digest_trash_after,
        )
        return repo.save(brief)

    def _generate_newsletter_digest(
        self,
        session,
        ai,
        account_email: str,
        last24h_records: list,
        exempted_senders: set,
    ) -> list[dict]:
        """Build newsletter digest items from emails with a List-Unsubscribe header."""
        from collections import defaultdict

        grouped: dict[str, list] = defaultdict(list)
        for r in last24h_records:
            if r.list_unsubscribe and r.list_unsubscribe.strip():
                key = (r.sender_email or "").lower()
                if key and key not in exempted_senders:
                    grouped[key].append(r)

        items = []
        for sender_email, records in grouped.items():
            records.sort(key=lambda r: r.internal_date or 0, reverse=True)
            sender_name = records[0].sender_name or records[0].sender_email or sender_email
            emails_info = [
                {"subject": r.subject or "", "snippet": r.snippet or ""}
                for r in records[:5]
            ]
            try:
                bullets = ai.summarize_newsletter_sender(sender_name, emails_info)
            except Exception:
                bullets = ["(Summary unavailable)"] * 3
            items.append({
                "sender": sender_name,
                "sender_email": sender_email,
                "email_ids": [r.gmail_id for r in records],
                "summary_bullets": bullets[:3],
                "exempted": False,
            })

        items.sort(key=lambda x: x["sender"].lower())
        return items

    def _generate_promo_digest(
        self,
        session,
        ai,
        account_email: str,
        deals_items: list[dict],
        exempted_senders: set,
        newsletter_senders: set,
    ) -> list[dict]:
        """Build promo digest items from deal-scored emails (excluding newsletters)."""
        from collections import defaultdict

        from postmind.core.storage import EmailRepo

        email_repo = EmailRepo(session)
        grouped: dict[str, list] = defaultdict(list)
        for item in deals_items:
            sender_email = (item.get("sender_email") or "").lower()
            if not sender_email:
                continue
            if sender_email in exempted_senders or sender_email in newsletter_senders:
                continue
            grouped[sender_email].append(item)

        items = []
        for sender_email, group_items in grouped.items():
            group_items.sort(key=lambda x: x.get("deal_score", 0), reverse=True)
            top = group_items[0]
            sender_name = top.get("sender") or sender_email
            gmail_id = top.get("gmail_id", "")
            record = email_repo.get(gmail_id) if gmail_id else None
            snippet = (record.snippet or "") if record else ""
            try:
                offer_line = ai.extract_promo_offer_line(
                    sender=sender_name,
                    subject=top.get("subject", ""),
                    snippet=snippet,
                )
            except Exception:
                offer_line = (top.get("subject", "") or "")[:80]
            items.append({
                "sender": sender_name,
                "sender_email": sender_email,
                "email_ids": [i.get("gmail_id") for i in group_items if i.get("gmail_id")],
                "offer_line": offer_line,
                "deal_score": top.get("deal_score", 1),
                "exempted": False,
            })

        items.sort(key=lambda x: x.get("deal_score", 0), reverse=True)
        return items

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

        # Classify any unread emails that haven't been seen by the AI yet so new
        # arrivals aren't silently skipped because they lack a classification entry.
        # Local LLMs are slow: keep batches small (matching the triage page) and
        # cap the total so brief generation stays within a reasonable wall-clock time.
        # Load behavioral signals before classification so priors can be passed to the AI
        from postmind.core.storage import UserActionRepo
        action_repo = UserActionRepo(session)
        trash_senders = action_repo.high_trash_senders(self.account_email)
        replied_senders = action_repo.replied_senders(self.account_email)

        settings = get_settings()
        if settings.ai_mode in ("cloud", "local"):
            is_local = settings.ai_mode == "local"
            batch_size = 3 if is_local else settings.ai_max_classify_batch
            max_to_classify = 9 if is_local else 50  # ~3 batches for local
            unclassified_records = [
                r for r in records if r.is_unread and r.gmail_id not in cached_cls
            ][:max_to_classify]
            if unclassified_records:
                try:
                    from concurrent.futures import ThreadPoolExecutor

                    from postmind.core.ai_engine import AIEngine
                    from postmind.core.gmail_client import Message, MessageHeader

                    def _chunks(lst, n):
                        for i in range(0, len(lst), n):
                            yield lst[i : i + n]

                    msgs = [
                        Message(
                            id=r.gmail_id,
                            thread_id=r.thread_id or r.gmail_id,
                            label_ids=[],
                            snippet=r.snippet or "",
                            headers=MessageHeader(
                                subject=r.subject or "",
                                from_=(
                                    f"{r.sender_name} <{r.sender_email}>"
                                    if r.sender_name
                                    else r.sender_email or ""
                                ),
                            ),
                            internal_date=r.internal_date or 0,
                        )
                        for r in unclassified_records
                    ]
                    ai = AIEngine()
                    priors = action_repo.sender_action_counts(self.account_email)
                    chunks = list(_chunks(msgs, batch_size))
                    workers = max(1, min(settings.ai_classify_parallelism, len(chunks)))
                    classified: list = []
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        for batch_result in pool.map(
                            lambda ch: ai.classify_batch(ch, sender_priors=priors), chunks
                        ):
                            classified.extend(batch_result)
                    # Carry deal_score transiently — not persisted to classification cache
                    fresh_deal_scores = {c.gmail_id: c.deal_score for c in classified if c.deal_score > 0}
                    new_cls = [
                        {
                            "gmail_id": c.gmail_id,
                            "category": c.category,
                            "priority": c.priority,
                            "explanation": c.explanation,
                            "suggested_action": c.suggested_action,
                            "requires_reply": c.requires_reply,
                            "deadline_hint": c.deadline_hint,
                        }
                        for c in classified
                    ]
                    ClassificationCacheRepo(session).upsert_many(new_cls)
                    for item in new_cls:
                        cached_cls[item["gmail_id"]] = item
                    # Attach deal_score transiently so the loop below can route them
                    for gid, score in fresh_deal_scores.items():
                        if gid in cached_cls:
                            cached_cls[gid]["deal_score"] = score
                except Exception as exc:
                    logger.warning("Daily brief: on-demand classification failed: %s", exc)

        high_priority_items = []
        deals_items = []
        for r in records:
            if not r.is_unread:
                continue
            cls = cached_cls.get(r.gmail_id, {})
            item_dict = {
                "gmail_id": r.gmail_id,
                "sender": r.sender_name or r.sender_email,
                "sender_email": (r.sender_email or "").lower(),
                "subject": r.subject or "(no subject)",
                "internal_date": r.internal_date or 0,
                "is_unread": r.is_unread,
            }
            score = cls.get("deal_score", 0)
            if score and score >= 1:
                deals_items.append({**item_dict, "deal_score": score})
            elif cls.get("priority") == "high" or cls.get("category") == "action_required":
                high_priority_items.append(item_dict)

        # Promote replied-to senders to the front of high_priority_items
        if replied_senders:
            high_priority_items.sort(
                key=lambda x: 0 if x.get("sender_email") in replied_senders else 1
            )

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

        # Unread emails from the last 7 days with no classification entry — surfaced
        # alongside high-priority items so new arrivals always appear regardless of
        # how deep they sit in the unclassified backlog. Deduplicate by sender (keep
        # only the most recent email per sender) before capping at 20. Skip senders
        # the user consistently trashes — they don't need to surface in the brief.
        week_ago_ms = int((now - timedelta(days=7)).timestamp() * 1000)
        hp_ids = {item["gmail_id"] for item in high_priority_items}
        seen_senders: set[str] = set()
        recent_unclassified: list[dict] = []
        for r in records:  # already sorted newest-first by get_inbox
            sender_key_email = (r.sender_email or "").lower()
            if not (
                r.is_unread
                and r.internal_date >= week_ago_ms
                and r.gmail_id not in cached_cls
                and r.gmail_id not in hp_ids
            ):
                continue
            # Skip senders the user consistently trashes
            if sender_key_email in trash_senders:
                continue
            # Dedup by name first (collapses Yelp, GitHub, etc. into one slot per org)
            # then fall back to email so anonymous senders aren't merged.
            sender_key = (r.sender_name or "").lower().strip() or sender_key_email
            if sender_key in seen_senders:
                continue
            seen_senders.add(sender_key)
            recent_unclassified.append({
                "gmail_id": r.gmail_id,
                "sender": r.sender_name or r.sender_email,
                "sender_email": sender_key_email,
                "subject": r.subject or "(no subject)",
                "internal_date": r.internal_date or 0,
                "is_unread": r.is_unread,
            })
            if len(recent_unclassified) >= 20:
                break

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

        deals_items.sort(key=lambda x: x.get("deal_score", 0), reverse=True)

        return {
            "unread_count": unread_count,
            "new_since_yesterday": new_since_yesterday,
            "high_priority_items": high_priority_items,
            "deals_items": deals_items,
            "recent_unclassified": recent_unclassified,
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
                    recent_unclassified=stats.get("recent_unclassified", []),
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

        ru = stats.get("recent_unclassified", [])
        if ru:
            lines.append(f"\nUnreviewed this week ({len(ru)}):")
            for item in ru[:5]:
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
