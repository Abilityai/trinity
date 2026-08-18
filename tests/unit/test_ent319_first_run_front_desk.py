"""Unit tests for the first-run front-desk state (ent#319, epic ent#54).

The whole feature rests on one predicate — *is this still a seed-only install
for the calling user* — and that predicate is easy to get wrong in exactly two
directions, both covered here:

  * **False negative** (the reason this issue exists): since ent#124 seeds a
    fleet on first run, "zero agents" is permanently false, so a browser-side
    freshness check never fires again. The seed is deployed under the admin
    account (`SEED_OWNER`), so no actor-based check can tell it apart either —
    only the seed's own naming contract can.
  * **False positive**, the worse one: a card that appears over somebody's
    mature fleet. Hence every failure path resolves to `first_run: False`.

True unit tests: no Docker, no Redis, no backend. The db seam and the manifest
resolution are patched.
"""
from __future__ import annotations

import pytest

import services.onboarding_service as onboarding_service
import services.system_seed_service as sss
from models import User

pytestmark = pytest.mark.unit

CORNELIUS = "cornelius"

MANIFEST = """
name: acme
description: test fleet
agents:
  scout:
    template: local:scout
  sage:
    template: local:sage
"""


@pytest.fixture()
def admin_user():
    return User(id=1, username="admin", email="admin@example.com", role="admin")


@pytest.fixture()
def plain_user():
    return User(id=2, username="bob", email="bob@example.com", role="user")


@pytest.fixture(autouse=True)
def bundled_manifest(monkeypatch):
    """Resolve the manifest to a fixed two-agent fleet, override-free."""
    monkeypatch.delenv(sss.MANIFEST_ENV_VAR, raising=False)
    monkeypatch.setattr(
        sss.system_seed_service, "_resolve_manifest",
        lambda override_raw: (MANIFEST, "bundled", None),
    )


def _patch_db(monkeypatch, *, user_row, metadata):
    fake = type("FakeDb", (), {})()
    fake.get_user_by_username = lambda username: user_row
    fake.get_all_agent_metadata = lambda email=None: metadata
    monkeypatch.setattr(onboarding_service, "db", fake)


def _meta(owner_id=1, is_system=False, shared=False):
    return {"owner_id": owner_id, "is_system": is_system, "is_shared_with_user": shared}


ADMIN_ROW = {"id": 1, "username": "admin", "email": "admin@example.com", "role": "admin"}
USER_ROW = {"id": 2, "username": "bob", "email": "bob@example.com", "role": "user"}


# --- seeded-name derivation -------------------------------------------------

def test_seeded_names_are_system_prefixed_plus_cornelius():
    names = onboarding_service.get_seeded_agent_names()
    assert names == {CORNELIUS, "acme-scout", "acme-sage"}


def test_disabled_seeding_still_knows_cornelius(monkeypatch):
    """Cornelius is seeded by its own service, not the manifest — disabling the
    manifest must not make it read as a user-created agent."""
    monkeypatch.setenv(sss.MANIFEST_ENV_VAR, "disabled")
    assert onboarding_service.get_seeded_agent_names() == {CORNELIUS}


def test_unresolvable_manifest_degrades_to_cornelius_only(monkeypatch):
    monkeypatch.setattr(
        sss.system_seed_service, "_resolve_manifest",
        lambda override_raw: (None, "override", "cannot read '/nope': missing"),
    )
    assert onboarding_service.get_seeded_agent_names() == {CORNELIUS}


def test_operator_override_names_follow_their_own_manifest(monkeypatch):
    monkeypatch.setenv(sss.MANIFEST_ENV_VAR, "/etc/trinity/mine.yaml")
    monkeypatch.setattr(
        sss.system_seed_service, "_resolve_manifest",
        lambda override_raw: ("name: mine\nagents:\n  one:\n    template: local:scout\n", "override", None),
    )
    assert onboarding_service.get_seeded_agent_names() == {CORNELIUS, "mine-one"}


# --- the first-run predicate ------------------------------------------------

