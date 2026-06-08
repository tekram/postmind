"""Tests for the MCP template gallery (Quick Connect) feature.

Covers:
- GET /settings/mcp-templates returns all 6 templates with required fields
- POST /settings/mcp-servers/from-template/<id> adds a server entry to account config
- Idempotent add returns already_configured flag
- Unknown template returns 404
- Configured templates are flagged in the GET response
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    """TestClient with a fixed active account."""
    from postmind.web import server

    account = "test@example.com"
    monkeypatch.setattr(server, "_get_web_account", lambda: account)
    return TestClient(server.app, raise_server_exceptions=True)


# ── helpers ───────────────────────────────────────────────────────────────────


def _account_config(tmp_path, monkeypatch, account="test@example.com"):
    """Wire account config to a temp directory and return (accounts_dir, account)."""
    import postmind.config as config

    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "ACCOUNTS_DIR", accounts_dir, raising=False)
    return accounts_dir, account


# ── tests ─────────────────────────────────────────────────────────────────────


def test_templates_endpoint_returns_6(client):
    """GET /settings/mcp-templates returns exactly 6 templates with required fields."""
    r = client.get("/settings/mcp-templates")
    assert r.status_code == 200
    data = r.json()
    templates = data["templates"]
    assert len(templates) == 6
    for t in templates:
        assert "id" in t
        assert "name" in t
        assert "tagline" in t
        assert "configured" in t


def test_add_memory_template_adds_to_config(monkeypatch, tmp_path):
    """POST from-template/memory writes an mcp_servers entry to account config."""
    from postmind.web import server

    accounts_dir, account = _account_config(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_get_web_account", lambda: account)

    c = TestClient(server.app, raise_server_exceptions=True)
    r = c.post("/settings/mcp-servers/from-template/memory")
    assert r.status_code == 200
    data = r.json()
    assert data.get("ok") is True
    assert "added" in data

    # Verify persisted config
    from postmind.config import load_account_config

    cfg = load_account_config(account)
    servers = cfg.get("mcp_servers") or []
    assert any(s["name"] == "memory" for s in servers)
    entry = next(s for s in servers if s["name"] == "memory")
    assert entry["command"] == "npx"
    assert "-y" in entry["args"]


def test_add_template_already_configured_returns_flag(monkeypatch, tmp_path):
    """Adding the same template twice returns already_configured: True."""
    from postmind.web import server

    accounts_dir, account = _account_config(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_get_web_account", lambda: account)

    c = TestClient(server.app, raise_server_exceptions=True)
    c.post("/settings/mcp-servers/from-template/memory")
    r2 = c.post("/settings/mcp-servers/from-template/memory")
    assert r2.status_code == 200
    assert r2.json().get("already_configured") is True


def test_add_unknown_template_returns_404(monkeypatch, tmp_path):
    """POST from-template/doesnotexist returns 404."""
    from postmind.web import server

    accounts_dir, account = _account_config(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_get_web_account", lambda: account)

    c = TestClient(server.app, raise_server_exceptions=True)
    r = c.post("/settings/mcp-servers/from-template/doesnotexist")
    assert r.status_code == 404


def test_templates_show_configured_flag(monkeypatch, tmp_path):
    """After adding 'linear' to the account config, GET templates marks it configured."""
    from postmind.config import load_account_config, save_account_config
    from postmind.web import server

    accounts_dir, account = _account_config(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_get_web_account", lambda: account)

    # Manually configure the linear server
    cfg = load_account_config(account)
    cfg["mcp_servers"] = [{"name": "linear", "url": "https://mcp.linear.app/mcp"}]
    save_account_config(account, cfg)

    c = TestClient(server.app, raise_server_exceptions=True)
    r = c.get("/settings/mcp-templates")
    assert r.status_code == 200
    templates = r.json()["templates"]

    linear = next(t for t in templates if t["id"] == "linear")
    assert linear["configured"] is True

    # Others should not be configured
    memory = next(t for t in templates if t["id"] == "memory")
    assert memory["configured"] is False
