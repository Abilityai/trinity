"""#3325 — an unstorable credential VALUE is a named 422, never "add the key".

``encrypt_secret_setting`` used to wrap the whole ``encrypt()`` call in
``except ValueError`` → ``MissingEncryptionKeyError``. A lone UTF-16 surrogate
in the value makes the UTF-8 encode raise ``UnicodeEncodeError`` (a
``ValueError``), so a configured install was told to add
``CREDENTIAL_ENCRYPTION_KEY`` and the settings routes answered 500.

Pins:
* the value error is ``SecretSettingValueError`` and carries the key name;
* the secret reaches neither the message nor the exception chain
  (``UnicodeEncodeError.object`` holds the WHOLE value);
* a missing key is still reported first, as ``MissingEncryptionKeyError``;
* ``PUT /api/settings/api-keys/anthropic`` (and the Slack routes) answer 422
  with a value-free body and log nothing carrying the value;
* ``main.py`` registers the handler, so the router-only harness below cannot
  pass while production lacks it.

No DB writes reach a row (the write is refused before it), no network, no Docker.
"""
from __future__ import annotations

import ast
import json
import logging
import os
import traceback
import types
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("cryptography")

os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", "ab" * 32)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.settings as sr  # noqa: E402
from dependencies import get_current_user  # noqa: E402
from error_handlers import secret_setting_value_error  # noqa: E402
from services.secret_settings import (  # noqa: E402
    MissingEncryptionKeyError,
    SecretSettingValueError,
    encrypt_secret_setting,
)

pytestmark = pytest.mark.unit

_MAIN = Path(__file__).resolve().parents[2] / "src" / "backend" / "main.py"
_SURROGATE = "\ud800"
_PREFIX = "sk-ant-api03-"
_SECRET = _PREFIX + "synthetic" + _SURROGATE


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "ab" * 32)
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY_SECONDARY", raising=False)


def _chain(exc):
    """Every exception reachable from ``exc`` the way a traceback walks it."""
    seen, todo = [], [exc]
    while todo:
        e = todo.pop()
        if e is None or e in seen:
            continue
        seen.append(e)
        todo += [e.__cause__, e.__context__]
    return seen


def _leaks(text: str) -> bool:
    return _SURROGATE in text or "synthetic" in text or _PREFIX in text


class TestService:
    def test_unencodable_value_is_a_named_value_error(self, key):
        with pytest.raises(SecretSettingValueError) as exc:
            encrypt_secret_setting("anthropic_api_key", _SECRET)
        err = exc.value
        assert isinstance(err, ValueError)                 # existing `except ValueError` still catch it
        assert not isinstance(err, MissingEncryptionKeyError)
        assert err.key == "anthropic_api_key"
        assert "anthropic_api_key" in str(err)
        assert "CREDENTIAL_ENCRYPTION_KEY" not in str(err)

    def test_the_value_reaches_neither_the_message_nor_the_chain(self, key):
        with pytest.raises(SecretSettingValueError) as exc:
            encrypt_secret_setting("anthropic_api_key", _SECRET)
        err = exc.value
        assert err.__cause__ is None and err.__context__ is None
        # Nothing a traceback would render carries the secret.
        for e in _chain(err):
            assert not _leaks(str(e))
            assert not any(isinstance(a, str) and _leaks(a) for a in e.args)
            assert not isinstance(e, UnicodeEncodeError)
        rendered = "".join(traceback.format_exception(type(err), err, err.__traceback__))
        assert not _leaks(rendered)

    def test_a_missing_key_is_reported_before_a_bad_value(self, monkeypatch):
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
        monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY_SECONDARY", raising=False)
        with pytest.raises(MissingEncryptionKeyError) as exc:
            encrypt_secret_setting("anthropic_api_key", _SECRET)
        assert not isinstance(exc.value, SecretSettingValueError)
        assert "CREDENTIAL_ENCRYPTION_KEY" in str(exc.value)


def _client():
    app = FastAPI()
    app.include_router(sr.router)
    # Registered exactly as main.py does — pinned by the AST test below.
    app.add_exception_handler(SecretSettingValueError, secret_setting_value_error)
    app.dependency_overrides[get_current_user] = lambda: types.SimpleNamespace(
        id=1, username="admin", role="admin", email="admin@example.com",
        agent_name=None, connector_agent=None, mcp_scope=None,
    )
    return TestClient(app, raise_server_exceptions=True)


def _put(client, method, path, body):
    # json.dumps escapes the surrogate as \ud800 — the shape a browser sends.
    return client.request(method, path, content=json.dumps(body),
                          headers={"Content-Type": "application/json"})


@pytest.mark.parametrize("method, path, body, setting", [
    pytest.param("PUT", "/api/settings/api-keys/anthropic", {"api_key": _SECRET},
                 "anthropic_api_key", id="anthropic"),
    pytest.param("PUT", "/api/settings/slack", {"client_secret": _SECRET},
                 "slack_client_secret", id="slack-oauth"),
    pytest.param("POST", "/api/settings/slack/connect", {"app_token": _SECRET},
                 "slack_app_token", id="slack-connect-no-try"),
])
def test_route_answers_a_value_free_422(key, caplog, method, path, body, setting):
    caplog.set_level(logging.DEBUG)
    r = _put(_client(), method, path, body)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert setting in detail
    assert "CREDENTIAL_ENCRYPTION_KEY" not in detail
    assert not _leaks(r.text)
    for rec in caplog.records:
        assert not _leaks(rec.getMessage())
        if rec.exc_info:
            assert not _leaks("".join(traceback.format_exception(*rec.exc_info)))


def test_main_registers_the_handler():
    """The live consumer is the app's handler table (wiring, not behaviour)."""
    tree = ast.parse(_MAIN.read_text())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "add_exception_handler"]
    pairs = {(ast.unparse(c.args[0]), ast.unparse(c.args[1])) for c in calls if len(c.args) == 2}
    assert ("_SecretSettingValueError", "_secret_setting_value_error") in pairs
