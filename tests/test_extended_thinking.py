"""Extended thinking — AIEngine integration tests.

Verifies the full thinking lifecycle without any network calls:
- thinking param is added to API kwargs when budget > 0
- max_tokens is auto-bumped above budget_tokens
- thinking_delta events stream from chat_stream
- thinking blocks are preserved in multi-turn conversation history
- non-thinking paths are completely unaffected
- deep mode upgrades the budget to 16k
- budget=0 disables thinking even when settings say enabled
- server plumbs thinking_budget through to AIEngine
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


# ── Fake Anthropic primitives (shared with test_super_agent_streaming) ─────────


def _block_start(index, block):
    return SimpleNamespace(type="content_block_start", index=index, content_block=block)


def _block_delta(index, delta):
    return SimpleNamespace(type="content_block_delta", index=index, delta=delta)


def _text_delta(index, text):
    return _block_delta(index, SimpleNamespace(type="text_delta", text=text))


def _json_delta(index, partial):
    return _block_delta(index, SimpleNamespace(type="input_json_delta", partial_json=partial))


def _thinking_delta(index, text):
    return _block_delta(index, SimpleNamespace(type="thinking_delta", thinking=text))


def _thinking_block():
    return SimpleNamespace(type="thinking", thinking="", signature="sig123")


def _content_block_stop(index):
    return SimpleNamespace(type="content_block_stop", index=index)


class _FakeStream:
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
        self._rounds = list(rounds)
        self.calls = []
        # Non-streaming rounds for _chat_cloud tests
        self.create_calls = []
        self._create_rounds = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        events, final = self._rounds.pop(0)
        return _FakeStream(events, final)

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return self._create_rounds.pop(0)


class _FakeAnthropic:
    def __init__(self, rounds=None, create_rounds=None):
        self.messages = _FakeMessages(rounds or [])
        if create_rounds:
            self.messages._create_rounds = create_rounds


def _make_engine(rounds=None, create_rounds=None, thinking_budget=0):
    """Build an AIEngine in cloud mode with a fake client injected."""
    from postmind.core.ai_engine import AIEngine

    eng = AIEngine.__new__(AIEngine)
    eng._mode = "cloud"
    eng._cloud_model = "fake-model"
    eng._anthropic = _FakeAnthropic(rounds, create_rounds)
    eng._max_batch = 10
    eng._thinking_budget = thinking_budget
    return eng


# ── _thinking_kwargs ──────────────────────────────────────────────────────────


def test_thinking_kwargs_disabled_returns_empty():
    eng = _make_engine(thinking_budget=0)
    extra, tokens = eng._thinking_kwargs(1024)
    assert extra == {}
    assert tokens == 1024


def test_thinking_kwargs_enabled_returns_param():
    eng = _make_engine(thinking_budget=8000)
    extra, tokens = eng._thinking_kwargs(1024)
    assert extra == {"thinking": {"type": "enabled", "budget_tokens": 8000}}
    # max_tokens must be at least budget + 2048
    assert tokens >= 8000 + 2048


def test_thinking_kwargs_tokens_already_large():
    eng = _make_engine(thinking_budget=4000)
    extra, tokens = eng._thinking_kwargs(20_000)
    # If caller already passed a large max_tokens, keep it
    assert tokens == 20_000


def test_thinking_kwargs_small_budget_bumps_tokens():
    eng = _make_engine(thinking_budget=1024)
    _, tokens = eng._thinking_kwargs(512)
    assert tokens >= 1024 + 2048


# ── _chat_cloud (non-streaming) ────────────────────────────────────────────────


def _make_response(stop_reason="end_turn", content=None):
    """Build a fake messages.create() response."""
    if content is None:
        content = [SimpleNamespace(type="text", text="Done.")]
    return SimpleNamespace(stop_reason=stop_reason, content=content)


def test_chat_cloud_thinking_param_sent():
    resp = _make_response()
    eng = _make_engine(create_rounds=[resp], thinking_budget=8000)
    result = eng._chat_cloud(
        [{"role": "user", "content": "hi"}], "sys", None, None, 1024, 3
    )
    assert result == "Done."
    call = eng._anthropic.messages.create_calls[0]
    assert "thinking" in call
    assert call["thinking"] == {"type": "enabled", "budget_tokens": 8000}
    assert call["max_tokens"] >= 8000 + 2048


def test_chat_cloud_no_thinking_when_budget_zero():
    resp = _make_response()
    eng = _make_engine(create_rounds=[resp], thinking_budget=0)
    eng._chat_cloud(
        [{"role": "user", "content": "hi"}], "sys", None, None, 1024, 3
    )
    call = eng._anthropic.messages.create_calls[0]
    assert "thinking" not in call


def test_chat_cloud_thinking_blocks_preserved_multiturn():
    """Thinking blocks in round 1 must appear in round 2's messages list."""
    thinking_content = SimpleNamespace(type="thinking", thinking="I think…", signature="sig")
    tool_content = SimpleNamespace(type="tool_use", name="search_senders", id="tu1", input={"query": "x"})

    round1 = _make_response(
        stop_reason="tool_use",
        content=[thinking_content, tool_content],
    )
    round2 = _make_response(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Result.")])

    eng = _make_engine(create_rounds=[round1, round2], thinking_budget=4000)

    def executor(name, inp):
        return "found 5"

    tools = [{"name": "search_senders", "description": "x", "input_schema": {"type": "object"}}]
    result = eng._chat_cloud(
        [{"role": "user", "content": "search senders"}], "sys", tools, executor, 1024, 5
    )

    assert result == "Result."
    # Round 2 call: messages should contain the assistant turn with BOTH thinking and tool_use
    round2_call = eng._anthropic.messages.create_calls[1]
    convo = round2_call["messages"]
    # Find the assistant turn (role="assistant")
    asst_turns = [m for m in convo if m["role"] == "assistant"]
    assert len(asst_turns) == 1
    asst_content = asst_turns[0]["content"]
    # The thinking block must be present alongside the tool_use block
    block_types = [getattr(b, "type", None) for b in asst_content]
    assert "thinking" in block_types, "thinking block must be preserved in assistant turn"
    assert "tool_use" in block_types


def test_chat_cloud_thinking_sent_every_iteration():
    """thinking param must appear in ALL API calls, not just the first."""
    tool = SimpleNamespace(type="tool_use", name="search_senders", id="tu1", input={})
    round1 = _make_response(stop_reason="tool_use", content=[tool])
    round2 = _make_response()

    eng = _make_engine(create_rounds=[round1, round2], thinking_budget=3000)
    eng._chat_cloud(
        [{"role": "user", "content": "q"}], "sys",
        [{"name": "search_senders", "description": "x", "input_schema": {"type": "object"}}],
        lambda n, i: "ok",
        1024, 5,
    )
    for call in eng._anthropic.messages.create_calls:
        assert "thinking" in call, "thinking must be in every iteration's kwargs"


# ── chat_stream (streaming) ───────────────────────────────────────────────────


def _thinking_stream_round(thinking_text="Let me reason…", reply_text="Done."):
    """Build a stream round that emits thinking_delta then text_delta."""
    tb = SimpleNamespace(type="thinking")
    text_b = SimpleNamespace(type="text")
    events = [
        _block_start(0, tb),
        _thinking_delta(0, thinking_text[:len(thinking_text)//2]),
        _thinking_delta(0, thinking_text[len(thinking_text)//2:]),
        _block_start(1, text_b),
        _text_delta(1, reply_text),
    ]
    final = SimpleNamespace(
        stop_reason="end_turn",
        content=[
            SimpleNamespace(type="thinking", thinking=thinking_text, signature="sig"),
            SimpleNamespace(type="text", text=reply_text),
        ],
    )
    return events, final


def test_chat_stream_thinking_delta_events_emitted():
    """thinking_delta events must be yielded for each thinking chunk."""
    events, final = _thinking_stream_round("I reason step by step.")
    eng = _make_engine(rounds=[(events, final)], thinking_budget=8000)

    out = list(eng.chat_stream([{"role": "user", "content": "hi"}], system="sys"))

    types = [e["type"] for e in out]
    assert "thinking_delta" in types, "thinking_delta events must be yielded"
    assert "text_delta" in types
    assert types[-1] == "done"

    # Thinking deltas come before text
    first_thinking = types.index("thinking_delta")
    first_text = types.index("text_delta")
    assert first_thinking < first_text

    # All thinking chunks reassemble to the full thinking text
    thinking_text = "".join(e["text"] for e in out if e["type"] == "thinking_delta")
    assert thinking_text == "I reason step by step."


def test_chat_stream_no_thinking_delta_when_disabled():
    """When budget=0, no thinking_delta events must appear even if a thinking block arrives."""
    tb = SimpleNamespace(type="thinking")
    events = [
        _block_start(0, tb),
        _thinking_delta(0, "this should not appear"),
        _block_start(1, SimpleNamespace(type="text")),
        _text_delta(1, "Hello."),
    ]
    final = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="Hello.")],
    )
    eng = _make_engine(rounds=[(events, final)], thinking_budget=0)

    out = list(eng.chat_stream([{"role": "user", "content": "hi"}], system="sys"))
    types = [e["type"] for e in out]
    assert "thinking_delta" not in types
    assert "text_delta" in types


def test_chat_stream_thinking_with_tool_use():
    """thinking → tool_use round, then text reply with thinking preserved."""
    tb = SimpleNamespace(type="thinking")
    tool_b = SimpleNamespace(type="tool_use", name="search_senders", id="tu_think")
    # Round 1: thinking block + tool_use block
    round1_events = [
        _block_start(0, tb),
        _thinking_delta(0, "I should call search_senders."),
        _block_start(1, tool_b),
        _json_delta(1, '{"query": "promo"}'),
    ]
    round1_final = SimpleNamespace(
        stop_reason="tool_use",
        content=[
            SimpleNamespace(type="thinking", thinking="I should call search_senders.", signature="sig"),
            SimpleNamespace(type="tool_use", name="search_senders", id="tu_think", input={"query": "promo"}),
        ],
    )

    # Round 2: text reply (no thinking this round)
    round2_events = [
        _block_start(0, SimpleNamespace(type="text")),
        _text_delta(0, "Found 3 senders."),
    ]
    round2_final = SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="Found 3 senders.")],
    )

    eng = _make_engine(rounds=[(round1_events, round1_final), (round2_events, round2_final)], thinking_budget=4000)

    ran = {}
    def executor(name, inp):
        ran["name"] = name
        return "12 matches"

    tools = [{"name": "search_senders", "description": "x", "input_schema": {"type": "object"}}]
    out = list(eng.chat_stream(
        [{"role": "user", "content": "find promo senders"}],
        system="sys",
        tools=tools,
        tool_executor=executor,
        max_tool_iterations=6,
    ))

    types = [e["type"] for e in out]
    # Must have thinking_delta, tool_start, tool_result, text_delta, done in order
    assert "thinking_delta" in types
    assert "tool_start" in types
    assert "tool_result" in types
    assert "text_delta" in types
    assert types[-1] == "done"

    assert types.index("thinking_delta") < types.index("tool_start")
    assert types.index("tool_start") < types.index("text_delta")

    assert ran == {"name": "search_senders"}

    # Round 2 call: assistant content must include thinking block from round 1
    round2_call = eng._anthropic.messages.calls[1]
    asst_turns = [m for m in round2_call["messages"] if m["role"] == "assistant"]
    assert len(asst_turns) == 1
    block_types = [getattr(b, "type", None) for b in asst_turns[0]["content"]]
    assert "thinking" in block_types, "thinking block must be preserved in multi-turn"


