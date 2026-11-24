"""python-jose → PyJWT (abilityai/trinity-enterprise#1124, the python-jose part).

The backend image pinned `python-jose[cryptography]==3.3.0` (7 OSV advisories);
the latest release still carries two with no fixed version, so a bump cannot
clear it. PyJWT (2.14.0) was already in the image; it is pinned at 2.15.1 here, the
first release with no open advisory. The OSS backend now signs and
verifies every platform token with PyJWT; python-jose stays installed only while
a private module still imports it.

What this file pins:

* **No `jose` import** anywhere under `src/backend/` (the private submodule is
  not this PR's) nor under `tests/`. A test that kept importing `jose` would go
  green on CI and red the day the pin is dropped.
* **Every `jwt.decode` in the OSS backend names the algorithm** as the explicit
  list `[ALGORITHM]`, or disables signature verification explicitly — never
  `algorithms=None`, never the header's own `alg`.
* **Tokens issued before the swap stay valid.** The two libraries produce the
  same bytes for the same claims, so a token of every kind the platform mints
  (session, MFA challenge, Workspace session, event loopback), minted once with
  python-jose 3.3.0 — the image's exact pin — and stored here as a fixture,
  still decodes; otherwise the upgrade signs every person out. PyJWT also
  reproduces one of those fixtures byte for byte.
* **PyJWT is pinned identically** in the image, in the test requirements and in
  the environment running this file — the version these assertions are about.
* **A bad token is a `None`/401, never a 500**, at every decode helper and both
  auth routes: expired, wrong key, malformed, empty, `None`, `alg=none`, a
  different HMAC algorithm, an MCP-key-shaped bearer. PyJWT's failures are all
  `PyJWTError` subclasses (`InvalidKeyError` is NOT an `InvalidTokenError`, so
  the catch must be the base class).
* **The unverified reads stay unverified.** `PUT /api/users/me/password` copies
  the `mode` claim out of the caller's bearer without checking it (the bearer
  was already verified by the auth dependency), so an expired or foreign token
  must still yield its `mode`, and garbage must yield `None`, not an error.
* **The header is unchanged**: `{"alg": "HS256", "typ": "JWT"}`.
"""
from __future__ import annotations

import ast
import asyncio
import base64
import json
import os
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault(
    "TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent1124.db")
)
os.environ.setdefault(
    "LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent1124-logs")
)

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "src" / "backend"
TESTS = REPO / "tests"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

pytestmark = pytest.mark.unit


# =============================================================================
# Static guards
# =============================================================================

