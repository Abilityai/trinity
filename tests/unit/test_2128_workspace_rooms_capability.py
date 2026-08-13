"""The Workspace roster tells the client whether a chat may hold MORE THAN ONE
agent (#2128).

`PortalAgentPicker` lets a Workspace client select several agents, captioned
"they will share this conversation". Two or more routes to `POST /api/rooms`,
which a community build does not serve at all — so the affordance was always
offered and could never work, dead-ending in a generic "Could not start that
chat."

The signal cannot come from the existing frontend entitlement store: that reads
`GET /api/settings/feature-flags`, which is platform-JWT gated, while the whole
point of the Workspace is that an external client reaches it on an email-OTP
session with no platform account. So the roster carries the bit.

What these tests pin, in order of how badly each would fail:

  1. the bit is derived from the ROOMS feature id specifically, not from
     "is anything enterprise registered" — a different module must not turn it on;
  2. it fails CLOSED on every unreadable state, because the error direction that
     matters is "offer less";
  3. it reads the LIVE singleton, so the supported test seam is observed — a
     module-scope import would freeze the boot-time instance;
  4. the field is additive: nothing else on the roster moves.

Runs against a throwaway sqlite seeded with the OSS tables the roster joins over
(the `test_ent357_workspace_owned_roster.py` harness).
"""
from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.unit

ROOMS_FEATURE_ID = "shared_sessions"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

@pytest.fixture()
def roster_db(tmp_path, monkeypatch):
    """Fresh sqlite with the OSS tables the roster joins over."""
    db_file = tmp_path / "trinity-rooms-capability.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.engine import get_engine
    from db.tables import (
        metadata as oss_metadata, agent_ownership, agent_sharing, system_settings, users,
    )
    # `system_settings` too: `get_roster` does a function-local
    # `from services import tts_service` for its voice check, so the real
    # settings read runs against this database.
    oss_metadata.create_all(
        get_engine(), tables=[users, agent_ownership, agent_sharing, system_settings]
    )
    yield get_engine()


@pytest.fixture()
def entitlements(monkeypatch):
    """A real, deterministic entitlement service, swapped through the supported
    seam and restored afterwards.

    B9 — the fixture pins `sys.modules["services.entitlement_service"]` to the
    module it just imported, instead of trusting a bare import to return the
    real thing.

    Three other test files replace that key with a bare `MagicMock()`. All three
    restore it (`patch.dict(...).stop()` in a fixture `finally`, plus
    `monkeypatch.delitem`), so there is no live leak today — measured on the full
    unit tier, not assumed. But a mock's `is_entitled` returns a mock, and
    pydantic coerces one to True on a bool field, so IF a future file ever left
    one behind, the capability would read *available* on a community build. The
    identity assertion below is what makes that fail loudly here rather than go
    green on a lie (verified by planting exactly that leak).
    """
    import importlib

    module = importlib.import_module("services.entitlement_service")
    monkeypatch.setitem(sys.modules, "services.entitlement_service", module)

    original = module.entitlement_service
    try:
        yield module
    finally:
        module._set_for_testing(original)


def _install(entitlements, *feature_ids, oss_only=False, monkeypatch=None):
    """Install a fresh service with exactly `feature_ids` registered.

    `oss_only` constructs the service AFTER setting the env var, because
    `_oss_only` is read once in `__init__` — setting the variable on an
    already-built instance does nothing at all, and a test written the obvious
    way would pass for the wrong reason.
    """
    if oss_only:
        assert monkeypatch is not None
        monkeypatch.setenv("TRINITY_OSS_ONLY", "1")
    svc = entitlements.EntitlementService()
    for fid in feature_ids:
        svc.register_module(fid)
    entitlements._set_for_testing(svc)
    return svc


def _seed(engine, *, email="client@example.com"):
    from db.tables import agent_ownership, agent_sharing, users
    now = "2026-08-12T00:00:00Z"
    with engine.begin() as conn:
        conn.execute(users.insert().values(
            id=1, username="owner", email="owner@example.com", role="user",
            created_at=now, updated_at=now,
        ))
        for name in ("scout", "sage"):
            conn.execute(agent_ownership.insert().values(
                agent_name=name, owner_id=1, created_at=now, is_system=0, deleted_at=None,
            ))
            conn.execute(agent_sharing.insert().values(
                agent_name=name, shared_with_email=email, shared_by_id=1, created_at=now,
            ))
    return email


# ---------------------------------------------------------------------------
# B1-B4 — the bit is derived from the ROOMS id, and only from it
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_registered_rooms_module_reports_available(roster_db, entitlements, monkeypatch):
    """B1 — an entitled instance offers multi-agent chat."""
    monkeypatch.delenv("TRINITY_OSS_ONLY", raising=False)
    _install(entitlements, ROOMS_FEATURE_ID)
    email = _seed(roster_db)

    from client_portal import service
    roster = await service.get_roster(email)
    assert roster.multi_agent_chat_available is True


@pytest.mark.asyncio
async def test_empty_registry_reports_unavailable(roster_db, entitlements, monkeypatch):
    """B2 — a community build: nothing registered, so nothing is offered."""
    monkeypatch.delenv("TRINITY_OSS_ONLY", raising=False)
    _install(entitlements)
    email = _seed(roster_db)

    from client_portal import service
    roster = await service.get_roster(email)
    assert roster.multi_agent_chat_available is False