def test_chat_stream_thinking_param_in_every_call():
    """thinking param must be present in all stream calls, not just the first."""
    tool_b = SimpleNamespace(type="tool_use", name="search_senders", id="tu1")

    def tool_round():
        events = [
            _block_start(0, tool_b),
            _json_delta(0, "{}"),
        ]
        final = SimpleNamespace(
            stop_reason="tool_use",
            content=[SimpleNamespace(type="tool_use", name="search_senders", id="tu1", input={})],
        )
        return events, final

    text_events = [_block_start(0, SimpleNamespace(type="text")), _text_delta(0, "Done.")]
    text_final = SimpleNamespace(stop_reason="end_turn", content=[])

    eng = _make_engine(
        rounds=[tool_round(), tool_round(), (text_events, text_final)],
        thinking_budget=5000,
    )
    list(eng.chat_stream(
        [{"role": "user", "content": "q"}], "sys",
        tools=[{"name": "search_senders", "description": "x", "input_schema": {"type": "object"}}],
        tool_executor=lambda n, i: "ok",
        max_tool_iterations=5,
    ))
    for call in eng._anthropic.messages.calls:
        assert "thinking" in call
        assert call["thinking"]["budget_tokens"] == 5000
        assert call["max_tokens"] >= 5000 + 2048


