"""The sharing heartbeat's dueness comes from the persisted stamp, not from process age (#2618).

Pre-fix, ``TelemetrySharingService._loop`` slept the whole interval from process start and then
sent unconditionally. A backend that restarted inside every 24h window therefore shared once (the
consent-time backfill) and never again — reproduced on a dev instance that sat 45h past its last
share with five reloads a day while Settings reported sharing as on. The payload layer
(``_resolve_window``) already keyed on ``telemetry_sharing_last_shared_at``; this file pins the
scheduler layer agreeing with it.

Harness mirrors ``test_ent437_telemetry_consent.py``: ``db`` is a settings store with real
write-once semantics, ``httpx`` is a fake client, ``_now`` is the module's one clock and is
patched instead of sleeping, and the tick marker runs on fakeredis. Sync tests drive coroutines
with ``asyncio.run`` because ``tests/unit/pytest.ini`` runs pytest-asyncio in strict mode.

Locked behaviour (the issue's AC, as amended at the plan gate):
  * due = the stamp is empty, unparseable or in the future, or older than the interval;
    NOT due only when the last share is provably inside the current interval
  * the loop sleeps FIRST: the earliest possible send is one wake in (no boot burst)
  * a fresh service instance (a restart) sends when the STORE says due, and not otherwise
  * the consent and hard-disable gates run before any stamp read or Redis touch
  * the marker's TTL follows the send cadence, never the wake; a failed send releases it, an
    acknowledged one keeps it; Redis down stays fail-open; only the lock created in THIS tick
    can ever be released
  * ``share_now`` True means "the receiver acknowledged", even if the stamp write raised
  * after five consecutive failures, at most one attempt per half-interval (persisted, no Redis)
  * a tick that raises does not end the loop; a cancellation mid-send releases the marker
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

T0 = datetime(2026, 9, 7, 14, 16, 34, 257400, tzinfo=timezone.utc)
H = timedelta(hours=1)
DAY = timedelta(hours=24)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _fake_db(store: dict) -> MagicMock:
    """A settings store with REAL write-once semantics plus coarse readers (the
    ent#437 harness). Every aggregate reader is fenced in the builder, so a
    Mock never reaches the validator."""
    mdb = MagicMock()
    mdb.get_setting_value.side_effect = lambda k, d=None: store.get(k, d)
    mdb.set_setting.side_effect = lambda k, v: store.update({k: v})
    mdb.delete_setting.side_effect = lambda k: store.pop(k, None)

    def _insert_if_absent(k, v):
        if k in store:
            return False
        store[k] = v
        return True
    mdb.insert_setting_if_absent.side_effect = _insert_if_absent

    mdb.get_fleet_execution_stats.return_value = {"total": 22, "success_count": 21, "failed_count": 1}
    mdb.count_product_events_by_type.return_value = {"setup_started": 2}
    mdb.count_non_system_agents.return_value = 3
    mdb.get_fleet_execution_timeline.return_value = []
    mdb.shape_execution_timeline.return_value = []
    mdb.count_terminal_executions_by_status.return_value = {"success": 21, "failed": 1}
    mdb.get_failure_event_counts_by_subscription.return_value = {}
    mdb.first_autonomous_success_at.return_value = None
    return mdb


@pytest.fixture
def tss():
    try:
        import services.telemetry_sharing_service as mod
    except ImportError:
        pytest.skip("backend venv required")
    store: dict = {}
    mdb = _fake_db(store)
    settings = MagicMock()
    settings.get_install_source.return_value = "script"
    with patch.object(mod, "db", mdb), \
         patch.object(mod, "settings_service", settings), \
         patch.object(mod, "resolve_release_version", return_value="0.9.5"), \
         patch.object(mod, "TELEMETRY_SHARING_ENABLED", True):
        yield mod, store, mdb


def _consent_on(mod, store: dict) -> None:
    store[mod.KEY_ENABLED] = "true"


def _fake_client(status_code=200):
    """A fake ``httpx.AsyncClient`` factory; ``client.post`` records every call."""
    resp = MagicMock()
    resp.status_code = status_code
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx), client


def _at(mod, when: datetime):
    """Pin the module's one clock."""
    return patch.object(mod, "_now", return_value=when)


def _failures(mod, newest: datetime, n: int, *, ok_at: int | None = None) -> list:
    """``n`` send-log entries newest-first, spaced one wake apart; ``ok_at`` marks one
    of them as an acknowledged send."""
    out = []
    for i in range(n):
        out.append({
            "sent_at": _iso(newest - timedelta(seconds=i * mod._WAKE_SECONDS)),
            "backfill": False, "window_days": 1,
            "ok": (i == ok_at), "http_status": 200 if i == ok_at else 503, "error": None,
            "payload": {},
        })
    return out


# ---------------------------------------------------------------------------
# A. The pure dueness rule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stamp, now, interval_h, expected", [
    ("", T0, 24, True),                                   # never shared ⇒ the owed backfill is due
    (None, T0, 24, True),                                 # NULL row
    ("not-a-date", T0, 24, True),                         # corrupt row self-heals on the next 2xx
    (42, T0, 24, True),                                   # a non-string (a Mock, a number)
    (_iso(T0), T0 + 25 * H, 24, True),                    # older than the interval
    (_iso(T0), T0 + 1 * H, 24, False),                    # provably inside the window
    (_iso(T0), T0 + DAY, 24, True),                       # exactly the interval is due (>=, never >)
    (_iso(T0), T0 - 1 * H, 24, True),                     # a stamp in the future: the clock moved; re-anchor
    ("2026-09-07T14:16:34.257400+00:00", T0 + 1 * H, 24, False),   # offset form
    ("2026-09-07T14:16:34", T0 + 1 * H, 24, False),       # naive ⇒ UTC
    (_iso(T0), T0 + timedelta(minutes=61), 1, True),      # a 1h interval
    (_iso(T0), T0 + timedelta(minutes=59), 1, False),
])
def test_is_share_due_matrix(tss, stamp, now, interval_h, expected):
    mod, _, _ = tss
    assert mod.is_share_due(stamp, interval_h * 3600, now=now) is expected


