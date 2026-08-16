"""#2215 — atomic SSH-port allocation (per-port Redis reservation).

`get_next_available_port()` was an unlocked check-then-act: two concurrent
creators (the fresh-install seeders under `--workers 2`) read the same label
snapshot, computed the same `max+1`, and the second `containers.run` failed
`Bind for 0.0.0.0:X failed: port is already allocated`. The fix makes the
per-port SETNX reservation (`port_alloc:{port}`, EX 600) the atomic arbiter —
the LAST gate after the label scan and `is_port_available`.

Contracts pinned here:
  * deterministic regression: two sequential calls against an UNCHANGED Docker
    snapshot return DIFFERENT ports (pre-fix: the same port)
  * threaded race belt: N concurrent calls -> N distinct ports
  * reservation is the LAST gate (a host-bound candidate leaves no key behind)
  * SETNX contention -> next candidate, never the contended port unreserved
  * `exclude` binds the forward scan AND the 2222-2500 fallback scan
  * every reservation carries EX 600 (a dropped TTL would make a stale key
    permanently unallocatable — no reaper exists)
  * a raised Redis error fails OPEN (returns the candidate unreserved, exactly
    one reservation attempt); the client is resolved once per call
  * a Docker listing fault RAISES (never allocates 2222 over an existing
    fleet); demo mode (`docker_client is None`) still allocates from the base
  * Redis-down double assignment is convergent via the D2-shaped
    `exclude=` retry
  * `reserve_port_for_recreate`: SET without NX (overwrites), fail-open

Harness rules (#2215 plan §4): real modules captured at module scope and
re-owned per test via `monkeypatch.setitem(sys.modules, ...)` — including
`redis_breaker_util` (not on conftest's invariant list; a leaked MagicMock
would make every SETNX "succeed" and silently defeat the race tests);
`is_port_available` is ALWAYS stubbed (it binds real sockets); fakeredis over
hand-rolled stubs (honors nx/ex); all thread coordination is bounded.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import fakeredis  # noqa: E402
import redis_breaker_util as _real_rbu  # noqa: E402
import services.docker_service as ds  # noqa: E402

pytestmark = pytest.mark.unit


def _agent(port) -> SimpleNamespace:
    """A container double carrying only what the strict label scan reads."""
    return SimpleNamespace(labels={"trinity.ssh-port": str(port)})


def _fake_docker(ports=(2222, 2223)) -> MagicMock:
    client = MagicMock()
    client.containers.list.return_value = [_agent(p) for p in ports]
    return client


@pytest.fixture
def env(monkeypatch):
    """Re-own the modules this file exercises; default: 2 agents (2222, 2223),
    every port host-available, one shared fakeredis server."""
    monkeypatch.setitem(sys.modules, "services.docker_service", ds)
    monkeypatch.setitem(sys.modules, "redis_breaker_util", _real_rbu)

    server = fakeredis.FakeServer()

    def _client():
        return fakeredis.FakeStrictRedis(server=server, decode_responses=True)

    monkeypatch.setattr(ds, "docker_client", _fake_docker())
    monkeypatch.setattr(ds, "is_port_available", lambda port: True)
    monkeypatch.setattr(ds, "get_breaker_redis", lambda: _client())

    return SimpleNamespace(
        server=server, client=_client, monkeypatch=monkeypatch
    )


# --- the deterministic regression (the bug in unit form) -----------------------

def test_sequential_allocations_against_unchanged_snapshot_differ(env):
    """Pre-fix both calls returned 2224 — the seeders' race in unit form: the
    Docker snapshot does not change between the check and the (~seconds-later)
    containers.run, so a second allocator must be excluded by the RESERVATION,
    not by the listing."""
    first = ds.get_next_available_port()
    second = ds.get_next_available_port()

    assert first == 2224
    assert second != first
    assert env.client().get(f"port_alloc:{first}") == "1"
    assert env.client().get(f"port_alloc:{second}") == "1"


def test_threaded_race_allocates_distinct_ports(env):
    """Belt over the deterministic test: N concurrent allocators, one shared
    Redis, an unchanged Docker snapshot -> N distinct ports."""
    n = 8
    barrier = threading.Barrier(n, timeout=5)
    results, errors = [], []
    lock = threading.Lock()

    def worker():
        try:
            barrier.wait()
            port = ds.get_next_available_port()
            with lock:
                results.append(port)
        except Exception as e:  # noqa: BLE001 — surfaced via the assert below
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads), "allocator deadlocked"

    assert errors == []
    assert len(results) == n
    assert len(set(results)) == n, f"duplicate assignment: {sorted(results)}"


# --- reservation semantics -----------------------------------------------------

def test_reservation_is_last_gate_host_bound_candidate_leaves_no_key(env):
    """Reserving before the availability check would leak 600s reservations
    onto candidates the scan walks past."""
    env.monkeypatch.setattr(ds, "is_port_available", lambda port: port != 2224)

    port = ds.get_next_available_port()

    assert port == 2225
    assert env.client().get("port_alloc:2224") is None
    assert env.client().get("port_alloc:2225") == "1"


def test_contended_candidate_skipped_never_returned(env):
    """SETNX contention means a CONCURRENT allocator holds the candidate —
    returning it unreserved would hand out the racing port exactly while the
    race is happening. The sibling's reservation is left untouched."""
    env.client().set("port_alloc:2224", "sibling", ex=600)

    port = ds.get_next_available_port()

    assert port == 2225
    assert env.client().get("port_alloc:2224") == "sibling"


