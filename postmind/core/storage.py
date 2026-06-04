"""SQLite storage layer — all state lives locally, nothing leaves your machine."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy import func
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

import postmind.config as _cfg

# ── Base ─────────────────────────────────────────────────────────────────────


class Base(DeclarativeBase):
    pass


# ── Models ───────────────────────────────────────────────────────────────────


class Account(Base):
    """A connected Gmail account."""

    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, unique=True, nullable=False)
    display_name = Column(String, default="")
    added_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_synced_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)
    welcomed_at = Column(DateTime, nullable=True)  # first-run welcome screen seen


class EmailRecord(Base):
    """Cached metadata for an email — avoids re-fetching from Gmail API."""

    __tablename__ = "emails"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_email = Column(String, nullable=False)
    gmail_id = Column(String, nullable=False, unique=True)
    thread_id = Column(String, nullable=False)
    subject = Column(String, default="")
    sender_email = Column(String, default="")
    sender_name = Column(String, default="")
    snippet = Column(Text, default="")
    label_ids_json = Column(Text, default="[]")  # JSON list of label IDs
    internal_date = Column(Integer, default=0)  # ms since epoch
    size_estimate = Column(Integer, default=0)
    is_unread = Column(Boolean, default=True)
    is_inbox = Column(Boolean, default=True)
    has_attachment = Column(Boolean, default=False)
    list_unsubscribe = Column(Text, default="")
    ai_category = Column(String, default="")  # AI-assigned category
    ai_explanation = Column(Text, default="")  # Why AI categorized it this way
    view_count = Column(Integer, default=0)  # Times surfaced but not acted on
    last_viewed_at = Column(DateTime, nullable=True)
    is_acted_on = Column(Boolean, default=False)  # Archived/replied/deleted
    synced_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    @property
    def label_ids(self) -> list[str]:
        return json.loads(self.label_ids_json or "[]")

    @label_ids.setter
    def label_ids(self, value: list[str]) -> None:
        self.label_ids_json = json.dumps(value)


class FollowUp(Base):
    """Sent emails being tracked for follow-up."""

    __tablename__ = "follow_ups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_email = Column(String, nullable=False)
    sent_message_id = Column(String, nullable=False)
    thread_id = Column(String, nullable=False)
    to_email = Column(String, nullable=False)
    subject = Column(String, default="")
    sent_at = Column(DateTime, nullable=False)
    remind_at = Column(DateTime, nullable=False)  # When to surface the reminder
    remind_only_if_no_reply = Column(Boolean, default=True)
    replied = Column(Boolean, default=False)  # A reply was detected
    replied_at = Column(DateTime, nullable=True)
    snoozed_until = Column(DateTime, nullable=True)
    dismissed = Column(Boolean, default=False)
    note = Column(Text, default="")


class DraftRecord(Base):
    """An AI-generated reply draft parked in Gmail Drafts for human review.

    Creating a draft is non-destructive and never reaches the recipient — only
    an explicit human Send does. One open draft per (account_email, thread_id);
    a new inbound message on the thread supersedes the prior draft.
    """

    __tablename__ = "draft_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_email = Column(String, nullable=False)
    gmail_draft_id = Column(String, default="")  # Gmail-side draft ID
    thread_id = Column(String, default="")
    in_reply_to_gmail_id = Column(String, default="")  # the message this replies to
    in_reply_to_rfc_id = Column(String, default="")  # original RFC-2822 Message-ID
    to_email = Column(String, nullable=False)
    subject = Column(String, default="")
    body = Column(Text, default="")
    trigger = Column(String, default="manual")  # manual | reply_needed | meeting | followup
    confidence = Column(Integer, default=0)  # 0–100
    model = Column(String, default="")  # which AI backend produced it
    status = Column(String, default="ready")  # ready | edited | sent | dismissed | stale
    draft_type = Column(String, default="gmail")  # "gmail" | "local"
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    reviewed_at = Column(DateTime, nullable=True)


class UndoLogEntry(Base):
    """A reversible bulk operation — kept for undo_window_days."""

    __tablename__ = "undo_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_email = Column(String, nullable=False)
    operation = Column(String, nullable=False)  # "archive", "trash", "label", "unlabel"
    description = Column(Text, default="")  # Human-readable summary
    message_ids_json = Column(Text, nullable=False)  # JSON list of affected IDs
    metadata_json = Column(Text, default="{}")  # Extra data (label names etc.)
    executed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    undone_at = Column(DateTime, nullable=True)
    is_undone = Column(Boolean, default=False)
    expires_at = Column(DateTime, nullable=False)  # Auto-purge after this

    @property
    def message_ids(self) -> list[str]:
        return json.loads(self.message_ids_json)

    @message_ids.setter
    def message_ids(self, value: list[str]) -> None:
        self.message_ids_json = json.dumps(value)

    @property
    def op_metadata(self) -> dict:
        return json.loads(self.metadata_json or "{}")

    @op_metadata.setter
    def op_metadata(self, value: dict) -> None:
        self.metadata_json = json.dumps(value)


class RuleDefinition(Base):
    """A user-defined rule (either NL-defined or manually created)."""

    __tablename__ = "rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_email = Column(String, nullable=False)
    name = Column(String, nullable=False)
    natural_language = Column(Text, default="")  # Original NL input
    gmail_query = Column(Text, nullable=False)  # Translated Gmail search query
    action = Column(String, nullable=False)  # "archive", "trash", "label", "mark_read"
    action_params_json = Column(Text, default="{}")  # e.g. {"label": "newsletters"}
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_run_at = Column(DateTime, nullable=True)
    run_count = Column(Integer, default=0)
    ai_explanation = Column(Text, default="")
    proposed_at = Column(DateTime, nullable=True)  # set for synthesized proposals awaiting confirmation

    @property
    def action_params(self) -> dict:
        return json.loads(self.action_params_json or "{}")

    @action_params.setter
    def action_params(self, value: dict) -> None:
        self.action_params_json = json.dumps(value)


class UnsubscribeRecord(Base):
    """Track unsubscribe attempts and their outcomes."""

    __tablename__ = "unsubscribes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_email = Column(String, nullable=False)
    sender_email = Column(String, nullable=False)
    sender_domain = Column(String, nullable=False)
    method = Column(String, default="")  # "header_mailto", "header_url", "headless"
    status = Column(String, default="pending")  # "pending", "success", "failed", "bounced"
    attempted_at = Column(DateTime, nullable=True)
    confirmed_at = Column(DateTime, nullable=True)
    last_received_at = Column(DateTime, nullable=True)  # Last email from this sender post-unsub


class ClassificationCacheRecord(Base):
    """Cached AI triage classification for a single message.

    Keyed by ``gmail_id`` — a Gmail message id is immutable and its
    subject/sender/snippet never change, so a classification is valid for the
    life of the message. This lets the Triage tab skip re-classifying (and re-
    paying the LLM latency for) messages it has already seen.
    """

    __tablename__ = "classification_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)
    gmail_id = Column(String, nullable=False, unique=True)
    category = Column(String, default="other")
    priority = Column(String, default="medium")
    explanation = Column(Text, default="")
    suggested_action = Column(String, default="keep")
    requires_reply = Column(Boolean, default=False)
    deadline_hint = Column(String, default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class SenderBlocklist(Base):
    """Senders the user has protected from future purge operations."""

    __tablename__ = "sender_blocklist"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_email = Column(String, nullable=False)
    sender_email = Column(String, nullable=False)
    sender_domain = Column(String, nullable=False)
    reason = Column(String, default="user_protected")  # "user_protected" | "undo_feedback"
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class CleanupFeedbackRecord(Base):
    """Per-decision record of what the user did with each batch member on the
    /cleanup page — drives the learning loop (per-sender confidence priors and
    'automate this' rule offers)."""

    __tablename__ = "cleanup_feedback"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_email = Column(String, nullable=False, index=True)
    sender_email = Column(String, nullable=False)
    batch_key = Column(String, nullable=False)
    action = Column(String, nullable=False)  # "trash" | "archive"
    decision = Column(String, nullable=False)  # "approved" | "skipped" | "dropped"
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class UserActionRecord(Base):
    """Per-message behavioral signal captured whenever the user acts on an email
    from any surface (brief, triage, bulk, agent). Powers the learning loop:
    prior injection into classification prompts, brief ranking, and rule synthesis."""

    __tablename__ = "user_actions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_email = Column(String, nullable=False, index=True)
    gmail_id = Column(String, nullable=False)  # not unique — undo + re-action possible
    sender_email = Column(String, nullable=False)
    sender_name = Column(String, default="")
    subject = Column(String, default="")
    action = Column(String, nullable=False)   # trash | archive | reply | keep | skip
    source = Column(String, nullable=False)   # brief | triage | bulk | agent
    ai_category = Column(String, default="")  # classification at time of action
    ai_priority = Column(String, default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)


# ── Engine / session factory ─────────────────────────────────────────────────


_engine = None
_SessionLocal = None


def _run_migrations(engine) -> None:
    """Apply incremental schema changes that SQLAlchemy's create_all cannot handle."""
    new_columns = {
        "agents": [
            ("voice_style", "TEXT"),
            ("user_context", "TEXT"),
            ("writing_guidelines", "TEXT"),
            ("run_rules", "INTEGER DEFAULT 1"),
            ("run_followups", "INTEGER DEFAULT 1"),
            ("run_avoidance", "INTEGER DEFAULT 0"),
            ("run_daily_brief", "INTEGER DEFAULT 0"),
            ("run_autodraft", "INTEGER DEFAULT 0"),
        ],
        "accounts": [
            ("welcomed_at", "DATETIME"),
        ],
        "daily_briefs": [
            ("items_json", "TEXT"),
            ("deals_json", "TEXT"),
        ],
        "draft_records": [
            ("draft_type", "TEXT DEFAULT 'gmail'"),
        ],
        "rules": [
            ("proposed_at", "DATETIME"),  # set when synthesized, cleared on confirm/dismiss
        ],
    }
    with engine.connect() as conn:
        for table, cols in new_columns.items():
            for col, col_type in cols:
                try:
                    conn.execute(
                        __import__("sqlalchemy").text(
                            f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"
                        )
                    )
                    conn.commit()
                except Exception:
                    pass  # column already exists — idempotent