def test_days_since_delegates_to_the_one_clock_and_keeps_the_floor(tss):
    """``_resolve_window`` keeps its whole-day floor; the delegation must not change it."""
    mod, _, _ = tss
    with _at(mod, T0 + 26 * H):
        assert mod._days_since(_iso(T0)) == 1
    with _at(mod, T0 - 1 * H):
        assert mod._days_since(_iso(T0)) == 0           # clamped, never negative
    assert mod._days_since("garbage") is None and mod._days_since("") is None


# ---------------------------------------------------------------------------
# B. One wake (`_tick`) on a store, without sleeping
# ---------------------------------------------------------------------------

def test_tick_sends_when_the_store_says_due_and_only_then(tss):
    mod, store, _ = tss
    _consent_on(mod, store)
    store[mod.KEY_LAST_SHARED_AT] = _iso(T0)
    svc = mod.TelemetrySharingService(interval_hours=24)
    send = AsyncMock(return_value=True)
    claim = MagicMock(return_value=True)
    with patch.object(mod, "share_now", send), patch.object(svc, "_claim_tick", claim):
        with _at(mod, T0 + 1 * H):
            assert asyncio.run(svc._tick()) is False
        send.assert_not_awaited()
        claim.assert_not_called()                        # not due ⇒ Redis is never touched
        with _at(mod, T0 + 25 * H):                      # the positive control
            assert asyncio.run(svc._tick()) is True
        send.assert_awaited_once_with(backfill=False)
        claim.assert_called_once()


def test_gates_run_before_any_stamp_read_or_redis_touch(tss):
    mod, store, mdb = tss
    store[mod.KEY_LAST_SHARED_AT] = _iso(T0)             # due, if anyone looked
    svc = mod.TelemetrySharingService(interval_hours=24)
    send = AsyncMock(return_value=True)
    claim = MagicMock(return_value=True)
    with patch.object(mod, "share_now", send), patch.object(svc, "_claim_tick", claim), \
         _at(mod, T0 + 25 * H):
        # consent OFF
        assert asyncio.run(svc._tick()) is False
        # consent ON but the config kill switch is on
        _consent_on(mod, store)
        with patch.object(mod, "TELEMETRY_SHARING_ENABLED", False):
            assert asyncio.run(svc._tick()) is False
    send.assert_not_awaited()
    claim.assert_not_called()
    read_keys = [c.args[0] for c in mdb.get_setting_value.call_args_list]
    assert mod.KEY_LAST_SHARED_AT not in read_keys       # the gate precedes the stamp read


