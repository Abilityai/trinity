"""#2322 — a login that did not issue a session must not look like one that did.

`POST /token` used to answer HTTP 200 with `access_token: null` **and**
`token_type: "bearer"` when enterprise 2FA deferred the login. Any client
reading the token field without checking it — our own MCP client and CLI both
did — stored nothing, reported success, and failed with an unexplained 401 on
its next call, far from the cause.

The fix is the issue's own suggested one: a password grant that issued no
session is an **error for the grant** (RFC 6749 §5.2), so the challenge is now
a **403** carrying `detail: "mfa_required"` plus the challenge fields, and no
token fields at all.

**403, not 401** — the issue permits either, and 401 is actively wrong here:
our CLI's `_handle_response` treats 401 as "your session expired, run
`trinity login`" and hard-exits, and the frontend registers a global axios 401
interceptor that logs out and redirects. Both would fire on a login that is
still in flight.

These tests pin all three shapes, because the fix moved all three:

* challenge   → 403, `detail: "mfa_required"`, no `access_token`, no `token_type`
* real grant  → 200, `access_token` + `token_type`, and **no** 2FA fields
  (before #2322 every successful login carried all four as `null`, including in
  OSS-only builds, contradicting `Token`'s own docstring)
* email route → unchanged 200 + raw challenge dict. Deliberate: the issue scopes
  the RFC change to "`/token` (the OAuth2-shaped surface)", and
  `/api/auth/email/verify` is not an OAuth2 grant. The invariant both satisfy is
  *no response ever presents a session that was not issued*; they satisfy it via
  the mechanism each contract calls for.

Edition-agnostic by construction: the provider is registered through the OSS
`mfa_gate` seam, so these run in an OSS-only checkout with no enterprise
submodule present.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _StubProvider:
    """Independent variable, not derived from the code under test.

    The gate's job is to turn (enrolled, required) into a decision, so a double
    that computed either from `mfa_gate` could never witness a wrong decision
    (learnings 2026-08-02). These two booleans are set by the test and read by
    nothing else.
    """

    def __init__(self, *, enrolled: bool, required: bool):
        self.enrolled = enrolled
        self.required = required
        self.calls = 0

    def gate_decision(self, user):
        self.calls += 1
        return {"enrolled": self.enrolled, "required": self.required}


@pytest.fixture
def auth_router(monkeypatch):
    """The real auth router with only the I/O boundaries stubbed."""
    import routers.auth as ra

    monkeypatch.setattr(ra, "is_setup_completed", lambda: True)
    monkeypatch.setattr(ra, "check_login_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(ra, "record_login_attempt", lambda *a, **k: None)
    monkeypatch.setattr(
        ra, "authenticate_user",
        lambda u, p: {"id": 1, "username": "admin", "role": "admin", "email": "admin@example.com"},
    )
    monkeypatch.setattr(ra.db, "update_last_login", lambda *a, **k: None)

    async def _noop_log(*a, **k):
        return None

    monkeypatch.setattr(ra.platform_audit_service, "log", _noop_log)
    return ra


@pytest.fixture
def client(auth_router):
    app = FastAPI()
    app.include_router(auth_router.router)
    return TestClient(app)


@pytest.fixture
def provider():
    """Register a provider for the test and always restore the OSS no-op path."""
    from services import mfa_gate

    registered = []

    def _register(*, enrolled, required):
        p = _StubProvider(enrolled=enrolled, required=required)
        mfa_gate.register_provider(p)
        registered.append(p)
        return p

    yield _register
    mfa_gate.clear_provider()


def _login(client):
    return client.post("/api/token", data={"username": "admin", "password": "pw"})


# ---------------------------------------------------------------- challenge

@pytest.mark.parametrize(
    "enrolled,required,enrollment_required",
    [
        # User enrolled in 2FA — the trigger the issue describes.
        (True, False, False),
        # Policy requires it for the role and nobody has enrolled yet — the
        # worse trigger: one admin toggle breaks every unattended credential
        # on that role before the rollout has started.
        (False, True, True),
        # Enrolled AND required.
        (True, True, False),
    ],
)
def test_challenge_response_carries_no_grant_fields(
    client, provider, enrolled, required, enrollment_required
):
    p = provider(enrolled=enrolled, required=required)
    r = _login(client)
    body = r.json()

    assert p.calls == 1, "the gate never ran — this test would pass vacuously"

    # A grant that issued no session is an error for the grant (RFC 6749 §5.2).
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {body}"
    assert body["detail"] == "mfa_required"

    # The regression itself. `access_token` was `null` and `token_type` was
    # `"bearer"`; both are now absent, so a client that reads either fails at
    # the login call instead of on some later request.
    assert "access_token" not in body, f"challenge must not carry access_token: {body}"
    assert "token_type" not in body, (
        "a grant that issued no session must not describe itself as a bearer "
        f"grant: {body}"
    )

    assert body["mfa_required"] is True
    assert body["mfa_enrolled"] is enrolled
    assert body["enrollment_required"] is enrollment_required
    assert body["challenge_token"], "the client needs this to complete the flow"


def test_challenge_token_is_challenge_scoped_not_a_session(client, provider):
    """The token in the challenge is not a session token.

    This is the half the issue's root-cause section reached for. It is real —
    `decode_token` refuses a challenge-scoped token — but it is NOT what
    produced the reported 401, because a client reads the *token field*, which
    was empty, and never presents `challenge_token` as a bearer. Pinned anyway:
    if the challenge token ever became session-usable, the fence above it would
    be the thing that failed.
    """
    from jose import jwt
    from config import SECRET_KEY, ALGORITHM
    from dependencies import MFA_CHALLENGE_SCOPE, decode_token

    provider(enrolled=True, required=False)
    body = _login(client).json()
    token = body["challenge_token"]

    claims = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    assert claims["scope"] == MFA_CHALLENGE_SCOPE
    assert claims["sub"] == "admin"
    assert claims["mode"] == "admin", "the eventual access token keeps the login mode"

    assert decode_token(token) is None, (
        "a challenge token must never resolve to a platform session"
    )


def test_challenge_carries_every_field_the_frontend_reads(client, provider):
    """`stores/auth.js::_setMfaChallenge` reads exactly these three.

    Since #2322 the store reads them off `error.response.data` on the 403 —
    same fields, error path. If they moved under `detail`, the 2FA prompt
    would never render and the user would see a raw "mfa_required" string.
    """
    provider(enrolled=True, required=False)
    r = _login(client)
    assert r.status_code == 403
    body = r.json()

    for field in ("challenge_token", "mfa_enrolled", "enrollment_required"):
        assert field in body, f"frontend 2FA prompt needs {field}: {body}"
    assert body.get("mfa_required") is True, "both call sites branch on this"


# -------------------------------------------------------------------- grant

def test_successful_grant_carries_no_2fa_fields(client, provider):
    """A provider that declines leaves an unchanged OAuth2 grant."""
    p = provider(enrolled=False, required=False)
    body = _login(client).json()

    assert p.calls == 1
    assert body["access_token"], "a declined gate must still issue the session"
    assert body["token_type"] == "bearer"
    for field in ("mfa_required", "mfa_enrolled", "enrollment_required", "challenge_token"):
        assert field not in body, f"a real grant must not carry {field}: {body}"


def test_oss_only_build_grant_is_exactly_two_fields(client):
    """No provider registered — the OSS-only path.

    `Token`'s docstring claimed the 2FA fields were "always absent in OSS-only
    builds". They were present and `null` on every successful login until
    #2322; this pins the claim.
    """
    from services import mfa_gate

    assert mfa_gate.get_provider() is None, "fixture leak — a provider is still registered"
    body = _login(client).json()

    assert set(body) == {"access_token", "token_type"}, (
        f"OSS-only grant must be exactly the OAuth2 pair, got {sorted(body)}"
    )
    assert body["token_type"] == "bearer"


def test_alias_route_matches(client, provider):
    """`/token` and `/api/token` are the same handler and must not drift."""
    provider(enrolled=True, required=False)
    r_bare = client.post("/token", data={"username": "admin", "password": "pw"})
    r_alias = client.post("/api/token", data={"username": "admin", "password": "pw"})
    assert r_bare.status_code == r_alias.status_code == 403, "both must refuse identically"
    bare, aliased = r_bare.json(), r_alias.json()

    assert set(bare) == set(aliased)
    assert "access_token" not in bare and "token_type" not in bare


# ------------------------------------------------------- gate is inert by default

def test_gate_is_a_no_op_without_a_provider(client):
    """The OSS build must be untouched by any of this."""
    from services import mfa_gate

    assert mfa_gate.gate_login({"id": 1, "username": "admin", "role": "admin"}, mode="admin") is None


# ------------------------------------------- the other OSS consumer (email)

@pytest.fixture
def email_client(auth_router, monkeypatch):
    """`/api/auth/email/verify` — the second of the three `gate_login` consumers."""
    ra = auth_router

    monkeypatch.setattr(ra.db, "get_setting_value",
                        lambda key, default=None: "true" if key == "email_auth_enabled" else default)
    monkeypatch.setattr(ra, "check_otp_rate_limit", lambda *a, **k: True)
    monkeypatch.setattr(ra, "record_otp_attempt", lambda *a, **k: None)
    # #2381: the route re-checks the allow-list before redeeming a code. Every
    # account that can legitimately email-login is allow-listed by construction,
    # so this stub models a real signed-in user rather than relaxing the gate.
    monkeypatch.setattr(ra.db, "is_email_whitelisted", lambda email: True)
    monkeypatch.setattr(ra.db, "verify_login_code", lambda email, code: True)
    monkeypatch.setattr(
        ra.db, "get_or_create_email_user",
        lambda email: {"id": 2, "username": email, "role": "admin", "email": email},
    )

    app = FastAPI()
    app.include_router(ra.router)
    return TestClient(app)


def _email_login(client):
    return client.post(
        "/api/auth/email/verify",
        json={"email": "admin@example.com", "code": "123456"},
    )


def test_email_route_challenge_also_carries_no_grant_fields(email_client, provider):
    """This route was ALREADY correct — pin it so it stays that way.

    It carries no `response_model`, so the raw challenge dict is returned as-is
    and there is no `access_token` key at all. That is load-bearing: three
    documents and this PR's own commit message cite it as the reference
    behaviour `/token` was brought in line with. Adding a `response_model` here
    later would silently reintroduce the #2322 shape on the one route that
    never had it, and nothing else would notice.
    """
    p = provider(enrolled=True, required=False)
    body = _email_login(email_client).json()

    assert p.calls == 1, "the gate never ran — this test would pass vacuously"
    assert "access_token" not in body, f"email challenge must not carry access_token: {body}"
    assert "token_type" not in body, f"email challenge must not carry token_type: {body}"
    assert body["mfa_required"] is True
    assert body["challenge_token"]


def test_email_route_grant_is_unchanged(email_client, provider):
    """A declined gate still issues the session + user profile the frontend reads."""
    provider(enrolled=False, required=False)
    body = _email_login(email_client).json()

    assert body["access_token"]
    assert body["token_type"] == "bearer"
    assert body["user"]["username"] == "admin@example.com"
    for field in ("mfa_required", "challenge_token"):
        assert field not in body, f"a real grant must not carry {field}: {body}"


def test_the_403_is_not_a_401(client, provider):
    """401 is the wrong status here, and both first-party clients prove it.

    The CLI's `_handle_response` treats 401 as "your session expired, run
    `trinity login`" and hard-exits — nonsense during login itself. The
    frontend registers a GLOBAL axios 401 interceptor that calls
    `authStore.logout()` and redirects to /login, which is wrong for a login
    still in flight (it is only safe today because of a `currentPath !==
    '/login'` guard). The issue permits "401/403"; this pins the choice so a
    later tidy-up cannot silently re-arm either behaviour.
    """
    provider(enrolled=True, required=False)
    r = _login(client)

    assert r.status_code == 403
    assert r.status_code != 401, (
        "401 triggers the CLI's re-login exit and the frontend's global "
        "logout interceptor — see the route comment in routers/auth.py"
    )


def test_wrong_password_is_still_401_and_carries_no_challenge(client, provider, auth_router, monkeypatch):
    """The two failure modes must stay distinguishable.

    A deferred grant (correct password, second factor pending) is 403 with a
    challenge; a rejected grant (wrong password) stays 401 with none. Collapsing
    them would hand an unauthenticated caller a challenge token.
    """
    monkeypatch.setattr(auth_router, "authenticate_user", lambda u, p: None)
    provider(enrolled=True, required=False)

    r = _login(client)
    assert r.status_code == 401
    body = r.json()
    assert "challenge_token" not in body, "a rejected password must never yield a challenge"
    assert "mfa_required" not in body