def test_seeded_only_install_is_first_run(monkeypatch, admin_user):
    """The case the browser could not see: a fleet is running, none of it yours."""
    _patch_db(monkeypatch, user_row=ADMIN_ROW, metadata={
        CORNELIUS: _meta(), "acme-scout": _meta(), "acme-sage": _meta(),
    })
    state = onboarding_service.get_first_run_state(admin_user)

    assert state["first_run"] is True
    assert state["own_agent_count"] == 0
    assert state["seeded_agents"] == ["acme-sage", "acme-scout", CORNELIUS]
    assert state["demo_agent"] == CORNELIUS


def test_one_agent_of_your_own_ends_the_first_run(monkeypatch, admin_user):
    _patch_db(monkeypatch, user_row=ADMIN_ROW, metadata={
        CORNELIUS: _meta(), "acme-scout": _meta(), "my-agent": _meta(),
    })
    state = onboarding_service.get_first_run_state(admin_user)

    assert state["first_run"] is False
    assert state["own_agent_count"] == 1


def test_empty_install_is_first_run_but_has_nothing_to_show(monkeypatch, admin_user):
    """Seeding disabled / pre-ent#124 shape: still first run, but no demo agent —
    the front end uses that to stand down and leave the wizard's auto-open alone."""
    _patch_db(monkeypatch, user_row=ADMIN_ROW, metadata={})
    state = onboarding_service.get_first_run_state(admin_user)

    assert state["first_run"] is True
    assert state["seeded_agents"] == []
    assert state["demo_agent"] is None


def test_system_agents_are_not_the_users_work(monkeypatch, admin_user):
    """`trinity-system` exists on every install; counting it as someone's own
    agent would make the front desk unreachable everywhere."""
    _patch_db(monkeypatch, user_row=ADMIN_ROW, metadata={
        "trinity-system": _meta(is_system=True), CORNELIUS: _meta(),
    })
    assert onboarding_service.get_first_run_state(admin_user)["first_run"] is True


def test_non_admin_sees_only_own_and_shared(monkeypatch, plain_user):
    """A colleague's agent is not this user's first-run evidence — bob has built
    nothing, so bob is still on his first run."""
    _patch_db(monkeypatch, user_row=USER_ROW, metadata={
        CORNELIUS: _meta(owner_id=1),          # admin's seeded agent, not shared
        "alices-agent": _meta(owner_id=3),     # someone else's, not shared
    })
    state = onboarding_service.get_first_run_state(plain_user)

    assert state["first_run"] is True
    assert state["own_agent_count"] == 0
    assert state["seeded_agents"] == []        # cornelius is not visible to bob
    assert state["demo_agent"] is None


def test_shared_agent_counts_as_the_users_own_fleet(monkeypatch, plain_user):
    _patch_db(monkeypatch, user_row=USER_ROW, metadata={
        "alices-agent": _meta(owner_id=3, shared=True),
    })
    assert onboarding_service.get_first_run_state(plain_user)["first_run"] is False


def test_demo_agent_prefers_cornelius_then_falls_back(monkeypatch, admin_user):
    _patch_db(monkeypatch, user_row=ADMIN_ROW, metadata={
        "acme-scout": _meta(), "acme-sage": _meta(),
    })
    assert onboarding_service.get_first_run_state(admin_user)["demo_agent"] == "acme-sage"


# --- failure behaviour ------------------------------------------------------

def test_a_db_failure_reads_as_not_first_run(monkeypatch, admin_user):
    """Fail toward silence. A missed nudge is a non-event; a first-run card over
    a mature fleet is noise no user can turn off for the install."""
    def boom(*_a, **_kw):
        raise RuntimeError("db down")

    fake = type("FakeDb", (), {})()
    fake.get_user_by_username = boom
    fake.get_all_agent_metadata = boom
    monkeypatch.setattr(onboarding_service, "db", fake)

    state = onboarding_service.get_first_run_state(admin_user)
    assert state == {
        "first_run": False, "seeded_agents": [], "own_agent_count": 0, "demo_agent": None,
    }


def test_unknown_user_is_not_first_run(monkeypatch, admin_user):
    """No user row -> no email, no id: every metadata row would fail the
    ownership test anyway, but assert it rather than infer it."""
    _patch_db(monkeypatch, user_row=None, metadata={"my-agent": _meta()})
    assert onboarding_service.get_first_run_state(admin_user)["first_run"] is True
