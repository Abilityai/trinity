"""The Workspace roster tells the client whether a chat may hold MORE THAN ONE
agent (#2128) — and since ent#443 the answer is unconditionally yes.

`PortalAgentPicker` lets a Workspace client select several agents, captioned
"they will share this conversation". Two or more routes to `POST /api/rooms`,
which a community build did not serve at all — so the affordance was offered and
could never work. #2128 fixed that by making the roster carry a capability bit
derived from the `shared_sessions` entitlement; ent#443 moved that module into
OSS core, so the bit is now true on every build and the entitlement read is gone.

The signal still cannot come from the frontend entitlement store: that reads
`GET /api/settings/feature-flags`, which is platform-JWT gated, while the whole
point of the Workspace is that an external client reaches it on an email-OTP
session with no platform account. So the roster keeps carrying the bit — the
field is the portal's only capability channel, and the shipped bundle gates the
picker, five room store actions and `/workspace/r/:roomId` on it.

What these tests pin now, in order of how badly each would fail:

  1. an OSS build with NOTHING registered reports the capability available —
     the exact inversion of the pre-ent#443 behaviour, and the regression that
     would silently undo the move;
  2. `TRINITY_OSS_ONLY=1` does not hide it either, since it is no longer an
     entitled module;
  3. the helper does not consult the entitlement registry at all — a registry
     that raises must not change the answer;
  4. the client-side model default stays False, so an OLDER backend that omits
     the field still fails closed on a newer bundle;
  5. the field is additive: nothing else on the roster moves.

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

@pytest.fixture(autouse=True)
def _pin_container_state(monkeypatch):
    """#2196: pin the container-state seams and quiet the briefing.

    `get_roster` now projects container state onto every card, and the real
    seam reads whatever Docker the machine happens to have (conftest re-imports
    `services.docker_service` after every test, re-running `docker.from_env()`).
    On a Docker-less machine every card reads `unknown`, which makes the
    briefing ATTEMPT its HTTP call — so a capability-bit test would make real
    network attempts to `agent-{name}:8000`. This file is about the rooms
    capability bit; both reads are pinned inert.
    """
    from client_portal import service

    async def _map(names):
        return {n: "ready" for n in names}

    async def _one(name):
        return "ready"

    async def _briefing(name, availability="ready"):
        return (None, [])

    monkeypatch.setattr(service, "_availability_map", _map)
    monkeypatch.setattr(service, "_agent_availability", _one)
    monkeypatch.setattr(service, "_agent_briefing", _briefing)


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
# B1-B3 — the capability is unconditional since ent#443
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_oss_build_with_nothing_registered_reports_available(
    roster_db, entitlements, monkeypatch
):
    """B1 — THE regression test for ent#443.

    Before the move this exact arrangement (no module registered — a community
    build) reported `False`, and the Workspace picker was single-select. Rooms
    are OSS core now, so an install with an empty entitlement registry must
    offer multi-agent chat. A change that reintroduces an entitlement read here
    silently un-ships the move.
    """
    monkeypatch.delenv("TRINITY_OSS_ONLY", raising=False)
    _install(entitlements)
    email = _seed(roster_db)

    from client_portal import service
    roster = await service.get_roster(email)
    assert roster.multi_agent_chat_available is True


@pytest.mark.asyncio
async def test_oss_only_lockdown_does_not_hide_rooms(
    roster_db, entitlements, monkeypatch
):
    """B2 — `TRINITY_OSS_ONLY=1` hard-empties the entitlement registry, and that
    must no longer touch rooms.

    The service is constructed AFTER `setenv` on purpose: `_oss_only` is read
    once in `__init__`, so setting the variable against the live singleton is
    inert and the test would pass without proving anything.
    """
    _install(entitlements, oss_only=True, monkeypatch=monkeypatch)
    email = _seed(roster_db)

    from client_portal import service
    roster = await service.get_roster(email)
    assert roster.multi_agent_chat_available is True


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
async def test_the_registry_is_not_consulted_at_all(
    roster_db, entitlements, monkeypatch, broken, label
):
    """B3 — a registry that cannot answer must not change the answer.

    This is the behavioural half of "the entitlement read is gone": under the
    pre-ent#443 helper both of these shapes produced `False` (fail-closed, which
    was correct then). If either one flips the bit now, something is reading the
    registry again — the read this move deleted.
    """
    monkeypatch.delenv("TRINITY_OSS_ONLY", raising=False)
    entitlements._set_for_testing(broken)
    email = _seed(roster_db)

    from client_portal import service
    roster = await service.get_roster(email)
    assert roster.multi_agent_chat_available is True, label
    assert sorted(a.name for a in roster.agents) == ["sage", "scout"], (
        "a capability read emptied the roster"
    )


def test_the_helper_holds_no_entitlement_read():
    """B3c — the source guard behind B3.

    `_multi_agent_chat_available` used to resolve the singleton function-locally
    so the test seam was observed. Now it must resolve nothing: a re-introduced
    `is_entitled` call would be invisible to the behavioural tests above the
    moment someone also re-adds a fail-open default.
    """
    import inspect

    from client_portal import service

    src = inspect.getsource(service._multi_agent_chat_available)
    assert "is_entitled" not in src
    assert "entitlement_service" not in src


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
# B8 — the fixture harness itself is honest
# ---------------------------------------------------------------------------

def test_the_registry_module_under_test_is_the_real_one(entitlements):
    """B8 — identity check, not a bare import.

    If a leaked `MagicMock` were sitting in `sys.modules` for this key, the
    "registry is not consulted" tests above would be measuring the mock rather
    than the real registry, and would pass for the wrong reason. Assert the
    module is real before trusting any of them.

    Kept after ent#443 even though the helper no longer reads the registry:
    these tests prove a NEGATIVE (nothing consults it), and a negative measured
    against a stub proves nothing at all.
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
