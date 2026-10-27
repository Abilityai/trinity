"""trinity-enterprise#695: atomic, ownership-checked renew/release on `SingleFlightLock`.

Apply mode holds `system_apply:<name>` for the whole of a run and renews it
every TTL/3. The existing `refresh_if_owned` / `release_if_owned` are
GET-then-EXPIRE and GET-then-DELETE: a holder whose lease lapses in the sliver
between the two calls EXTENDS or DELETES a successor's lease, and two applies
then write over each other. The atomic pair does the compare and the mutation
in one Lua call.

The suite's fakeredis has no Lua, so this emulator honours `register_script`
and EXECUTES the production script TEXT through `_ent695_lua`, so a mutated
script turns these red. `race_on_get` injects a successor between a client-side GET and the
next command — the window the non-atomic methods have and a script does not
(a script's body runs inside Redis, so nothing can interleave with it).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Dict, Optional

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import redis_breaker_util as rbu  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `_ent695_lua`
import _ent695_lua as lua  # noqa: E402
from redis_breaker_util import LeaseState, SingleFlightLock  # noqa: E402

pytestmark = pytest.mark.unit

KEY = "system_apply:acme"
TTL = 900


class LuaFakeRedis:
    """SET NX EX / GET / EXPIRE / DELETE plus an EXECUTING `register_script`.

    Every command is appended to `commands`, so a test can also assert that a
    path issued nothing at all. The script resolves its target from the
    `client=` kwarg at call time: `ScriptCache` memoises registered scripts
    process-wide, so closing over the first fake would leak between tests.
    """

    def __init__(self) -> None:
        self.store: Dict[str, str] = {}
        self.ttls: Dict[str, int] = {}
        self.commands: list = []
        self.race_on_get: Optional[Callable[[], None]] = None
        self.fail = False

    def register_script(self, src: str):
        """Return a redis-py `Script`-shaped callable that EXECUTES `src`.

        The script's own text decides the outcome (`_ent695_lua.run`); this
        wrapper only records the call and honours `fail`. The target comes
        from the `client=` kwarg at call time: `ScriptCache` memoises
        registered scripts process-wide, so closing over the first fake would
        leak between tests."""
        kind = "release" if "'del'" in src else "refresh"

        def _run(keys=None, args=None, client=None):
            target = client if client is not None else self
            if target.fail:
                raise ConnectionError("redis down")
            target.commands.append(("script", kind, (args or [None])[0]))
            return lua.run(src, target.store, target.ttls, keys, args)

        return _run

    def set(self, key, value, nx=False, ex=None):
        if self.fail:
            raise ConnectionError("redis down")
        self.commands.append(("set", key))
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = int(ex)
        return True

    def get(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        self.commands.append(("get", key))
        value = self.store.get(key)
        if self.race_on_get is not None:
            hook, self.race_on_get = self.race_on_get, None
            hook()
        return value

    def expire(self, key, ttl):
        self.commands.append(("expire", key))
        if key not in self.store:
            return False
        self.ttls[key] = int(ttl)
        return True

    def delete(self, key):
        self.commands.append(("delete", key))
        self.ttls.pop(key, None)
        return 1 if self.store.pop(key, None) is not None else 0


@pytest.fixture(autouse=True)
def _fresh_script_cache():
    rbu._ATOMIC_LEASE_SCRIPTS.reset()
    yield
    rbu._ATOMIC_LEASE_SCRIPTS.reset()


def _lapse_and_succeed(r: LuaFakeRedis) -> SingleFlightLock:
    """A's lease lapses (TTL expiry, modelled as the key vanishing) and B wins."""
    r.store.pop(KEY, None)
    r.ttls.pop(KEY, None)
    successor = SingleFlightLock(KEY, TTL, client=r)
    assert successor.acquire() and successor.held
    r.ttls[KEY] = 17  # a marker: any renewal by the stale holder would move it
    return successor


def test_the_holder_renews_its_own_lease():
    r = LuaFakeRedis()
    lock = SingleFlightLock(KEY, TTL, client=r)
    assert lock.acquire() and lock.held
    r.ttls[KEY] = 5
    assert lock.refresh_if_owned_atomic() is LeaseState.OWNED
    assert r.ttls[KEY] == TTL


def test_a_stale_holder_never_renews_a_successors_lease():
    r = LuaFakeRedis()
    stale = SingleFlightLock(KEY, TTL, client=r)
    assert stale.acquire()
    successor = _lapse_and_succeed(r)

    assert stale.refresh_if_owned_atomic() is LeaseState.LOST
    assert r.store[KEY] == successor.token
    assert r.ttls[KEY] == 17


def test_a_stale_holder_never_deletes_a_successors_lease():
    r = LuaFakeRedis()
    stale = SingleFlightLock(KEY, TTL, client=r)
    assert stale.acquire()
    successor = _lapse_and_succeed(r)

    stale.release_if_owned_atomic()
    assert r.store[KEY] == successor.token


