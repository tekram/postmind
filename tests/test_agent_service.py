"""Harness-independent AgentService: resolve → stage → confirm → execute.

Exercises the safety boundary the MCP server and web Super Agent share: WRITE
actions only stage (no execution), confirm tokens are single-use and bound to
server-resolved message IDs, the blocklist/sensitive gates apply, and confirm
executes against a provider while recording an undo log.
"""

from __future__ import annotations

import pytest

import postmind.config as cfg
from postmind.core.agent_service import AgentService
from postmind.core.storage import (
    BlocklistRepo,
    EmailRecord,
    RuleRepo,
    UndoLogRepo,
    get_session,
)


class FakeProvider:
    """Records calls; supports labels/unsubscribe like Gmail."""

    def __init__(self):
        self.trashed: list[str] = []
        self.archived: list[str] = []

    def supports(self, capability: str) -> bool:
        return capability in ("labels", "unsubscribe")

    def batch_trash(self, ids):
        self.trashed.extend(ids)
        return len(ids)

    def batch_archive(self, ids):
        self.archived.extend(ids)
        return len(ids)

    def batch_label(self, ids, add=None, remove=None):
        return len(ids)


def _seed(account="me@x.com", senders=None):
    """Seed inbox EmailRecords. ``senders`` maps email → count."""
    senders = senders or {"news@promo.com": 5, "deals@shop.com": 3}
    session = get_session()
    n = 0
    for email, count in senders.items():
        for i in range(count):
            session.add(
                EmailRecord(
                    account_email=account,
                    gmail_id=f"g{n}",
                    thread_id=f"t{n}",
                    sender_email=email,
                    sender_name=email.split("@")[0],
                    is_inbox=True,
                    is_unread=True,
                    size_estimate=1000,
                )
            )
            n += 1
    session.commit()
    cfg.set_active_account(account)


def _svc(account="me@x.com", ai=None):
    svc = AgentService(account_email=account, ai=ai)
    svc._provider = FakeProvider()  # bypass real Gmail/IMAP construction
    return svc


# ── Resolution + safety gating ────────────────────────────────────────────────


def test_resolve_targets_matches_query_and_filters_blocklist(clean_db):
    _seed()
    BlocklistRepo(get_session()).add("me@x.com", "deals@shop.com")
    svc = _svc()
    staged, blocked, sensitive = svc.resolve_targets(senders=None, query="shop.com")
    # deals@shop.com is blocklisted → excluded and reported.
    assert [g.sender_email for g in staged] == []
    assert blocked == ["deals@shop.com"]


def test_resolve_targets_explicit_emails(clean_db):
    _seed()
    svc = _svc()
    staged, blocked, sensitive = svc.resolve_targets(senders=["news@promo.com"], query="")
    assert [g.sender_email for g in staged] == ["news@promo.com"]
    assert blocked == []


# ── Stage → confirm trash ───────────────────────────────────────────────────


def test_stage_trash_then_confirm_executes_once(clean_db):
    _seed()
    svc = _svc()
    desc = svc.stage_cleanup("trash", senders=["news@promo.com"])
    assert "token" in desc and desc["kind"] == "trash"
    assert desc["email_count"] == 5
    assert desc["undoable"] is True
    # Staging executes nothing.
    assert svc._provider.trashed == []

    token = desc["token"]
    result = svc.confirm(token)
    assert result["ok"] is True
    assert result["affected"] == 5
    assert len(svc._provider.trashed) == 5
    # Undo log recorded.
    assert UndoLogRepo(get_session()).get(result["undo_id"]).operation == "trash"
    # Token is single-use.
    again = svc.confirm(token)
    assert "error" in again


def test_confirm_unknown_token_errors(clean_db):
    _seed()
    svc = _svc()
    assert "error" in svc.confirm("nope")


def test_stage_trash_no_match_returns_error(clean_db):
    _seed()
    svc = _svc()
    out = svc.stage_cleanup("trash", query="nonexistent.example")
    assert "error" in out
    assert svc._provider.trashed == []


# ── Archive requires label support; cancel discards ───────────────────────────


def test_stage_archive_blocked_when_provider_lacks_labels(clean_db):
    _seed()
    svc = _svc()

    class NoLabels(FakeProvider):
        def supports(self, capability):
            return False

    svc._provider = NoLabels()
    out = svc.stage_cleanup("archive", senders=["news@promo.com"])
    assert "error" in out


def test_cancel_removes_staged_action(clean_db):
    _seed()
    svc = _svc()
    token = svc.stage_cleanup("archive", senders=["news@promo.com"])["token"]
    assert svc.list_staged()
    assert svc.cancel(token).get("ok") is True
    assert svc.list_staged() == []


# ── Send validation ───────────────────────────────────────────────────────────


def test_stage_send_rejects_bad_recipient(clean_db):
    _seed()
    svc = _svc()
    assert "error" in svc.stage_send("not-an-email", "Hi", "body")
    assert "error" in svc.stage_send("a@b.com, c@d.com", "Hi", "body")
    ok = svc.stage_send("a@b.com", "Hi", "body")
    assert ok["kind"] == "send"


# ── Create rule via mock AI ───────────────────────────────────────────────────