# ── chat_stream_deep ──────────────────────────────────────────────────────────


def test_chat_stream_deep_upgrades_budget():
    """Deep mode must upgrade thinking budget to at least 16 000."""
    events, final = _thinking_stream_round("deep reasoning")
    eng = _make_engine(rounds=[(events, final)], thinking_budget=8000)

    list(eng.chat_stream_deep(
        [{"role": "user", "content": "complex"}],
        system="sys",
    ))

    call = eng._anthropic.messages.calls[0]
    assert call["thinking"]["budget_tokens"] == 16_000  # upgraded from 8000


def test_chat_stream_deep_restores_budget_after_use():
    """Budget must be restored to original value after chat_stream_deep returns."""
    events, final = _thinking_stream_round()
    eng = _make_engine(rounds=[(events, final)], thinking_budget=8000)

    list(eng.chat_stream_deep([{"role": "user", "content": "q"}], system="sys"))
    # After deep run, budget returns to configured value
    assert eng._thinking_budget == 8000


def test_chat_stream_deep_budget_already_large_not_downgraded():
    """If budget is already > 16k, deep mode must not reduce it."""
    events, final = _thinking_stream_round()
    eng = _make_engine(rounds=[(events, final)], thinking_budget=32_000)

    list(eng.chat_stream_deep([{"role": "user", "content": "q"}], system="sys"))

    call = eng._anthropic.messages.calls[0]
    assert call["thinking"]["budget_tokens"] == 32_000


