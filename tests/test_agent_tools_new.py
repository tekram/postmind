"""Tests for new agent tools: summarize_thread and run_sql power mode."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

from postmind.core import agent_tools
from postmind.core.sender_stats import SenderGroup

# ── summarize_thread tests ─────────────────────────────────────────────────────


def _fake_provider_with_thread(thread_id: str, messages_text: str):
    """Return a mock provider whose get_thread_messages returns a single fake msg."""
    provider = MagicMock()
    provider.supports.return_value = True  # supports threads

    msg = MagicMock()
    msg.snippet = messages_text
    msg.headers.subject = "Test Subject"
    msg.headers.from_ = "sender@example.com"
    msg.headers.date = "2024-01-01"

    provider.get_thread_messages.return_value = [msg]
    return provider


def test_summarize_thread_no_ai_returns_content():
    """When ai=None, summarize_thread returns thread content with no-AI note."""
    provider = _fake_provider_with_thread("thread-123", "Hello, this is a test email body.")
    result = agent_tools.summarize_thread(provider, None, "thread-123")
    assert "Thread content:" in result
    assert "AI summarization not available" in result
    assert "AI mode is off" in result


def test_summarize_thread_ai_error_fallback():
    """When ai._complete raises, returns thread content with error note."""
    provider = _fake_provider_with_thread("thread-abc", "Some thread content here.")

    class BadAI:
        def _complete(self, system, prompt, max_tokens=300):
            raise RuntimeError("API timeout")

    result = agent_tools.summarize_thread(provider, BadAI(), "thread-abc")
    assert "Thread content:" in result
    assert "Could not summarize" in result
    assert "API timeout" in result


def test_summarize_thread_bad_thread_id():
    """When get_thread returns 'No thread_id provided.', summarize_thread passes it through."""
    provider = MagicMock()
    result = agent_tools.summarize_thread(provider, None, "")
    assert result == "No thread_id provided."


def test_summarize_thread_ai_returns_summary():
    """When ai._complete succeeds, returns the AI summary."""
    provider = _fake_provider_with_thread("thread-xyz", "Project discussion thread.")

    class GoodAI:
        def _complete(self, system, prompt, max_tokens=300):
            return "• Point 1\n• Point 2\n• Point 3"

    result = agent_tools.summarize_thread(provider, GoodAI(), "thread-xyz")
    assert "Point 1" in result
    assert "Point 2" in result
    assert "Point 3" in result


# ── run_sql power mode tests ──────────────────────────────────────────────────


def test_run_sql_tool_schema_present_when_power_mode_on(monkeypatch):
    """When agent_power_mode=True, _agent_tools_for includes run_sql; when False, does not."""
    import postmind.config as config
    from postmind.web.server import _agent_tools_for

    # Power mode OFF
    monkeypatch.setenv("POSTMIND_AGENT_POWER_MODE", "false")
    config._settings = None
    tools_off = _agent_tools_for("test@example.com")
    tool_names_off = {t["name"] for t in tools_off}
    assert "run_sql" not in tool_names_off

    # Power mode ON
    monkeypatch.setenv("POSTMIND_AGENT_POWER_MODE", "true")
    config._settings = None
    tools_on = _agent_tools_for("test@example.com")
    tool_names_on = {t["name"] for t in tools_on}
    assert "run_sql" in tool_names_on

    # All existing tools still present in both cases
    for tool in agent_tools.ALL_TOOLS:
        assert tool["name"] in tool_names_on
        assert tool["name"] in tool_names_off


def test_run_sql_executor_delegates_to_agent_service(monkeypatch):
    """Monkeypatching AgentService.run_sql verifies the executor delegates to it."""
    import postmind.config as config
    from postmind.web import server

    account = "test@example.com"

    # Seed a scan cache entry so the agent endpoint doesn't bail out.
    now = datetime.now(timezone.utc)
    group = SenderGroup(
        sender_email="news@example.com",
        sender_name="News",
        count=5,
        total_size_bytes=1_000_000,
        earliest_date=now,
        latest_date=now,
        sample_subjects=["hi"],
        message_ids=["msg1", "msg2"],
        has_unsubscribe=True,
    )
    server._scan_cache[account] = {
        "groups": [group],
        "profile": {},
        "account_email": account,
        "scanned_at": "now",
        "expires": time.time() + 300,
    }

    monkeypatch.setattr(server, "_get_web_account", lambda: account)

    # Force cloud mode so the agent proceeds.
    monkeypatch.setenv("POSTMIND_AI_MODE", "cloud")
    monkeypatch.setenv("POSTMIND_CHAT_AI_MODE", "cloud")
    monkeypatch.setenv("POSTMIND_AGENT_POWER_MODE", "true")
    config._settings = None

    captured = {}

    # Monkeypatch AgentService.run_sql to capture the call.
    import postmind.core.agent_service as agent_service_mod

    def fake_run_sql(self, query):
        captured["query"] = query
        return "account_email\ntest@example.com\n(1 row)"

    monkeypatch.setattr(agent_service_mod.AgentService, "run_sql", fake_run_sql)

    # Patch AIEngine to fire the run_sql tool.
    class FakeAI:
        def __init__(self, *a, **k):
            pass

        def chat(self, messages, system=None, tools=None, tool_executor=None, **kw):
            result = tool_executor("run_sql", {"query": "SELECT account_email FROM emails LIMIT 1"})
            captured["result"] = result
            return "Done."

        def compose_email(self, *a, **k):
            return ""

        def translate_rule(self, nl):
            raise AssertionError("should not be called")

    import postmind.core.ai_engine as ai_mod

    monkeypatch.setattr(ai_mod, "AIEngine", FakeAI)

    from fastapi.testclient import TestClient

    client = TestClient(server.app)
    resp = client.post("/agent", json={"messages": [{"role": "user", "content": "run sql"}]})
    assert resp.status_code == 200

    assert captured.get("query") == "SELECT account_email FROM emails LIMIT 1"
    assert "account_email" in captured.get("result", "")

    # Cleanup
    server._scan_cache.clear()
    config._settings = None
