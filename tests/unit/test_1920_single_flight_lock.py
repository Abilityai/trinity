"""#1920 / trinity#4028 — behaviour of the shared ``SingleFlightLock`` primitive.

`redis_breaker_util.SingleFlightLock` consolidates the SETNX single-flight lock
idiom that was hand-rolled in seven sync sites (ops, ephemeral, skill×2,
system_seed, cornelius, compat_fix) with divergent — and, in the
system_seed/cornelius/compat_fix constant-"1" case, buggy — behaviour.

Since trinity#4028 every renew and release is ONE Lua script (compare and
mutate inside Redis), so a holder whose lease lapsed between two client
commands can never extend or delete its successor's lease. fakeredis here has
no Lua, so the clients are `_ent695_lua.LeaseRedis` (a store whose race hook
fires only on CLIENT commands, never inside a script) and fakeredis behind
`_ent695_lua.with_lua` (its real data model, scripts executed against it).
Both execute the production script TEXT, so a mutated script turns these red.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import fakeredis
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `_ent695_lua`
import _ent695_lua as lua  # noqa: E402
from redis_breaker_util import (
    LeaseState,
    SingleFlightLock,
    reset_breaker_redis_client,
)  # noqa: E402

KEY = "test:single_flight"
TTL = 60


@pytest.fixture
def r():
    """A fresh fakeredis client per test (decode_responses=True, like the live
    breaker client) with an executing `register_script`."""
    client = lua.with_lua(fakeredis.FakeStrictRedis(decode_responses=True))
    yield client
    reset_breaker_redis_client()


def _lapse_and_succeed(r: lua.LeaseRedis) -> SingleFlightLock:
    """Our lease lapses (TTL expiry, modelled as the key vanishing) and a
    successor wins SET NX with its own token."""
    r.store.pop(KEY, None)
    r.ttls.pop(KEY, None)
    successor = SingleFlightLock(KEY, TTL, client=r)
    assert successor.acquire() and successor.held
    r.ttls[KEY] = 17  # a marker: any renewal by the stale holder would move it
    return successor


def _refresh(lock: SingleFlightLock, retake: bool) -> LeaseState:
    """The default refresh is called WITHOUT the keyword, as its callers do."""
    return (
        lock.refresh_if_owned(retake_lapsed=True) if retake else lock.refresh_if_owned()
    )


# ---------------------------------------------------------------------------
# acquire
# ---------------------------------------------------------------------------


def test_fresh_acquire_wins_and_issues_setnx(r):
    lock = SingleFlightLock(KEY, TTL, client=r)
    assert lock.acquire() is True
    assert lock.held is True
    assert r.get(KEY) == lock.token  # the token, not the constant "1"


def test_second_lock_on_a_held_key_is_busy(r):
    a = SingleFlightLock(KEY, TTL, client=r)
    assert a.acquire() is True
    b = SingleFlightLock(KEY, TTL, client=r)
    assert b.acquire() is False  # SETNX lost → busy
    assert b.held is False


def test_second_acquire_while_held_is_a_noop_no_second_setnx():
    client = MagicMock()
    client.set.return_value = True
    lock = SingleFlightLock(KEY, TTL, client=client)
    assert lock.acquire() is True
    assert lock.acquire() is True  # total state machine — no re-SETNX
    client.set.assert_called_once()


def test_client_none_fails_open_degraded_and_noops():
    """No Redis → sole-worker: acquire returns True but held stays False, so
    refresh/release never touch Redis."""
    lock = SingleFlightLock(KEY, TTL, client=None)
    assert lock.acquire() is True
    assert lock.held is False
    assert lock.refresh_if_owned() is LeaseState.DEGRADED
    assert lock.refresh_if_owned(retake_lapsed=True) is LeaseState.DEGRADED
    lock.release_if_owned()  # no raise, no client call


def test_acquire_setnx_error_fails_open():
    client = MagicMock()
    client.set.side_effect = RuntimeError("redis down")
    lock = SingleFlightLock(KEY, TTL, client=client)
    assert lock.acquire() is True  # fail-open: proceed as sole worker
    assert lock.held is False  # but NOT a real lease
    # refresh/release are no-ops (held is False) — never call the broken client.
    assert lock.refresh_if_owned() is LeaseState.DEGRADED
    lock.release_if_owned()
    client.register_script.assert_not_called()
    client.get.assert_not_called()
    client.delete.assert_not_called()


# ---------------------------------------------------------------------------
# refresh_if_owned — default: never re-takes (system_apply's contract)
# ---------------------------------------------------------------------------


def test_refresh_owned_extends_the_lease():
    r = lua.LeaseRedis()
    lock = SingleFlightLock(KEY, TTL, client=r)
    lock.acquire()
    r.ttls[KEY] = 5
    assert lock.refresh_if_owned() is LeaseState.OWNED
    assert r.store[KEY] == lock.token and r.ttls[KEY] == TTL


def test_refresh_absent_is_lost_and_never_retaken_by_default():
    """Someone may have held the lock inside the lapse, so a caller relying on
    exclusion (system apply) must stop: LOST, and the key stays absent."""
    r = lua.LeaseRedis()
    lock = SingleFlightLock(KEY, TTL, client=r)
    lock.acquire()
    r.store.pop(KEY)
    assert lock.refresh_if_owned() is LeaseState.LOST
    assert KEY not in r.store


def test_refresh_foreign_token_is_lost_and_left_alone():
    r = lua.LeaseRedis()
    lock = SingleFlightLock(KEY, TTL, client=r)
    lock.acquire()
    successor = _lapse_and_succeed(r)
    assert lock.refresh_if_owned() is LeaseState.LOST
    assert r.store[KEY] == successor.token and r.ttls[KEY] == 17


# ---------------------------------------------------------------------------
# refresh_if_owned(retake_lapsed=True) — the fleet restart's contract
# ---------------------------------------------------------------------------


def test_retake_owned_extends_the_lease():
    r = lua.LeaseRedis()
    lock = SingleFlightLock(KEY, TTL, client=r)
    lock.acquire()
    r.ttls[KEY] = 5
    assert lock.refresh_if_owned(retake_lapsed=True) is LeaseState.OWNED
    assert r.ttls[KEY] == TTL


def test_retake_absent_reacquires_with_the_same_token_and_ttl():
    r = lua.LeaseRedis()
    lock = SingleFlightLock(KEY, TTL, client=r)
    lock.acquire()
    r.store.pop(KEY)
    r.ttls.pop(KEY)
    assert lock.refresh_if_owned(retake_lapsed=True) is LeaseState.REACQUIRED
    assert r.store[KEY] == lock.token and r.ttls[KEY] == TTL


def test_retake_never_overwrites_a_foreign_holder():
    """The re-take is `SET NX` inside the script: a successor's lease is LOST
    to us, never replaced by our token."""
    r = lua.LeaseRedis()
    lock = SingleFlightLock(KEY, TTL, client=r)
    lock.acquire()
    successor = _lapse_and_succeed(r)
    assert lock.refresh_if_owned(retake_lapsed=True) is LeaseState.LOST
    assert r.store[KEY] == successor.token and r.ttls[KEY] == 17


@pytest.mark.parametrize("retake", [False, True])
def test_refresh_issues_one_script_and_no_client_command(retake):
    r = lua.LeaseRedis()
    lock = SingleFlightLock(KEY, TTL, client=r)
    lock.acquire()
    r.commands.clear()
    lock.refresh_if_owned(retake_lapsed=retake)
    assert r.commands == [("script", "retake" if retake else "refresh", KEY)]


# ---------------------------------------------------------------------------
# THE #4028 property — a successor that wins inside a client-side GET
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("retake", [False, True])
def test_a_successor_in_the_get_expire_sliver_is_not_renewed(retake):
    """The race the GET-then-EXPIRE refresh had: its GET sees OUR token, the
    lease lapses and a successor wins, then its EXPIRE lands on the successor's
    key. `race_on_get` fires inside the first CLIENT GET; the script issues
    none, so the window does not exist."""
    r = lua.LeaseRedis()
    stale = SingleFlightLock(KEY, TTL, client=r)
    assert stale.acquire()
    holder: dict = {}
    r.race_on_get = lambda: holder.setdefault("b", _lapse_and_succeed(r))

    _refresh(stale, retake)

    successor = holder.get("b") or _lapse_and_succeed(r)
    assert r.store[KEY] == successor.token
    assert r.ttls[KEY] == 17, "the stale holder re-armed the successor's lease"
    assert _refresh(stale, retake) is LeaseState.LOST


def test_a_successor_in_the_get_delete_sliver_is_not_deleted():
    r = lua.LeaseRedis()
    stale = SingleFlightLock(KEY, TTL, client=r)
    assert stale.acquire()
    holder: dict = {}
    r.race_on_get = lambda: holder.setdefault("b", _lapse_and_succeed(r))

    stale.release_if_owned()

    successor = holder.get("b")
    if successor is None:  # no client GET happened, so lapse now and re-release
        successor = _lapse_and_succeed(r)
        stale.release_if_owned()
    assert (
        r.store.get(KEY) == successor.token
    ), "the stale holder deleted the successor's lease"


# ---------------------------------------------------------------------------
# refresh — Redis errors and replies that are not Redis replies
# ---------------------------------------------------------------------------


def test_refresh_error_is_degraded_and_does_not_clear_held():
    """A Redis blip mid-refresh is NOT lease loss: DEGRADED, and — critically —
    it must NOT clear the acquire-time `held`, or a caller that keeps going after
    a blip would abandon a live lock for the full TTL (the ops 2100s-wedge
    class)."""
    r = lua.LeaseRedis()
    lock = SingleFlightLock(KEY, TTL, client=r)
    assert lock.acquire() is True
    r.fail_scripts = True
    assert lock.refresh_if_owned() is LeaseState.DEGRADED
    assert lock.refresh_if_owned(retake_lapsed=True) is LeaseState.DEGRADED
    assert lock.held is True  # NOT cleared

    r.fail_scripts = False  # the blip clears; release still deletes OUR lock
    lock.release_if_owned()
    assert KEY not in r.store


def test_refresh_before_acquire_is_degraded():
    r = lua.LeaseRedis()
    lock = SingleFlightLock(KEY, TTL, client=r)
    assert lock.refresh_if_owned() is LeaseState.DEGRADED  # never held → no-op
    assert r.commands == []


def test_a_mock_reply_is_degraded_never_owned():
    """`int(MagicMock())` is 1: a client whose `register_script` does not
    execute anything must not read as a held lease."""
    client = MagicMock()
    client.set.return_value = True
    lock = SingleFlightLock(KEY, TTL, client=client)
    assert lock.acquire() is True
    assert lock.refresh_if_owned() is LeaseState.DEGRADED
    assert lock.refresh_if_owned(retake_lapsed=True) is LeaseState.DEGRADED


@pytest.mark.parametrize(
    "reply,state",
    [
        (1, LeaseState.OWNED),
        (b"1", LeaseState.OWNED),
        ("1", LeaseState.OWNED),
        (0, LeaseState.LOST),
        (b"0", LeaseState.LOST),
        (None, LeaseState.DEGRADED),
        (True, LeaseState.DEGRADED),
        (b"OK", LeaseState.DEGRADED),
        (7, LeaseState.DEGRADED),
        (-1, LeaseState.DEGRADED),
        ([1], LeaseState.DEGRADED),
        # never raised out of a refresh: `isdigit` accepts these, `int` does not
        ("²", LeaseState.DEGRADED),
        (b"\xff", LeaseState.DEGRADED),
        ("１", LeaseState.DEGRADED),
    ],
)
def test_refresh_reads_only_integer_replies(reply, state):
    client = MagicMock()
    client.set.return_value = True
    client.register_script.return_value = MagicMock(return_value=reply)
    lock = SingleFlightLock(KEY, TTL, client=client)
    lock.acquire()
    assert lock.refresh_if_owned() is state


def test_the_retake_code_is_reacquired_only_when_asked_for():
    client = MagicMock()
    client.set.return_value = True
    client.register_script.return_value = MagicMock(return_value=2)
    lock = SingleFlightLock(KEY, TTL, client=client)
    lock.acquire()
    assert lock.refresh_if_owned(retake_lapsed=True) is LeaseState.REACQUIRED
    assert lock.refresh_if_owned() is LeaseState.DEGRADED  # 2 is not a refresh reply


# ---------------------------------------------------------------------------
# Scripts are registered on the injected client, per call
# ---------------------------------------------------------------------------


def test_each_lock_runs_its_scripts_on_its_own_client():
    """A process-wide script cache would keep the FIRST client's script objects;
    a double that ignores `client=` (a MagicMock) would then become every later
    lock's Redis. Registration is per call, on the lock's own client."""
    first, second = lua.LeaseRedis(), lua.LeaseRedis()
    a = SingleFlightLock(KEY, TTL, client=first)
    a.acquire()
    a.refresh_if_owned()
    b = SingleFlightLock(KEY, TTL, client=second)
    b.acquire()
    second.commands.clear()
    first.commands.clear()

    assert b.refresh_if_owned() is LeaseState.OWNED
    b.release_if_owned()
    assert [c[1] for c in second.commands] == ["refresh", "release"]
    assert first.commands == [] and first.store[KEY] == a.token


# ---------------------------------------------------------------------------
# bytes-token (a client configured WITHOUT decode_responses)
# ---------------------------------------------------------------------------


def test_bytes_client_renews_and_releases_its_own_lease():
    """`get_breaker_redis` sets decode_responses=True, so bytes is not a live
    path, but Redis compares bytes in the script either way."""
    client = lua.with_lua(fakeredis.FakeStrictRedis(decode_responses=False))
    lock = SingleFlightLock(KEY, TTL, client=client)
    assert lock.acquire() is True
    assert lock.refresh_if_owned() is LeaseState.OWNED
    lock.release_if_owned()
    assert client.get(KEY) is None


def test_bytes_store_still_refuses_a_foreign_token():
    r = lua.LeaseRedis(decode_responses=False)
    lock = SingleFlightLock(KEY, TTL, client=r)
    lock.acquire()
    r.store[KEY] = b"someone-else"
    assert lock.refresh_if_owned() is LeaseState.LOST
    lock.release_if_owned()
    assert r.store[KEY] == b"someone-else"


# ---------------------------------------------------------------------------
# release_if_owned
# ---------------------------------------------------------------------------


def test_release_deletes_only_on_token_match(r):
    lock = SingleFlightLock(KEY, TTL, client=r)
    lock.acquire()
    lock.release_if_owned()
    assert r.get(KEY) is None


def test_release_never_touches_a_foreign_token(r):
    lock = SingleFlightLock(KEY, TTL, client=r)
    lock.acquire()
    r.set(KEY, "someone-else")
    lock.release_if_owned()
    assert r.get(KEY) == "someone-else"  # foreign token untouched


def test_release_issues_one_script_and_no_client_command():
    r = lua.LeaseRedis()
    lock = SingleFlightLock(KEY, TTL, client=r)
    lock.acquire()
    r.commands.clear()
    lock.release_if_owned()
    assert r.commands == [("script", "release", KEY)]


def test_release_before_acquire_is_a_noop():
    client = MagicMock()
    lock = SingleFlightLock(KEY, TTL, client=client)
    lock.release_if_owned()  # never held → no Redis calls
    client.register_script.assert_not_called()
    client.get.assert_not_called()
    client.delete.assert_not_called()


def test_release_swallows_redis_errors():
    r = lua.LeaseRedis()
    lock = SingleFlightLock(KEY, TTL, client=r)
    lock.acquire()
    r.fail = True
    lock.release_if_owned()  # must not raise; TTL expires it


def test_release_swallows_a_client_without_scripting():
    """A client that cannot register a script at all fails like a Redis error:
    the release never raises (the TTL expires the lease)."""

    class _NoScripts:
        def set(self, *a, **k):
            return True

    lock = SingleFlightLock(KEY, TTL, client=_NoScripts())
    lock.acquire()
    lock.release_if_owned()
    assert lock.refresh_if_owned() is LeaseState.DEGRADED


# ---------------------------------------------------------------------------
# THE #1920 property — successor safety
# ---------------------------------------------------------------------------


def test_release_never_deletes_a_successor_lock(r):
    """The exact bug #1920 fixes: worker A acquires, A's TTL lapses, worker B
    SETNX-acquires a FRESH token, A finishes and releases — A's ownership-checked
    release must leave B's live lock intact."""
    a = SingleFlightLock(KEY, TTL, client=r)
    assert a.acquire() is True

    r.delete(KEY)  # A's lease lapses
    b = SingleFlightLock(KEY, TTL, client=r)
    assert b.acquire() is True
    assert b.token != a.token

    a.release_if_owned()
    assert r.get(KEY) == b.token  # B's lock survives A's release


def test_unique_token_per_acquire_closes_the_constant_one_bug(r):
    """Two locks over the same key mint DISTINCT tokens — the property a naive
    compare-and-delete against a constant "1" lacks."""
    a = SingleFlightLock(KEY, TTL, client=r)
    b = SingleFlightLock(KEY, TTL, client=r)
    assert a.token != b.token
    assert a.token != "1" and b.token != "1"
