"""Offline test defaults.

A real run caches decisions on disk and can write evidence. Tests must do neither: one
test's cached answer could satisfy the next test's request, and evidence files would leak
between runs. Keys are cleared so a developer's .env can never turn an offline test into a
paid call.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _offline_defaults(monkeypatch):
    monkeypatch.setenv("JEV_CACHE", "0")
    monkeypatch.setenv("JEV_MODES", "off")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("ESCALATION_MODEL_API_KEY", raising=False)
    # The recorded flights task date must stay pinned in tests, whatever the shell has.
    monkeypatch.delenv("JEV_FLIGHTS_DATE", raising=False)


@pytest.fixture(scope="session", autouse=True)
def _offline_modes_root(tmp_path_factory):
    """A shell JEV_MODES_DIR must never point tests at the user's real mode memory."""
    os.environ.setdefault("JEV_MODES_DIR", str(tmp_path_factory.mktemp("modes")))
    yield