@pytest.mark.asyncio
async def test_a_different_module_does_not_turn_it_on(roster_db, entitlements, monkeypatch):
    """B3 — id-specific, not "is anything enterprise present".

    The frontend's own gate is `hasAnyEnterprise` in places; copying that shape
    here would advertise rooms on any instance carrying any other paid module.
    """
    monkeypatch.delenv("TRINITY_OSS_ONLY", raising=False)
    _install(entitlements, "a2a")
    email = _seed(roster_db)

    from client_portal import service
    roster = await service.get_roster(email)
    assert roster.multi_agent_chat_available is False


@pytest.mark.asyncio
async def test_oss_only_lockdown_reports_unavailable(roster_db, entitlements, monkeypatch):
    """B4 — `TRINITY_OSS_ONLY=1` wins over a registered module.

    The service is constructed AFTER `setenv` on purpose: `_oss_only` is read
    once in `__init__`, so setting the variable against the live singleton is
    inert and the test would pass without proving anything.
    """
    _install(entitlements, ROOMS_FEATURE_ID, oss_only=True, monkeypatch=monkeypatch)
    email = _seed(roster_db)

    from client_portal import service
    roster = await service.get_roster(email)
    assert roster.multi_agent_chat_available is False


# ---------------------------------------------------------------------------
# B5 — fail closed, and never at the roster's expense
# ---------------------------------------------------------------------------

class _Raises:
    def __init__(self, exc):
        self._exc = exc

    def is_entitled(self, feature_id):
        raise self._exc


class _NoSuchMethod:
    """A registry object that has no `is_entitled` at all — the second silent
    shape (AttributeError), not just an exception from inside the call."""


@pytest.mark.parametrize("broken, label", [
    (_Raises(RuntimeError("registry exploded")), "raises"),
    (_NoSuchMethod(), "attribute missing"),
])
@pytest.mark.asyncio
async def test_an_unreadable_registry_fails_closed(
    roster_db, entitlements, monkeypatch, broken, label
):
    """B5a/B5b — unreadable is not "available", and the roster still loads.

    Fail-open here would reintroduce the exact bug: a picker offering an
    affordance that dead-ends.
    """
    monkeypatch.delenv("TRINITY_OSS_ONLY", raising=False)
    entitlements._set_for_testing(broken)
    email = _seed(roster_db)

    from client_portal import service
    roster = await service.get_roster(email)
    assert roster.multi_agent_chat_available is False, label
    assert sorted(a.name for a in roster.agents) == ["sage", "scout"], (
        "a capability read failure emptied the roster"
    )


# ---------------------------------------------------------------------------
# B6-B7 — the field is additive and safe by default
# ---------------------------------------------------------------------------

def test_the_model_default_is_unavailable():
    """B6 — an older backend, a partial payload or a hand-built model must not
    advertise the affordance."""
    from client_portal.models import PortalRoster

    assert PortalRoster(agents=[]).multi_agent_chat_available is False


@pytest.mark.asyncio
@pytest.mark.parametrize("registered", [(), (ROOMS_FEATURE_ID,)])
async def test_the_rest_of_the_roster_is_unchanged(
    roster_db, entitlements, monkeypatch, registered
):
    """B7 — additive in BOTH arms: agents, email and per-agent capabilities are
    identical whether or not rooms are available."""
    monkeypatch.delenv("TRINITY_OSS_ONLY", raising=False)
    _install(entitlements, *registered)
    email = _seed(roster_db)

    from client_portal import service
    roster = await service.get_roster(email)

    assert roster.client_email == email
    assert sorted(a.name for a in roster.agents) == ["sage", "scout"]
    assert all(a.voice_available is False for a in roster.agents)


# ---------------------------------------------------------------------------
# B8-B9 — the seam really is observed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_roster_observes_a_singleton_swapped_after_import(
    roster_db, entitlements, monkeypatch
):
    """B8 — the helper reads the LIVE singleton.

    `_set_for_testing` rebinds a MODULE GLOBAL. A module-scope
    `from services.entitlement_service import entitlement_service` would bind the
    boot-time instance once and never see the swap — so this test is what makes
    the function-local import load-bearing rather than stylistic. It swaps twice
    against one already-imported `client_portal.service` so a cached binding
    cannot satisfy both halves.
    """
    monkeypatch.delenv("TRINITY_OSS_ONLY", raising=False)
    email = _seed(roster_db)
    from client_portal import service

    _install(entitlements, ROOMS_FEATURE_ID)
    assert (await service.get_roster(email)).multi_agent_chat_available is True

    _install(entitlements)
    assert (await service.get_roster(email)).multi_agent_chat_available is False


def test_the_registry_module_under_test_is_the_real_one(entitlements):
    """B9 — identity check, not a bare import.

    If a leaked `MagicMock` were sitting in `sys.modules` for this key, every
    assertion above would be measuring the mock. Assert the module is real
    before trusting any of them.
    """
    from unittest.mock import Mock

    module = sys.modules["services.entitlement_service"]
    assert module is entitlements
    assert not isinstance(module, Mock), (
        "services.entitlement_service is a leaked test stub — a mock's "
        "is_entitled() returns a mock, which pydantic coerces to True"
    )
    assert hasattr(module, "EntitlementService")
    assert hasattr(module, "_set_for_testing")
