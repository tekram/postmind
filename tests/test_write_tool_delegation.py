"""WRITE tool staging delegation to AgentService.

These tests verify that the non-autopilot WRITE tools in
``_build_agent_tool_executor`` delegate their staging logic to
``AgentService`` and build the correct confirm card from the returned
descriptor.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch  # noqa: F401

# ── Helpers ────────────────────────────────────────────────────────────────────


def _executor(account="me@test.com", monkeypatch=None):
    """Build an executor with a fake provider, autopilot off, and empty cache."""
    from postmind.web import server

    # Autopilot off so we always hit the AgentService delegation path.
    if monkeypatch is not None:
        monkeypatch.setattr(server, "_autopilot_on", lambda: False)
        monkeypatch.setattr(server, "_cache_get", lambda: None)
        # Provider supports labels (Gmail-like) but we won't actually call it.
        prov = MagicMock()
        prov.supports.return_value = True
        monkeypatch.setattr(server, "_build_provider", lambda: prov)

    cards: list[dict] = []
    actions: list[dict] = []
    ai = MagicMock()
    executor = server._build_agent_tool_executor(account, ai, actions, cards)
    return executor, cards


def _fake_cleanup_descriptor(kind="archive", senders=None, email_count=5):
    return {
        "token": "tok-abc",
        "kind": kind,
        "summary": f"{kind} {email_count} emails",
        "senders": senders or ["a@b.com"],
        "email_count": email_count,
        "params": {
            "label_name": "",
            "blocked": [],
            "sensitive": [],
        },
        "undoable": True,
    }


# ── stage_archive / stage_label / stage_mark_read ─────────────────────────────


def test_stage_archive_uses_agentservice_staging(monkeypatch):
    """Non-autopilot stage_archive delegates to AgentService.stage_cleanup and
    appends a bulk_action card containing the returned token."""

    descriptor = _fake_cleanup_descriptor("archive", senders=["a@b.com"], email_count=5)

    with patch("postmind.core.agent_service.AgentService.stage_cleanup", return_value=descriptor):
        executor, cards = _executor("me@test.com", monkeypatch)
        result = executor("stage_archive", {"senders": ["a@b.com"], "query": ""})

    assert len(cards) == 1
    card = cards[0]
    assert card["type"] == "bulk_action"
    assert card["fields"]["token"] == "tok-abc"
    assert card["fields"]["action"] == "archive"
    assert card["fields"]["total_count"] == 5
    assert card["fields"]["undoable"] is True
    assert "1" in result  # mentions sender count
    assert "5" in result  # mentions email count


def test_stage_archive_error_returns_message(monkeypatch):
    """When AgentService.stage_cleanup returns an error, the executor returns the
    error string and appends no card."""
    with patch(
        "postmind.core.agent_service.AgentService.stage_cleanup",
        return_value={"error": "No senders found"},
    ):
        executor, cards = _executor("me@test.com", monkeypatch)
        result = executor("stage_archive", {"senders": [], "query": ""})

    assert result == "No senders found"
    assert cards == []


def test_stage_label_passes_label_name_to_agentservice(monkeypatch):
    """stage_label forwards label_name to AgentService.stage_cleanup."""
    captured: dict = {}

    def _fake_cleanup(self, action, senders, query, label_name):
        captured["label_name"] = label_name
        captured["action"] = action
        return _fake_cleanup_descriptor("label", senders=["a@b.com"], email_count=3)

    with patch("postmind.core.agent_service.AgentService.stage_cleanup", _fake_cleanup):
        executor, cards = _executor("me@test.com", monkeypatch)
        executor("stage_label", {"senders": ["a@b.com"], "label_name": "Newsletters"})

    assert captured["label_name"] == "Newsletters"
    assert captured["action"] == "label"
    assert len(cards) == 1
    assert cards[0]["fields"]["label_name"] == "Newsletters"


def test_stage_label_missing_label_name_returns_error(monkeypatch):
    """stage_label short-circuits with an error message if label_name is empty."""
    executor, cards = _executor("me@test.com", monkeypatch)
    result = executor("stage_label", {"senders": ["a@b.com"], "label_name": ""})

    assert "label name is required" in result.lower()
    assert cards == []


def test_stage_mark_read_delegates_to_agentservice(monkeypatch):
    """stage_mark_read delegates to AgentService.stage_cleanup with action='mark_read'."""
    descriptor = _fake_cleanup_descriptor("mark_read", senders=["x@y.com"], email_count=2)

    with patch("postmind.core.agent_service.AgentService.stage_cleanup", return_value=descriptor):
        executor, cards = _executor("me@test.com", monkeypatch)
        result = executor("stage_mark_read", {"senders": ["x@y.com"]})

    assert len(cards) == 1
    assert cards[0]["type"] == "bulk_action"
    assert cards[0]["fields"]["action"] == "mark_read"
    assert cards[0]["fields"]["token"] == "tok-abc"
    assert "mark read" in result.lower()


def test_stage_archive_enriches_targets_from_cache(monkeypatch):
    """When scan cache has groups, targets are enriched with count/size metadata."""
    from postmind.web import server

    # Fake scan-cache group for a@b.com
    fake_group = MagicMock()
    fake_group.sender_email = "a@b.com"
    fake_group.display_name = "A Sender"
    fake_group.count = 42
    fake_group.total_size_mb = 1.5
    fake_group.total_size_bytes = 1_500_000

    monkeypatch.setattr(server, "_autopilot_on", lambda: False)
    monkeypatch.setattr(server, "_cache_get", lambda: {"groups": [fake_group]})
    prov = MagicMock()
    prov.supports.return_value = True
    monkeypatch.setattr(server, "_build_provider", lambda: prov)

    descriptor = _fake_cleanup_descriptor("archive", senders=["a@b.com"], email_count=42)
    with patch("postmind.core.agent_service.AgentService.stage_cleanup", return_value=descriptor):
        cards: list[dict] = []
        ai = MagicMock()
        executor = server._build_agent_tool_executor("me@test.com", ai, [], cards)
        executor("stage_archive", {"senders": ["a@b.com"]})

    assert len(cards) == 1
    targets = cards[0]["fields"]["targets"]
    assert len(targets) == 1
    assert targets[0]["sender_email"] == "a@b.com"
    assert targets[0]["count"] == 42
    assert "1.5 MB" in targets[0]["size_str"]


# ── stage_unsubscribe ─────────────────────────────────────────────────────────


def test_stage_unsubscribe_appends_card(monkeypatch):
    """stage_unsubscribe delegates to AgentService.stage_unsubscribe and appends
    an unsubscribe card containing the returned token."""
    descriptor = {
        "token": "tok-unsub",
        "kind": "unsubscribe",
        "summary": "unsubscribe from 2 sender(s)",
        "senders": ["news@promo.com", "deals@shop.com"],
        "email_count": 10,
        "params": {"also_trash": False, "blocked": [], "sensitive": []},
        "undoable": False,
    }

    with patch(
        "postmind.core.agent_service.AgentService.stage_unsubscribe", return_value=descriptor
    ):
        executor, cards = _executor("me@test.com", monkeypatch)
        result = executor("stage_unsubscribe", {"senders": ["news@promo.com", "deals@shop.com"]})

    assert len(cards) == 1
    card = cards[0]
    assert card["type"] == "unsubscribe"
    assert card["fields"]["token"] == "tok-unsub"
    assert card["fields"]["total_count"] == 10
    assert "2" in result  # mentions sender count


def test_stage_unsubscribe_error_returns_message(monkeypatch):
    """When AgentService.stage_unsubscribe returns an error the executor returns it."""
    with patch(
        "postmind.core.agent_service.AgentService.stage_unsubscribe",
        return_value={"error": "No matching senders to unsubscribe from."},
    ):
        executor, cards = _executor("me@test.com", monkeypatch)
        result = executor("stage_unsubscribe", {"senders": []})

    assert "No matching senders" in result
    assert cards == []


# ── send_email ────────────────────────────────────────────────────────────────


def test_send_email_appends_card(monkeypatch):
    """send_email delegates to AgentService.stage_send and appends a send_email card."""
    descriptor = {
        "token": "tok-send",
        "kind": "send",
        "summary": "send email to bob@example.com",
        "senders": [],
        "email_count": 0,
        "params": {
            "to": "bob@example.com",
            "subject": "Hello",
            "body": "Hi Bob",
        },
        "undoable": False,
    }

    with patch("postmind.core.agent_service.AgentService.stage_send", return_value=descriptor):
        executor, cards = _executor("me@test.com", monkeypatch)
        result = executor(
            "send_email",
            {"to": "bob@example.com", "subject": "Hello", "body": "Hi Bob"},
        )

    assert len(cards) == 1
    card = cards[0]
    assert card["type"] == "send_email"
    assert card["fields"]["token"] == "tok-send"
    assert card["fields"]["to"] == "bob@example.com"
    assert card["fields"]["subject"] == "Hello"
    assert card["fields"]["body"] == "Hi Bob"
    assert "bob@example.com" in result


def test_send_email_error_returns_message(monkeypatch):
    """When AgentService.stage_send returns an error the executor returns it."""
    with patch(
        "postmind.core.agent_service.AgentService.stage_send",
        return_value={"error": "A single valid recipient address is required."},
    ):
        executor, cards = _executor("me@test.com", monkeypatch)
        result = executor("send_email", {"to": "not-an-email", "subject": "x", "body": "y"})

    assert "valid recipient" in result.lower()
    assert cards == []


# ── create_agent ──────────────────────────────────────────────────────────────


def test_create_agent_appends_card(monkeypatch):
    """create_agent delegates to AgentService.stage_create_agent and appends a
    create_agent card containing the token and all param fields."""
    descriptor = {
        "token": "tok-agent",
        "kind": "create_agent",
        "summary": "create heartbeat agent for me@test.com",
        "senders": [],
        "email_count": 0,
        "params": {
            "email": "me@test.com",
            "name": "Me",
            "interval_minutes": 30,
            "voice_style": "",
            "user_context": "",
            "run_rules": True,
            "run_followups": True,
            "run_avoidance": False,
        },
        "undoable": False,
    }

    with patch(
        "postmind.core.agent_service.AgentService.stage_create_agent", return_value=descriptor
    ):
        executor, cards = _executor("me@test.com", monkeypatch)
        result = executor(
            "create_agent",
            {
                "email": "me@test.com",
                "name": "Me",
                "interval_minutes": 30,
                "run_rules": True,
                "run_followups": True,
                "run_avoidance": False,
            },
        )

    assert len(cards) == 1
    card = cards[0]
    assert card["type"] == "create_agent"
    assert card["fields"]["token"] == "tok-agent"
    assert card["fields"]["email"] == "me@test.com"
    assert card["fields"]["name"] == "Me"
    assert card["fields"]["interval_minutes"] == 30
    assert card["fields"]["run_rules"] is True
    assert "me@test.com" in result


def test_create_agent_error_returns_message(monkeypatch):
    """When AgentService.stage_create_agent returns an error the executor returns it."""
    with patch(
        "postmind.core.agent_service.AgentService.stage_create_agent",
        return_value={"error": "No account to attach the agent to — connect an account first."},
    ):
        executor, cards = _executor("", monkeypatch)
        result = executor("create_agent", {"email": ""})

    assert "No account" in result
    assert cards == []


# ── create_rule ───────────────────────────────────────────────────────────────


def test_create_rule_appends_card(monkeypatch):
    """create_rule delegates to AgentService.stage_create_rule and appends a
    create_rule card with token, query, action, and explanation."""
    descriptor = {
        "token": "tok-rule",
        "kind": "create_rule",
        "summary": "archive newsletters (query: label:newsletters, action: archive)",
        "senders": [],
        "email_count": 0,
        "params": {
            "natural_language": "archive all newsletters",
            "gmail_query": "label:newsletters",
            "action": "archive",
            "action_params": {},
            "explanation": "Archive any email tagged as a newsletter.",
            "warnings": [],
        },
        "undoable": False,
    }

    with patch(
        "postmind.core.agent_service.AgentService.stage_create_rule", return_value=descriptor
    ):
        executor, cards = _executor("me@test.com", monkeypatch)
        result = executor("create_rule", {"natural_language": "archive all newsletters"})

    assert len(cards) == 1
    card = cards[0]
    assert card["type"] == "create_rule"
    assert card["fields"]["token"] == "tok-rule"
    assert card["fields"]["natural_language"] == "archive all newsletters"
    assert card["fields"]["gmail_query"] == "label:newsletters"
    assert card["fields"]["action"] == "archive"
    assert card["fields"]["explanation"] == "Archive any email tagged as a newsletter."
    assert card["fields"]["warnings"] == []
    assert "Archive any email tagged as a newsletter." in result


def test_create_rule_error_returns_message(monkeypatch):
    """When AgentService.stage_create_rule returns an error the executor returns it."""
    with patch(
        "postmind.core.agent_service.AgentService.stage_create_rule",
        return_value={"error": "Need the rule in plain English."},
    ):
        executor, cards = _executor("me@test.com", monkeypatch)
        result = executor("create_rule", {"natural_language": ""})

    assert "Need the rule" in result
    assert cards == []