def get_engine():
    global _engine
    if _engine is None:
        from sqlalchemy.pool import NullPool

        _engine = create_engine(
            f"sqlite:///{_cfg.DB_PATH}",
            connect_args={"check_same_thread": False},
            poolclass=NullPool,
            echo=False,
        )
        Base.metadata.create_all(_engine)
        _run_migrations(_engine)
    return _engine


def get_session() -> Session:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionLocal()


# ── Repository helpers ───────────────────────────────────────────────────────


class EmailRepo:
    """CRUD for EmailRecord."""

    def __init__(self, session: Session):
        self.s = session

    def upsert(self, record: EmailRecord) -> None:
        existing = self.s.query(EmailRecord).filter_by(gmail_id=record.gmail_id).first()
        if existing:
            for col in EmailRecord.__table__.columns:
                if col.name not in ("id",):
                    setattr(existing, col.name, getattr(record, col.name))
        else:
            self.s.add(record)
        self.s.commit()

    def upsert_many(self, records: list[EmailRecord]) -> None:
        """Bulk-upsert a list of EmailRecords in a single transaction.

        Uses SQLite's INSERT OR REPLACE (via Core-level insert) to replace the
        old per-row SELECT+COMMIT loop that was O(n) round-trips.  For 10 k
        records this is ~100× faster: one transaction instead of 10 k commits.

        The implementation falls back to the original row-by-row path when the
        SQLAlchemy dialect does not support ``insert().prefix_with("OR REPLACE")``,
        so it is safe on any backend.
        """
        if not records:
            return

        try:
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            # Build a list of plain dicts — one per record, excluding the
            # auto-generated ``id`` column so SQLite assigns it on INSERT.
            col_names = [c.name for c in EmailRecord.__table__.columns if c.name != "id"]
            # Python-side column defaults (e.g. synced_at) only fire on ORM
            # flush; the core insert below bypasses that, so stamp them here
            # or every bulk-synced row lands with synced_at = NULL.
            now = datetime.now(timezone.utc)
            rows = [
                {
                    col: (
                        getattr(r, col)
                        if getattr(r, col) is not None
                        else (now if col == "synced_at" else getattr(r, col))
                    )
                    for col in col_names
                }
                for r in records
            ]
            stmt = sqlite_insert(EmailRecord.__table__).values(rows)
            # ON CONFLICT(gmail_id) DO UPDATE — keeps the most recent data.
            update_cols = {c: stmt.excluded[c] for c in col_names if c != "gmail_id"}
            stmt = stmt.on_conflict_do_update(
                index_elements=["gmail_id"],
                set_=update_cols,
            )
            self.s.execute(stmt)
            self.s.commit()
        except Exception:
            # Fallback: row-by-row (works on non-SQLite backends / schema edge cases)
            self.s.rollback()
            for r in records:
                self.upsert(r)

    def existing_gmail_ids(self, account_email: str) -> set[str]:
        """Return the set of gmail_ids already in the DB for this account.

        Used by ``sync --skip-existing`` to avoid re-fetching metadata for
        messages that are already cached locally.
        """
        rows = self.s.query(EmailRecord.gmail_id).filter_by(account_email=account_email).all()
        return {r.gmail_id for r in rows}

    def get(self, gmail_id: str) -> EmailRecord | None:
        return self.s.query(EmailRecord).filter_by(gmail_id=gmail_id).first()

    def get_inbox(self, account_email: str, limit: int = 100) -> list[EmailRecord]:
        return (
            self.s.query(EmailRecord)
            .filter_by(account_email=account_email, is_inbox=True)
            .order_by(EmailRecord.internal_date.desc())
            .limit(limit)
            .all()
        )

    def find_avoided(self, account_email: str, threshold: int | None = None) -> list[EmailRecord]:
        """Emails viewed >= threshold times but not acted on."""
        t = threshold or _cfg.get_settings().avoidance_view_threshold
        return (
            self.s.query(EmailRecord)
            .filter(
                EmailRecord.account_email == account_email,
                EmailRecord.is_inbox.is_(True),
                EmailRecord.is_acted_on.is_(False),
                EmailRecord.view_count >= t,
            )
            .order_by(EmailRecord.view_count.desc())
            .all()
        )

    def increment_view(self, gmail_id: str) -> None:
        rec = self.get(gmail_id)
        if rec:
            rec.view_count += 1
            rec.last_viewed_at = datetime.now(timezone.utc)
            self.s.commit()

    def mark_acted_on(self, gmail_id: str) -> None:
        rec = self.get(gmail_id)
        if rec:
            rec.is_acted_on = True
            self.s.commit()