def _python_files(root: Path, *, skip_parts: frozenset[str]) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root).parts
        if any(part in skip_parts for part in rel[:-1]):
            continue
        out.append(p)
    return out


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _jose_imports(path: Path) -> list[str]:
    """`import jose`, `from jose import …`, `from jose.x import …` — the
    python-jose distribution. `from services.aauth import jose` is the project's
    own Ed25519 JWS module and does not count."""
    hits = []
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "jose" or alias.name.startswith("jose."):
                    hits.append(f"{path.relative_to(REPO)}:{node.lineno}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level == 0 and (mod == "jose" or mod.startswith("jose.")):
                hits.append(f"{path.relative_to(REPO)}:{node.lineno}")
    return hits


def test_the_oss_backend_imports_no_python_jose():
    """`enterprise/` is the private submodule — its own repo moves it."""
    offenders = []
    for p in _python_files(BACKEND, skip_parts=frozenset({"enterprise"})):
        offenders += _jose_imports(p)
    assert offenders == [], (
        "python-jose is still imported by the OSS backend; swap to PyJWT "
        "(`import jwt`, `except jwt.PyJWTError`):\n  " + "\n  ".join(offenders)
    )


def test_no_test_imports_python_jose():
    """Pre-swap compatibility is proven with FIXTURES minted once by python-jose
    3.3.0 (below), so no test needs the old library: a live import would run the
    test floor's version against the image's, and would turn the proof into a
    silent skip or a red suite the day the pin is dropped."""
    offenders = []
    for p in _python_files(TESTS, skip_parts=frozenset()):
        offenders += _jose_imports(p)
    assert offenders == [], (
        "a test still imports python-jose; mint and read with PyJWT "
        "(`import jwt`; unverified read: `jwt.decode(t, options={'verify_signature': False})`):\n  "
        + "\n  ".join(offenders)
    )


def _names_algorithm(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == "ALGORITHM") or (
        isinstance(node, ast.Attribute) and node.attr == "ALGORITHM"
    )


def _disables_signature_check(options: ast.AST) -> bool:
    if not isinstance(options, ast.Dict):
        return False
    for k, v in zip(options.keys, options.values):
        if (isinstance(k, ast.Constant) and k.value == "verify_signature"
                and isinstance(v, ast.Constant) and v.value is False):
            return True
    return False


def _jwt_calls(path: Path, attr: str):
    for node in ast.walk(_parse(path)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == attr
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "jwt"):
            yield node


def test_every_jwt_decode_in_the_oss_backend_names_the_algorithm_or_disables_verification_explicitly():
    """`algorithms=[ALGORITHM]` is the whole defence against `alg=none` and
    algorithm confusion; a decode that omits it, or passes `None`, would let the
    token's own header pick. The only other acceptable shape is the deliberate
    unverified read, spelled out as `options={"verify_signature": False}`."""
    bad = []
    for p in _python_files(BACKEND, skip_parts=frozenset({"enterprise", "aauth"})):
        for call in _jwt_calls(p, "decode"):
            kw = {k.arg: k.value for k in call.keywords}
            explicit_alg = (
                isinstance(kw.get("algorithms"), ast.List)
                and len(kw["algorithms"].elts) == 1
                and _names_algorithm(kw["algorithms"].elts[0])
            )
            unverified = _disables_signature_check(kw.get("options"))
            if len(call.args) > 2 or not (explicit_alg or unverified):
                bad.append(f"{p.relative_to(REPO)}:{call.lineno}")
        for call in _jwt_calls(p, "encode"):
            kw = {k.arg: k.value for k in call.keywords}
            if len(call.args) > 2 or not _names_algorithm(kw.get("algorithm")):
                bad.append(f"{p.relative_to(REPO)}:{call.lineno} (encode)")
    assert bad == [], "jwt call without an explicit `[ALGORITHM]`:\n  " + "\n  ".join(bad)


# =============================================================================
# Behaviour
# =============================================================================

@pytest.fixture()
def deps(monkeypatch):
    """`dependencies` with a fake Redis, a one-user database and the default
    Workspace-session policy. Resolved through `sys.modules` (the unit conftest
    restores the module between tests; a bare import can hand back a different
    object than the one the routers hold — learnings 2026-09-23)."""
    import importlib
    importlib.import_module("dependencies")
    mod = sys.modules["dependencies"]

    import fakeredis
    fake = fakeredis.FakeRedis()
    monkeypatch.setattr(mod, "get_breaker_redis", lambda: fake)
    monkeypatch.setattr(mod, "_portal_session_policy", lambda: (7 * 86400, 30 * 86400))
    users = {
        "alice": {"id": 7, "username": "alice", "email": "alice@example.com",
                  "role": "admin", "suspended_at": None},
    }
    monkeypatch.setattr(mod, "db", SimpleNamespace(get_user_by_username=users.get))
    return mod


def _b64u(obj: dict) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _bad_tokens() -> dict[str, object]:
    """Every shape a decode helper must answer with None, never an exception."""
    import jwt as pyjwt
    from config import ALGORITHM, SECRET_KEY
    now = int(time.time())
    return {
        "expired": pyjwt.encode({"sub": "alice", "exp": now - 1}, SECRET_KEY, algorithm=ALGORITHM),
        "wrong_key": pyjwt.encode({"sub": "alice", "exp": now + 300}, "k" * 32, algorithm=ALGORITHM),
        "malformed": "not.a.jwt",
        "two_segments": _b64u({"alg": "HS256", "typ": "JWT"}) + "." + _b64u({"sub": "alice"}),
        "empty": "",
        "none": None,
        "alg_none": _b64u({"alg": "none", "typ": "JWT"}) + "." + _b64u({"sub": "alice", "exp": now + 300}) + ".",
        "other_hmac": pyjwt.encode({"sub": "alice", "exp": now + 300}, SECRET_KEY, algorithm="HS512"),
        "mcp_key_shaped": "trinity_mcp_" + "a" * 43,
    }


BAD_KINDS = ("expired", "wrong_key", "malformed", "two_segments", "empty", "none",
             "alg_none", "other_hmac", "mcp_key_shaped")


def test_the_bad_token_matrix_is_the_one_the_parametrize_reads():
    assert set(_bad_tokens()) == set(BAD_KINDS)


DECODE_HELPERS = (
    ("decode_token", None),
    ("decode_mfa_challenge", None),
    ("decode_portal_session", None),
    ("portal_session_minted_by", None),
    ("portal_session_is_interactive", False),
    ("portal_session_needs_rotation", False),
    ("renew_portal_session", None),
)


@pytest.mark.parametrize("kind", BAD_KINDS)
@pytest.mark.parametrize("helper,refusal", DECODE_HELPERS, ids=[h for h, _ in DECODE_HELPERS])
def test_a_bad_token_is_refused_quietly_at_every_decode_helper(deps, helper, refusal, kind):
    token = _bad_tokens()[kind]
    assert getattr(deps, helper)(token) is refusal


@pytest.mark.parametrize("kind", BAD_KINDS)
def test_the_validate_route_answers_401_to_a_bad_token(deps, kind):
    """`GET /api/auth/validate` is how nginx and the MCP server check a token."""
    from fastapi import HTTPException
    from routers import auth as auth_router
    token = _bad_tokens()[kind]
    request = SimpleNamespace(
        headers={"Authorization": f"Bearer {token}"} if token is not None else {},
        cookies={}, query_params={},
    )
    with pytest.raises(HTTPException) as e:
        asyncio.run(auth_router.validate_token(request))
    assert e.value.status_code == 401


@pytest.mark.parametrize("kind", BAD_KINDS)
def test_logout_with_a_bearer_that_is_not_a_session_token_is_a_quiet_no_op(deps, kind):
    """An MCP API key authenticates `get_current_user` too but carries no
    `jti`; `POST /api/auth/logout` must answer `logged_out`, not 500."""
    from routers import auth as auth_router
    principal = SimpleNamespace(username="alice")
    out = asyncio.run(auth_router.logout(current_user=principal, token=_bad_tokens()[kind]))
    assert out == {"status": "logged_out"}


# -----------------------------------------------------------------------------
# Pre-swap fixtures — minted ONCE with python-jose 3.3.0, the version the backend
# image pins (learnings 2026-10-26: the test floor would run 3.5.0). Re-mint with:
#   pip install "python-jose[cryptography]==3.3.0"
#   from jose import jwt; jwt.encode(dict(CLAIMS[kind]), FIXTURE_KEY, algorithm="HS256")
# with the claim dicts below in exactly this key order (neither library sorts the
# payload, so the bytes depend on insertion order). `exp` is 2100-01-01, `iat`
# 2026-11-17 00:00Z. The reverse direction (a PyJWT token read by python-jose,
# the rollback case) was proven once by hand on 2026-11-17 and is not kept as a
# test: it would need the old library installed.
# -----------------------------------------------------------------------------
FIXTURE_KEY = "ent1124-fixture-key-0123456789abcdef"
FIXTURE_EXP = 4102444800
FIXTURE_IAT = 1763337600
CLAIMS = {
    "session": {"sub": "alice", "interactive": True, "exp": FIXTURE_EXP, "mode": "admin",
                "jti": "fixture-session", "iat": FIXTURE_IAT, "iat_ms": FIXTURE_IAT * 1000},
    "mfa": {"sub": "alice", "scope": "mfa_challenge", "exp": FIXTURE_EXP, "mode": "email",
            "jti": "fixture-mfa", "iat": FIXTURE_IAT, "iat_ms": FIXTURE_IAT * 1000},
    "portal": {"scope": "portal_session", "email": "Bob@Example.com", "iat": FIXTURE_IAT,
               "sst": FIXTURE_IAT, "minted_by": "code", "interactive": True, "exp": FIXTURE_EXP,
               "mode": "email", "jti": "fixture-portal"},
    "loopback": {"sub": "alice", "scope": "event_loopback", "source_agent": "scout",
                 "chain_depth": 2, "exp": FIXTURE_EXP},
    "identity": {"sub": "alice", "exp": FIXTURE_EXP, "mode": "admin", "jti": "fixture-identity",
                 "iat": FIXTURE_IAT, "iat_ms": FIXTURE_IAT * 1000},
}
PRE_SWAP = {
    "session": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhbGljZSIsImludGVyYWN0aXZlIjp0cnVlLCJleHAiOjQxMDI0NDQ4MDAsIm1vZGUiOiJhZG1pbiIsImp0aSI6ImZpeHR1cmUtc2Vzc2lvbiIsImlhdCI6MTc2MzMzNzYwMCwiaWF0X21zIjoxNzYzMzM3NjAwMDAwfQ.gVujEEwF1GElTq3QOZYRL7nsOWbs4kJyG8jPLKa7u7w",
    "mfa": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhbGljZSIsInNjb3BlIjoibWZhX2NoYWxsZW5nZSIsImV4cCI6NDEwMjQ0NDgwMCwibW9kZSI6ImVtYWlsIiwianRpIjoiZml4dHVyZS1tZmEiLCJpYXQiOjE3NjMzMzc2MDAsImlhdF9tcyI6MTc2MzMzNzYwMDAwMH0.wLpU1-ewk5_RCT77Jxmeg3t820UVBBPWD5ilnKjls7U",
    "portal": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzY29wZSI6InBvcnRhbF9zZXNzaW9uIiwiZW1haWwiOiJCb2JARXhhbXBsZS5jb20iLCJpYXQiOjE3NjMzMzc2MDAsInNzdCI6MTc2MzMzNzYwMCwibWludGVkX2J5IjoiY29kZSIsImludGVyYWN0aXZlIjp0cnVlLCJleHAiOjQxMDI0NDQ4MDAsIm1vZGUiOiJlbWFpbCIsImp0aSI6ImZpeHR1cmUtcG9ydGFsIn0.RRmfwz1sIaNYzQrfEMNByI4xwlSGidrpuySc_pgZvzs",
    "loopback": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhbGljZSIsInNjb3BlIjoiZXZlbnRfbG9vcGJhY2siLCJzb3VyY2VfYWdlbnQiOiJzY291dCIsImNoYWluX2RlcHRoIjoyLCJleHAiOjQxMDI0NDQ4MDB9.BlxUgma0AC6tO8Kv5t7XvMQs7vb4B5_yjxqurpqTql0",
    "identity": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhbGljZSIsImV4cCI6NDEwMjQ0NDgwMCwibW9kZSI6ImFkbWluIiwianRpIjoiZml4dHVyZS1pZGVudGl0eSIsImlhdCI6MTc2MzMzNzYwMCwiaWF0X21zIjoxNzYzMzM3NjAwMDAwfQ.-kUY25FJMKrLSEjqGuXGw3uaovPKa6prhDL1fRCTQSg",
}


@pytest.fixture()
def pre_swap(deps, monkeypatch):
    """The helpers verify against the fixture key; the Workspace cap is widened
    so the 2026 `sst` is not past the 30-day default by the time this runs."""
    monkeypatch.setattr(deps, "SECRET_KEY", FIXTURE_KEY)
    century = 100 * 365 * 86400
    monkeypatch.setattr(deps, "_portal_session_policy", lambda: (century, century))
    return PRE_SWAP


def test_a_token_minted_by_python_jose_before_the_swap_still_authenticates(deps, pre_swap):
    """Every token kind the platform mints, signed by the OLD library at the
    image's exact version, decodes through the helpers the NEW library backs.
    If this fails, the upgrade signs every person out (7-day sessions) and
    breaks every open Workspace."""
    out = deps.decode_token(pre_swap["session"])
    assert out and out["sub"] == "alice" and out["mode"] == "admin"

    assert deps.decode_mfa_challenge(pre_swap["mfa"]) == {"username": "alice", "mode": "email"}
    assert deps.decode_token(pre_swap["mfa"]) is None, "a challenge token is still not a session"

    assert deps.decode_portal_session(pre_swap["portal"]) == "bob@example.com"
    assert deps.portal_session_minted_by(pre_swap["portal"]) == "code"
    assert deps.portal_session_is_interactive(pre_swap["portal"]) is True
    assert deps.decode_token(pre_swap["portal"]) is None, "a Workspace session is still not a platform session"

    request = SimpleNamespace(method="POST", scope={"path": "/api/agents/scribe/task"},
                              headers={}, client=None)
    principal = asyncio.run(deps.resolve_platform_user_unfloored(request, pre_swap["loopback"]))
    assert principal.is_event_loopback is True
    assert principal.vouched_source_agent == "scout"


def test_pyjwt_reproduces_a_python_jose_token_byte_for_byte(pre_swap):
    """Same header, same payload serialisation, same signature — the property
    that makes the swap invisible to every token already in a browser."""
    import jwt as pyjwt
    assert pyjwt.encode(dict(CLAIMS["identity"]), FIXTURE_KEY, algorithm="HS256") == pre_swap["identity"]
    assert pyjwt.decode(pre_swap["identity"], FIXTURE_KEY, algorithms=["HS256"]) == CLAIMS["identity"]
    assert pyjwt.get_unverified_header(pre_swap["identity"]) == {"alg": "HS256", "typ": "JWT"}


def _pinned(path: Path, package: str) -> str:
    import re
    pattern = re.compile(
        r'^\s*"?' + re.escape(package) + r'(?:\[[^\]]+\])?==([0-9][^"\s\\]*)"?\s*\\?\s*$',
        re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(path.read_text())
    assert match, f"{package} is not pinned exactly in {path.relative_to(REPO)}"
    return match.group(1)


def test_pyjwt_is_pinned_identically_in_the_image_and_in_ci_and_is_what_runs_here():
    """Every behaviour this file asserts is PyJWT's; the version it asserts it
    on must be the one the image ships (learnings 2026-10-26). `pyjwt` also sits
    in `test_ent679_payments_pin_parity.py`'s TRANSITIVE_PINS as a payments-py
    dependency — if payments-py ever drops it, that entry goes and this one is
    what keeps the auth library from floating."""
    import importlib.metadata as md
    image = _pinned(REPO / "docker" / "backend" / "Dockerfile", "pyjwt")
    ci = _pinned(REPO / "tests" / "requirements-test.txt", "pyjwt")
    assert image == ci == md.version("pyjwt"), (image, ci, md.version("pyjwt"))


def test_the_header_is_unchanged(deps):
    import jwt as pyjwt
    token = deps.create_access_token({"sub": "alice"}, timedelta(minutes=5))
    assert pyjwt.get_unverified_header(token) == {"alg": "HS256", "typ": "JWT"}


@pytest.fixture()
def password_route(deps, monkeypatch):
    """`PUT /api/users/me/password` with the auth dependency overridden and the
    service stubbed: the only thing under test is what the route reads out of
    the bearer it was handed."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import users as users_router
    from services import password_service

    seen = {}

    def fake_change(username, current, new, token_mode, revoke_keys=False):
        seen["token_mode"] = token_mode
        return password_service.PasswordChangeResult(
            access_token="fresh", other_sessions_signed_out=True, admin_env_stale=False,
            revoked_key_ids=[], active_key_count=0)

    monkeypatch.setattr(users_router.password_service, "change_own_password", fake_change)
    monkeypatch.setattr(users_router.platform_audit_service, "log", AsyncMock())
    principal = users_router.User(id=7, username="alice", role="admin")
    app = FastAPI()
    app.include_router(users_router.router)
    app.dependency_overrides[users_router.require_interactive] = lambda: principal
    app.dependency_overrides[users_router.get_current_user] = lambda: principal

    def put(bearer: str):
        client = TestClient(app, headers={"Authorization": f"Bearer {bearer}"})
        r = client.put("/api/users/me/password",
                       json={"current_password": "Old-Passw0rd-xyz!", "new_password": "New-Passw0rd-xyz!"})
        assert r.status_code == 200, r.text
        return seen.get("token_mode")

    return put


def test_the_password_route_reads_mode_from_the_bearer_without_verifying_it(deps, password_route):
    """The bearer was already verified by the auth dependency; the route only
    copies its `mode` into the fresh token. An expired or foreign-signed token
    must still yield its mode — a verification sneaking into that read would
    turn the person's own password change into a 500 at the edge of expiry."""
    import jwt as pyjwt
    from config import ALGORITHM, SECRET_KEY
    now = int(time.time())
    expired = pyjwt.encode({"sub": "alice", "mode": "email", "exp": now - 60}, SECRET_KEY, algorithm=ALGORITHM)
    assert password_route(expired) == "email"
    foreign = pyjwt.encode({"sub": "alice", "mode": "email", "exp": now + 60}, "k" * 32, algorithm=ALGORITHM)
    assert password_route(foreign) == "email"
    assert password_route("trinity_mcp_" + "a" * 43) is None, "a non-JWT bearer yields no mode, not an error"
