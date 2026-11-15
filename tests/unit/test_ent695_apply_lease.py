"""trinity-enterprise#695 / trinity#4028: atomic, ownership-checked renew/release on `SingleFlightLock`.

Apply mode holds `system_apply:<name>` for the whole of a run and renews it
every TTL/3. A GET-then-EXPIRE / GET-then-DELETE pair lets a holder whose lease
lapses in the sliver between the two calls EXTEND or DELETE a successor's
lease, and two applies then write over each other. `refresh_if_owned` /
`release_if_owned` do the compare and the mutation in one Lua call (they were
the `_atomic` siblings until trinity#4028 made them the only pair).

The suite's fakeredis has no Lua, so this emulator honours `register_script`
and EXECUTES the production script TEXT through `_ent695_lua`, so a mutated
script turns these red. `race_on_get` injects a successor between a client-side GET and the
next command — the window the non-atomic methods have and a script does not
(a script's body runs inside Redis, so nothing can interleave with it).
"""
from __future__ import annotations

import sys
from pathlib import Path

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


LuaFakeRedis = lua.LeaseRedis  # the shared executing emulator (trinity#4028)


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
    assert lock.refresh_if_owned() is LeaseState.OWNED
    assert r.ttls[KEY] == TTL


def test_a_stale_holder_never_renews_a_successors_lease():
    r = LuaFakeRedis()
    stale = SingleFlightLock(KEY, TTL, client=r)
    assert stale.acquire()
    successor = _lapse_and_succeed(r)

    assert stale.refresh_if_owned() is LeaseState.LOST
    assert r.store[KEY] == successor.token
    assert r.ttls[KEY] == 17


def test_a_stale_holder_never_deletes_a_successors_lease():
    r = LuaFakeRedis()
    stale = SingleFlightLock(KEY, TTL, client=r)
    assert stale.acquire()
    successor = _lapse_and_succeed(r)

    stale.release_if_owned()
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

    stale.refresh_if_owned()

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

    stale.release_if_owned()

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

    assert lock.refresh_if_owned() is LeaseState.LOST
    assert KEY not in r.store


def test_the_holder_releases_its_own_lease():
    r = LuaFakeRedis()
    lock = SingleFlightLock(KEY, TTL, client=r)
    assert lock.acquire()
    lock.release_if_owned()
    assert KEY not in r.store


def test_a_redis_error_is_degraded_not_owned():
    r = LuaFakeRedis()
    lock = SingleFlightLock(KEY, TTL, client=r)
    assert lock.acquire()
    r.fail = True
    assert lock.refresh_if_owned() is LeaseState.DEGRADED
    lock.release_if_owned()  # never raises


def test_a_fail_open_lock_holds_nothing_to_renew_or_release():
    """`acquire()` with no client returns True and holds nothing: that is
    DEGRADED, which apply refuses on (it never runs unlocked)."""
    lock = SingleFlightLock(KEY, TTL, client=None)
    assert lock.acquire() is True and lock.held is False
    assert lock.refresh_if_owned() is LeaseState.DEGRADED
    lock.release_if_owned()


def test_neither_method_issues_a_client_side_get():
    """The window lives in a client GET followed by a second command; the only
    Redis call either method makes is its script."""
    import inspect

    for method in (SingleFlightLock.refresh_if_owned, SingleFlightLock.release_if_owned):
        src = inspect.getsource(method)
        assert "register_script(" in src
        for command in (".get(", ".expire(", ".delete(", ".set("):
            assert command not in src, f"{method.__name__} issues a client-side {command}"


# ---------------------------------------------------------------------------
# The evaluator executes the text: a mutated script changes the outcome
# ---------------------------------------------------------------------------

def _stale_and_successor():
    store, ttls = {KEY: "successor"}, {KEY: 17}
    return store, ttls


