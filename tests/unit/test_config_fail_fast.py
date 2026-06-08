"""Issue #589: backend config refuses to import without creds-bearing REDIS_URL."""

import importlib
import sys

import pytest


def _reload_config():
    sys.modules.pop("config", None)
    return importlib.import_module("config")


# Snapshot/restore sys.modules["config"] around each test. Without this,
# `test_config_accepts_url_with_credentials` reloads `config` against the
# current env (REDIS_URL via monkeypatch + SECRET_KEY setdefault'd by
# test_voice_auth.py) and leaves the new module in sys.modules. Subsequent
# tests that do a runtime `from config import SECRET_KEY, ALGORITHM` (e.g.
# routers/voice.py inside test_voice_auth.py) then read a SECRET_KEY that
# differs from the one captured at their module-collection time — JWT decode
# fails, the ownership-gate tests close 4001 instead of 4003/accept, and CI
# goes red only under pytest-randomly seeds that order test_config_fail_fast
# before test_voice_auth.
#
# Uses the project-standard snapshot/restore helper pair recognized by
# tests/lint_sys_modules.py — `_STUBBED_MODULE_NAMES` + `_restore_sys_modules`
# (precedent: tests/unit/test_telegram_webhook_backfill.py).
_STUBBED_MODULE_NAMES = ["config"]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


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


# ---------------------------------------------------------------------------
# Regression: urlparse-based check rejects URLs that the old `"@" in url`
# substring check let through (Issue #589 follow-up).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_url", [
    "redis://@redis:6379",        # empty user, empty pass
    "redis://user@redis:6379",    # user only, no password
    "redis://:@redis:6379",       # both empty
    "redis://:secret@redis:6379", # password only, no user
])
def test_config_rejects_malformed_credentials(monkeypatch, bad_url):
    monkeypatch.setenv("REDIS_URL", bad_url)
    with pytest.raises(RuntimeError, match="REDIS_URL must include credentials"):
        _reload_config()
