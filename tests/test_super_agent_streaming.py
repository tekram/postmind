"""Streaming Super Agent — AIEngine.chat_stream and the SSE /agent/stream path.

The streaming engine is driven by a FAKE Anthropic client whose
``messages.stream(...)`` is a context manager yielding a scripted sequence of
streaming events (one tool_use round, then a final text turn). No network/AI is
required. We assert:

- chat_stream yields tool_start → tool_result → text_delta → done, in order.
- the tool_executor actually ran (its side effect is observable).
- input_json_delta fragments are accumulated into the tool input.
- the non-streaming /agent endpoint still returns {reply, actions, cards}
  (Phase 1/2 contract unchanged).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


# ── Fake Anthropic streaming primitives ───────────────────────────────────────


def _block_start(index, block):
    return SimpleNamespace(type="content_block_start", index=index, content_block=block)


def _block_delta(index, delta):
    return SimpleNamespace(type="content_block_delta", index=index, delta=delta)


def _text_delta(index, text):
    return _block_delta(index, SimpleNamespace(type="text_delta", text=text))


def _json_delta(index, partial):
    return _block_delta(index, SimpleNamespace(type="input_json_delta", partial_json=partial))


class _FakeStream:
    """Context-manager mimicking ``client.messages.stream(...)``."""

    def __init__(self, events, final_message):
        self._events = events
        self._final = final_message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._events)

    def get_final_message(self):
        return self._final


class _FakeMessages:
    def __init__(self, rounds):
        # rounds: list of (events, final_message); consumed one per stream() call.
        self._rounds = list(rounds)
        self.calls = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        events, final = self._rounds.pop(0)
        return _FakeStream(events, final)


class _FakeAnthropic:
    def __init__(self, rounds):
        self.messages = _FakeMessages(rounds)


def _make_engine(rounds):
    """Build an AIEngine in cloud mode with a fake anthropic client injected."""
    from postmind.core.ai_engine import AIEngine

    eng = AIEngine.__new__(AIEngine)
    eng._mode = "cloud"
    eng._cloud_model = "fake-model"
    eng._anthropic = _FakeAnthropic(rounds)
    eng._max_batch = 10
    eng._thinking_budget = 0  # thinking disabled by default in existing tests
    return eng


# ── Tests ──────────────────────────────────────────────────────────────────


def test_chat_stream_tool_round_then_text():
    # Round 1: model emits one tool_use block (input streamed as JSON fragments).
    tool_block = SimpleNamespace(type="tool_use", name="search_senders", id="tu_1")
    round1_events = [
        _block_start(0, tool_block),
        _json_delta(0, '{"query":'),
        _json_delta(0, ' "promo"}'),
    ]
    # The final message of round 1 carries the authoritative tool_use block.
    round1_final = SimpleNamespace(
        stop_reason="tool_use",
        content=[SimpleNamespace(type="tool_use", name="search_senders", id="tu_1", input={"query": "promo"})],
    )

    # Round 2: model emits the final assistant text.
    round2_events = [
        _block_start(0, SimpleNamespace(type="text")),
        _text_delta(0, "Found "),
        _text_delta(0, "3 senders."),
    ]
    round2_final = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="Found 3 senders.")],
    )

    eng = _make_engine([(round1_events, round1_final), (round2_events, round2_final)])

    ran = {}

    def executor(name, tool_input):
        ran["name"] = name
        ran["input"] = tool_input
        return "12 matches across 3 senders."

    tools = [{"name": "search_senders", "description": "x", "input_schema": {"type": "object"}}]
    events = list(
        eng.chat_stream(
            [{"role": "user", "content": "find promo senders"}],
            system="sys",
            tools=tools,
            tool_executor=executor,
            max_tool_iterations=6,
        )
    )

    types = [e["type"] for e in events]
    # Order: tool_start → tool_result → text_delta(s) → done
    assert types[0] == "tool_start"
    assert types[1] == "tool_result"
    assert "text_delta" in types
    assert types[-1] == "done"
    assert types.index("tool_start") < types.index("tool_result") < types.index("text_delta")

    # tool_start carries the accumulated input; tool_result carries the summary.
    tool_start = events[0]
    assert tool_start["name"] == "search_senders"
    assert tool_start["input"] == {"query": "promo"}
    tool_result = events[1]
    assert tool_result["name"] == "search_senders"
    assert tool_result["summary"] == "12 matches across 3 senders."

    # The executor actually ran with the accumulated JSON input.
    assert ran == {"name": "search_senders", "input": {"query": "promo"}}

    # Streamed text reassembles to the full reply.
    text = "".join(e["text"] for e in events if e["type"] == "text_delta")
    assert text == "Found 3 senders."

    # Two streamed requests were made (one per loop iteration).
    assert len(eng._anthropic.messages.calls) == 2


def test_chat_stream_text_only_no_tools():
    events = [
        _block_start(0, SimpleNamespace(type="text")),
        _text_delta(0, "Hello."),
    ]
    final = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Hello.")])
    eng = _make_engine([(events, final)])

    out = list(eng.chat_stream([{"role": "user", "content": "hi"}], system="sys"))
    assert [e["type"] for e in out] == ["text_delta", "done"]
    assert out[0]["text"] == "Hello."


def test_chat_stream_caps_iterations():
    # Every round wants another tool call → must stop at the cap and emit done.
    def tool_round():
        events = [
            _block_start(0, SimpleNamespace(type="tool_use", name="search_senders", id="tu")),
            _json_delta(0, "{}"),
        ]
        final = SimpleNamespace(
            stop_reason="tool_use",
            content=[SimpleNamespace(type="tool_use", name="search_senders", id="tu", input={})],
        )
        return (events, final)

    eng = _make_engine([tool_round() for _ in range(5)])
    out = list(
        eng.chat_stream(
            [{"role": "user", "content": "loop"}],
            system="sys",
            tools=[{"name": "search_senders", "description": "x", "input_schema": {"type": "object"}}],
            tool_executor=lambda n, i: "ok",
            max_tool_iterations=3,
        )
    )
    assert out[-1]["type"] == "done"
    # Capped at 3 iterations → 3 stream calls.
    assert len(eng._anthropic.messages.calls) == 3


def test_chat_stream_local_mode_raises():
    from postmind.core.ai_engine import AIEngine

    eng = AIEngine.__new__(AIEngine)
    eng._mode = "local"
    with pytest.raises(ValueError):
        list(eng.chat_stream([{"role": "user", "content": "hi"}], system="s"))


# ── Non-streaming /agent contract is unchanged ───────────────────────────────


def test_nonstreaming_agent_still_returns_reply_actions_cards(monkeypatch):
    monkeypatch.setenv("POSTMIND_AI_MODE", "cloud")
    monkeypatch.setenv("POSTMIND_CHAT_AI_MODE", "cloud")
    import postmind.config as config
    config._settings = None

    from fastapi.testclient import TestClient

    import postmind.core.ai_engine as ai_mod
    from postmind.web import server

    monkeypatch.setattr(server, "_get_web_account", lambda: "user@example.com")

    class FakeAI:
        def __init__(self, *a, **k):
            pass

        def chat(self, messages, system=None, tools=None, tool_executor=None, **kw):
            return "All set."

    monkeypatch.setattr(ai_mod, "AIEngine", FakeAI)

    client = TestClient(server.app)
    resp = client.post("/agent", json={"messages": [{"role": "user", "content": "hi"}]})
    data = resp.json()
    assert resp.status_code == 200
    assert data == {"reply": "All set.", "actions": [], "cards": []}
    config._settings = None


def test_agent_stream_route_registered():
    from postmind.web import server

    paths = {getattr(r, "path", None) for r in server.app.routes}
    assert "/agent/stream" in paths
    assert "/agent" in paths


# ── Deep task mode tests ──────────────────────────────────────────────────────


def test_chat_stream_deep_runs_more_iterations():
    """chat_stream_deep should allow more than the default 6 iterations."""

    def tool_round():
        events = [
            _block_start(0, SimpleNamespace(type="tool_use", name="search_senders", id="tu")),
            _json_delta(0, "{}"),
        ]
        final = SimpleNamespace(
            stop_reason="tool_use",
            content=[SimpleNamespace(type="tool_use", name="search_senders", id="tu", input={})],
        )
        return (events, final)

    # Provide 15 tool rounds — more than the normal 12 cap in _produce().
    # chat_stream_deep allows up to 30, so all 15 should run.
    text_events = [
        _block_start(0, SimpleNamespace(type="text")),
        _text_delta(0, "Done."),
    ]
    text_final = SimpleNamespace(stop_reason="end_turn", content=[])
    eng = _make_engine([tool_round() for _ in range(15)] + [(text_events, text_final)])

    out = list(
        eng.chat_stream_deep(
            [{"role": "user", "content": "complex task"}],
            system="sys",
            tools=[{"name": "search_senders", "description": "x", "input_schema": {"type": "object"}}],
            tool_executor=lambda n, i: "ok",
        )
    )
    assert out[-1]["type"] == "done"
    # All 15 tool rounds + final text = 16 stream calls.
    assert len(eng._anthropic.messages.calls) == 16


def test_chat_stream_deep_same_event_protocol():
    """chat_stream_deep yields the same event types as chat_stream."""
    tool_block = SimpleNamespace(type="tool_use", name="get_inbox_overview", id="tu_deep")
    round1_events = [
        _block_start(0, tool_block),
        _json_delta(0, "{}"),
    ]
    round1_final = SimpleNamespace(
        stop_reason="tool_use",
        content=[SimpleNamespace(type="tool_use", name="get_inbox_overview", id="tu_deep", input={})],
    )
    text_events = [
        _block_start(0, SimpleNamespace(type="text")),
        _text_delta(0, "Summary ready."),
    ]
    text_final = SimpleNamespace(stop_reason="end_turn", content=[])

    eng = _make_engine([(round1_events, round1_final), (text_events, text_final)])
    out = list(
        eng.chat_stream_deep(
            [{"role": "user", "content": "summarize all email"}],
            system="sys",
            tools=[{"name": "get_inbox_overview", "description": "x", "input_schema": {"type": "object"}}],
            tool_executor=lambda n, i: "overview result",
        )
    )
    types = [e["type"] for e in out]
    assert "tool_start" in types
    assert "tool_result" in types
    assert "text_delta" in types
    assert out[-1]["type"] == "done"


def test_is_deep_task_detects_chained_intent():
    from postmind.web.server import _is_deep_task

    positives = [
        [{"role": "user", "content": "find every vendor thread that went silent and draft follow-ups"}],
        [{"role": "user", "content": "for each email from acme.com, write a reply"}],
        [{"role": "user", "content": "scan my inbox for newsletters and then archive them"}],
        [{"role": "user", "content": "find all threads with no reply and draft responses"}],
        [{"role": "user", "content": "summarize all emails from last month"}],
    ]
    negatives = [
        [{"role": "user", "content": "find newsletters older than 2 years"}],
        [{"role": "user", "content": "delete emails from promo@example.com"}],
        [{"role": "user", "content": "what is eating my storage?"}],
        [],
    ]
    for msgs in positives:
        assert _is_deep_task(msgs), f"expected deep task for: {msgs[-1]['content'] if msgs else '(empty)'}"
    for msgs in negatives:
        assert not _is_deep_task(msgs), f"expected NOT deep task for: {msgs[-1]['content'] if msgs else '(empty)'}"


def test_is_deep_task_off_mode_skips_deep(monkeypatch):
    """When deep_task_mode='off', _produce() should use the normal 12-iteration path."""
    monkeypatch.setenv("POSTMIND_AI_MODE", "cloud")
    monkeypatch.setenv("POSTMIND_CHAT_AI_MODE", "cloud")
    monkeypatch.setenv("POSTMIND_DEEP_TASK_MODE", "off")
    import postmind.config as config
    config._settings = None

    import postmind.core.ai_engine as ai_mod
    from postmind.web import server

    monkeypatch.setattr(server, "_get_web_account", lambda: "user@example.com")

    captured = {}

    class FakeAI:
        def __init__(self, *a, **k):
            pass

        def chat_stream(self, messages, system=None, tools=None, tool_executor=None,
                        max_tool_iterations=6, max_tokens=1024, **kw):
            captured["iterations"] = max_tool_iterations
            captured["tokens"] = max_tokens
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "done"}

        def chat_stream_deep(self, *a, **k):
            raise AssertionError("chat_stream_deep should NOT be called when mode is off")

    monkeypatch.setattr(ai_mod, "AIEngine", FakeAI)

    from fastapi.testclient import TestClient
    client = TestClient(server.app)
    # A message that would trigger deep task heuristic if mode weren't off
    resp = client.post(
        "/agent/stream",
        json={"messages": [{"role": "user", "content": "find every vendor thread and draft follow-ups"}]},
        headers={"Accept": "text/event-stream"},
    )
    assert resp.status_code == 200
    assert captured.get("iterations") == 12  # normal cap, not 30
    config._settings = None
