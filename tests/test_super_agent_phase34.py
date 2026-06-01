"""Phase 3 + 4 Super Agent: local Ollama tool-use, never-open detection,
floating→agent handoff, and opt-in autopilot for reversible actions."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import postmind.config as cfg
import postmind.core.ai_engine as ae
import postmind.core.storage as st
import postmind.web.server as s
from postmind.core.agent_tools import find_unopened_subscriptions
from postmind.core.sender_stats import SenderGroup
from postmind.core.storage import Base, EmailRecord


def _grp(email, name, count, size):
    return SenderGroup(
        sender_email=email, sender_name=name, count=count, total_size_bytes=size,
        earliest_date=datetime(2023, 1, 1, tzinfo=timezone.utc),
        latest_date=datetime(2024, 6, 1, tzinfo=timezone.utc),
        sample_subjects=["x"], message_ids=[f"m{i}" for i in range(count)],
        has_unsubscribe=True, impact_score=10,
    )


# ── Phase 3a: local Ollama tool-use loop ──────────────────────────────────────

def test_local_tool_use_loop_runs_tool_then_returns_text():
    eng = ae.AIEngine.__new__(ae.AIEngine)
    eng._mode = "local"
    eng._ollama_url = "http://x"
    eng._ollama_model = "qwen2.5:32b"

    r1 = MagicMock(raise_for_status=lambda: None)
    r1.json = lambda: {"message": {"content": "", "tool_calls": [{"function": {"name": "get_inbox_overview", "arguments": "{}"}}]}}
    r2 = MagicMock(raise_for_status=lambda: None)
    r2.json = lambda: {"message": {"content": "You have 2 senders."}}
    seq = [r1, r2]
    invoked = []

    def fake_post(url, **k):
        return seq.pop(0)

    def exec_tool(name, inp):
        invoked.append(name)
        return "overview text"

    tools = [{"name": "get_inbox_overview", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    with patch("httpx.post", fake_post):
        out = eng._chat_local_tools([{"role": "user", "content": "hi"}], "sys", tools, exec_tool, 512, 6)
    assert out == "You have 2 senders."
    assert invoked == ["get_inbox_overview"]


def test_local_mode_falls_back_to_conversation_on_tool_error():
    eng = ae.AIEngine.__new__(ae.AIEngine)
    eng._mode = "local"
    eng._ollama_url = "http://x"
    eng._ollama_model = "m"
    eng._max_batch = 20
    with patch.object(ae.AIEngine, "_chat_local_tools", side_effect=RuntimeError("no tools")), \
         patch.object(ae.AIEngine, "_complete", return_value="plain answer") as comp:
        out = eng.chat([{"role": "user", "content": "hi"}], "sys",
                       tools=[{"name": "t", "input_schema": {}}], tool_executor=lambda *a: "x")
    assert out == "plain answer"
    assert comp.called


# ── Phase 3b: never-open detection ────────────────────────────────────────────

def test_find_unopened_subscriptions():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    sess = sessionmaker(bind=eng)()
    for i in range(10):  # 90% unread newsletter w/ unsubscribe header
        sess.add(EmailRecord(account_email="me@x.com", gmail_id=f"n{i}", thread_id=f"t{i}",
                             sender_email="news@promo.com", list_unsubscribe="<http://u>",
                             is_unread=(i < 9), is_inbox=True))
    for i in range(4):  # read, no unsubscribe — must be ignored
        sess.add(EmailRecord(account_email="me@x.com", gmail_id=f"r{i}", thread_id=f"rt{i}",
                             sender_email="boss@work.com", list_unsubscribe="",
                             is_unread=False, is_inbox=True))
    sess.commit()
    rows = find_unopened_subscriptions(sess, "me@x.com", 3, 10)
    assert len(rows) == 1
    assert rows[0]["sender_email"] == "news@promo.com"
    assert rows[0]["unread_pct"] == 90


# ── Phase 4a: floating chat handoff to /agent ─────────────────────────────────

def test_floating_chat_can_navigate_to_agent():
    nav = next(t for t in s._CHAT_TOOLS if t["name"] == "navigate")
    assert "/agent" in nav["input_schema"]["properties"]["page"]["enum"]
    assert "/agent" in s._PAGES


# ── Phase 4b: opt-in autopilot ────────────────────────────────────────────────

def test_autopilot_excludes_destructive_actions():
    assert s._AUTOPILOT_ACTIONS == ("archive", "label", "mark_read")


def _setup_agent(monkeypatch):
    """Wire up a cloud agent with stub provider/blocklist via monkeypatch so all
    global patches auto-restore after the test (no cross-test pollution)."""
    monkeypatch.setattr(s, "_get_web_account", lambda: "me@example.com")
    s._scan_cache["me@example.com"] = {
        "groups": [_grp("news@promo.com", "Promo", 12, 2048)],
        "profile": {}, "account_email": "me@example.com",
        "scanned_at": "now", "expires": time.time() + 300,
    }
    monkeypatch.setattr(s, "_chat_mode", lambda: "cloud")
    monkeypatch.setattr(s, "_chat_engine_kwargs", lambda: {"mode": "cloud", "cloud_model": "m", "ollama_model": "o"})

    class FakeBlock:
        def __init__(self, *a, **k):
            pass

        def blocked_emails(self, a):
            return set()

    monkeypatch.setattr(st, "BlocklistRepo", FakeBlock)
    monkeypatch.setattr(s, "_build_provider", lambda: type("P", (), {"supports": lambda self, x: True})())


def test_autopilot_on_archives_without_card(monkeypatch):
    _setup_agent(monkeypatch)
    monkeypatch.setattr(s, "_autopilot_on", lambda: True)
    ran = []
    monkeypatch.setattr(s, "_execute_reversible_action",
                        lambda acct, action, staged, label="": (ran.append(action), (7, 12))[1])

    class AIon:
        def __init__(self, *a, **k):
            pass

        def chat(self, m, system, tools=None, tool_executor=None, **k):
            assert "AUTOPILOT is currently ON" in system
            assert "Autopilot" in tool_executor("stage_archive", {"query": "promo"})
            return "done"

    monkeypatch.setattr(ae, "AIEngine", AIon)
    d = TestClient(s.app).post("/agent", json={"messages": [{"role": "user", "content": "archive promo"}]}).json()
    assert ran == ["archive"]
    assert any(a["href"] == "/undo" for a in d["actions"])
    assert not d["cards"]  # auto-executed, no confirm card


def test_autopilot_off_stages_card(monkeypatch):
    _setup_agent(monkeypatch)
    monkeypatch.setattr(s, "_autopilot_on", lambda: False)
    ran = []
    monkeypatch.setattr(s, "_execute_reversible_action", lambda *a, **k: (ran.append(1), (1, 1))[1])

    class AIoff:
        def __init__(self, *a, **k):
            pass

        def chat(self, m, system, tools=None, tool_executor=None, **k):
            assert "AUTOPILOT is currently OFF" in system
            return tool_executor("stage_archive", {"query": "promo"})

    monkeypatch.setattr(ae, "AIEngine", AIoff)
    d = TestClient(s.app).post("/agent", json={"messages": [{"role": "user", "content": "archive promo"}]}).json()
    assert ran == []  # nothing executed
    assert any(c["type"] == "bulk_action" for c in d["cards"])


def test_autopilot_holds_sensitive_senders_for_confirmation(monkeypatch):
    """Even with autopilot on, bank/legal/health senders must NOT auto-execute —
    they get routed to a confirm card."""
    monkeypatch.setattr(s, "_get_web_account", lambda: "me@example.com")
    s._scan_cache["me@example.com"] = {
        "groups": [_grp("news@promo.com", "Promo", 12, 2048), _grp("alerts@chase.com", "Chase", 5, 1024)],
        "profile": {}, "account_email": "me@example.com", "scanned_at": "now", "expires": time.time() + 300,
    }
    monkeypatch.setattr(s, "_chat_mode", lambda: "cloud")
    monkeypatch.setattr(s, "_chat_engine_kwargs", lambda: {"mode": "cloud", "cloud_model": "m", "ollama_model": "o"})
    monkeypatch.setattr(s, "_autopilot_on", lambda: True)

    class FakeBlock:
        def __init__(self, *a, **k):
            pass

        def blocked_emails(self, a):
            return set()

    monkeypatch.setattr(st, "BlocklistRepo", FakeBlock)
    monkeypatch.setattr(s, "_build_provider", lambda: type("P", (), {"supports": lambda self, x: True})())

    archived = []
    monkeypatch.setattr(s, "_execute_reversible_action",
                        lambda acct, action, staged, label="": (archived.extend(g.sender_email for g in staged), (1, 12))[1])

    class AI:
        def __init__(self, *a, **k):
            pass

        def chat(self, m, system, tools=None, tool_executor=None, **k):
            return tool_executor("stage_archive", {"query": "@"})  # matches both senders

    monkeypatch.setattr(ae, "AIEngine", AI)
    d = TestClient(s.app).post("/agent", json={"messages": [{"role": "user", "content": "archive all"}]}).json()
    # promo auto-archived; chase (sensitive) held for a confirm card
    assert archived == ["news@promo.com"], archived
    assert any(c["type"] == "bulk_action" for c in d["cards"])
    assert "alerts@chase.com" in str(d["cards"])


def test_local_mode_allowed_for_agent():
    assert s._agent_mode_guidance("local") is None
    assert s._agent_mode_guidance("cloud") is None
    assert s._agent_mode_guidance("off") is not None