def test_empty_stamp_is_due_at_the_first_wake(tss):
    """An install whose consent-time backfill failed owes it; it must not wait a full interval."""
    mod, store, _ = tss
    _consent_on(mod, store)
    assert mod.KEY_LAST_SHARED_AT not in store
    svc = mod.TelemetrySharingService(interval_hours=24)
    send = AsyncMock(return_value=True)
    with patch.object(mod, "share_now", send), patch.object(svc, "_claim_tick", return_value=True), \
         _at(mod, T0):
        assert asyncio.run(svc._tick()) is True
    send.assert_awaited_once_with(backfill=False)


def test_reconsent_leaves_a_stale_stamp_and_the_due_send_is_a_backfill(tss):
    """Revoke keeps the stamp and deletes the delivered marker; the first due wake therefore sends
    the owed backfill through `_resolve_window`'s upgrade even though the loop asks for a heartbeat."""
    mod, store, _ = tss
    _consent_on(mod, store)
    store[mod.KEY_LAST_SHARED_AT] = _iso(T0)
    store[mod.KEY_BACKFILL_DAYS] = "30"
    assert mod.KEY_BACKFILL_DELIVERED_AT not in store
    svc = mod.TelemetrySharingService(interval_hours=24)
    fake_ac, client = _fake_client(200)
    with patch.object(mod.httpx, "AsyncClient", fake_ac), patch.object(mod, "get_breaker_redis", return_value=None), \
         _at(mod, T0 + 25 * H):
        assert asyncio.run(svc._tick()) is True
    body = client.post.call_args.kwargs["json"]
    assert body["backfill"] is True and body["window_days"] == 30
    assert store[mod.KEY_LAST_SHARED_AT] == _iso(T0 + 25 * H)   # stamped by the one clock
    assert store.get(mod.KEY_BACKFILL_DELIVERED_AT)


# ---------------------------------------------------------------------------
# C/D. The loop: sleep first, then dueness from the store across a "restart"
# ---------------------------------------------------------------------------

def _drive_loop(mod, svc, clock: dict, *, stop_when, max_sleeps=400):
    """Run `_loop` with a sleep stub that advances the fake clock by the slept seconds
    (never the real `asyncio.sleep`) and stops the loop once `stop_when()` holds."""
    sleeps: list = []

    async def _sleep(seconds):
        sleeps.append(seconds)
        clock["now"] = clock["now"] + timedelta(seconds=seconds)
        if stop_when() or len(sleeps) >= max_sleeps:
            svc._running = False

    svc._running = True
    with patch.object(mod.asyncio, "sleep", _sleep), patch.object(mod.random, "uniform", return_value=0.0), \
         patch.object(mod, "_now", side_effect=lambda: clock["now"]):
        asyncio.run(svc._loop())
    return sleeps


