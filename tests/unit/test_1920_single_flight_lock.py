"""#1920 — behaviour of the shared ``SingleFlightLock`` primitive.

`redis_breaker_util.SingleFlightLock` consolidates the SETNX single-flight lock
idiom that was hand-rolled in seven sync sites (ops, ephemeral, skill×2,
system_seed, cornelius, compat_fix) with divergent — and, in the
system_seed/cornelius/compat_fix constant-"1" case, buggy — behaviour.
This suite pins the primitive's contract against **fakeredis** (a real Redis
data model with no server) plus MagicMock clients for the error-injection and
call-count cases fakeredis can't force.

The headline property is `test_release_never_deletes_a_successor_lock` — the
exact #1920 bug: a slow holder whose TTL lapsed must never delete the successor
that re-took the key. Everything else exists so a future refactor of the
primitive can't silently regress one of the sites that now depends on it.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import fakeredis
import pytest

from redis_breaker_util import LeaseState, SingleFlightLock, reset_breaker_redis_client

KEY = "test:single_flight"
TTL = 60


@pytest.fixture
def r():
    """A fresh fakeredis client per test (decode_responses=True, like the live
    breaker client). Never touches the module-cached client."""
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    yield client
    # Belt: nothing here uses the cached client, but keep teardown honest.
    reset_breaker_redis_client()


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
    lock.release_if_owned()  # no raise, no client call


def test_acquire_setnx_error_fails_open(caplog):
    client = MagicMock()
    client.set.side_effect = RuntimeError("redis down")
    lock = SingleFlightLock(KEY, TTL, client=client)
    assert lock.acquire() is True  # fail-open: proceed as sole worker
    assert lock.held is False  # but NOT a real lease
    # refresh/release are no-ops (held is False) — never call the broken client.
    assert lock.refresh_if_owned() is LeaseState.DEGRADED
    lock.release_if_owned()
    client.get.assert_not_called()
    client.delete.assert_not_called()


# ---------------------------------------------------------------------------
# refresh_if_owned  (only ops.py refreshes)
# ---------------------------------------------------------------------------


def test_refresh_owned_extends_the_lease(r):
    lock = SingleFlightLock(KEY, TTL, client=r)
    lock.acquire()
    assert lock.refresh_if_owned() is LeaseState.OWNED
    assert r.get(KEY) == lock.token  # still ours


def test_refresh_absent_reacquires_same_token(r):
    lock = SingleFlightLock(KEY, TTL, client=r)
    lock.acquire()
    r.delete(KEY)  # lease lapsed, nobody racing us
    assert lock.refresh_if_owned() is LeaseState.REACQUIRED
    assert r.get(KEY) == lock.token  # re-took it with the SAME token


def test_refresh_foreign_token_is_lost(r):
    lock = SingleFlightLock(KEY, TTL, client=r)
    lock.acquire()
    r.set(KEY, "intruder-token")  # a concurrent holder took over
    assert lock.refresh_if_owned() is LeaseState.LOST
    assert lock.last_current == "intruder-token"  # for the caller's foreign WORD


def test_refresh_absent_losing_the_reacquire_race_is_lost():
    """Absent at GET, but a second caller wins the SETNX between our read and
    our re-acquire → that is a foreign holder: LOST."""
    client = MagicMock()
    client.set.side_effect = [True, None]  # acquire wins, re-acquire loses
    client.get.return_value = None  # absent at refresh
    lock = SingleFlightLock(KEY, TTL, client=client)
    assert lock.acquire() is True
    assert lock.refresh_if_owned() is LeaseState.LOST
    assert lock.last_current is None  # renders as 'absent', not 'foreign'


def test_refresh_error_is_degraded_and_does_not_clear_held(r):
    """A Redis blip mid-refresh is NOT lease loss: DEGRADED, and — critically —
    it must NOT clear the acquire-time `held`, or a caller that reclaims its own
    lock after a stale false-foreign read would abandon a live lock for the full
    TTL (the ops 2100s-wedge class)."""
    client = MagicMock()
    client.set.return_value = True
    client.get.side_effect = RuntimeError("redis blip")
    lock = SingleFlightLock(KEY, TTL, client=client)
    assert lock.acquire() is True
    assert lock.refresh_if_owned() is LeaseState.DEGRADED
    assert lock.held is True  # NOT cleared

    # Now the blip clears and our token is actually still there → release must
    # still delete OUR live lock (proves held was preserved).
    store = {KEY: lock.token}
    client.get.side_effect = lambda k: store.get(k)
    client.delete.side_effect = lambda k: store.pop(k, None)
    lock.release_if_owned()
    client.delete.assert_called_once_with(KEY)


def test_refresh_before_acquire_is_degraded(r):
    lock = SingleFlightLock(KEY, TTL, client=r)
    assert lock.refresh_if_owned() is LeaseState.DEGRADED  # never held → no-op


# ---------------------------------------------------------------------------
# refresh — the vanished-key sliver (matches ops's get/set call-counts)
# ---------------------------------------------------------------------------


def test_vanished_key_expire_zero_goes_straight_to_setnx_no_reget():
    """GET says ours, but EXPIRE returns 0 (the key vanished in the GET→EXPIRE
    sliver, creating nothing): fall STRAIGHT to the same-token SETNX — no second
    GET — matching ops's call-counts."""
    client = MagicMock()
    client.set.return_value = True  # acquire + reacquire both win
    client.get.return_value = None  # GET value doesn't matter here...
    lock = SingleFlightLock(KEY, TTL, client=client)
    lock.acquire()

    # ...override GET to return our token so refresh sees 'owned', then EXPIRE 0.
    client.get.return_value = lock.token
    client.expire.return_value = 0
    client.get.reset_mock()
    client.set.reset_mock()

    assert lock.refresh_if_owned() is LeaseState.REACQUIRED
    client.get.assert_called_once()  # exactly ONE GET, no re-GET
    client.set.assert_called_once()  # the same-token SETNX
    assert client.set.call_args.kwargs.get("nx") is True


