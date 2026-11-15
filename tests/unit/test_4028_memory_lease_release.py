"""trinity#4028 — the memory retain lease is released through the lock's script.

`memory_service._release_lease` swallows every error, so a wrong method name
there would leave the per-scope lease held for its full TTL with nothing red.
These drive `_acquire_lease` / `_release_lease` against an executing script
emulator and assert on the store.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `_ent695_lua`
import _ent695_lua as lua  # noqa: E402

pytestmark = pytest.mark.unit

ms = pytest.importorskip("services.memory_service")


@pytest.fixture
def redis(monkeypatch):
    client = lua.LeaseRedis()
    monkeypatch.setattr(ms, "_redis", lambda: client)
    return client


def test_a_won_lease_is_released(redis):
    lock, key, token = ms._acquire_lease("agent-a", "scope-1")
    assert token and redis.store[key] == token

    ms._release_lease(lock, key, token)

    assert key not in redis.store
    assert ("script", "release", key) in redis.commands


def test_a_lease_another_worker_took_over_is_left_alone(redis):
    lock, key, token = ms._acquire_lease("agent-a", "scope-1")
    redis.store[key] = "another-worker"  # ours lapsed, a sibling re-took it

    ms._release_lease(lock, key, token)

    assert redis.store[key] == "another-worker"


def test_a_busy_lease_is_not_ours_to_release(redis):
    redis.store["memory:retain:agent-a:scope-1"] = "another-worker"
    lock, key, token = ms._acquire_lease("agent-a", "scope-1")
    assert token is None

    ms._release_lease(lock, key, token)

    assert redis.store[key] == "another-worker"