def test_exclude_binds_forward_scan(env):
    port = ds.get_next_available_port(exclude={2224})
    assert port == 2225


def test_exclude_binds_fallback_scan(env):
    """The exclude set is merged ONCE before both loops — an exclusion that
    only bound the forward scan would re-offer the failed port from the
    2222-2500 fallback."""
    env.monkeypatch.setattr(ds, "docker_client", _fake_docker(ports=(2499,)))
    # Forward scan starts at 2500: reject everything >= 2500 so it exhausts.
    env.monkeypatch.setattr(ds, "is_port_available", lambda port: port < 2500)

    port = ds.get_next_available_port(exclude={2222})

    assert port == 2223


def test_every_reservation_carries_the_ttl(env):
    port = ds.get_next_available_port()
    ttl = env.client().ttl(f"port_alloc:{port}")
    assert ttl == ds._PORT_RESERVATION_TTL_SECONDS == 600


def test_malformed_ssh_port_label_skips_that_container_only(env):
    broken = SimpleNamespace(labels={"trinity.ssh-port": "not-a-port"})
    client = MagicMock()
    client.containers.list.return_value = [broken, _agent(2222)]
    env.monkeypatch.setattr(ds, "docker_client", client)

    assert ds.get_next_available_port() == 2223


# --- fail-open vs fail-loud ----------------------------------------------------

def test_redis_exception_fails_open_with_one_reservation_attempt(env):
    """A RAISED Redis error (vs SETNX contention) fails open: the current
    candidate is returned unreserved, and no further reservation is attempted
    in this call (D2's bind-retry converges any resulting collision)."""
    broken = MagicMock()
    broken.set.side_effect = RuntimeError("redis down mid-call")
    env.monkeypatch.setattr(ds, "get_breaker_redis", lambda: broken)

    port = ds.get_next_available_port()

    assert port == 2224
    assert broken.set.call_count == 1


def test_redis_client_resolved_once_per_call(env):
    calls = []
    real = env.client

    def counting():
        calls.append(1)
        return real()

    env.monkeypatch.setattr(ds, "get_breaker_redis", counting)

    ds.get_next_available_port()

    assert len(calls) == 1


def test_docker_listing_fault_raises_instead_of_allocating_from_empty(env):
    """`list_all_agents_fast`'s #1131 swallow degrades a listing fault to [] —
    here that would compute start_port=2222 and confidently reserve an existing
    agent's port. The allocator's own scan is STRICT: fail loud."""
    client = MagicMock()
    client.containers.list.side_effect = PermissionError(
        "[Errno 13] Permission denied: '/var/run/docker.sock'"
    )
    env.monkeypatch.setattr(ds, "docker_client", client)

    with pytest.raises(RuntimeError, match="Docker listing failed"):
        ds.get_next_available_port()

    # And nothing was reserved on the way out.
    assert env.client().keys("port_alloc:*") == []


def test_demo_mode_still_allocates_from_base(env):
    """`docker_client is None` (demo mode) keeps the empty-set behaviour —
    crud's own 503 fires later; the allocator must not start raising there."""
    env.monkeypatch.setattr(ds, "docker_client", None)
    assert ds.get_next_available_port() == 2222


def test_redis_down_double_assignment_converges_via_exclude_retry(env):
    """Redis down => today's racy behaviour (both callers get the same port),
    stated as *convergent-under-retry*: the D2 bind-conflict retry re-allocates
    with the failed port excluded and the two callers end distinct."""
    env.monkeypatch.setattr(ds, "get_breaker_redis", lambda: None)

    a = ds.get_next_available_port()
    b = ds.get_next_available_port()
    assert a == b == 2224  # the documented degraded mode

    # The loser's bind fails at containers.run; crud retries with exclude=.
    b_retry = ds.get_next_available_port(exclude={b})
    assert b_retry != a


# --- reserve_port_for_recreate -------------------------------------------------

def test_recreate_reservation_overwrites_without_nx(env):
    """It is that agent's OWN port: a stale reservation (e.g. an expired
    allocator key from this agent's original create) must never block the
    recreate — SET without NX, TTL re-asserted."""
    env.client().set("port_alloc:2300", "stale", ex=100)

    ds.reserve_port_for_recreate(2300)

    r = env.client()
    assert r.get("port_alloc:2300") == "1"
    assert r.ttl("port_alloc:2300") == 600


def test_recreate_reservation_fails_open_on_redis_error(env):
    broken = MagicMock()
    broken.set.side_effect = RuntimeError("redis down")
    env.monkeypatch.setattr(ds, "get_breaker_redis", lambda: broken)

    ds.reserve_port_for_recreate(2300)  # must not raise


def test_recreate_reservation_noop_without_redis(env):
    env.monkeypatch.setattr(ds, "get_breaker_redis", lambda: None)
    ds.reserve_port_for_recreate(2300)  # must not raise