def test_restart_inside_the_window_never_sends_and_the_due_send_arrives_after_a_restart(tss):
    """Three service instances (three boots) over ONE store: nothing sends while the stamp is inside
    the interval, exactly one send lands once the clock passes it, and a further restart right after
    stays silent. Process age plays no part — that is the bug."""
    mod, store, _ = tss
    _consent_on(mod, store)
    store[mod.KEY_LAST_SHARED_AT] = _iso(T0)
    store[mod.KEY_BACKFILL_DELIVERED_AT] = _iso(T0)     # the backfill is not owed
    clock = {"now": T0}
    fake_ac, client = _fake_client(200)
    with patch.object(mod.httpx, "AsyncClient", fake_ac), patch.object(mod, "get_breaker_redis", return_value=None):
        # boot 1: 23 hours of wakes inside the window
        sleeps = _drive_loop(mod, mod.TelemetrySharingService(interval_hours=24), clock,
                             stop_when=lambda: clock["now"] >= T0 + 23 * H)
        assert sleeps and client.post.await_count == 0
        # boot 2 (a restart mid-window): the first wake past the 24h mark sends, once
        sleeps = _drive_loop(mod, mod.TelemetrySharingService(interval_hours=24), clock,
                             stop_when=lambda: client.post.await_count >= 1)
        assert client.post.await_count == 1
        assert clock["now"] >= T0 + DAY
        # Stamped by the one clock at the moment of the send: at or after the 24h mark, and equal
        # to the send-log entry's own time (the stop condition fires inside the NEXT sleep, so the
        # clock has moved one wake past the send by the time the loop returns).
        stamp = store[mod.KEY_LAST_SHARED_AT]
        assert _iso(T0 + DAY) <= stamp <= _iso(clock["now"])
        assert json.loads(store[mod.KEY_RECENT_SENDS])[0]["sent_at"] == stamp
        # boot 3 (another restart right after): inside the new window ⇒ silent
        n = len(_drive_loop(mod, mod.TelemetrySharingService(interval_hours=24), clock,
                            stop_when=lambda: False, max_sleeps=5))
        assert n == 5 and client.post.await_count == 1


def test_no_boot_burst_the_first_sleep_precedes_any_send(tss):
    """One ordered timeline of sleeps and sends: a due store still waits one wake."""
    mod, store, _ = tss
    _consent_on(mod, store)
    store[mod.KEY_LAST_SHARED_AT] = _iso(T0)
    svc = mod.TelemetrySharingService(interval_hours=24)
    timeline: list = []

    async def _sleep(seconds):
        timeline.append(("sleep", seconds))
        if sum(1 for t in timeline if t[0] == "sleep") >= 2:
            svc._running = False

    async def _send(**kwargs):
        timeline.append(("send", kwargs))
        return True

    svc._running = True
    with patch.object(mod.asyncio, "sleep", _sleep), patch.object(mod.random, "uniform", return_value=123.0), \
         patch.object(mod, "share_now", _send), patch.object(svc, "_claim_tick", return_value=True), \
         _at(mod, T0 + 25 * H):
        asyncio.run(svc._loop())
    assert timeline[0] == ("sleep", mod._WAKE_SECONDS + 123.0)
    assert timeline[1] == ("send", {"backfill": False})
    assert timeline[2][0] == "sleep"
    for kind, value in timeline:
        if kind == "sleep":
            assert mod._WAKE_SECONDS <= value <= mod._WAKE_SECONDS + mod._WAKE_JITTER_SECONDS


def test_loop_survives_a_tick_that_raises(tss, caplog):
    mod, _, _ = tss
    svc = mod.TelemetrySharingService(interval_hours=24)
    sleeps: list = []

    async def _sleep(_seconds):
        sleeps.append(1)
        if len(sleeps) >= 3:
            svc._running = False

    ticks: list = []

    async def _tick():
        ticks.append(1)
        if len(ticks) == 1:
            raise RuntimeError("boom")
        return False

    svc._running = True
    with patch.object(mod.asyncio, "sleep", _sleep), patch.object(svc, "_tick", _tick), \
         caplog.at_level(logging.ERROR, logger=mod.logger.name):
        asyncio.run(svc._loop())
    # sleep → tick (raises) → sleep → tick → sleep (stops): a second tick ran after the raise
    assert len(ticks) == 2 and len(sleeps) == 3
    assert any("tick failed" in r.getMessage() for r in caplog.records)


def test_stop_cancels_the_loop_and_waits_for_it_to_unwind(tss):
    """The lifespan shutdown path (main.py): `stop()` cancels the task and returns only once the loop
    has unwound, so a cancellation mid-send has taken the tick's release path before the process exits."""
    mod, _, _ = tss
    svc = mod.TelemetrySharingService(interval_hours=24)

    async def _main():
        svc.start()
        await asyncio.sleep(0)                           # let the loop reach its first (real) sleep
        task = svc._task
        assert task is not None and not task.done()
        await svc.stop()
        return task

    task = asyncio.run(_main())
    assert task.done() and svc._task is None and svc._running is False


