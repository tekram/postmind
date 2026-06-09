"""Tests verifying that _build_agent_tool_executor delegates READ tools to AgentService.

This ensures AgentService is the single source of truth for READ tool logic and
that the executor properly wires cache, provider, and AI instance through to it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture()
def shared_db(monkeypatch):
    import postmind.core.storage as storage

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    storage.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(storage, "_engine", engine)
    monkeypatch.setattr(storage, "_SessionLocal", factory)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def _ai_mode_off(monkeypatch):
    monkeypatch.setenv("POSTMIND_AI_MODE", "off")
    monkeypatch.setenv("POSTMIND_CHAT_AI_MODE", "off")
    import postmind.config as config

    config._settings = None
    yield
    config._settings = None


def _make_executor(monkeypatch, account="user@example.com"):
    """Build an executor closure with a no-op provider and no real AI."""
    from postmind.web import server

    monkeypatch.setattr(server, "_build_provider", lambda: MagicMock())
    monkeypatch.setattr(server, "_cache_get", lambda: None)
    ai = MagicMock()
    return server._build_agent_tool_executor(account, ai, [], []), ai


def test_read_tools_use_agentservice(shared_db, monkeypatch):
    """get_inbox_overview delegates to AgentService.inbox_overview."""
    from postmind.core.agent_service import AgentService
    from postmind.web import server

    account = "user@example.com"
    monkeypatch.setattr(server, "_build_provider", lambda: MagicMock())
    monkeypatch.setattr(server, "_cache_get", lambda: None)

    with patch.object(AgentService, "inbox_overview", return_value="mocked overview") as mock_method:
        executor = server._build_agent_tool_executor(account, MagicMock(), [], [])
        result = executor("get_inbox_overview", {})

    assert result == "mocked overview"
    mock_method.assert_called_once()


def test_analyze_storage_uses_cache(shared_db, monkeypatch):
    """analyze_storage passes web cache groups into svc._groups_cache before calling."""
    from postmind.core.agent_service import AgentService
    from postmind.web import server

    account = "user@example.com"
    fake_groups = [MagicMock(), MagicMock()]
    monkeypatch.setattr(server, "_build_provider", lambda: MagicMock())
    monkeypatch.setattr(server, "_cache_get", lambda: {"groups": fake_groups})

    captured = {}

    def _capture_analyze(self, group_by="sender", top_n=10):
        captured["groups_cache"] = self._groups_cache
        return "storage summary"

    with patch.object(AgentService, "analyze_storage", _capture_analyze):
        executor = server._build_agent_tool_executor(account, MagicMock(), [], [])
        result = executor("analyze_storage", {"group_by": "sender", "top_n": 5})

    assert result == "storage summary"
    assert captured["groups_cache"] is fake_groups


def test_svc_gets_ai_instance(shared_db, monkeypatch):
    """AgentService is constructed with the same ai= instance as the executor."""
    from postmind.core.agent_service import AgentService
    from postmind.web import server

    account = "user@example.com"
    my_ai = MagicMock(name="my_ai")
    monkeypatch.setattr(server, "_build_provider", lambda: MagicMock())
    monkeypatch.setattr(server, "_cache_get", lambda: None)

    constructed_ais = []
    original_init = AgentService.__init__

    def _capture_init(self, account_email=None, ai=None):
        constructed_ais.append(ai)
        original_init(self, account_email=account_email, ai=ai)

    with patch.object(AgentService, "__init__", _capture_init):
        with patch.object(AgentService, "inbox_overview", return_value="ok"):
            executor = server._build_agent_tool_executor(account, my_ai, [], [])
            executor("get_inbox_overview", {})

    assert constructed_ais, "AgentService was never constructed"
    assert constructed_ais[0] is my_ai


def test_svc_gets_provider_from_build(shared_db, monkeypatch):
    """_build_provider result is set on svc._provider before any tool runs."""
    from postmind.core.agent_service import AgentService
    from postmind.web import server

    account = "user@example.com"
    fake_provider = MagicMock(name="fake_provider")
    monkeypatch.setattr(server, "_build_provider", lambda: fake_provider)
    monkeypatch.setattr(server, "_cache_get", lambda: None)

    captured = {}

    def _capture_overview(self):
        captured["provider"] = self._provider
        return "overview"

    with patch.object(AgentService, "inbox_overview", _capture_overview):
        executor = server._build_agent_tool_executor(account, MagicMock(), [], [])
        executor("get_inbox_overview", {})

    assert captured.get("provider") is fake_provider
