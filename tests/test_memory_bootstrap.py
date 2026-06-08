"""Tests for automatic memory server provisioning.

Covers:
- ensure_memory_server: returns None when npx is unavailable, correct dict when available
- bootstrap_memory_for_account: writes config on first call, no-ops on repeat
- GET /agent/memory endpoint: no-account path, empty-memory path
"""

import json

# ── ensure_memory_server ──────────────────────────────────────────────────────


def test_ensure_memory_server_returns_none_when_no_npx(monkeypatch):
    import shutil

    import postmind.core.mcp_client as mcp_client

    monkeypatch.setattr(shutil, "which", lambda _: None)
    result = mcp_client.ensure_memory_server("test@x.com")
    assert result is None


def test_ensure_memory_server_returns_config_when_npx_available(monkeypatch, tmp_path):
    import shutil

    import postmind.config as config
    import postmind.core.mcp_client as mcp_client

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/npx")

    # Point MEMORY_DIR at tmp_path so memory_dir_for uses tmp_path
    memory_dir = tmp_path / "memory"
    monkeypatch.setattr(config, "MEMORY_DIR", memory_dir)

    result = mcp_client.ensure_memory_server("test@x.com")

    assert result is not None
    assert result["name"] == "memory"
    assert result["command"] == "npx"
    assert "-y" in result["args"]
    assert "@modelcontextprotocol/server-memory" in result["args"]
    assert "MEMORY_FILE_PATH" in result["env"]
    assert "memory.json" in result["env"]["MEMORY_FILE_PATH"]


# ── bootstrap_memory_for_account ─────────────────────────────────────────────


def test_bootstrap_memory_adds_to_config(monkeypatch, tmp_path):
    import shutil

    import postmind.config as config
    import postmind.core.mcp_client as mcp_client

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/npx")

    # Redirect data dirs so no real ~/.postmind is touched
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir(parents=True)
    memory_dir = tmp_path / "memory"
    monkeypatch.setattr(config, "ACCOUNTS_DIR", accounts_dir)
    monkeypatch.setattr(config, "MEMORY_DIR", memory_dir)

    result = mcp_client.bootstrap_memory_for_account("test@x.com")

    assert result is True

    # Verify the config file was written with the memory server entry
    # save_account_config uses email.lower().replace("/", "_") — "@" is NOT replaced
    safe = "test@x.com"
    cfg_file = accounts_dir / f"{safe}.json"
    assert cfg_file.exists()
    cfg = json.loads(cfg_file.read_text())
    servers = cfg.get("mcp_servers", [])
    assert len(servers) == 1
    assert servers[0]["name"] == "memory"
    assert servers[0]["command"] == "npx"


def test_bootstrap_memory_skips_if_already_present(monkeypatch, tmp_path):
    import shutil

    import postmind.config as config
    import postmind.core.mcp_client as mcp_client

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/npx")

    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir(parents=True)
    memory_dir = tmp_path / "memory"
    monkeypatch.setattr(config, "ACCOUNTS_DIR", accounts_dir)
    monkeypatch.setattr(config, "MEMORY_DIR", memory_dir)

    # Pre-populate config with a memory server entry
    # save_account_config uses email.lower().replace("/", "_") — "@" is NOT replaced
    safe = "test@x.com"
    cfg_file = accounts_dir / f"{safe}.json"
    cfg_file.write_text(
        json.dumps(
            {
                "provider": "gmail",
                "mcp_servers": [
                    {
                        "name": "memory",
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-memory"],
                        "env": {"MEMORY_FILE_PATH": "/some/path/memory.json"},
                    }
                ],
            }
        )
    )

    result = mcp_client.bootstrap_memory_for_account("test@x.com")

    assert result is False

    # Verify only one entry exists — no duplicates
    cfg = json.loads(cfg_file.read_text())
    servers = cfg.get("mcp_servers", [])
    assert len(servers) == 1


# ── GET /agent/memory endpoint ────────────────────────────────────────────────


def test_memory_endpoint_no_account(monkeypatch):
    """When no account is active, the endpoint returns configured=False."""
    from fastapi.testclient import TestClient

    from postmind.web.server import app

    monkeypatch.setattr("postmind.web.server._get_web_account", lambda: "")

    client = TestClient(app, raise_server_exceptions=True)
    resp = client.get("/agent/memory")
    assert resp.status_code == 200
    data = resp.json()
    assert data["configured"] is False
    assert data["entity_count"] == 0