class FollowUpRepo:
    def __init__(self, session: Session):
        self.s = session

    def create(self, fu: FollowUp) -> FollowUp:
        self.s.add(fu)
        self.s.commit()
        return fu

    def get_due(self, account_email: str) -> list[FollowUp]:
        now = datetime.now(timezone.utc)
        return (
            self.s.query(FollowUp)
            .filter(
                FollowUp.account_email == account_email,
                FollowUp.remind_at <= now,
                FollowUp.replied.is_(False),
                FollowUp.dismissed.is_(False),
                FollowUp.snoozed_until.is_(None),
            )
            .all()
        )

    def mark_replied(self, thread_id: str) -> None:
        now = datetime.now(timezone.utc)
        rows = self.s.query(FollowUp).filter_by(thread_id=thread_id, replied=False).all()
        for r in rows:
            r.replied = True
            r.replied_at = now
        self.s.commit()

    def dismiss(self, follow_up_id: int) -> None:
        fu = self.s.get(FollowUp, follow_up_id)
        if fu:
            fu.dismissed = True
            self.s.commit()

    def snooze(self, follow_up_id: int, until: datetime) -> None:
        fu = self.s.get(FollowUp, follow_up_id)
        if fu:
            fu.snoozed_until = until
            self.s.commit()


class DraftRepo:
    """Tracks AI-generated reply drafts awaiting human review."""

    def __init__(self, session: Session):
        self.s = session

    def upsert_for_thread(self, draft: DraftRecord) -> DraftRecord:
        """Store a draft, replacing any prior open (ready/edited) draft for the
        same thread so a thread never accumulates duplicate autodrafts."""
        existing = (
            self.s.query(DraftRecord)
            .filter(
                DraftRecord.account_email == draft.account_email,
                DraftRecord.thread_id == draft.thread_id,
                DraftRecord.status.in_(("ready", "edited")),
            )
            .first()
            if draft.thread_id
            else None
        )
        if existing:
            for col in (
                "gmail_draft_id",
                "in_reply_to_gmail_id",
                "in_reply_to_rfc_id",
                "to_email",
                "subject",
                "body",
                "trigger",
                "confidence",
                "model",
                "draft_type",
            ):
                setattr(existing, col, getattr(draft, col))
            existing.status = "ready"
            existing.created_at = datetime.now(timezone.utc)
            existing.reviewed_at = None
            self.s.commit()
            return existing
        self.s.add(draft)
        self.s.commit()
        return draft

    def get(self, draft_id: int) -> DraftRecord | None:
        return self.s.get(DraftRecord, draft_id)

    def open_for_thread(self, account_email: str, thread_id: str) -> DraftRecord | None:
        return (
            self.s.query(DraftRecord)
            .filter(
                DraftRecord.account_email == account_email,
                DraftRecord.thread_id == thread_id,
                DraftRecord.status.in_(("ready", "edited")),
            )
            .first()
        )

    def list_open(self, account_email: str, limit: int = 50) -> list[DraftRecord]:
        return (
            self.s.query(DraftRecord)
            .filter(
                DraftRecord.account_email == account_email,
                DraftRecord.status.in_(("ready", "edited")),
            )
            .order_by(DraftRecord.confidence.desc(), DraftRecord.created_at.desc())
            .limit(limit)
            .all()
        )

    def count_open(self, account_email: str) -> int:
        return (
            self.s.query(DraftRecord)
            .filter(
                DraftRecord.account_email == account_email,
                DraftRecord.status.in_(("ready", "edited")),
            )
            .count()
        )

    def set_status(self, draft_id: int, status: str) -> None:
        row = self.get(draft_id)
        if row:
            row.status = status
            row.reviewed_at = datetime.now(timezone.utc)
            self.s.commit()


