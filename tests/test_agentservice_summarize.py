"""Tests for AgentService.summarize_thread / find_and_summarize_thread and
their corresponding MCP tool registrations."""

from __future__ import annotations

import pytest

from postmind.core.agent_service import AgentService
from postmind.core.gmail_client import Message, MessageHeader

# ── Fakes ─────────────────────────────────────────────────────────────────────


def _msg(
    id: str = "msg1",
    thread_id: str = "t1",
    subject: str = "Test subject",
    from_: str = "alice@example.com",
    body: str = "Hello world",
) -> Message:
    return Message(
        id=id,
        thread_id=thread_id,
        label_ids=["INBOX"],
        snippet=body[:100],
        headers=MessageHeader(subject=subject, from_=from_),
        body_text=body,
    )


class FakeProvider:
    """Minimal provider stub for summarize tests."""

    def __init__(self, messages: list[Message] | None = None):
        self._messages = messages or [_msg()]

    def supports(self, capability: str) -> bool:
        return False

    def list_message_ids(self, query: str = "", max_results: int | None = None) -> list[str]:
        return [m.id for m in self._messages]

    def get_messages_metadata(self, ids: list[str]) -> list[Message]:
        return [m for m in self._messages if m.id in ids]

    def get_messages_batch(self, ids: list[str]) -> list[Message]:
        return [m for m in self._messages if m.id in ids]


class FakeAI:
    """Minimal AI stub whose _complete returns a fixed string."""

    def __init__(self, response: str = "• p1\n• p2\n• p3"):
        self._response = response
        self.calls: list[tuple[str, str]] = []

    def _complete(self, system: str, prompt: str, max_tokens: int = 300) -> str:
        self.calls.append((system, prompt))
        return self._response


def _svc(provider: FakeProvider | None = None, ai: FakeAI | None = None) -> AgentService:
    svc = AgentService(account_email="test@x.com", ai=ai or FakeAI())
    svc._provider = provider or FakeProvider()
    return svc


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_summarize_thread_delegates_to_agent_tools():
    """summarize_thread should call agent_tools.summarize_thread and return its output."""
    ai = FakeAI(response="• p1\n• p2\n• p3")
    provider = FakeProvider(messages=[_msg(id="msg1", thread_id="thread_id_123")])
    svc = _svc(provider=provider, ai=ai)

    result = svc.summarize_thread("thread_id_123")

    assert "p1" in result
    # AI was actually called
    assert len(ai.calls) == 1


def test_find_and_summarize_thread_delegates():
    """find_and_summarize_thread should search → resolve thread_id → summarize."""
    ai = FakeAI(response="• summary bullet one\n• two\n• three")
    provider = FakeProvider(messages=[_msg(id="id1", thread_id="t1", body="Thread body text")])
    svc = _svc(provider=provider, ai=ai)

    result = svc.find_and_summarize_thread("some query")

    assert "summary" in result
    assert len(ai.calls) == 1


def test_summarize_thread_mcp_tool_registered():
    """build_server should register a tool named 'summarize_thread'."""
    pytest.importorskip("mcp")
    import asyncio

    from postmind.core.agent_mcp import build_server

    srv = build_server("test@x.com")
    names = {t.name for t in asyncio.run(srv.list_tools())}
    assert "summarize_thread" in names


def test_find_and_summarize_mcp_tool_registered():
    """build_server should register a tool named 'find_and_summarize_thread'."""
    pytest.importorskip("mcp")
    import asyncio

    from postmind.core.agent_mcp import build_server

    srv = build_server("test@x.com")
    names = {t.name for t in asyncio.run(srv.list_tools())}
    assert "find_and_summarize_thread" in names