# ---------------------------------------------------------------------------
# E. The tick marker (fakeredis): cadence-keyed TTL, fresh per claim, released on failure only
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("interval_hours", [1, 24])
def test_marker_ttl_follows_the_send_cadence_never_the_wake(tss, interval_hours):
    mod, _, _ = tss
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.FakeRedis()
    svc = mod.TelemetrySharingService(interval_hours=interval_hours)
    with patch.object(mod, "get_breaker_redis", return_value=client):
        assert svc._claim_tick() is True
    ttl = client.ttl(mod._TICK_LOCK_KEY)
    max_wake = mod._WAKE_SECONDS + mod._WAKE_JITTER_SECONDS
    assert max_wake < ttl <= svc.interval_seconds // 2


def test_marker_is_a_fresh_setnx_per_claim(tss):
    """A reused SingleFlightLock answers True forever after its first win; the loop must not."""
    mod, _, _ = tss
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.FakeRedis()
    svc = mod.TelemetrySharingService(interval_hours=24)
    with patch.object(mod, "get_breaker_redis", return_value=client):
        assert svc._claim_tick() is True
        assert svc._claim_tick() is False                # the key is held: a fresh SETNX loses
        assert mod.TelemetrySharingService(interval_hours=24)._claim_tick() is False


def test_failed_send_releases_the_marker_so_a_sibling_retries_at_the_next_wake(tss):
    mod, store, _ = tss
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.FakeRedis()
    _consent_on(mod, store)
    store[mod.KEY_LAST_SHARED_AT] = _iso(T0)
    a = mod.TelemetrySharingService(interval_hours=24)
    b = mod.TelemetrySharingService(interval_hours=24)
    fake_ac, http = _fake_client(503)
    with patch.object(mod, "get_breaker_redis", return_value=client), patch.object(mod.httpx, "AsyncClient", fake_ac), \
         _at(mod, T0 + 25 * H):
        assert asyncio.run(a._tick()) is False
        assert http.post.await_count == 1
        assert client.exists(mod._TICK_LOCK_KEY) == 0    # released on the store, not just in memory
        assert mod.KEY_LAST_SHARED_AT in store and store[mod.KEY_LAST_SHARED_AT] == _iso(T0)   # 2xx-only stamp
        assert b._claim_tick() is True                   # the sibling's next wake can retry
        assert client.exists(mod._TICK_LOCK_KEY) == 1


def test_acknowledged_send_keeps_the_marker_even_when_the_stamp_write_raises(tss, caplog):
    """`share_now` True means "the receiver acknowledged". With the stamp unwritable the marker is the
    only thing standing between a sibling and a duplicate of an accepted snapshot."""
    mod, store, mdb = tss
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.FakeRedis()
    _consent_on(mod, store)
    store[mod.KEY_LAST_SHARED_AT] = _iso(T0)
    store[mod.KEY_BACKFILL_DELIVERED_AT] = _iso(T0)

    def _set(k, v):
        if k == mod.KEY_LAST_SHARED_AT:
            raise RuntimeError("disk full")
        store[k] = v
    mdb.set_setting.side_effect = _set

    a = mod.TelemetrySharingService(interval_hours=24)
    b = mod.TelemetrySharingService(interval_hours=24)
    fake_ac, http = _fake_client(200)
    with patch.object(mod, "get_breaker_redis", return_value=client), patch.object(mod.httpx, "AsyncClient", fake_ac), \
         _at(mod, T0 + 25 * H), caplog.at_level(logging.WARNING, logger=mod.logger.name):
        assert asyncio.run(a._tick()) is True
        assert http.post.await_count == 1
        assert client.exists(mod._TICK_LOCK_KEY) == 1    # kept
        assert asyncio.run(b._tick()) is False           # the sibling is due (stale stamp) but cannot claim
        assert http.post.await_count == 1
    assert store[mod.KEY_LAST_SHARED_AT] == _iso(T0)     # the write really failed
    sends = json.loads(store[mod.KEY_RECENT_SENDS])
    assert sends[0]["ok"] is True                        # the acknowledged send is in the log
    assert any("stamp" in r.getMessage() and "disk full" not in r.getMessage() for r in caplog.records)