class UndoLogRepo:
    def __init__(self, session: Session):
        self.s = session

    def record(
        self,
        account_email: str,
        operation: str,
        message_ids: list[str],
        description: str = "",
        metadata: dict | None = None,
    ) -> UndoLogEntry:
        from datetime import timedelta

        settings = _cfg.get_settings()
        entry = UndoLogEntry(
            account_email=account_email,
            operation=operation,
            description=description,
            message_ids_json=json.dumps(message_ids),
            metadata_json=json.dumps(metadata or {}),
            expires_at=datetime.now(timezone.utc) + timedelta(days=settings.undo_window_days),
        )
        self.s.add(entry)
        self.s.commit()
        return entry

    def get(self, entry_id: int) -> UndoLogEntry | None:
        return self.s.get(UndoLogEntry, entry_id)

    def list_recent(self, account_email: str, limit: int = 20) -> list[UndoLogEntry]:
        return (
            self.s.query(UndoLogEntry)
            .filter_by(account_email=account_email, is_undone=False)
            .order_by(UndoLogEntry.executed_at.desc())
            .limit(limit)
            .all()
        )

    def mark_undone(self, entry_id: int) -> None:
        entry = self.get(entry_id)
        if entry:
            entry.is_undone = True
            entry.undone_at = datetime.now(timezone.utc)
            self.s.commit()

    def purge_expired(self) -> int:
        now = datetime.now(timezone.utc)
        count = self.s.query(UndoLogEntry).filter(UndoLogEntry.expires_at < now).delete()
        self.s.commit()
        return count