def test_vanished_key_then_setnx_loses_is_lost():
    client = MagicMock()
    client.set.side_effect = [True, None]  # acquire wins, reacquire loses
    lock = SingleFlightLock(KEY, TTL, client=client)
    lock.acquire()
    client.get.return_value = lock.token
    client.expire.return_value = 0  # vanished
    assert lock.refresh_if_owned() is LeaseState.LOST
    assert lock.last_current is None  # vanished == absent


# ---------------------------------------------------------------------------
# bytes-token (a client configured WITHOUT decode_responses)
# ---------------------------------------------------------------------------


def test_bytes_token_matches_via_lock_token_matches():
    """`get_breaker_redis` sets decode_responses=True so bytes is not a live
    path — but the helper compares via `lock_token_matches`, whose bytes branch
    must not be untested dead code (a bare `==` would fail here)."""
    client = fakeredis.FakeStrictRedis(decode_responses=False)  # GET returns bytes
    lock = SingleFlightLock(KEY, TTL, client=client)
    assert lock.acquire() is True
    assert lock.refresh_if_owned() is LeaseState.OWNED  # bytes token still owned
    lock.release_if_owned()
    assert client.get(KEY) is None  # released via bytes match


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


def test_release_before_acquire_is_a_noop():
    client = MagicMock()
    lock = SingleFlightLock(KEY, TTL, client=client)
    lock.release_if_owned()  # never held → no Redis calls
    client.get.assert_not_called()
    client.delete.assert_not_called()


def test_release_swallows_redis_errors(r):
    client = MagicMock()
    client.set.return_value = True
    client.get.side_effect = RuntimeError("redis down")
    lock = SingleFlightLock(KEY, TTL, client=client)
    lock.acquire()
    lock.release_if_owned()  # must not raise; TTL expires it


# ---------------------------------------------------------------------------
# THE #1920 property — successor safety
# ---------------------------------------------------------------------------


def test_release_never_deletes_a_successor_lock(r):
    """The exact bug #1920 fixes: worker A acquires, A's TTL lapses, worker B
    SETNX-acquires a FRESH token, A finishes and releases — A's ownership-checked
    release must leave B's live lock intact. The pre-#1920 tokenless delete (and
    a naive compare-and-delete against a constant "1") removed it."""
    a = SingleFlightLock(KEY, TTL, client=r)
    assert a.acquire() is True

    # A's lease TTL-expires (simulate via delete — fakeredis won't wall-clock
    # expire reliably) and worker B acquires a fresh lock with a DIFFERENT token.
    r.delete(KEY)
    b = SingleFlightLock(KEY, TTL, client=r)
    assert b.acquire() is True
    assert b.token != a.token

    # A finishes and releases — must NOT delete B's successor lock.
    a.release_if_owned()
    assert r.get(KEY) == b.token  # B's lock survives A's release


def test_unique_token_per_acquire_closes_the_constant_one_bug(r):
    """Two locks over the same key mint DISTINCT tokens — the property a naive
    compare-and-delete against a constant "1" lacks (both would store "1", so A's
    release would match B's lock)."""
    a = SingleFlightLock(KEY, TTL, client=r)
    b = SingleFlightLock(KEY, TTL, client=r)
    assert a.token != b.token
    assert a.token != "1" and b.token != "1"
