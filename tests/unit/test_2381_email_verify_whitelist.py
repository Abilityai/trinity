"""#2381 — `/api/auth/email/verify` must re-check the allow-list before redeeming.

Login codes live in ONE table keyed on (email, code) and nothing binds a code to
the channel that minted it. `/api/auth/email/request` checks the allow-list before
minting; three other producers do not — Telegram and WhatsApp `/login <email>`
mint after a shape check only, and the MCP inline-auth service mints for any
address the `users` table already knows.

Those producers' own redeemers only grant channel- or connector-scope. This
redeemer is the one that issues a full platform JWT carrying whatever role the
matched account has — and `get_or_create_email_user` resolves by the email column
alone, so an attacker-controlled address bound to the admin row turned any of
those mints into an admin session, surviving restarts and password resets.

The refusal must be indistinguishable from a wrong code: same status, same
message, same audit row, same rate-limit accounting. Otherwise this endpoint
becomes an oracle for "is this address allow-listed".

Import isolation: `routers.auth` pulls the backend chain, so the import is
deferred out of collection — see `test_setup_operator_profile.py`.
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit


def _auth():
    import routers.auth as m
    return m


class _Req:
    """The handler reads client.host, url.path, state and json()."""

    def __init__(self, email, code):
        self._body = {"email": email, "code": code}
        self.client = SimpleNamespace(host="203.0.113.9")
        self.url = SimpleNamespace(path="/api/auth/email/verify")
        self.state = SimpleNamespace(request_id="req-1")

    async def json(self):
        return self._body


class _DB:
    def __init__(self, whitelisted, raises=False):
        self._whitelisted = whitelisted
        self._raises = raises
        self.verify_calls = []

    # --- the two calls under test -------------------------------------------
    def is_email_whitelisted(self, email):
        if self._raises:
            raise RuntimeError("db down")
        return self._whitelisted

    def verify_login_code(self, email, code):
        self.verify_calls.append((email, code))
        return {"email": email}

    # --- everything past the gate (only reached on the allowed path) --------
    def get_setting_value(self, key, default=None):
        return "true" if key == "email_auth_enabled" else default

    def get_or_create_email_user(self, email):
        return {"username": "admin", "email": email, "role": "admin"}

    def update_last_login(self, username):
        pass


@pytest.fixture
def patched(monkeypatch):
    auth = _auth()
    monkeypatch.setattr(auth, "is_setup_completed", lambda: True)
    monkeypatch.setattr(auth, "check_login_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(auth, "check_otp_rate_limit", lambda *a, **k: None)

    attempts = []
    monkeypatch.setattr(
        auth, "record_login_attempt",
        lambda ip, success, account=None: attempts.append(("login", success)),
    )
    monkeypatch.setattr(
        auth, "record_otp_attempt",
        lambda email, success: attempts.append(("otp", success)),
    )

    audits = []

    async def _log(**kw):
        audits.append(kw)

    monkeypatch.setattr(auth.platform_audit_service, "log", _log)

    def _apply(db):
        monkeypatch.setattr(auth, "db", db)
        return db

    return SimpleNamespace(apply=_apply, attempts=attempts, audits=audits)


def _run(email="attacker@example.com", code="123456"):
    return asyncio.run(_auth().verify_email_login_code(_Req(email, code)))


def test_non_whitelisted_email_is_refused(patched):
    """The escalation path: a code minted by Telegram/WhatsApp for any address."""
    db = patched.apply(_DB(whitelisted=False))

    with pytest.raises(HTTPException) as exc:
        _run()

    assert exc.value.status_code == 401
    # The code is never even consulted, so it is not consumed either.
    assert db.verify_calls == []


def test_refusal_is_indistinguishable_from_a_bad_code(patched):
    """Same status, message, audit action and failure accounting as a bad code.

    Any divergence turns this endpoint into an allow-list membership oracle.
    """
    auth = _auth()

    not_listed = patched.apply(_DB(whitelisted=False))
    with pytest.raises(HTTPException) as refused:
        _run()
    refused_audits = list(patched.audits)
    refused_attempts = list(patched.attempts)

    patched.attempts.clear()
    patched.audits.clear()

    # Allow-listed, but the code itself is wrong.
    bad_code = patched.apply(_DB(whitelisted=True))
    bad_code.verify_login_code = lambda email, code: None
    with pytest.raises(HTTPException) as wrong:
        _run()

    assert refused.value.status_code == wrong.value.status_code
    assert refused.value.detail == wrong.value.detail
    assert refused_attempts == patched.attempts
    assert [a["event_action"] for a in refused_audits] == [
        a["event_action"] for a in patched.audits
    ]


def test_whitelisted_email_still_redeems(patched):
    """The legitimate web flow is untouched — it already passed this check at mint."""
    db = patched.apply(_DB(whitelisted=True))

    result = _run(email="operator@acme.com")

    assert db.verify_calls == [("operator@acme.com", "123456")]
    assert result.access_token  # EmailLoginResponse model, not a dict


def test_allowlist_read_failure_fails_closed(patched):
    """A DB error must not fall through into redeeming the code."""
    db = patched.apply(_DB(whitelisted=True, raises=True))

    with pytest.raises(HTTPException) as exc:
        _run()

    assert exc.value.status_code == 401
    assert db.verify_calls == []