class RuleRepo:
    def __init__(self, session: Session):
        self.s = session

    def create(self, rule: RuleDefinition) -> RuleDefinition:
        self.s.add(rule)
        self.s.commit()
        return rule

    def list_active(self, account_email: str) -> list[RuleDefinition]:
        return (
            self.s.query(RuleDefinition)
            .filter_by(account_email=account_email, is_active=True)
            .all()
        )

    def deactivate(self, rule_id: int) -> None:
        r = self.s.get(RuleDefinition, rule_id)
        if r:
            r.is_active = False
            self.s.commit()

    def record_run(self, rule_id: int) -> None:
        r = self.s.get(RuleDefinition, rule_id)
        if r:
            r.last_run_at = datetime.now(timezone.utc)
            r.run_count += 1
            self.s.commit()

    def list_proposed(self, account_email: str) -> list[RuleDefinition]:
        """Return synthesized rules awaiting user confirmation (is_active=False, proposed_at set)."""
        return (
            self.s.query(RuleDefinition)
            .filter(
                RuleDefinition.account_email == account_email,
                RuleDefinition.is_active == False,  # noqa: E712
                RuleDefinition.proposed_at.isnot(None),
            )
            .order_by(RuleDefinition.proposed_at.desc())
            .all()
        )

    def confirm_proposal(self, rule_id: int) -> bool:
        """Activate a proposed rule. Returns True if found."""
        r = self.s.get(RuleDefinition, rule_id)
        if r and r.proposed_at is not None:
            r.is_active = True
            r.proposed_at = None
            self.s.commit()
            return True
        return False

    def dismiss_proposal(self, rule_id: int) -> bool:
        """Delete a proposed rule. Returns True if found."""
        r = self.s.get(RuleDefinition, rule_id)
        if r and r.proposed_at is not None:
            self.s.delete(r)
            self.s.commit()
            return True
        return False


class CleanupFeedbackRepo:
    """Records and aggregates /cleanup decisions for the learning loop."""

    def __init__(self, session: Session):
        self.s = session

    def record_many(self, account_email: str, items: list[dict]) -> None:
        """Bulk-insert feedback rows. Each item: {sender_email, batch_key, action, decision}.

        Best-effort: rolls back on error and never raises to the caller.
        """
        if not items:
            return
        try:
            rows = [
                CleanupFeedbackRecord(
                    account_email=account_email,
                    sender_email=item["sender_email"],
                    batch_key=item["batch_key"],
                    action=item["action"],
                    decision=item["decision"],
                )
                for item in items
            ]
            self.s.add_all(rows)
            self.s.commit()
        except Exception:
            self.s.rollback()

    def sender_priors(self, account_email: str, max_adjust: int = 15) -> dict[str, int]:
        """Per-sender confidence adjustment learned from past decisions.

        For each sender with >=1 feedback row: ``rate = approved / total`` over
        all decisions (approved+skipped+dropped). ``adjustment =
        round((rate - 0.5) * 2 * max_adjust)``, clamped to
        ``[-max_adjust, +max_adjust]``. Only senders with a non-zero adjustment
        are returned; senders never seen are absent (caller treats missing as 0).
        """
        rows = (
            self.s.query(CleanupFeedbackRecord.sender_email, CleanupFeedbackRecord.decision)
            .filter_by(account_email=account_email)
            .all()
        )
        totals: dict[str, int] = {}
        approved: dict[str, int] = {}
        for sender_email, decision in rows:
            totals[sender_email] = totals.get(sender_email, 0) + 1
            if decision == "approved":
                approved[sender_email] = approved.get(sender_email, 0) + 1
        priors: dict[str, int] = {}
        for sender_email, total in totals.items():
            rate = approved.get(sender_email, 0) / total
            adjustment = round((rate - 0.5) * 2 * max_adjust)
            adjustment = max(-max_adjust, min(max_adjust, adjustment))
            if adjustment != 0:
                priors[sender_email] = adjustment
        return priors

    def batch_session_counts(self, account_email: str) -> dict[str, int]:
        """For each batch_key, the number of DISTINCT calendar days on which that
        batch_key had at least one 'approved' decision. Used to decide when to
        offer an 'automate this' rule (>=3 sessions).
        """
        rows = (
            self.s.query(CleanupFeedbackRecord.batch_key, CleanupFeedbackRecord.created_at)
            .filter_by(account_email=account_email, decision="approved")
            .all()
        )
        days_by_batch: dict[str, set] = {}
        for batch_key, created_at in rows:
            day = created_at.date() if created_at is not None else None
            days_by_batch.setdefault(batch_key, set()).add(day)
        return {batch_key: len(days) for batch_key, days in days_by_batch.items()}


class BlocklistRepo:
    """CRUD for SenderBlocklist — senders protected from purge operations."""

    def __init__(self, session: Session):
        self.s = session

    def add(
        self,
        account_email: str,
        sender_email: str,
        reason: str = "user_protected",
    ) -> SenderBlocklist:
        """Add a sender to the blocklist. Idempotent — returns existing entry if already present."""
        domain = (
            sender_email.split("@")[-1].lower() if "@" in sender_email else sender_email.lower()
        )
        existing = (
            self.s.query(SenderBlocklist)
            .filter_by(account_email=account_email, sender_email=sender_email)
            .first()
        )
        if existing:
            return existing
        entry = SenderBlocklist(
            account_email=account_email,
            sender_email=sender_email,
            sender_domain=domain,
            reason=reason,
        )
        self.s.add(entry)
        self.s.commit()
        return entry

    def remove(self, account_email: str, sender_email: str) -> bool:
        """Remove a sender from the blocklist. Returns True if it existed."""
        entry = (
            self.s.query(SenderBlocklist)
            .filter_by(account_email=account_email, sender_email=sender_email)
            .first()
        )
        if not entry:
            return False
        self.s.delete(entry)
        self.s.commit()
        return True

    def list_all(self, account_email: str) -> list[SenderBlocklist]:
        return (
            self.s.query(SenderBlocklist)
            .filter_by(account_email=account_email)
            .order_by(SenderBlocklist.created_at.desc())
            .all()
        )

    def blocked_emails(self, account_email: str) -> set[str]:
        """Return the set of blocked sender email addresses for fast membership tests."""
        rows = (
            self.s.query(SenderBlocklist.sender_email).filter_by(account_email=account_email).all()
        )
        return {r.sender_email for r in rows}