def test_stage_and_confirm_create_rule(clean_db):
    from postmind.core.mock_ai import MockAIEngine

    _seed()
    svc = _svc(ai=MockAIEngine())
    desc = svc.stage_create_rule("archive newsletters older than 30 days")
    assert desc["kind"] == "create_rule"
    res = svc.confirm(desc["token"])
    assert res["ok"] is True
    assert RuleRepo(get_session()).list_active("me@x.com")


# ── run_sql: read-only analytics ──────────────────────────────────────────────


def _email_count() -> int:
    """Live row count of the `emails` table via a normal repo read."""
    return get_session().query(EmailRecord).count()


def test_run_sql_group_by_returns_rows(clean_db):
    _seed()  # news@promo.com: 5, deals@shop.com: 3
    svc = _svc()
    out = svc.run_sql(
        "SELECT sender_email, COUNT(*) AS n FROM emails GROUP BY sender_email ORDER BY n DESC"
    )
    assert "sender_email | n" in out
    assert "news@promo.com | 5" in out
    assert "deals@shop.com | 3" in out


@pytest.mark.parametrize(
    "bad",
    [
        "UPDATE emails SET subject='x'",
        "DELETE FROM emails",
        "DROP TABLE emails",
        "INSERT INTO emails (gmail_id, thread_id) VALUES ('z', 'z')",
        "ATTACH DATABASE 'evil.db' AS evil",
        "PRAGMA query_only=0",
        "SELECT 1; DROP TABLE emails",
    ],
)
def test_run_sql_rejects_writes_and_leaves_db_intact(clean_db, bad):
    _seed()
    before = _email_count()
    svc = _svc()
    out = svc.run_sql(bad)
    assert out.lower().startswith("error")
    # The real DB is untouched — assert via a normal repo read.
    assert _email_count() == before
    assert before == 8


def test_run_sql_injection_in_data_is_returned_verbatim(clean_db):
    _seed(senders={"attacker@evil.com": 1})
    session = get_session()
    row = session.query(EmailRecord).first()
    row.subject = "'; DROP TABLE emails; --"
    session.commit()

    svc = _svc()
    out = svc.run_sql("SELECT subject FROM emails")
    # Returned verbatim as data; nothing dropped.
    assert "'; DROP TABLE emails; --" in out
    assert _email_count() == 1


def test_run_sql_row_cap_truncates(clean_db):
    # Seed more than the cap to force truncation; use a tiny cap to keep it fast.
    _seed(senders={f"s{i}@x.com": 1 for i in range(10)})
    svc = _svc()
    out = svc.run_sql("SELECT gmail_id FROM emails", row_cap=5)
    assert "truncated at 5" in out
    # Exactly 5 data rows between header and the truncation note.
    body = out.splitlines()
    assert len(body) == 1 + 5 + 1  # header + 5 rows + note


def test_run_sql_comment_hidden_second_statement_is_safe(clean_db):
    # A '/* ; DROP */' or '-- ; DROP' must be treated as a single read statement
    # (comments stripped before the statement-count check) and change nothing.
    _seed(senders={"a@x.com": 2})
    svc = _svc()
    for q in (
        "SELECT gmail_id FROM emails /* ; DROP TABLE emails */",
        "SELECT gmail_id FROM emails -- ; DROP TABLE emails",
    ):
        out = svc.run_sql(q)
        assert not out.lower().startswith("error"), (q, out)
    assert _email_count() == 2


def test_run_sql_blocks_dangerous_functions_and_memory_bombs(clean_db):
    _seed(senders={"a@x.com": 3})
    svc = _svc()
    # Dangerous functions denied by the authorizer.
    assert svc.run_sql("SELECT load_extension('x')").lower().startswith("error")
    assert svc.run_sql("SELECT randomblob(200000000)").lower().startswith("error")
    # A huge single cell is capped (SQLITE_LIMIT_LENGTH) → error, not an OOM.
    assert svc.run_sql("SELECT zeroblob(50000000)").lower().startswith("error")
    assert _email_count() == 3


def test_run_sql_authorizer_denies_writes_even_if_validator_bypassed(clean_db):
    # The authorizer is the real backstop: drive the snapshot connection directly
    # (skipping _validate_sql) and confirm a write opcode is denied.
    import sqlite3

    _seed(senders={"a@x.com": 2})
    svc = _svc()
    conn = svc._sql_connection()
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM emails")
    assert _email_count() == 2


def test_run_sql_snapshot_is_independent_of_live_db(clean_db):
    # Even with the authorizer removed, writing the snapshot must not touch the
    # live DB — the snapshot is an in-memory copy.
    _seed(senders={"a@x.com": 4})
    svc = _svc()
    conn = svc._sql_connection()
    conn.set_authorizer(None)
    conn.execute("DELETE FROM emails")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 0
    assert _email_count() == 4  # live DB untouched


# ── MCP server exposes the catalog (skipped if mcp not installed) ─────────────


def test_mcp_server_exposes_all_tools():
    pytest.importorskip("mcp")
    import asyncio

    from postmind.core.agent_mcp import build_server

    srv = build_server("me@x.com")
    names = {t.name for t in asyncio.run(srv.list_tools())}
    # READ + stage_* + confirm/cancel/list — the full safety-bounded surface.
    assert {"get_inbox_overview", "stage_trash", "confirm_action", "cancel_action"} <= names
    # run_sql is part of the read surface.
    assert "run_sql" in names
    # No filesystem/bash/web tools leak in — domain surface only.
    assert not any(n in names for n in ("bash", "read_file", "write_file", "web_fetch"))
