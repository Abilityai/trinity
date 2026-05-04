"""Issue #589: backend config refuses to import without creds-bearing REDIS_URL."""

import importlib
import sys

import pytest


def _reload_config():
    sys.modules.pop("config", None)
    return importlib.import_module("config")


def test_config_raises_when_redis_url_missing(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    with pytest.raises(RuntimeError, match="REDIS_URL must include credentials"):
        _reload_config()


def test_config_raises_when_redis_url_lacks_credentials(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379")  # no creds
    with pytest.raises(RuntimeError, match="REDIS_URL must include credentials"):
        _reload_config()


def test_config_accepts_url_with_credentials(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://backend:secret@redis:6379")
    cfg = _reload_config()
    assert cfg.REDIS_URL == "redis://backend:secret@redis:6379"
