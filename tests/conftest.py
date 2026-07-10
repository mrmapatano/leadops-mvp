from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Fresh isolated SQLite DB per test."""
    monkeypatch.setenv("LEADOPS_DB", str(tmp_path / "test.db"))
    from leadops.db import init_db
    init_db(reset=True)
    return tmp_path


@pytest.fixture()
def sample_dir():
    return ROOT / "sample_data"


@pytest.fixture()
def loaded(db, sample_dir):
    """A fully imported pipeline: suppression -> contacts -> ai messages."""
    from leadops.importers import import_ai_messages, import_contacts, import_suppression
    import_suppression(sample_dir / "suppression_sample.csv")
    import_contacts(sample_dir / "contacts_sample.csv")
    import_ai_messages(sample_dir / "smart_drop_output_sample.csv")
    return db


@pytest.fixture()
def client(db, monkeypatch):
    """Flask test client on the isolated DB (CSRF active)."""
    import importlib
    import app as app_module
    importlib.reload(app_module)
    flask_app = app_module.create_app()
    flask_app.config.update(TESTING=True)
    return flask_app.test_client()
