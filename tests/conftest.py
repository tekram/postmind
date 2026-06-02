"""Shared pytest fixtures for the postmind test suite.

``clean_db``  — provides an isolated in-memory SQLite database for every test
that touches the storage layer.  Using in-memory SQLite means:

* No file-system side effects — nothing written to ~/.postmind
* Each test starts with an empty schema — zero shared state between tests
* No dependency on cfg.DB_PATH or POSTMIND_DIR environment variables

Usage
-----
Add ``clean_db`` to any test function or class that calls
``get_session()``, ``BlocklistRepo``, ``EmailRepo``, etc.

To apply automatically to all tests in a module, add an autouse fixture
that depends on ``clean_db``::

    @pytest.fixture(autouse=True)
    def _use_clean_db(clean_db):
        pass
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    """
    Isolate the on-disk data directory (~/.postmind) for every test.

    The config module computes ``DATA_DIR`` and a family of derived path
    constants (ACCOUNTS_DIR, ACTIVE_ACCOUNT_PATH, TOKENS_DIR, …) at import
    time.  Without isolation, ``get_active_account()`` and
    ``load_account_config()`` read the developer's real ~/.postmind, so a
    persisted IMAP account leaks into tests (and tests that write config
    pollute the real directory). Repoint every path constant at a fresh
    per-test temp dir so tests neither read nor write the real directory.
    """
    import postmind.config as config

    data_dir = tmp_path / "postmind_home"
    data_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("POSTMIND_DIR", str(data_dir))
    monkeypatch.setattr(config, "DATA_DIR", data_dir, raising=False)
    monkeypatch.setattr(config, "DB_PATH", data_dir / "postmind.db", raising=False)
    monkeypatch.setattr(config, "CREDENTIALS_PATH", data_dir / "credentials.json", raising=False)
    monkeypatch.setattr(config, "TOKEN_PATH", data_dir / "token.json", raising=False)
    monkeypatch.setattr(config, "UNDO_LOG_DIR", data_dir / "undo_logs", raising=False)
    monkeypatch.setattr(config, "TOKENS_DIR", data_dir / "tokens", raising=False)
    monkeypatch.setattr(config, "ACCOUNTS_DIR", data_dir / "accounts", raising=False)
    monkeypatch.setattr(config, "ACTIVE_ACCOUNT_PATH", data_dir / "active_account", raising=False)
    yield


@pytest.fixture(autouse=True)
def _reset_settings(monkeypatch):
    """
    Prevent tests from reading the real ~/.postmind/.env.

    pydantic-settings gives env vars higher priority than .env file values,
    so setting POSTMIND_PROVIDER=gmail here ensures tests always start with
    the default (Gmail) configuration regardless of what's persisted locally.
    Resets the settings cache before and after each test.
    """
    import postmind.config as config

    config._settings = None
    # Set explicit values (not just delete) so pydantic-settings env var precedence
    # wins over any values in the user's real ~/.postmind/.env file.
    monkeypatch.setenv("POSTMIND_PROVIDER", "gmail")
    monkeypatch.setenv("POSTMIND_IMAP_SERVER", "")
    monkeypatch.setenv("POSTMIND_IMAP_USER", "")
    monkeypatch.setenv("POSTMIND_IMAP_PORT", "993")
    monkeypatch.setenv("POSTMIND_IMAP_FOLDER", "INBOX")
    yield
    config._settings = None


@pytest.fixture()
def clean_db(monkeypatch):
    """Inject a fresh in-memory SQLite engine into the storage module.

    Resets ``_engine`` and ``_SessionLocal`` to a brand-new in-memory
    database before the test and restores the originals (via monkeypatch)
    after it completes — regardless of whether the test passes or fails.
    """
    import postmind.core.storage as storage

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        echo=False,
    )
    storage.Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    monkeypatch.setattr(storage, "_engine", engine)
    monkeypatch.setattr(storage, "_SessionLocal", session_factory)

    yield engine

    engine.dispose()