@pytest.mark.parametrize("script,args", [
    (rbu.LEASE_REFRESH_LUA, ["stale", TTL]),
    (rbu.LEASE_REFRESH_OR_RETAKE_LUA, ["stale", TTL]),
    (rbu.LEASE_RELEASE_LUA, ["stale"]),
])
def test_the_production_scripts_leave_a_successors_lease_alone(script, args):
    store, ttls = _stale_and_successor()
    assert lua.run(script, store, ttls, [KEY], args) == 0
    assert store == {KEY: "successor"} and ttls == {KEY: 17}


def test_the_retake_script_takes_only_an_absent_key():
    store, ttls = {}, {}
    assert lua.run(rbu.LEASE_REFRESH_OR_RETAKE_LUA, store, ttls, [KEY], ["mine", TTL]) == 2
    assert store == {KEY: "mine"} and ttls == {KEY: TTL}
    assert lua.run(rbu.LEASE_REFRESH_OR_RETAKE_LUA, store, ttls, [KEY], ["mine", TTL]) == 1


def _mutate(script: str, old: str, new: str) -> str:
    assert old in script, f"mutation anchor {old!r} not in the production script"
    return script.replace(old, new, 1)


@pytest.mark.parametrize("script,old,new,args", [
    # the token compare dropped / inverted
    (rbu.LEASE_REFRESH_LUA, "== ARGV[1]", "~= ARGV[1]", ["stale", TTL]),
    (rbu.LEASE_RELEASE_LUA, "== ARGV[1]", "~= ARGV[1]", ["stale"]),
    (rbu.LEASE_REFRESH_OR_RETAKE_LUA, "== ARGV[1]", "~= ARGV[1]", ["stale", TTL]),
    # the key read from the wrong slot (the compare then never matches the key it mutates)
    (rbu.LEASE_RELEASE_LUA, "redis.call('get', KEYS[1]) == ARGV[1]", "ARGV[1] == ARGV[1]", ["stale"]),
    # the re-take without NX overwrites a foreign holder
    (rbu.LEASE_REFRESH_OR_RETAKE_LUA, ", 'NX'", "", ["stale", TTL]),
])
def test_a_mutated_script_touches_a_successors_lease(script, old, new, args):
    """The evaluator is not a second implementation: change the production
    text and the successor's lease is touched."""
    store, ttls = _stale_and_successor()
    lua.run(_mutate(script, old, new), store, ttls, [KEY], args)
    assert (store, ttls) != ({KEY: "successor"}, {KEY: 17})


def test_a_construct_outside_the_subset_is_refused_not_skipped():
    with pytest.raises(lua.UnsupportedLua):
        lua.run("local v = redis.call('get', KEYS[1])\nreturn v", {}, {}, [KEY], [])


# ---------------------------------------------------------------------------
# The evaluator follows Redis/Lua where a mutated script would differ
# ---------------------------------------------------------------------------

def test_an_if_tests_lua_truth_not_python_truth():
    """In Lua 0 is true: EXPIRE on an absent key answers 0 and the branch runs."""
    src = "if redis.call('expire', KEYS[1], ARGV[1]) then\n    return 1\nend\nreturn 0"
    assert lua.run(src, {}, {}, [KEY], [TTL]) == 1


def test_a_chained_comparison_is_refused():
    src = "if redis.call('get', KEYS[1]) == ARGV[1] == ARGV[1] then\n    return 1\nend\nreturn 0"
    with pytest.raises(lua.UnsupportedLua):
        lua.run(src, {}, {}, [KEY], ["t"])


def test_expire_with_a_non_positive_ttl_deletes_the_key():
    store, ttls = {KEY: "mine"}, {KEY: 5}
    assert lua.run(rbu.LEASE_REFRESH_LUA, store, ttls, [KEY], ["mine", 0]) == 1
    assert store == {} and ttls == {}


def test_set_with_a_non_positive_ttl_is_a_script_error():
    with pytest.raises(lua.ScriptError):
        lua.run(rbu.LEASE_REFRESH_OR_RETAKE_LUA, {}, {}, [KEY], ["mine", 0])