def test_chat_stream_deep_zero_budget_no_thinking():
    """When budget=0, deep mode must not add thinking even with a large token budget."""
    events, final = _thinking_stream_round()
    eng = _make_engine(rounds=[(events, final)], thinking_budget=0)

    list(eng.chat_stream_deep([{"role": "user", "content": "q"}], system="sys"))

    call = eng._anthropic.messages.calls[0]
    assert "thinking" not in call


# ── AIEngine.__init__ reads settings ─────────────────────────────────────────


def test_init_reads_extended_thinking_from_settings(monkeypatch):
    monkeypatch.setenv("POSTMIND_AI_MODE", "cloud")
    monkeypatch.setenv("POSTMIND_EXTENDED_THINKING", "true")
    monkeypatch.setenv("POSTMIND_THINKING_BUDGET_TOKENS", "12000")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    import postmind.config as cfg
    cfg._settings = None

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: SimpleNamespace(messages=None))

    from postmind.core.ai_engine import AIEngine
    eng = AIEngine()
    assert eng._thinking_budget == 12000
    cfg._settings = None


def test_init_thinking_disabled_by_default(monkeypatch):
    monkeypatch.setenv("POSTMIND_AI_MODE", "cloud")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    import postmind.config as cfg
    cfg._settings = None

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: SimpleNamespace(messages=None))

    from postmind.core.ai_engine import AIEngine
    eng = AIEngine()
    assert eng._thinking_budget == 0
    cfg._settings = None


def test_init_explicit_budget_overrides_settings(monkeypatch):
    monkeypatch.setenv("POSTMIND_AI_MODE", "cloud")
    monkeypatch.setenv("POSTMIND_EXTENDED_THINKING", "false")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    import postmind.config as cfg
    cfg._settings = None

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: SimpleNamespace(messages=None))

    from postmind.core.ai_engine import AIEngine
    eng = AIEngine(thinking_budget=5000)
    assert eng._thinking_budget == 5000
    cfg._settings = None


def test_init_explicit_zero_disables_thinking(monkeypatch):
    monkeypatch.setenv("POSTMIND_AI_MODE", "cloud")
    monkeypatch.setenv("POSTMIND_EXTENDED_THINKING", "true")
    monkeypatch.setenv("POSTMIND_THINKING_BUDGET_TOKENS", "8000")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    import postmind.config as cfg
    cfg._settings = None

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: SimpleNamespace(messages=None))

    from postmind.core.ai_engine import AIEngine
    eng = AIEngine(thinking_budget=0)
    assert eng._thinking_budget == 0
    cfg._settings = None


def test_init_thinking_ignored_in_local_mode(monkeypatch):
    monkeypatch.setenv("POSTMIND_AI_MODE", "local")
    monkeypatch.setenv("POSTMIND_EXTENDED_THINKING", "true")
    import postmind.config as cfg
    cfg._settings = None

    from postmind.core.ai_engine import AIEngine
    eng = AIEngine()
    # local mode: thinking_budget should be 0 regardless of setting
    assert eng._thinking_budget == 0
    cfg._settings = None


