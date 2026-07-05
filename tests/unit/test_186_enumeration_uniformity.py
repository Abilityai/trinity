"""#186 — user & agent enumeration uniformity regression tests.

Guards the two differential-response oracles UnderDefense flagged (pentest 3.3.3):

  1. Agent enumeration — the four agent-access dependency helpers must return a
     BYTE-IDENTICAL denial (status + detail) for a non-existent agent and an
     existing-but-inaccessible one, so agent existence can't be enumerated.
     These assertions run the REAL ``db.get_agent_owner`` / ``can_user_access_agent``
     / ``can_user_share_agent`` against a real (temp) schema — NOT dep-overrides,
     which would bypass the very db calls under test (plan Decision #10).

  2. User (email) enumeration — ``POST /api/auth/email/request`` must return an
     identical body for a whitelisted vs a non-whitelisted email, and must NOT
     emit a 429 (a status differential) on the whitelisted rate-limited path.

  3. Tier-4 authz hole — the agent_config GET handlers that previously took a raw
     ``agent_name: str`` with no access check are now bound to the
     ``get_authorized_agent_by_name`` dependency (uniform 404, no 404-vs-200
     existence oracle).

The MCP third surface (chat.ts uniform reason + owner-username removal) is
TypeScript and is verified separately via the MCP-client probe — not here.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

# Make src/backend importable first (mirrors the other real-db unit tests).
_BACKEND = str(Path(__file__).resolve().parents[2] / "src" / "backend")
while _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)

# db_harness lives in tests/ — pull the real-schema backend fixture + seeds.
_TESTS = str(Path(__file__).resolve().parents[1])
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

from db_harness import db_backend, run, seed_agent, seed_user  # noqa: E402,F401

# Capture the REAL services package + docker_service module at collection time,
# while sys.modules is still clean. A sibling unit test stubs these into
# sys.modules as Mocks during its own run; under some pytest-randomly orderings
# that state leaks past its teardown, and `_agent_exists`'s function-local
# `from services.docker_service import get_agent_by_name` then resolves a truthy
# Mock — reporting a non-existent agent as existing. The `_no_docker` fixture
# pins these back (monkeypatch.setitem, auto-restored) before stubbing the probe
# so resolution is deterministic regardless of inbound pollution (#186).
import services  # noqa: E402
import services.docker_service  # noqa: E402
_REAL_SERVICES_PKG = sys.modules["services"]
_REAL_DOCKER_SERVICE = sys.modules["services.docker_service"]


# =============================================================================
# 1. Agent-access dependency uniformity (real DB, real db calls)
# =============================================================================

_OWNER_ID = 1
_STRANGER_ID = 2
_ADMIN_ID = 3
_OWNER = "owner-a"
_STRANGER = "stranger-b"
_ADMIN = "admin-c"
_AGENT = "agent-a"
_MISSING = "does-not-exist-xyz"


@pytest.fixture
def seeded(db_backend):
    """Seed two regular users, an admin, and one agent owned by user A."""
    seed_user(_OWNER_ID, _OWNER, "user")
    seed_user(_STRANGER_ID, _STRANGER, "user")
    seed_user(_ADMIN_ID, _ADMIN, "admin")
    seed_agent(_AGENT, owner_id=_OWNER_ID)
    return db_backend


def _user(username: str, role: str = "user"):
    from models import User
    return User(id=0, username=username, role=role)


def _denial(dep, name_kwarg: str, name: str, user):
    """Invoke a dependency helper and return its raised HTTPException."""
    with pytest.raises(HTTPException) as exc:
        dep(**{name_kwarg: name, "current_user": user})
    return exc.value


# The four helpers × their path-param kwarg name.
_READ_DEPS = [
    ("get_authorized_agent", "name"),
    ("get_authorized_agent_by_name", "agent_name"),
]
_OWNER_DEPS = [
    ("get_owned_agent", "name"),
    ("get_owned_agent_by_name", "agent_name"),
]


@pytest.mark.parametrize("dep_name,kw", _READ_DEPS + _OWNER_DEPS)
def test_nonexistent_and_inaccessible_are_byte_identical(seeded, dep_name, kw):
    """A non-existent name and an existing-but-inaccessible agent must raise the
    SAME status + detail (uniform 404) for every dependency helper."""
    import dependencies

    dep = getattr(dependencies, dep_name)
    stranger = _user(_STRANGER)

    missing = _denial(dep, kw, _MISSING, stranger)
    inaccessible = _denial(dep, kw, _AGENT, stranger)

    assert missing.status_code == 404
    assert inaccessible.status_code == 404
    # Byte-identical detail — no "Access denied" vs "Agent not found" split.
    assert missing.detail == inaccessible.detail == "Agent not found"


@pytest.mark.parametrize("dep_name,kw", _READ_DEPS + _OWNER_DEPS)
def test_admin_on_nonexistent_still_404s(seeded, dep_name, kw):
    """can_user_access/share returns True for an admin on ANY name, so the
    existence check must still 404 an admin against a non-existent agent
    (otherwise admins get a 200-vs-404 oracle)."""
    import dependencies

    dep = getattr(dependencies, dep_name)
    admin = _user(_ADMIN, role="admin")
    err = _denial(dep, kw, _MISSING, admin)
    assert err.status_code == 404
    assert err.detail == "Agent not found"


@pytest.mark.parametrize("dep_name,kw", _READ_DEPS + _OWNER_DEPS)
def test_owner_passes(seeded, dep_name, kw):
    """Positive control — the real owner is authorized and the name is returned."""
    import dependencies

    dep = getattr(dependencies, dep_name)
    owner = _user(_OWNER)
    assert dep(**{kw: _AGENT, "current_user": owner}) == _AGENT


def test_shared_reader_on_owner_endpoint_404s_not_403(seeded):
    """A shared reader hitting an owner-only dependency now gets a uniform 404
    (accepted UX cost, enumeration-safe) — never a distinguishing 403."""
    import dependencies

    # No share row seeded — the stranger is a plain non-owner; the owner deps
    # must 404 identically whether the agent exists or not.
    stranger = _user(_STRANGER)
    err = _denial(dependencies.get_owned_agent_by_name, "agent_name", _AGENT, stranger)
    assert err.status_code == 404
    assert err.detail == "Agent not found"


# =============================================================================
# 2. Email-request body/status uniformity (real handler, patched db + mailer)
# =============================================================================

_GENERIC = {
    "success": True,
    "message": "If your email is registered, you'll receive a code shortly",
}


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _run_email_request(email, *, whitelisted, recent_requests, monkeypatch):
    """Drive request_email_login_code once; return (response, sent_codes)."""
    import routers.auth as auth_mod
    import services.email_service as email_service_mod

    sent: list = []

    class _FakeEmailService:
        async def send_verification_code(self, target, code, context_label=None):
            sent.append((target, code))
            return True

    monkeypatch.setattr(auth_mod, "is_setup_completed", lambda: True)
    monkeypatch.setattr(auth_mod.db, "get_setting_value", lambda *a, **k: "true")
    monkeypatch.setattr(auth_mod.db, "is_email_whitelisted", lambda e: whitelisted)
    monkeypatch.setattr(
        auth_mod.db, "count_recent_code_requests", lambda e, minutes=10: recent_requests
    )
    monkeypatch.setattr(
        auth_mod.db,
        "create_login_code",
        lambda e, expiry_minutes=10: {"code": "123456", "expires_in_seconds": 600},
    )
    monkeypatch.setattr(email_service_mod, "EmailService", _FakeEmailService)

    async def _go():
        resp = await auth_mod.request_email_login_code(_FakeRequest({"email": email}))
        # Let the fire-and-forget dispatch task run to completion before asserting.
        await asyncio.sleep(0.05)
        return resp

    return asyncio.run(_go()), sent


def test_email_request_identical_body_whitelisted_vs_not(monkeypatch):
    """Whitelisted and non-whitelisted emails return a byte-identical body with
    no whitelisted-only fields — no membership oracle."""
    wl, wl_sent = _run_email_request(
        "known@example.com", whitelisted=True, recent_requests=0, monkeypatch=monkeypatch
    )
    non, non_sent = _run_email_request(
        "unknown@example.com", whitelisted=False, recent_requests=0, monkeypatch=monkeypatch
    )

    assert wl == non == _GENERIC
    assert "expires_in_seconds" not in wl
    # The whitelisted path actually dispatched a code; the non-whitelisted did not.
    assert wl_sent and not non_sent


def test_email_request_over_limit_no_429_differential(monkeypatch):
    """A whitelisted email over the rate limit returns the SAME generic 200 body
    (not a 429) and does NOT dispatch a code — no status/timing oracle."""
    resp, sent = _run_email_request(
        "known@example.com", whitelisted=True, recent_requests=99, monkeypatch=monkeypatch
    )
    assert resp == _GENERIC
    assert not sent  # over-limit suppresses the send (WARN-logged server-side)


# =============================================================================
# 3. Tier-4 — agent_config GET handlers now carry the access dependency
# =============================================================================

def test_tier4_config_gets_are_access_gated():
    """The four previously-open GET handlers must bind get_authorized_agent_by_name
    on their agent_name param — closes the authz hole + 404-vs-200 oracle (#186)."""
    import routers.agent_config as agent_config
    from dependencies import get_authorized_agent_by_name

    handlers = [
        agent_config.get_agent_capabilities,
        agent_config.get_agent_timeout,
        agent_config.get_public_channel_model,
        agent_config.get_agent_guardrails,
    ]
    for fn in handlers:
        ann = fn.__annotations__["agent_name"]
        meta = getattr(ann, "__metadata__", ())
        deps = [getattr(m, "dependency", None) for m in meta]
        assert get_authorized_agent_by_name in deps, (
            f"{fn.__name__} is not bound to get_authorized_agent_by_name"
        )


# =============================================================================
# 4. nevermined.py router-level access helpers — uniform 404 (Tier 3)
# =============================================================================
#
# nevermined.py keeps its OWN module-level _require_read_access /
# _require_write_access — they predate the dependency helpers and gate the
# payment-config endpoints directly. #186 converted their historic
# 404-then-403 split into a single uniform 404, and the follow-up removed the
# read-path short-circuit so the query count (hence timing) is identical for
# the non-existent and the existing-but-inaccessible case. These guard the
# SIBLING codepath the dependency-helper tests above don't reach (incomplete-fix
# escape class): both helpers must stay enumeration-safe, and the write helper
# must still enforce owner-only.
#
# _agent_exists() calls services.docker_service.get_agent_by_name (Docker) then
# falls back to the real db.get_agent_owner. Only the Docker probe is stubbed
# (to "no container") — every access-check db call under test stays real.


@pytest.fixture
def _no_docker(monkeypatch):
    """Stub only the Docker existence probe so _agent_exists resolves purely
    from the real db.get_agent_owner. Keeps the db access-check calls real.

    First pin the real ``services`` / ``services.docker_service`` modules back
    into ``sys.modules`` (a sibling unit test can leave them as Mocks under some
    pytest-randomly orderings — see the module-level capture above), so
    ``_agent_exists``'s function-local ``from services.docker_service import
    get_agent_by_name`` deterministically resolves the real module. Then stub the
    probe to report "no container" — with a leaked Mock in place the probe would
    otherwise return a truthy Mock and a non-existent agent would read as
    existing, defeating the enumeration-uniformity 404 (#186)."""
    monkeypatch.setitem(sys.modules, "services", _REAL_SERVICES_PKG)
    monkeypatch.setitem(sys.modules, "services.docker_service", _REAL_DOCKER_SERVICE)
    monkeypatch.setattr(
        _REAL_DOCKER_SERVICE, "get_agent_by_name", lambda name: None, raising=False
    )


def _seed_shared_reader(email: str = "stranger@example.com") -> None:
    """Give the stranger an email and a share row on _AGENT so
    can_user_access_agent is True (shared) while can_user_share_agent is False."""
    run("UPDATE users SET email = :e WHERE id = :id", e=email, id=_STRANGER_ID)
    run(
        "INSERT INTO agent_sharing "
        "(agent_name, shared_with_email, shared_by_id, created_at) "
        "VALUES (:a, :e, :o, :n)",
        a=_AGENT, e=email.lower(), o=_OWNER_ID, n="2026-01-01T00:00:00Z",
    )


def _nvm_denial(fn, name, user):
    """Invoke a nevermined helper and return the raised HTTPException."""
    import routers.nevermined as nvm

    with pytest.raises(HTTPException) as exc:
        getattr(nvm, fn)(name, user)
    return exc.value


@pytest.mark.parametrize("helper", ["_require_read_access", "_require_write_access"])
def test_nvm_nonexistent_and_inaccessible_are_byte_identical(seeded, _no_docker, helper):
    """Both nevermined access helpers must raise an IDENTICAL 404 for a
    non-existent agent and an existing-but-inaccessible one — no 404-vs-403
    existence oracle on the payment-config surface (#186 Tier 3)."""
    stranger = _user(_STRANGER)
    missing = _nvm_denial(helper, _MISSING, stranger)
    inaccessible = _nvm_denial(helper, _AGENT, stranger)

    assert missing.status_code == inaccessible.status_code == 404
    assert missing.detail == inaccessible.detail == "Agent not found"


@pytest.mark.parametrize("helper", ["_require_read_access", "_require_write_access"])
def test_nvm_admin_on_nonexistent_still_404s(seeded, _no_docker, helper):
    """An admin passes can_user_access/share for ANY name, so the existence
    check must still 404 an admin against a non-existent agent (no 200-vs-404
    oracle for admins)."""
    admin = _user(_ADMIN, role="admin")
    err = _nvm_denial(helper, _MISSING, admin)
    assert err.status_code == 404
    assert err.detail == "Agent not found"


@pytest.mark.parametrize("helper", ["_require_read_access", "_require_write_access"])
def test_nvm_owner_passes(seeded, _no_docker, helper):
    """Positive control — the owner clears both read and write helpers."""
    import routers.nevermined as nvm

    getattr(nvm, helper)(_AGENT, _user(_OWNER))  # no raise


def test_nvm_shared_reader_reads_but_cannot_write(seeded, _no_docker):
    """A shared (non-owner) reader clears _require_read_access but _require_write_access
    still 404s — the write path keeps owner-only enforcement while the denial stays
    a uniform 404 (never a distinguishing 403). Sibling-path completeness for the
    read-vs-write asymmetry."""
    import routers.nevermined as nvm

    _seed_shared_reader()
    stranger = _user(_STRANGER)

    nvm._require_read_access(_AGENT, stranger)  # shared → allowed, no raise

    err = _nvm_denial("_require_write_access", _AGENT, stranger)
    assert err.status_code == 404
    assert err.detail == "Agent not found"


# =============================================================================
# 4. avatar.py path containment — agent_name (a URL path param) used to build
# filesystem paths under AVATAR_DIR. The mutating handlers authorize via
# OwnedAgentByName (#186), which hides the existence/ownership constraint from
# CodeQL's py/path-injection tracking; _avatar_path restores an explicit
# normpath + within-root barrier (defense-in-depth + static-analysis). Uniform
# 404 on escape keeps the no-existence-oracle contract.
# =============================================================================


def test_avatar_path_builds_within_root_for_valid_name():
    from routers.avatar import AVATAR_DIR, _avatar_path

    p = _avatar_path("my-agent_1.2", "_ref.png")
    assert p == AVATAR_DIR / "my-agent_1.2_ref.png"


@pytest.mark.parametrize(
    "bad",
    ["../etc/passwd", "/abs/evil", "../../root", "../..", "a/../../b"],
)
def test_avatar_path_rejects_escape_with_404(bad):
    """Any agent_name whose normalized path leaves AVATAR_DIR is a uniform 404."""
    from routers.avatar import _avatar_path

    with pytest.raises(HTTPException) as exc:
        _avatar_path(bad, "_ref.png")
    assert exc.value.status_code == 404
    assert exc.value.detail == "Agent not found"
