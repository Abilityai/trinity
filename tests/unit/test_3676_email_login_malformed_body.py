"""#3676 — a malformed body to the email-login endpoints is a 422.

Missing field, non-object JSON and non-JSON bodies each reached the handler's
hand-rolled parse and escaped as a 500 with a traceback.
"""
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

REQUEST = "/api/auth/email/request"
VERIFY = "/api/auth/email/verify"


class _Reached(Exception):
    pass


@pytest.fixture
def ctx(monkeypatch):
    import routers.auth as ra
    from error_handlers import validation_error_without_input

    monkeypatch.setattr(ra, "is_setup_completed", lambda: True)
    monkeypatch.setattr(ra.db, "get_setting_value",
                        lambda k, d=None: "true" if k == "email_auth_enabled" else d)
    monkeypatch.setattr(ra.login_policy_gate, "email_code_allowed", lambda: True)

    def _never(*a, **k):
        raise _Reached()

    # A malformed body must stop before any lookup or per-account accounting.
    monkeypatch.setattr(ra.db, "is_email_whitelisted", _never)
    monkeypatch.setattr(ra, "check_otp_rate_limit", _never)
    recorded = []
    monkeypatch.setattr(ra, "check_login_rate_limit", lambda *a, **k: True)
    monkeypatch.setattr(ra, "record_login_attempt",
                        lambda ip, success, account=None: recorded.append((success, account)))

    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_error_without_input)
    app.include_router(ra.router)
    return TestClient(app, raise_server_exceptions=False), recorded


def _post(client, path, body):
    if isinstance(body, str):
        return client.post(path, content=body, headers={"Content-Type": "application/json"})
    return client.post(path, json=body)


@pytest.mark.parametrize("path,body", [
    (REQUEST, {}),
    (REQUEST, ["a@example.com"]),
    (REQUEST, "not json"),
    (VERIFY, {"email": "test@example.com"}),
    (VERIFY, {"code": "123456"}),
    (VERIFY, "null"),
    (VERIFY, "not json"),
])
def test_malformed_body_is_422(ctx, path, body):
    client, _ = ctx
    r = _post(client, path, body)
    assert r.status_code == 422, r.text


def test_request_422_is_identical_for_any_address(ctx):
    """#186: the refusal names the shape problem and never the address."""
    client, _ = ctx
    a = _post(client, REQUEST, {"email": None})
    b = _post(client, REQUEST, {"email": ["nobody@elsewhere.test"]})
    assert a.status_code == b.status_code == 422
    assert "nobody@elsewhere.test" not in b.text


@pytest.mark.parametrize("body", [{"email": "test@example.com"}, "not json"])
def test_verify_malformed_body_counts_against_ip_limit(ctx, body):
    client, recorded = ctx
    assert _post(client, VERIFY, body).status_code == 422
    assert recorded == [(False, None)]