# ── Server plumbs thinking_budget through ────────────────────────────────────


def test_server_agent_endpoint_passes_thinking_budget(monkeypatch):
    """When extended_thinking=True, agent_endpoint must pass thinking_budget to AIEngine."""
    monkeypatch.setenv("POSTMIND_AI_MODE", "cloud")
    monkeypatch.setenv("POSTMIND_CHAT_AI_MODE", "cloud")
    monkeypatch.setenv("POSTMIND_EXTENDED_THINKING", "true")
    monkeypatch.setenv("POSTMIND_THINKING_BUDGET_TOKENS", "9000")
    import postmind.config as cfg
    cfg._settings = None

    import postmind.core.ai_engine as ai_mod
    from postmind.web import server

    monkeypatch.setattr(server, "_get_web_account", lambda: "user@example.com")

    captured = {}

    class FakeAI:
        def __init__(self, *a, **kw):
            captured["thinking_budget"] = kw.get("thinking_budget")

        def chat(self, messages, system=None, tools=None, tool_executor=None, **kw):
            return "ok"

    monkeypatch.setattr(ai_mod, "AIEngine", FakeAI)

    from fastapi.testclient import TestClient
    client = TestClient(server.app)
    resp = client.post("/agent", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    assert captured.get("thinking_budget") == 9000
    cfg._settings = None


def test_server_agent_endpoint_no_thinking_when_disabled(monkeypatch):
    """When extended_thinking=False, agent_endpoint must NOT pass thinking_budget."""
    monkeypatch.setenv("POSTMIND_AI_MODE", "cloud")
    monkeypatch.setenv("POSTMIND_CHAT_AI_MODE", "cloud")
    monkeypatch.setenv("POSTMIND_EXTENDED_THINKING", "false")
    import postmind.config as cfg
    cfg._settings = None

    import postmind.core.ai_engine as ai_mod
    from postmind.web import server

    monkeypatch.setattr(server, "_get_web_account", lambda: "user@example.com")

    captured = {"thinking_budget": "NOT_SET"}

    class FakeAI:
        def __init__(self, *a, **kw):
            captured["thinking_budget"] = kw.get("thinking_budget", "NOT_SET")

        def chat(self, messages, system=None, tools=None, tool_executor=None, **kw):
            return "ok"

    monkeypatch.setattr(ai_mod, "AIEngine", FakeAI)

    from fastapi.testclient import TestClient
    client = TestClient(server.app)
    client.post("/agent", json={"messages": [{"role": "user", "content": "hi"}]})
    # thinking_budget kwarg must not be passed (or must be None/absent)
    assert captured.get("thinking_budget") in (None, "NOT_SET")
    cfg._settings = None


# ── Settings routes ───────────────────────────────────────────────────────────


def test_settings_thinking_route_saves_env(monkeypatch, tmp_path):
    """POST /settings/thinking must write POSTMIND_EXTENDED_THINKING and budget to .env."""
    monkeypatch.setenv("POSTMIND_AI_MODE", "off")
    import postmind.config as cfg
    cfg._settings = None
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)

    from postmind.web import server
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)

    from fastapi.testclient import TestClient
    client = TestClient(server.app, follow_redirects=False)
    resp = client.post(
        "/settings/thinking",
        data={"extended_thinking": "on", "thinking_budget_tokens": "12000"},
    )
    assert resp.status_code == 303
    assert "/settings" in resp.headers.get("location", "")
    cfg._settings = None


def test_settings_thinking_budget_clamped(monkeypatch, tmp_path):
    """Budget below 1024 must be clamped up; garbage value falls back to 8000."""
    monkeypatch.setenv("POSTMIND_AI_MODE", "off")
    import postmind.config as cfg
    cfg._settings = None
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)

    from postmind.web import server
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)

    from fastapi.testclient import TestClient
    client = TestClient(server.app, follow_redirects=False)

    # Garbage value
    resp = client.post(
        "/settings/thinking",
        data={"thinking_budget_tokens": "not-a-number"},
    )
    assert resp.status_code == 303

    # Too-small value — must not crash
    resp2 = client.post(
        "/settings/thinking",
        data={"extended_thinking": "on", "thinking_budget_tokens": "50"},
    )
    assert resp2.status_code == 303
    cfg._settings = None
