"""Tests for _mcp_guidance_for and its integration into _build_agent_system."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _save_mcp_servers(servers: list[dict]) -> None:
    """Write account config with mcp_servers to the isolated tmp ACCOUNTS_DIR."""
    from postmind.config import save_account_config

    save_account_config("test@x.com", {"provider": "gmail", "mcp_servers": servers})


# ---------------------------------------------------------------------------
# _mcp_guidance_for tests
# ---------------------------------------------------------------------------


def test_no_mcp_servers_returns_empty_guidance():
    """With no mcp_servers in account config, guidance is empty."""
    from postmind.web.server import _mcp_guidance_for

    result = _mcp_guidance_for("test@x.com")
    assert result == ""


def test_memory_server_adds_guidance():
    """memory server produces guidance mentioning mcp_memory_search_nodes."""
    _save_mcp_servers(
        [
            {
                "name": "memory",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-memory"],
            }
        ]
    )

    from postmind.web.server import _mcp_guidance_for

    result = _mcp_guidance_for("test@x.com")
    assert "mcp_memory_search_nodes" in result
    assert "BEFORE composing" in result


def test_linear_server_adds_guidance():
    """linear server produces guidance mentioning mcp_linear and create_issue."""
    _save_mcp_servers([{"name": "linear", "command": "npx", "args": ["-y", "@linear/mcp"]}])

    from postmind.web.server import _mcp_guidance_for

    result = _mcp_guidance_for("test@x.com")
    assert "mcp_linear" in result
    assert "create_issue" in result


def test_multiple_servers_all_included():
    """memory + linear + brave-search all get their own guidance blocks."""
    _save_mcp_servers(
        [
            {"name": "memory"},
            {"name": "linear"},
            {"name": "brave-search"},
        ]
    )

    from postmind.web.server import _mcp_guidance_for

    result = _mcp_guidance_for("test@x.com")
    assert "mcp_memory_search_nodes" in result
    assert "mcp_linear" in result
    assert "mcp_brave-search_" in result


def test_unknown_server_gets_generic_guidance():
    """A server name not in the known list gets a generic guidance block."""
    _save_mcp_servers([{"name": "mytools"}])

    from postmind.web.server import _mcp_guidance_for

    result = _mcp_guidance_for("test@x.com")
    assert "mcp_mytools_*" in result


def test_empty_account_email_returns_empty():
    """Empty account email returns empty string immediately."""
    from postmind.web.server import _mcp_guidance_for

    assert _mcp_guidance_for("") == ""


def test_exception_in_config_returns_empty(monkeypatch):
    """Any exception from load_account_config is swallowed and returns empty."""
    import postmind.config as config

    monkeypatch.setattr(
        config,
        "load_account_config",
        lambda _email: (_ for _ in ()).throw(RuntimeError("disk error")),
    )

    from postmind.web.server import _mcp_guidance_for

    result = _mcp_guidance_for("test@x.com")
    assert result == ""


# ---------------------------------------------------------------------------
# _build_agent_system integration
# ---------------------------------------------------------------------------


def test_system_prompt_includes_mcp_guidance():
    """When memory server is configured, system prompt contains memory guidance."""
    _save_mcp_servers([{"name": "memory"}])

    from postmind.web.server import _build_agent_system

    result = _build_agent_system("test@x.com", "cloud")
    assert "mcp_memory_search_nodes" in result


def test_system_prompt_no_mcp_when_not_configured():
    """Without mcp_servers, system prompt has no MCP guidance block."""
    from postmind.web.server import _build_agent_system

    result = _build_agent_system("test@x.com", "cloud")
    assert "Connected external tools" not in result


# ---------------------------------------------------------------------------
# agent_page context test
# ---------------------------------------------------------------------------


def test_agent_page_passes_mcp_names(monkeypatch):
    """GET /agent response contains the MCP server name pill in HTML."""
    from fastapi.testclient import TestClient

    _save_mcp_servers([{"name": "memory"}])

    # Patch _get_web_account to return our test account
    import postmind.web.server as srv

    monkeypatch.setattr(srv, "_get_web_account", lambda: "test@x.com")

    client = TestClient(srv.app, raise_server_exceptions=True)
    resp = client.get("/agent")
    assert resp.status_code == 200
    assert "memory" in resp.text
