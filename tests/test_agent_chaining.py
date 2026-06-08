"""Tests for Super Agent tool chaining — find_and_summarize_thread and related."""

from __future__ import annotations

from unittest.mock import MagicMock


# ── Helpers ────────────────────────────────────────────────────────────────────


def _meta(mid, thread_id, sender, subject="Test", internal_date=1_700_000_000_000):
    """Build a minimal message-metadata mock."""
    m = MagicMock()
    m.id = mid
    m.thread_id = thread_id
    m.sender_email = sender
    m.sender_name = sender.split("@")[0]
    m.snippet = "snippet text"
    m.internal_date = internal_date
    m.size_estimate = 10_000
    m.headers = MagicMock()
    m.headers.subject = subject
    m.headers.from_ = sender
    m.headers.to = "me@example.com"
    m.headers.date = "Mon, 01 Jan 2024 12:00:00 +0000"
    m.headers.list_unsubscribe = ""
    return m


def _full_msg(mid, body="Email body text"):
    m = MagicMock()
    m.id = mid
    m.thread_id = mid
    m.snippet = body[:100]
    m.body_text = body
    m.size_estimate = len(body)
    m.headers = MagicMock()
    m.headers.subject = "Test subject"
    m.headers.from_ = "sender@example.com"
    m.headers.date = "Mon, 01 Jan 2024 12:00:00 +0000"
    return m


def _provider_with_two_ids():
    """Provider returning 2 message IDs and metadata with distinct thread_ids."""
    prov = MagicMock()
    prov.list_message_ids.return_value = ["mid-1", "mid-2"]
    prov.supports.return_value = True
    prov.get_messages_metadata.return_value = [
        _meta("mid-1", "thread-aaa", "alice@example.com", "Meeting notes"),
        _meta("mid-2", "thread-bbb", "alice@example.com", "Follow-up"),
    ]
    prov.get_thread_messages.return_value = [_full_msg("mid-1", "Thread body here")]
    return prov


# ── Test 1: full chain ─────────────────────────────────────────────────────────


def test_find_and_summarize_thread_full_chain():
    """Full chain: search → pick first thread_id → summarize."""
    from postmind.core import agent_tools

    prov = _provider_with_two_ids()

    ai = MagicMock()
    ai._complete.return_value = "• Point 1\n• Point 2\n• Point 3"

    result = agent_tools.find_and_summarize_thread(prov, ai, "test query")

    assert "Point 1" in result
    # Should include a header showing which thread was summarized
    assert "thread-aaa" in result or "Meeting notes" in result or "Summarizing" in result


# ── Test 2: no results ─────────────────────────────────────────────────────────


def test_find_and_summarize_thread_no_results():
    """When provider returns no IDs, report 'No emails found'."""
    from postmind.core import agent_tools

    prov = MagicMock()
    prov.list_message_ids.return_value = []

    ai = MagicMock()

    result = agent_tools.find_and_summarize_thread(prov, ai, "missing topic")

    assert "No emails found" in result or "no emails" in result.lower()
    ai._complete.assert_not_called()


# ── Test 3: result_index selects the correct thread ───────────────────────────


def test_find_and_summarize_thread_result_index():
    """result_index=2 should pick the third thread_id."""
    from postmind.core import agent_tools

    prov = MagicMock()
    prov.list_message_ids.return_value = ["m1", "m2", "m3"]
    prov.supports.return_value = True
    prov.get_messages_metadata.return_value = [
        _meta("m1", "thread-001", "a@x.com", "Alpha"),
        _meta("m2", "thread-002", "b@x.com", "Beta"),
        _meta("m3", "thread-003", "c@x.com", "Gamma"),
    ]
    prov.get_thread_messages.return_value = [_full_msg("m3", "Gamma body")]

    ai = MagicMock()
    ai._complete.return_value = "• G1\n• G2\n• G3"

    result = agent_tools.find_and_summarize_thread(prov, ai, "gamma topic", result_index=2)

    # The 3rd thread_id (thread-003) should have been passed to get_thread
    prov.get_thread_messages.assert_called_once_with("thread-003")
    assert "G1" in result


# ── Test 4: description hints in READ_TOOLS ────────────────────────────────────


def test_tool_description_hints_chain():
    """summarize_thread and get_thread descriptions must hint at find_emails_by_topic."""
    from postmind.core.agent_tools import READ_TOOLS

    by_name = {t["name"]: t for t in READ_TOOLS}

    summarize_desc = by_name["summarize_thread"]["description"]
    get_thread_desc = by_name["get_thread"]["description"]

    assert "find_emails_by_topic" in summarize_desc, (
        "summarize_thread description should mention find_emails_by_topic for chaining"
    )
    assert "find_emails_by_topic" in get_thread_desc, (
        "get_thread description should mention find_emails_by_topic for chaining"
    )


# ── Test 5: system prompt contains chaining rules ─────────────────────────────


def test_system_prompt_contains_chaining_rules(monkeypatch):
    """_build_agent_system must include find_and_summarize_thread and no-ask rule."""
    from postmind.web import server

    # Stub out the inbox overview so it doesn't need a real DB
    monkeypatch.setattr(server, "_chat_overview_text", lambda _: "0 emails, 0 senders.")

    prompt = server._build_agent_system("test@example.com", "cloud")

    assert "find_and_summarize_thread" in prompt
    # Case-insensitive check for the "never ask the user" rule
    assert "never ask the user" in prompt.lower()