class ClassificationCacheRepo:
    """Read/write cached triage classifications, keyed by gmail_id.

    Works with plain dicts (keys mirror :class:`ClassificationCacheRecord`
    columns minus ``id``/``created_at``) so the storage layer stays decoupled
    from the AI engine's ``ClassifiedEmail`` dataclass.
    """

    _FIELDS = (
        "category",
        "priority",
        "explanation",
        "suggested_action",
        "requires_reply",
        "deadline_hint",
    )

    def __init__(self, session: Session):
        self.s = session

    def get_many(self, gmail_ids: list[str]) -> dict[str, dict]:
        """Return {gmail_id: {field: value, ...}} for whichever ids are cached."""
        if not gmail_ids:
            return {}
        rows = (
            self.s.query(ClassificationCacheRecord)
            .filter(ClassificationCacheRecord.gmail_id.in_(gmail_ids))
            .all()
        )
        return {r.gmail_id: {f: getattr(r, f) for f in self._FIELDS} for r in rows}

    def upsert_many(self, items: list[dict]) -> None:
        """Bulk-upsert classifications. Each item must include ``gmail_id``."""
        if not items:
            return
        try:
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            cols = ["gmail_id", *self._FIELDS]
            rows = [{c: item.get(c) for c in cols} for item in items]
            stmt = sqlite_insert(ClassificationCacheRecord.__table__).values(rows)
            update_cols = {c: stmt.excluded[c] for c in self._FIELDS}
            stmt = stmt.on_conflict_do_update(index_elements=["gmail_id"], set_=update_cols)
            self.s.execute(stmt)
            self.s.commit()
        except Exception:
            self.s.rollback()
            for item in items:
                gid = item.get("gmail_id")
                if not gid:
                    continue
                existing = self.s.query(ClassificationCacheRecord).filter_by(gmail_id=gid).first()
                if existing:
                    for f in self._FIELDS:
                        setattr(existing, f, item.get(f))
                else:
                    self.s.add(
                        ClassificationCacheRecord(
                            gmail_id=gid, **{f: item.get(f) for f in self._FIELDS}
                        )
                    )
            self.s.commit()


class DailyBrief(Base):
    """One AI-generated (or stats-only) morning brief per account per calendar date."""

    __tablename__ = "daily_briefs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_email = Column(String, nullable=False)
    brief_date = Column(String, nullable=False)  # ISO "YYYY-MM-DD"
    content = Column(Text, nullable=False)  # plain-text output
    ai_used = Column(Boolean, default=False)  # True if Claude generated it
    unread_count = Column(Integer, default=0)
    new_since_yesterday = Column(Integer, default=0)
    high_priority_count = Column(Integer, default=0)
    overdue_followups_count = Column(Integer, default=0)
    avoided_count = Column(Integer, default=0)
    # JSON list of the emails the brief identified, each {gmail_id, sender,
    # subject}, so the UI can render clickable "open in Gmail" deep links.
    items_json = Column(Text, nullable=True)
    deals_json = Column(Text, nullable=True)  # JSON list of deal/offer emails, same shape as items_json
    generated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Agent(Base):
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)  # e.g. "Work", "Personal"
    account_email = Column(String, nullable=False, unique=True)
    interval_minutes = Column(Integer, default=30)
    is_active = Column(Boolean, default=True)
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    last_found_count = Column(Integer, default=0)  # emails found in last heartbeat
    status = Column(String, default="idle")  # "idle" | "running" | "error"
    error_message = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)

    # Soul config — persistent persona that shapes email composition voice
    voice_style = Column(
        String, nullable=True
    )  # "professional" | "casual" | "warm" | "direct" | "diplomatic"
    user_context = Column(Text, nullable=True)  # who the user is, their role, key relationships
    writing_guidelines = Column(Text, nullable=True)  # style rules, things to always/never do

    # Heartbeat feature toggles — which tasks run each cycle
    run_rules = Column(Boolean, default=True)  # execute active automation rules
    run_followups = Column(Boolean, default=True)  # sync follow-up reply detection
    run_avoidance = Column(Boolean, default=False)  # detect avoided emails (requires AI)
    run_daily_brief = Column(Boolean, default=False)  # generate AI morning brief once per day
    run_autodraft = Column(Boolean, default=False)  # pre-draft replies for review (cloud AI, Gmail)