def test_release_only_touches_the_lock_created_in_this_tick(tss):
    """A fail-open claim must not later release a marker a previous, successful claim still holds."""
    mod, _, _ = tss
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.FakeRedis()
    svc = mod.TelemetrySharingService(interval_hours=24)
    with patch.object(mod, "get_breaker_redis", return_value=client):
        assert svc._claim_tick() is True
    svc._tick_lock = None                                # the previous tick acknowledged and dropped its handle
    with patch.object(mod, "get_breaker_redis", return_value=None):
        assert svc._claim_tick() is True                 # Redis down ⇒ fail-open
        svc._release_tick()                              # a no-op on the degraded lock
    assert client.exists(mod._TICK_LOCK_KEY) == 1        # the live marker survived


def test_redis_down_stays_fail_open_end_to_end(tss):
    mod, store, _ = tss
    _consent_on(mod, store)
    store[mod.KEY_LAST_SHARED_AT] = _iso(T0)
    svc = mod.TelemetrySharingService(interval_hours=24)
    fake_ac, http = _fake_client(503)
    with patch.object(mod, "get_breaker_redis", return_value=None), patch.object(mod.httpx, "AsyncClient", fake_ac), \
         _at(mod, T0 + 25 * H):
        assert asyncio.run(svc._tick()) is False         # sent (fail-open), the receiver said 503
    assert http.post.await_count == 1


def test_cancellation_mid_send_releases_the_marker(tss):
    """A reload or SIGTERM during the POST must not leave a dead process's marker blocking the next boot."""
    mod, store, _ = tss
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.FakeRedis()
    _consent_on(mod, store)
    store[mod.KEY_LAST_SHARED_AT] = _iso(T0)
    svc = mod.TelemetrySharingService(interval_hours=24)

    async def _cancelled(**_kwargs):
        raise asyncio.CancelledError()

    with patch.object(mod, "get_breaker_redis", return_value=client), patch.object(mod, "share_now", _cancelled), \
         _at(mod, T0 + 25 * H):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(svc._tick())
    assert client.exists(mod._TICK_LOCK_KEY) == 0


# ---------------------------------------------------------------------------
# F/G. Retry, the stamp, and an unreadable store
# ---------------------------------------------------------------------------

def test_failed_send_retries_at_the_next_wake_and_stamps_only_on_2xx(tss):
    mod, store, _ = tss
    _consent_on(mod, store)
    store[mod.KEY_LAST_SHARED_AT] = _iso(T0)
    store[mod.KEY_BACKFILL_DELIVERED_AT] = _iso(T0)
    svc = mod.TelemetrySharingService(interval_hours=24)
    with patch.object(mod, "get_breaker_redis", return_value=None):
        fake_ac, http = _fake_client(503)
        with patch.object(mod.httpx, "AsyncClient", fake_ac), _at(mod, T0 + 25 * H):
            assert asyncio.run(svc._tick()) is False
        assert store[mod.KEY_LAST_SHARED_AT] == _iso(T0)
        fake_ac, http = _fake_client(200)
        wake2 = T0 + 25 * H + timedelta(seconds=mod._WAKE_SECONDS)
        with patch.object(mod.httpx, "AsyncClient", fake_ac), _at(mod, wake2):
            assert asyncio.run(svc._tick()) is True
        assert http.post.await_count == 1
        assert store[mod.KEY_LAST_SHARED_AT] == _iso(wake2)


def test_unreadable_store_never_sends(tss):
    mod, _, mdb = tss
    mdb.get_setting_value.side_effect = RuntimeError("db unreadable")
    svc = mod.TelemetrySharingService(interval_hours=24)
    send = AsyncMock(return_value=True)
    with patch.object(mod, "share_now", send), patch.object(svc, "_claim_tick", return_value=True), _at(mod, T0):
        assert asyncio.run(svc._tick()) is False         # never raises, never sends
    send.assert_not_awaited()


