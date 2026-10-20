"""#3655 — the Login page knows whether this install can deliver email.

With no email provider configured (the default for a localhost install that
skipped the Resend key) the sender falls back to console mode: the code goes to
the backend log and the send reports success. The page said "We sent a 6-digit
code" and the person waited for an email that never came.

Pinned here:

* `EmailService.can_deliver()` follows the per-send resolution — `console` and
  an unknown provider cannot deliver; `resend` needs its key (Settings or env);
  `smtp` needs host, user and password; `sendgrid` needs its key;
* an unreadable config answers True — the page then behaves exactly as today;
* `GET /api/auth/mode` carries it as `email_delivery_available`, an
  install-level flag beside the ent#849 one;
* the per-request `POST /api/auth/email/request` never consults it, so its
  response stays identical for every address (#186).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"


@pytest.fixture
def svc(monkeypatch):
    import services.email_service as es

    service = es.EmailService()
    state = {"cfg": ("console", "", "noreply@example.com")}
    monkeypatch.setattr(service, "_runtime_config", lambda: state["cfg"])

    def set_cfg(provider, key=""):
        state["cfg"] = (provider, key, "noreply@example.com")
    service.set_cfg = set_cfg
    return service, es


@pytest.mark.parametrize("provider,key,expected", [
    ("console", "", False),
    ("console", "re_key", False),
    ("resend", "", False),
    ("resend", "re_key", True),
    ("carrier-pigeon", "x", False),
])
def test_can_deliver_follows_the_provider_and_its_key(svc, provider, key, expected):
    service, _ = svc
    service.set_cfg(provider, key)
    assert service.can_deliver() is expected


def test_smtp_needs_host_user_and_password(svc, monkeypatch):
    service, es = svc
    service.set_cfg("smtp")
    monkeypatch.setattr(es, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(es, "SMTP_USER", "mailer")
    monkeypatch.setattr(es, "SMTP_PASSWORD", "")
    assert service.can_deliver() is False
    monkeypatch.setattr(es, "SMTP_PASSWORD", "secret")
    assert service.can_deliver() is True


def test_sendgrid_needs_its_key(svc, monkeypatch):
    service, es = svc
    service.set_cfg("sendgrid")
    monkeypatch.setattr(es, "SENDGRID_API_KEY", "")
    assert service.can_deliver() is False
    monkeypatch.setattr(es, "SENDGRID_API_KEY", "SG.key")
    assert service.can_deliver() is True


def test_an_unreadable_config_behaves_as_today(svc, monkeypatch):
    service, _ = svc

    def _boom():
        raise RuntimeError("settings unreadable")
    monkeypatch.setattr(service, "_runtime_config", _boom)
    assert service.can_deliver() is True


@pytest.fixture
def mode(monkeypatch):
    import routers.auth as ra

    monkeypatch.setattr(ra, "is_setup_completed", lambda: True)
    monkeypatch.setattr(ra.db, "get_setting_value",
                        lambda k, d=None: "true" if k == "email_auth_enabled" else d)
    import services.email_service as es

    state = {"deliver": True}
    monkeypatch.setattr(es.email_service, "can_deliver", lambda: state["deliver"])
    app = FastAPI()
    app.include_router(ra.router)
    client = TestClient(app)

    def read(deliver):
        state["deliver"] = deliver
        r = client.get("/api/auth/mode")
        assert r.status_code == 200
        return r.json()
    return read


@pytest.mark.parametrize("deliver", [True, False])
def test_auth_mode_says_whether_this_install_delivers_email(mode, deliver):
    assert mode(deliver)["email_delivery_available"] is deliver


def test_the_code_request_never_consults_the_install_flag():
    """#186: the per-request response must stay byte-identical for every
    address. The install flag rides on the mode read only."""
    tree = ast.parse((_BACKEND / "routers" / "auth.py").read_text())
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        routes = [d for d in fn.decorator_list if isinstance(d, ast.Call)
                  and any(isinstance(a, ast.Constant) and a.value == "/api/auth/email/request"
                          for a in d.args)]
        if routes:
            names = {getattr(n, "attr", getattr(n, "id", None)) for n in ast.walk(fn)}
            assert "can_deliver" not in names and "email_delivery_available" not in names
            return
    raise AssertionError("POST /api/auth/email/request handler not found")
