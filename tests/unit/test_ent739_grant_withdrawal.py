"""abilityai/trinity-enterprise#739 — withdrawing an agent_permissions edge
withdraws the access it granted, not only the next peer call.

Two things the same table gates were checked only when the grant was used the
first time, never again:

1. **Shared-folder consume mounts.** `check_shared_folder_mounts_match` (the
   drift check behind a start-time recreate) checked consume mounts in one
   direction only — "permitted but not mounted". A mount that was present but
   no longer permitted (or present while consume was disabled) was never seen
   as drift, so it survived every restart. It is now two-directional, like the
   expose branch beside it: the next start recreates the container without it.
2. **Event subscriptions.** Permission was checked when the subscription was
   created and never at delivery, so an ex-subscriber kept being woken (under
   the admin loopback) after the grant was gone. `trigger_subscription` now
   re-reads the edge per delivery, with the create-time rule: self, or a row.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

SHARED_OUT = "/home/developer/shared-out"


def _in(src):
    return f"/home/developer/shared-in/{src}"


# --- leg 1: mounts --------------------------------------------------------------

class _Db:
    """The four reads the drift check makes, over a mutable permission set."""

    def __init__(self, *, expose=False, consume=True, exposing=("alpha", "beta"), permitted=("alpha", "beta")):
        self.cfg = SimpleNamespace(expose_enabled=expose, consume_enabled=consume)
        self.exposing = list(exposing)
        self.permitted = set(permitted)

    def get_shared_folder_config(self, name):
        return self.cfg

    def get_available_shared_folders(self, name):
        # The real accessor filters exposing agents through is_permitted.
        return [a for a in self.exposing if a in self.permitted]

    def get_shared_mount_path(self, src):
        return _in(src)

    def get_shared_volume_name(self, src):
        return f"agent-{src}-shared"


def _container(*dests):
    return SimpleNamespace(attrs={"Mounts": [{"Destination": d} for d in dests]})


@pytest.fixture
def helpers(monkeypatch):
    try:
        from services.agent_service import helpers as h
    except ImportError:
        pytest.skip("backend venv required")
    monkeypatch.setattr(h, "volume_get", AsyncMock(return_value=object()))
    return h


def _match(h, monkeypatch, db, container):
    monkeypatch.setattr(h, "db", db)
    return asyncio.run(h.check_shared_folder_mounts_match(container, "consumer"))


def test_mounts_that_match_the_grants_are_not_drift(helpers, monkeypatch):
    assert _match(helpers, monkeypatch, _Db(), _container(_in("alpha"), _in("beta"))) is True


def test_a_withdrawn_grant_makes_its_mount_drift(helpers, monkeypatch):
    db = _Db()
    db.permitted.discard("beta")                      # the owner withdrew consumer -> beta
    assert _match(helpers, monkeypatch, db, _container(_in("alpha"), _in("beta"))) is False


def test_consume_disabled_with_mounts_left_is_drift(helpers, monkeypatch):
    db = _Db(consume=False)
    assert _match(helpers, monkeypatch, db, _container(_in("alpha"))) is False


def test_a_permitted_but_missing_mount_is_still_drift(helpers, monkeypatch):
    assert _match(helpers, monkeypatch, _Db(), _container(_in("alpha"))) is False


def test_a_source_whose_volume_is_gone_is_not_expected(helpers, monkeypatch):
    """A permitted source with no volume yet is skipped, as before — not drift."""
    async def vol(name):
        if name == "agent-beta-shared":
            raise LookupError("no volume")
        return object()
    monkeypatch.setattr(helpers, "volume_get", vol)
    assert _match(helpers, monkeypatch, _Db(), _container(_in("alpha"))) is True


def test_the_expose_mount_is_not_mistaken_for_a_consume_mount(helpers, monkeypatch):
    db = _Db(expose=True, permitted=())
    assert _match(helpers, monkeypatch, db, _container(SHARED_OUT)) is True


# --- leg 2: event delivery ------------------------------------------------------

class _Loopback:
    posts: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        _Loopback.posts.append(url)
        return SimpleNamespace(status_code=200, text="")


@pytest.fixture
def eds(monkeypatch):
    try:
        from services import event_dispatch_service as m
    except ImportError:
        pytest.skip("backend venv required")
    monkeypatch.setattr(m, "_within_fire_budget", AsyncMock(return_value=True))
    _Loopback.posts.clear()
    return m


def _deliver(eds, monkeypatch, *, subscriber="orch", source="worker-a", permitted):
    calls = []

    def is_permitted(a, b):
        calls.append((a, b))
        if isinstance(permitted, Exception):
            raise permitted
        return permitted

    monkeypatch.setattr(eds.db, "is_agent_permitted", is_permitted)
    sub = SimpleNamespace(id="s1", subscriber_agent=subscriber, source_agent=source,
                          event_type="done", target_message="go")
    event = SimpleNamespace(id="e1", source_agent=source, event_type="done", payload=None)
    with patch("httpx.AsyncClient", _Loopback):
        asyncio.run(eds.trigger_subscription(sub, event, agent_originated=True))
    return calls


def test_a_permitted_subscription_still_delivers(eds, monkeypatch):
    calls = _deliver(eds, monkeypatch, permitted=True)
    assert calls == [("orch", "worker-a")]            # subscriber -> source, the create-time edge
    assert len(_Loopback.posts) == 1


def test_a_withdrawn_grant_no_longer_wakes_the_ex_subscriber(eds, monkeypatch):
    _deliver(eds, monkeypatch, permitted=False)
    assert _Loopback.posts == []


def test_a_self_subscription_needs_no_edge(eds, monkeypatch):
    calls = _deliver(eds, monkeypatch, subscriber="worker-a", source="worker-a", permitted=False)
    assert calls == []
    assert len(_Loopback.posts) == 1


def test_an_unreadable_grant_fails_closed(eds, monkeypatch):
    _deliver(eds, monkeypatch, permitted=RuntimeError("db down"))
    assert _Loopback.posts == []