def test_a_successor_in_the_get_expire_sliver_is_not_renewed():
    """The race the non-atomic refresh has: its GET sees OUR token, the lease
    lapses and a successor wins, then its EXPIRE lands on the successor's key.

    `race_on_get` fires inside the first client-side GET. The atomic refresh
    issues none, so the window does not exist; swapping in `refresh_if_owned`
    turns this red (it reports OWNED and re-arms the successor's lease)."""
    r = LuaFakeRedis()
    stale = SingleFlightLock(KEY, TTL, client=r)
    assert stale.acquire()
    holder: dict = {}
    r.race_on_get = lambda: holder.setdefault("b", _lapse_and_succeed(r))

    stale.refresh_if_owned_atomic()

    if "b" in holder:  # the race fired inside the renewal
        assert r.store[KEY] == holder["b"].token
        assert r.ttls[KEY] == 17, "the stale holder re-armed the successor's lease"
    assert ("get", KEY) not in r.commands, "a client-side GET opens the sliver"


def test_a_successor_in_the_get_delete_sliver_is_not_deleted():
    r = LuaFakeRedis()
    stale = SingleFlightLock(KEY, TTL, client=r)
    assert stale.acquire()
    holder: dict = {}
    r.race_on_get = lambda: holder.setdefault("b", _lapse_and_succeed(r))

    stale.release_if_owned_atomic()

    if "b" in holder:  # the race fired inside the release
        assert r.store.get(KEY) == holder["b"].token, "the stale holder deleted the successor's lease"
    assert ("get", KEY) not in r.commands, "a client-side GET opens the sliver"


def test_a_lapsed_lease_is_lost_and_never_retaken():
    """Unlike `refresh_if_owned`'s REACQUIRED: someone may have held it in
    between, so a caller relying on exclusion must stop."""
    r = LuaFakeRedis()
    lock = SingleFlightLock(KEY, TTL, client=r)
    assert lock.acquire()
    r.store.pop(KEY)

    assert lock.refresh_if_owned_atomic() is LeaseState.LOST
    assert KEY not in r.store


def test_the_holder_releases_its_own_lease():
    r = LuaFakeRedis()
    lock = SingleFlightLock(KEY, TTL, client=r)
    assert lock.acquire()
    lock.release_if_owned_atomic()
    assert KEY not in r.store


def test_a_redis_error_is_degraded_not_owned():
    r = LuaFakeRedis()
    lock = SingleFlightLock(KEY, TTL, client=r)
    assert lock.acquire()
    r.fail = True
    assert lock.refresh_if_owned_atomic() is LeaseState.DEGRADED
    lock.release_if_owned_atomic()  # never raises


def test_a_fail_open_lock_holds_nothing_to_renew_or_release():
    """`acquire()` with no client returns True and holds nothing: that is
    DEGRADED, which apply refuses on (it never runs unlocked)."""
    lock = SingleFlightLock(KEY, TTL, client=None)
    assert lock.acquire() is True and lock.held is False
    assert lock.refresh_if_owned_atomic() is LeaseState.DEGRADED
    lock.release_if_owned_atomic()


def test_the_existing_methods_are_untouched():
    """Their callers (the ops loop, the seeder) keep the GET-then-EXPIRE /
    GET-then-DELETE shapes they were reviewed against."""
    import inspect

    refresh = inspect.getsource(SingleFlightLock.refresh_if_owned)
    release = inspect.getsource(SingleFlightLock.release_if_owned)
    assert "self._client.get(self._key)" in refresh and "_ATOMIC" not in refresh
    assert "self._client.get(self._key)" in release and "_ATOMIC" not in release


# ---------------------------------------------------------------------------
# The evaluator executes the text: a mutated script changes the outcome
# ---------------------------------------------------------------------------

def _stale_and_successor():
    store, ttls = {KEY: "successor"}, {KEY: 17}
    return store, ttls


@pytest.mark.parametrize("script,args", [
    (rbu._ATOMIC_REFRESH_LUA, [KEY, "stale", TTL]),
    (rbu._ATOMIC_RELEASE_LUA, [KEY, "stale"]),
])
def test_the_production_scripts_leave_a_successors_lease_alone(script, args):
    store, ttls = _stale_and_successor()
    assert lua.run(script, store, ttls, [], args) == 0
    assert store == {KEY: "successor"} and ttls == {KEY: 17}


@pytest.mark.parametrize("mutated", [
    "return redis.call('expire', ARGV[1], ARGV[3])",                         # compare dropped
    "if redis.call('get', ARGV[1]) ~= ARGV[2] then\n    return redis.call('expire', ARGV[1], ARGV[3])\nend\nreturn 0",
])
def test_a_script_without_its_token_compare_would_renew_a_successor(mutated):
    """The evaluator is not a second implementation: change the text and the
    outcome changes. (Sanity for the harness, the reason it exists.)"""
    store, ttls = _stale_and_successor()
    assert lua.run(mutated, store, ttls, [], [KEY, "stale", TTL]) == 1
    assert ttls[KEY] == TTL


def test_a_construct_outside_the_subset_is_refused_not_skipped():
    with pytest.raises(lua.UnsupportedLua):
        lua.run("local v = redis.call('get', ARGV[1])\nreturn v", {}, {}, [], [KEY])