def test_a_stamp_read_that_alone_raises_reads_as_due(tss):
    """Documented, not hidden: consent readable + the stamp unreadable ⇒ due (bounded by the marker)."""
    mod, store, mdb = tss
    _consent_on(mod, store)

    def _get(k, d=None):
        if k == mod.KEY_LAST_SHARED_AT:
            raise RuntimeError("row unreadable")
        return store.get(k, d)
    mdb.get_setting_value.side_effect = _get
    svc = mod.TelemetrySharingService(interval_hours=24)
    send = AsyncMock(return_value=True)
    with patch.object(mod, "share_now", send), patch.object(svc, "_claim_tick", return_value=True), _at(mod, T0):
        assert asyncio.run(svc._tick()) is True
    send.assert_awaited_once()


def test_a_failure_before_the_post_is_recorded_and_the_cap_can_see_it(tss):
    """A raise ahead of the POST (a settings read, the id claim, the aggregate build) used to return
    False with NOTHING in the send log; under retry-at-next-wake the aggregate would then be rebuilt
    every wake, unthrottled and invisible. It is now recorded like any failed attempt (#2654 review)."""
    mod, store, _ = tss
    _consent_on(mod, store)
    store[mod.KEY_LAST_SHARED_AT] = _iso(T0)
    store[mod.KEY_BACKFILL_DELIVERED_AT] = _iso(T0)
    svc = mod.TelemetrySharingService(interval_hours=24)
    builds: list = []

    def _boom(*_a, **_k):
        builds.append(1)
        raise RuntimeError("schema drift in one aggregate")

    wake = timedelta(seconds=mod._WAKE_SECONDS)
    with patch.object(mod, "get_breaker_redis", return_value=None), \
         patch.object(mod, "build_aggregate_payload", _boom):
        t = T0 + 25 * H
        for _ in range(8):                               # eight wakes against a raising build
            with _at(mod, t):
                assert asyncio.run(svc._tick()) is False
            t += wake
    assert len(builds) == mod.RECENT_SENDS_LIMIT         # five attempts, then the cap engaged
    sends = json.loads(store[mod.KEY_RECENT_SENDS])
    assert len(sends) == mod.RECENT_SENDS_LIMIT
    newest = sends[0]
    assert (newest["ok"], newest["http_status"], newest["error"], newest["payload"]) == (False, None, "RuntimeError", None)
    assert newest["window_days"] == 1 and newest["backfill"] is False   # resolved before the raise
    assert "schema drift" not in json.dumps(sends)       # the class, never the text
    assert mod.receiver_hint(sends) == "failed"          # the panel can see it
    assert store[mod.KEY_LAST_SHARED_AT] == _iso(T0)


def test_an_inner_record_is_never_duplicated_and_an_acknowledged_send_stays_true(tss):
    """The 2xx path records the acknowledged send; a raise after that (here: the logging sink) must
    neither add a second, contradictory entry nor turn the acknowledgement into False."""
    mod, store, _ = tss
    _consent_on(mod, store)
    store[mod.KEY_BACKFILL_DELIVERED_AT] = _iso(T0)
    fake_ac, _client = _fake_client(200)
    real_info = mod.logger.info

    def _info(msg, *a, **k):
        if "shared (share" in msg:
            raise RuntimeError("logging sink exploded")
        return real_info(msg, *a, **k)

    with patch.object(mod.httpx, "AsyncClient", fake_ac), patch.object(mod.logger, "info", _info), \
         _at(mod, T0 + 25 * H):
        assert asyncio.run(mod.share_now(backfill=False)) is True
    sends = json.loads(store[mod.KEY_RECENT_SENDS])
    assert len(sends) == 1 and sends[0]["ok"] is True and sends[0]["http_status"] == 200