class UserActionRepo:
    """Records and queries per-message behavioral signals from all action surfaces."""

    def __init__(self, session: Session):
        self.s = session

    def record(
        self,
        account_email: str,
        gmail_id: str,
        sender_email: str,
        sender_name: str,
        subject: str,
        action: str,
        source: str,
        ai_category: str = "",
        ai_priority: str = "",
    ) -> None:
        """Best-effort insert — never raises so callers don't need try/except."""
        try:
            self.s.add(
                UserActionRecord(
                    account_email=account_email,
                    gmail_id=gmail_id,
                    sender_email=sender_email,
                    sender_name=sender_name,
                    subject=subject,
                    action=action,
                    source=source,
                    ai_category=ai_category,
                    ai_priority=ai_priority,
                )
            )
            self.s.commit()
        except Exception:
            self.s.rollback()

    def sender_action_counts(
        self, account_email: str, lookback_days: int = 90
    ) -> dict[str, dict[str, int]]:
        """{sender_email_lower: {action: count}} for the lookback window."""
        from datetime import timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        rows = (
            self.s.query(
                UserActionRecord.sender_email,
                UserActionRecord.action,
                func.count().label("n"),
            )
            .filter(
                UserActionRecord.account_email == account_email,
                UserActionRecord.created_at >= cutoff,
            )
            .group_by(UserActionRecord.sender_email, UserActionRecord.action)
            .all()
        )
        result: dict[str, dict[str, int]] = {}
        for sender_email, action, n in rows:
            key = (sender_email or "").lower()
            result.setdefault(key, {})[action] = n
        return result

    def high_trash_senders(
        self,
        account_email: str,
        min_actions: int = 3,
        trash_rate: float = 0.80,
    ) -> set[str]:
        """Sender emails (lowercased) where trash/(all actions) >= trash_rate."""
        counts = self.sender_action_counts(account_email)
        result = set()
        for sender, actions in counts.items():
            total = sum(actions.values())
            if total < min_actions:
                continue
            if actions.get("trash", 0) / total >= trash_rate:
                result.add(sender)
        return result

    def replied_senders(self, account_email: str, lookback_days: int = 90) -> set[str]:
        """Sender emails (lowercased) the user has replied to at least once."""
        from datetime import timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        rows = (
            self.s.query(UserActionRecord.sender_email)
            .filter(
                UserActionRecord.account_email == account_email,
                UserActionRecord.action == "reply",
                UserActionRecord.created_at >= cutoff,
            )
            .distinct()
            .all()
        )
        return {(r.sender_email or "").lower() for r in rows}

    def candidates_for_rule_synthesis(
        self,
        account_email: str,
        min_trash: int = 5,
        trash_rate: float = 0.85,
    ) -> list[dict]:
        """Return senders ripe for an auto-rule proposal.

        Returns list of {sender_email, sender_name, trash_count, sample_subjects}.
        Excludes senders that already have an active or proposed rule.
        """
        from datetime import timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(days=90)

        # Count trash actions per sender
        rows = (
            self.s.query(
                UserActionRecord.sender_email,
                UserActionRecord.action,
                func.count().label("n"),
            )
            .filter(
                UserActionRecord.account_email == account_email,
                UserActionRecord.created_at >= cutoff,
            )
            .group_by(UserActionRecord.sender_email, UserActionRecord.action)
            .all()
        )
        by_sender: dict[str, dict[str, int]] = {}
        for sender_email, action, n in rows:
            key = (sender_email or "").lower()
            by_sender.setdefault(key, {})[action] = n

        # Find candidates
        candidates = []
        for sender_key, actions in by_sender.items():
            total = sum(actions.values())
            trash = actions.get("trash", 0)
            if trash < min_trash or total < min_trash:
                continue
            if trash / total < trash_rate:
                continue
            candidates.append(sender_key)

        if not candidates:
            return []

        # Exclude senders already covered by a rule
        existing_queries = {
            r.gmail_query
            for r in self.s.query(RuleDefinition)
            .filter_by(account_email=account_email)
            .all()
        }

        result = []
        for sender_key in candidates:
            if any(sender_key in q for q in existing_queries):
                continue
            # Get sender_name + sample subjects from recent actions
            recent = (
                self.s.query(UserActionRecord)
                .filter(
                    UserActionRecord.account_email == account_email,
                    func.lower(UserActionRecord.sender_email) == sender_key,
                    UserActionRecord.action == "trash",
                )
                .order_by(UserActionRecord.created_at.desc())
                .limit(5)
                .all()
            )
            if not recent:
                continue
            result.append({
                "sender_email": recent[0].sender_email,
                "sender_name": recent[0].sender_name or recent[0].sender_email,
                "trash_count": by_sender[sender_key].get("trash", 0),
                "sample_subjects": [r.subject for r in recent if r.subject],
            })
        return result


class DailyBriefRepo:
    def __init__(self, session: Session):
        self.s = session

    def get_today(self, account_email: str, today_str: str) -> DailyBrief | None:
        return (
            self.s.query(DailyBrief)
            .filter_by(account_email=account_email, brief_date=today_str)
            .first()
        )

    def save(self, brief: DailyBrief) -> DailyBrief:
        existing = self.get_today(brief.account_email, brief.brief_date)
        if existing:
            for col in (
                "content",
                "ai_used",
                "unread_count",
                "new_since_yesterday",
                "high_priority_count",
                "overdue_followups_count",
                "avoided_count",
                "items_json",
                "deals_json",
            ):
                setattr(existing, col, getattr(brief, col))
            existing.generated_at = datetime.now(timezone.utc)
            self.s.commit()
            return existing
        self.s.add(brief)
        self.s.commit()
        return brief

    def list_recent(self, account_email: str, limit: int = 7) -> list[DailyBrief]:
        return (
            self.s.query(DailyBrief)
            .filter_by(account_email=account_email)
            .order_by(DailyBrief.brief_date.desc())
            .limit(limit)
            .all()
        )


