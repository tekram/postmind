"""Tests for the web onboarding wizard: IMAP account support and the
"enable AI" step.

These exercise rendering of each wizard step and that the new routes are
registered. They use TestClient with redirects disabled so the POST routes'
303 advances can be asserted without following them into pages that need a
real provider/account.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from postmind.web.server import app


@pytest.fixture(autouse=True)
def _no_accounts(monkeypatch):
    """Force a fresh-install state (no connected accounts) so the wizard
    renders its choice/forms rather than the 'already connected' shortcut."""
    import postmind.core.account_registry as registry

    monkeypatch.setattr(registry, "list_accounts", lambda: [])


def _client():
    return TestClient(app, follow_redirects=False)


def test_routes_registered():
    paths = {r.path for r in app.routes}
    assert "/onboarding/connect/imap" in paths
    assert "/onboarding/ai-mode" in paths


def test_step1_gmail_and_imap_tabs():
    client = _client()
    # Gmail tab (default)
    r = client.get("/onboarding?step=1&tab=gmail")
    assert r.status_code == 200
    assert "Sign in with Google" in r.text or "Upload credentials" in r.text
    # both tab links are present so the user can choose
    assert "/onboarding?step=1&tab=imap" in r.text
    assert "/onboarding?step=1&tab=gmail" in r.text

    # IMAP tab shows the IMAP form posting to the onboarding connect route
    r = client.get("/onboarding?step=1&tab=imap")
    assert r.status_code == 200
    assert 'action="/onboarding/connect/imap"' in r.text
    for field in ("imap_server", "imap_user", "imap_password", "imap_port", "imap_folder", "display_name"):
        assert f'name="{field}"' in r.text


def test_step2_enable_ai_options_present():
    client = _client()
    r = client.get("/onboarding?step=2")
    assert r.status_code == 200
    assert 'action="/onboarding/ai-mode"' in r.text
    # off / local / cloud all selectable
    assert 'value="off"' in r.text
    assert 'value="local"' in r.text
    assert 'value="cloud"' in r.text
    # AI is presented as optional and skippable
    assert "optional" in r.text.lower()
    assert "Skip" in r.text
    # config inputs for local + cloud
    assert 'name="ollama_base_url"' in r.text
    assert 'name="ollama_model"' in r.text
    assert 'name="anthropic_api_key"' in r.text


def test_step3_done():
    client = _client()
    r = client.get("/onboarding?step=3")
    assert r.status_code == 200
    assert "all set" in r.text.lower()


def test_ai_mode_off_is_default():
    client = _client()
    r = client.get("/onboarding?step=2")
    assert r.status_code == 200
    # "off" radio is checked by default (conftest forces default settings)
    assert 'value="off"' in r.text and "checked" in r.text


def test_onboarding_ai_mode_persists_and_advances(tmp_path, monkeypatch):
    import postmind.config as config
    import postmind.web.server as server

    env_dir = tmp_path
    monkeypatch.setattr(server, "DATA_DIR", env_dir)
    config._settings = None

    client = _client()
    r = client.post("/onboarding/ai-mode", data={"mode": "off"})
    assert r.status_code == 303
    assert r.headers["location"] == "/onboarding?step=3"

    env_file = env_dir / ".env"
    assert env_file.exists()
    assert "POSTMIND_AI_MODE=off" in env_file.read_text()


def test_onboarding_ai_mode_cloud_writes_key(tmp_path, monkeypatch):
    import postmind.config as config
    import postmind.web.server as server

    env_dir = tmp_path
    monkeypatch.setattr(server, "DATA_DIR", env_dir)
    config._settings = None

    client = _client()
    r = client.post(
        "/onboarding/ai-mode",
        data={"mode": "cloud", "anthropic_api_key": "sk-ant-test123"},
    )
    assert r.status_code == 303
    text = (env_dir / ".env").read_text()
    assert "POSTMIND_AI_MODE=cloud" in text
    assert "ANTHROPIC_API_KEY=sk-ant-test123" in text


def test_onboarding_imap_validation_error_rerenders_step1(monkeypatch):
    client = _client()
    # Missing fields -> re-renders the wizard (step 1, imap tab) with an error,
    # 200 not a redirect.
    r = client.post("/onboarding/connect/imap", data={"imap_server": "", "imap_user": "", "imap_password": ""})
    assert r.status_code == 200
    assert "required" in r.text.lower()
    assert 'action="/onboarding/connect/imap"' in r.text