@pytest.mark.parametrize("stamp, expected", [
    ("2026-09-07T14:16:34.257400Z", 1),        # the utc_now_iso shape the column has always held
    ("2026-09-07T14:16:34Z", 1),               # no fraction
    ("2026-09-07T14:16:34.257400+00:00", 1),   # offset form
    ("2026-09-07T16:16:34.257400+02:00", 1),   # a non-UTC offset, converted
    ("2026-09-07T14:16:34", 1),                # naive ⇒ UTC
    ("2026-09-08T10:16:34Z", 0),               # six hours ago ⇒ floor 0
    ("2026-09-09T20:00:00Z", 0),               # in the future ⇒ clamped to 0
    ("garbage", None), ("", None), (None, None), (42, None),
])
def test_days_since_pins_every_shape_the_column_has_held(tss, stamp, expected):
    """`_days_since` now parses through `parse_iso_timestamp`; it feeds `_resolve_window`'s disclosed
    window, so its answer across the stamp shapes is pinned rather than assumed (#2654 review)."""
    mod, _, _ = tss
    with _at(mod, T0 + 26 * H):                          # 2026-09-08T16:16:34Z
        assert mod._days_since(stamp) == expected


# ---------------------------------------------------------------------------
# I. The persisted retry cap (AC 5 as amended): five consecutive failures ⇒ once per half-interval
# ---------------------------------------------------------------------------

def test_retry_throttle_rule(tss):
    mod, _, _ = tss
    now = T0 + 25 * H
    day = 24 * 3600
    assert mod._retry_throttled(_failures(mod, now - 1 * H, 5), day, now=now) is True
    assert mod._retry_throttled(_failures(mod, now - 13 * H, 5), day, now=now) is False    # ≥ interval/2 ago
    assert mod._retry_throttled(_failures(mod, now - 1 * H, 4), day, now=now) is False     # fewer than five
    assert mod._retry_throttled(_failures(mod, now - 1 * H, 5, ok_at=2), day, now=now) is False   # a success inside
    assert mod._retry_throttled([], day, now=now) is False
    bad = _failures(mod, now - 1 * H, 5)
    bad[0]["sent_at"] = "garbage"
    assert mod._retry_throttled(bad, day, now=now) is False                                 # unmeasurable ⇒ the AC's default


def test_tick_honours_the_throttle_before_claiming(tss):
    mod, store, _ = tss
    _consent_on(mod, store)
    store[mod.KEY_LAST_SHARED_AT] = _iso(T0)
    now = T0 + 25 * H
    store[mod.KEY_RECENT_SENDS] = json.dumps(_failures(mod, now - 1 * H, 5))
    svc = mod.TelemetrySharingService(interval_hours=24)
    send = AsyncMock(return_value=True)
    claim = MagicMock(return_value=True)
    with patch.object(mod, "share_now", send), patch.object(svc, "_claim_tick", claim):
        with _at(mod, now):
            assert asyncio.run(svc._tick()) is False
        send.assert_not_awaited()
        claim.assert_not_called()
        with _at(mod, now + 12 * H):                     # half an interval later: one attempt
            assert asyncio.run(svc._tick()) is True
        send.assert_awaited_once()


def test_a_dead_receiver_costs_five_attempts_then_twice_a_day(tss):
    """End to end through the real `share_now`: attempts on the first five wakes, then silence until
    half an interval after the fifth, then one more."""
    mod, store, _ = tss
    _consent_on(mod, store)
    store[mod.KEY_LAST_SHARED_AT] = _iso(T0)
    store[mod.KEY_BACKFILL_DELIVERED_AT] = _iso(T0)
    svc = mod.TelemetrySharingService(interval_hours=24)
    fake_ac, http = _fake_client(503)
    wake = timedelta(seconds=mod._WAKE_SECONDS)
    with patch.object(mod, "get_breaker_redis", return_value=None), patch.object(mod.httpx, "AsyncClient", fake_ac):
        t = T0 + 25 * H
        for _ in range(10):                              # ten wakes against a dead receiver
            with _at(mod, t):
                asyncio.run(svc._tick())
            t += wake
        assert http.post.await_count == mod.RECENT_SENDS_LIMIT
        with _at(mod, t + 12 * H):                       # half an interval after the last attempt
            asyncio.run(svc._tick())
        assert http.post.await_count == mod.RECENT_SENDS_LIMIT + 1


# ---------------------------------------------------------------------------
# K. The stale wording cannot come back
# ---------------------------------------------------------------------------

def test_the_module_no_longer_says_the_marker_is_never_released(tss):
    mod, _, _ = tss
    assert "never released" not in inspect.getsource(mod)