class AgentRepo:
    def __init__(self, session: Session):
        self.s = session

    def register(self, account_email: str, name: str, interval_minutes: int = 30) -> Agent:
        existing = self.s.query(Agent).filter_by(account_email=account_email).first()
        if existing:
            existing.name = name
            existing.interval_minutes = interval_minutes
            self.s.commit()
            return existing
        from datetime import datetime, timezone

        agent = Agent(
            name=name,
            account_email=account_email,
            interval_minutes=interval_minutes,
            is_active=True,
            status="idle",
            created_at=datetime.now(timezone.utc),
        )
        self.s.add(agent)
        self.s.commit()
        return agent

    def list_all(self) -> list[Agent]:
        return self.s.query(Agent).order_by(Agent.created_at).all()

    def get_by_email(self, account_email: str) -> Agent | None:
        return self.s.query(Agent).filter_by(account_email=account_email).first()

    def set_active(self, account_email: str, active: bool) -> None:
        agent = self.get_by_email(account_email)
        if agent:
            agent.is_active = active
            self.s.commit()

    def update_after_run(
        self,
        account_email: str,
        found_count: int,
        status: str = "idle",
        error: str | None = None,
    ) -> None:
        from datetime import datetime, timezone

        agent = self.get_by_email(account_email)
        if agent:
            agent.last_run_at = datetime.now(timezone.utc)
            agent.last_found_count = found_count
            agent.status = status
            agent.error_message = error
            self.s.commit()

    def update_soul(
        self,
        account_email: str,
        voice_style: str | None = None,
        user_context: str | None = None,
        writing_guidelines: str | None = None,
    ) -> None:
        agent = self.get_by_email(account_email)
        if agent:
            agent.voice_style = voice_style or None
            agent.user_context = user_context or None
            agent.writing_guidelines = writing_guidelines or None
            self.s.commit()

    def update_features(
        self,
        account_email: str,
        run_rules: bool,
        run_followups: bool,
        run_avoidance: bool,
        run_daily_brief: bool = False,
        run_autodraft: bool = False,
    ) -> None:
        agent = self.get_by_email(account_email)
        if agent:
            agent.run_rules = run_rules
            agent.run_followups = run_followups
            agent.run_avoidance = run_avoidance
            agent.run_daily_brief = run_daily_brief
            agent.run_autodraft = run_autodraft
            self.s.commit()

    def delete(self, account_email: str) -> None:
        agent = self.get_by_email(account_email)
        if agent:
            self.s.delete(agent)
            self.s.commit()


class AccountRepo:
    def __init__(self, session: Session):
        self.s = session

    def register(
        self,
        email: str,
        provider: str = "gmail",
        display_name: str = "",
    ) -> "Account":
        existing = self.s.query(Account).filter_by(email=email).first()
        if existing:
            existing.is_active = True
            self.s.commit()
            return existing
        from datetime import datetime, timezone

        acct = Account(
            email=email,
            display_name=display_name or email,
            added_at=datetime.now(timezone.utc),
            is_active=True,
        )
        self.s.add(acct)
        self.s.commit()
        return acct

    def list_all(self) -> list["Account"]:
        return self.s.query(Account).order_by(Account.added_at).all()

    def get(self, email: str) -> "Account | None":
        return self.s.query(Account).filter_by(email=email).first()

    def deactivate(self, email: str) -> None:
        acct = self.get(email)
        if acct:
            acct.is_active = False
            self.s.commit()

    def update_last_synced(self, email: str) -> None:
        from datetime import datetime, timezone

        acct = self.get(email)
        if acct:
            acct.last_synced_at = datetime.now(timezone.utc)
            self.s.commit()

    def backfill_last_synced(self, email: str) -> None:
        """Repair a missing last_synced_at for accounts that already have
        cached emails.

        Big or interrupted syncs commit emails chunk-by-chunk but historically
        only stamped last_synced_at after the *entire* run finished, so a
        mailbox could hold tens of thousands of cached emails yet still report
        "Never" synced. If the account has cached emails but no timestamp, use
        the freshest available row time (falling back to now) so the UI reports
        the truth: this mailbox has been synced.
        """
        acct = self.get(email)
        if not acct or acct.last_synced_at is not None:
            return
        from datetime import datetime, timezone

        from sqlalchemy import func

        newest = (
            self.s.query(func.max(EmailRecord.synced_at))
            .filter(EmailRecord.account_email == email)
            .scalar()
        )
        cached_any = self.s.query(EmailRecord.id).filter(EmailRecord.account_email == email).first()
        if not cached_any:
            return
        acct.last_synced_at = newest or datetime.now(timezone.utc)
        self.s.commit()

    def mark_welcomed(self, email: str) -> None:
        from datetime import datetime, timezone

        acct = self.get(email)
        if acct and acct.welcomed_at is None:
            acct.welcomed_at = datetime.now(timezone.utc)
            self.s.commit()

    def count(self) -> int:
        return self.s.query(Account).count()
