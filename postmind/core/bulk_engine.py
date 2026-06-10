"""Semantic bulk operations — NL → preview → execute → 30-day undo."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from postmind.config import get_settings
from postmind.core.ai_engine import AIEngine, BulkOperation, NLRule
from postmind.core.gmail_client import GmailClient, Message
from postmind.core.storage import (
    EmailRepo,
    RuleDefinition,
    RuleRepo,
    UndoLogRepo,
    get_session,
)

# ── Data models ─────────────────────────────────────────────────────────────


@dataclass
class BulkPreview:
    operation: BulkOperation | NLRule
    message_ids: list[str]
    sample_messages: list[Message]  # First few messages for user review
    total_count: int
    estimated_size_mb: float


@dataclass
class BulkResult:
    undo_log_id: int
    affected_count: int
    action: str
    description: str
    dry_run: bool


# ── Bulk Engine ──────────────────────────────────────────────────────────────


class BulkEngine:
    """
    Executes bulk email operations with:
    - Natural language input → Gmail query translation via AI
    - Dry-run preview showing affected messages before anything changes
    - 30-day undo window for all destructive operations
    - Transparent explanations for every action
    """

    def __init__(self, client: GmailClient, account_email: str, ai: AIEngine | None = None):
        self.client = client
        self.account_email = account_email
        self.ai = ai or AIEngine()
        self.session = get_session()
        self.undo_repo = UndoLogRepo(self.session)
        self.email_repo = EmailRepo(self.session)
        self.rule_repo = RuleRepo(self.session)

    # ── One-off bulk operations ──────────────────────────────────────────────

    def preview(self, instruction: str, max_sample: int = 5) -> BulkPreview:
        """
        Parse a natural language bulk instruction and return a preview
        (message IDs + sample messages) WITHOUT executing anything.
        """
        op = self.ai.parse_bulk_intent(instruction)
        message_ids = self.client.list_message_ids(query=op.gmail_query, max_results=1000)

        sample_ids = message_ids[:max_sample]
        sample_messages = self.client.get_messages_batch(sample_ids) if sample_ids else []

        total_size = sum(m.size_estimate for m in sample_messages)
        avg_size = total_size / len(sample_messages) if sample_messages else 0
        estimated_size_mb = (avg_size * len(message_ids)) / (1024 * 1024)

        return BulkPreview(
            operation=op,
            message_ids=message_ids,
            sample_messages=sample_messages,
            total_count=len(message_ids),
            estimated_size_mb=round(estimated_size_mb, 2),
        )

    def execute(
        self,
        preview: BulkPreview,
        dry_run: bool | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> BulkResult:
        """
        Execute the previewed bulk operation.
        Records an undo log entry before making any changes.
        """
        settings = get_settings()
        is_dry_run = dry_run if dry_run is not None else settings.dry_run
        op = preview.operation
        message_ids = preview.message_ids

        if not message_ids:
            return BulkResult(
                undo_log_id=-1,
                affected_count=0,
                action=op.action,
                description="No messages matched the query.",
                dry_run=is_dry_run,
            )

        # Record undo log BEFORE making changes (so we can always reverse)
        undo_entry = self.undo_repo.record(
            account_email=self.account_email,
            operation=op.action,
            message_ids=message_ids,
            description=op.explanation,
            metadata={"action_params": op.action_params, "gmail_query": op.gmail_query},
        )

        if is_dry_run:
            return BulkResult(
                undo_log_id=undo_entry.id,
                affected_count=len(message_ids),
                action=op.action,
                description=f"[DRY RUN] Would {op.action} {len(message_ids)} messages.",
                dry_run=True,
            )

        count = self._execute_action(op.action, op.action_params, message_ids, progress_callback)

        # Mark as acted on in local cache
        for mid in message_ids:
            self.email_repo.mark_acted_on(mid)
        if op.action == "trash":
            self.email_repo.mark_trashed(message_ids)

        return BulkResult(
            undo_log_id=undo_entry.id,
            affected_count=count,
            action=op.action,
            description=op.explanation,
            dry_run=False,
        )

    def undo(self, undo_log_id: int) -> int:
        """
        Reverse a bulk operation using its undo log entry.
        Returns number of messages restored.
        """
        entry = self.undo_repo.get(undo_log_id)
        if not entry:
            raise ValueError(f"Undo log entry {undo_log_id} not found.")
        if entry.is_undone:
            raise ValueError(f"Entry {undo_log_id} has already been undone.")

        message_ids = entry.message_ids
        action = entry.operation

        if action == "archive":
            # Restore to inbox
            count = self.client.batch_label(message_ids, add=["INBOX"])
        elif action == "trash":
            for mid in message_ids:
                self.client.untrash(mid)
            count = len(message_ids)
        elif action == "label":
            label_name = entry.op_metadata.get("action_params", {}).get("label_name", "")
            if label_name:
                label_id = self.client.get_or_create_label(label_name)
                count = self.client.batch_label(message_ids, remove=[label_id])
            else:
                count = 0
        elif action == "mark_read":
            count = self.client.batch_label(message_ids, add=["UNREAD"])
        else:
            raise ValueError(f"Cannot undo action type: {action}")

        self.undo_repo.mark_undone(undo_log_id)
        return count

    # ── Recurring rules ──────────────────────────────────────────────────────

    def create_rule(self, natural_language: str) -> RuleDefinition:
        """Create a recurring rule from a natural language instruction."""
        nl_rule = self.ai.translate_rule(natural_language)

        rule = RuleDefinition(
            account_email=self.account_email,
            name=natural_language[:80],
            natural_language=natural_language,
            gmail_query=nl_rule.gmail_query,
            action=nl_rule.action,
            ai_explanation=nl_rule.explanation,
        )
        rule.action_params = nl_rule.action_params
        return self.rule_repo.create(rule)

    def run_rules(self, dry_run: bool = False) -> dict[int, BulkResult]:
        """Run all active rules and return results keyed by rule ID."""
        rules = self.rule_repo.list_active(self.account_email)
        results: dict[int, BulkResult] = {}

        for rule in rules:
            message_ids = self.client.list_message_ids(query=rule.gmail_query, max_results=500)
            if not message_ids:
                self.rule_repo.record_run(rule.id)
                results[rule.id] = BulkResult(
                    undo_log_id=-1,
                    affected_count=0,
                    action=rule.action,
                    description=f"Rule '{rule.name}': no matching messages.",
                    dry_run=dry_run,
                )
                continue

            undo_entry = self.undo_repo.record(
                account_email=self.account_email,
                operation=rule.action,
                message_ids=message_ids,
                description=f"Rule: {rule.name}",
                metadata={"rule_id": rule.id, "action_params": rule.action_params},
            )

            if not dry_run:
                count = self._execute_action(rule.action, rule.action_params, message_ids)
                self.rule_repo.record_run(rule.id)
            else:
                count = len(message_ids)

            results[rule.id] = BulkResult(
                undo_log_id=undo_entry.id,
                affected_count=count,
                action=rule.action,
                description=f"Rule '{rule.name}': {rule.ai_explanation}",
                dry_run=dry_run,
            )

        return results

    # ── Execution dispatch ───────────────────────────────────────────────────

    def _execute_action(
        self,
        action: str,
        params: dict,
        message_ids: list[str],
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> int:
        if action == "archive":
            return self.client.batch_archive(message_ids)
        elif action == "trash":
            return self.client.batch_trash(message_ids)
        elif action == "label":
            label_name = params.get("label_name", "postmind/auto")
            label_id = self.client.get_or_create_label(label_name)
            return self.client.batch_label(message_ids, add=[label_id])
        elif action == "mark_read":
            return self.client.batch_label(message_ids, remove=["UNREAD"])
        elif action == "unsubscribe":
            # Unsubscribe is handled at the CLI layer (requires per-message headers).
            # When emitted by AI in bulk context, fall back to archive.
            return self.client.batch_archive(message_ids)
        else:
            raise ValueError(f"Unknown action: {action}")
