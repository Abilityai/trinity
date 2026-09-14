"""#2742 — cross-worker leader election on the sync-health poll loop.

`main.py` starts `SyncHealthService` in EVERY uvicorn worker and production runs
`--workers 2` (`docker-compose.prod.yml`), so every git-enabled agent was asked
for `/api/git/status` twice a minute — and each ask runs a 30 s credentialed
`git fetch origin` inside the agent container, plus (before this issue) a
`git status --porcelain` that takes `.git/index.lock`. The lease makes it one.

Mirrors `test_1464_monitoring_leader_lock.py` (FakeRedis verbatim in shape), with
two additions this lease owes:

  - **compare-and-delete release.** Monitoring releases with GET-then-DEL, which
    is not atomic: a worker whose lease lapsed between the two calls deletes a
    sibling's FRESH grant, and for one whole cycle there are two pollers again.
    This lease releases with a single Lua CAD, so the FakeRedis here models
    `eval` and the test proves a non-holder's release is a no-op.
  - **the alert-timing consequence is NOT tested here** — it needs the real
    `upsert_sync_state` increment, so it lives beside the DB harness in
    `test_sync_health_service.py::TestLeaderLeaseAlertTiming`. Read the two
    together: this file proves one worker polls, that one proves what one
    worker instead of two does to time-to-`sync_failing` (~90 s → ~180 s).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# Import-time stubs for the two heavy deps the module pulls at import
# (`from database import db`, `from services.agent_client import AgentClient`).
# Neither is exercised by the lease itself; `_poll_cycle` reaches `db` and is
# patched per-test. Snapshot/restore pair per Issue #762 so these cannot leak
# into a sibling file that imports the real ones.
_STUBBED_MODULE_NAMES = ["database", "services", "services.agent_client"]
_SAVED_AT_IMPORT = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}

if "database" not in sys.modules:
    _db_stub = types.ModuleType("database")
    _db_stub.db = MagicMock()
    sys.modules["database"] = _db_stub
if "services" not in sys.modules:
    _svc_pkg = types.ModuleType("services")
    _svc_pkg.__path__ = [str(_BACKEND / "services")]
    sys.modules["services"] = _svc_pkg
if "services.agent_client" not in sys.modules:
    _ac_stub = types.ModuleType("services.agent_client")
    _ac_stub.AgentClient = MagicMock()
    sys.modules["services.agent_client"] = _ac_stub

_spec = importlib.util.spec_from_file_location(
    "sync_health_service_lock_under_test",
    str(_BACKEND / "services" / "sync_health_service.py"),
)
sync_health_service = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync_health_service)


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


class FakeRedis:
    """In-memory stand-in for the shared breaker client.

    Models SET NX EX / GET / EXPIRE / DEL **and `eval`** — the last one is the
    point of divergence from #1464: this lease releases with a compare-and-delete
    script, so a FakeRedis without `eval` would let the release silently no-op
    and every release assertion below would pass for the wrong reason.

    TTL is not time-simulated; expiry is modelled by deleting the key directly,
    which is exactly what a real lapse presents to the next `set(nx=True)`.
    """

    def __init__(self):
        self.store = {}
        self.eval_calls = 0

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def expire(self, key, ttl):
        return key in self.store

    def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0

    def eval(self, script, numkeys, *args):
        """Only the CAD script is ever sent; execute its semantics directly."""
        self.eval_calls += 1
        assert numkeys == 1
        key, expected = args[0], args[1]
        if self.store.get(key) == expected:
            del self.store[key]
            return 1
        return 0


def _svc(poll_interval: int = 60):
    """A service with a unique worker id (fresh instances differ)."""
    return sync_health_service.SyncHealthService(poll_interval=poll_interval)


def _use_redis(monkeypatch, fake):
    monkeypatch.setattr(sync_health_service, "get_breaker_redis", lambda: fake)


# ---------------------------------------------------------------------------
# Lease mechanics
# ---------------------------------------------------------------------------


def test_distinct_worker_ids():
    a, b = _svc(), _svc()
    assert a._worker_id != b._worker_id


def test_only_one_worker_becomes_leader(monkeypatch):
    fake = FakeRedis()
    _use_redis(monkeypatch, fake)
    a, b = _svc(), _svc()

    assert a._try_acquire_leadership() is True
    assert b._try_acquire_leadership() is False
    assert fake.get(sync_health_service._LEADER_KEY) == a._worker_id


def test_leader_refreshes_and_keeps_leadership(monkeypatch):
    fake = FakeRedis()
    _use_redis(monkeypatch, fake)
    a = _svc()

    assert a._try_acquire_leadership() is True
    assert a._try_acquire_leadership() is True
    assert a._try_acquire_leadership() is True


def test_redis_down_fails_open_to_leader(monkeypatch):
    """Fail OPEN: no Redis ⇒ every worker polls, i.e. the pre-#2742 behaviour.

    Failing closed would darken the only feed that ever raises `sync_failing`,
    precisely when the infrastructure is already degraded.
    """
    monkeypatch.setattr(sync_health_service, "get_breaker_redis", lambda: None)
    a, b = _svc(), _svc()
    assert a._try_acquire_leadership() is True
    assert b._try_acquire_leadership() is True


def test_redis_error_fails_open_to_leader(monkeypatch):
    class BoomRedis(FakeRedis):
        def set(self, *a, **k):
            raise RuntimeError("redis boom")

    _use_redis(monkeypatch, BoomRedis())
    assert _svc()._try_acquire_leadership() is True


def test_release_hands_off_immediately(monkeypatch):
    fake = FakeRedis()
    _use_redis(monkeypatch, fake)
    a, b = _svc(), _svc()

    assert a._try_acquire_leadership() is True
    assert b._try_acquire_leadership() is False

    a._release_leadership()
    assert sync_health_service._LEADER_KEY not in fake.store
    assert b._try_acquire_leadership() is True


def test_release_only_deletes_own_lease(monkeypatch):
    """The compare-and-delete half. A GET-then-DEL release would pass this test
    too — what it cannot do is survive the interleave `b` reads a stale value on:
    `test_release_is_atomic_compare_and_delete` below covers that."""
    fake = FakeRedis()
    _use_redis(monkeypatch, fake)
    a, b = _svc(), _svc()

    assert a._try_acquire_leadership() is True
    b._release_leadership()
    assert fake.get(sync_health_service._LEADER_KEY) == a._worker_id


def test_release_is_atomic_compare_and_delete(monkeypatch):
    """One round trip, not GET-then-DEL.

    The non-atomic shape loses to this interleave: worker A's lease lapses, B
    acquires, A then calls release — whose GET raced *before* B's acquire — and
    A deletes B's fresh grant, so the next cycle has two pollers again. Proven
    structurally: the release issues exactly one `eval` and never a bare
    `delete`, so there is no window between the read and the write.
    """
    fake = FakeRedis()
    deletes = []
    fake.delete = lambda key: deletes.append(key)  # type: ignore[method-assign]
    _use_redis(monkeypatch, fake)

    a = _svc()
    assert a._try_acquire_leadership() is True
    a._release_leadership()

    assert fake.eval_calls == 1, "release must go through the CAD script"
    assert deletes == [], "release must not issue a bare DEL"
    assert sync_health_service._LEADER_KEY not in fake.store


def test_release_survives_a_redis_error(monkeypatch):
    """Never raises: `stop()` calls it on a shutdown path."""
    class BoomRedis(FakeRedis):
        def eval(self, *a, **k):
            raise RuntimeError("redis boom")

    _use_redis(monkeypatch, BoomRedis())
    a = _svc()
    a._is_leader = True
    a._release_leadership()  # must not raise
    assert a._is_leader is False


def test_ttl_expiry_lets_sibling_take_over(monkeypatch):
    fake = FakeRedis()
    _use_redis(monkeypatch, fake)
    a, b = _svc(), _svc()

    assert a._try_acquire_leadership() is True
    fake.delete(sync_health_service._LEADER_KEY)  # a died; lease lapsed
    assert b._try_acquire_leadership() is True
    assert fake.get(sync_health_service._LEADER_KEY) == b._worker_id


def test_stop_releases_the_lease(monkeypatch):
    fake = FakeRedis()
    _use_redis(monkeypatch, fake)
    a = _svc()
    assert a._try_acquire_leadership() is True

    a.stop()
    assert sync_health_service._LEADER_KEY not in fake.store


def test_leader_ttl_floor_and_scaling():
    """`3x interval` with a 30 s floor: the floor covers the poll_interval=0
    test construction, and 180 s at the 60 s default comfortably outlasts one
    worst-case cycle plus the inter-cycle sleep (leadership must not flap —
    a flap puts both workers back on the fleet)."""
    assert _svc(poll_interval=0)._leader_ttl() == 30
    assert _svc(poll_interval=5)._leader_ttl() == 30
    assert _svc(poll_interval=60)._leader_ttl() == 180


# ---------------------------------------------------------------------------
# The gate actually gates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_cycle_lists_agents_only_when_leader(monkeypatch):
    for leader, expected_calls in ((True, 1), (False, 0)):
        svc = _svc(poll_interval=0)
        monkeypatch.setattr(svc, "_try_acquire_leadership", lambda: leader)

        lister = MagicMock(return_value=[])
        monkeypatch.setattr(sync_health_service.db, "list_git_enabled_agents", lister)

        await svc._poll_cycle()

        assert lister.call_count == expected_calls


@pytest.mark.asyncio
async def test_non_leader_issues_no_agent_http(monkeypatch):
    """The point of the lease: a non-leader must not touch the agent at all —
    every `/api/git/status` costs a 30 s credentialed `git fetch` in-container."""
    svc = _svc(poll_interval=0)
    monkeypatch.setattr(svc, "_try_acquire_leadership", lambda: False)
    monkeypatch.setattr(
        sync_health_service.db,
        "list_git_enabled_agents",
        MagicMock(return_value=[{"agent_name": "alpha"}]),
    )
    fetch = AsyncMock(return_value=None)
    monkeypatch.setattr(svc, "_fetch_git_status", fetch)

    await svc._poll_cycle()

    assert fetch.await_count == 0


# ---------------------------------------------------------------------------
# The poll-interval knob (TD5) — shipped, default unchanged
# ---------------------------------------------------------------------------


def test_poll_interval_default_is_unchanged(monkeypatch):
    """AC1 is written as 'the 60 s sync-health poll', and the sampler evidence
    was taken at 60 s. The knob ships; the default deliberately does not move."""
    monkeypatch.delenv("SYNC_HEALTH_POLL_INTERVAL_SECONDS", raising=False)
    assert sync_health_service.DEFAULT_POLL_INTERVAL == 60
    assert sync_health_service._poll_interval_seconds() == 60
    assert _svc(poll_interval=None).poll_interval == 60


def test_poll_interval_env_is_read_at_call_time(monkeypatch):
    """An import-time copy is how an env knob becomes silently inert — the
    module-level singleton is constructed at import."""
    svc = _svc(poll_interval=None)
    monkeypatch.setenv("SYNC_HEALTH_POLL_INTERVAL_SECONDS", "300")
    assert svc.poll_interval == 300
    monkeypatch.setenv("SYNC_HEALTH_POLL_INTERVAL_SECONDS", "900")
    assert svc.poll_interval == 900


@pytest.mark.parametrize("raw", ["0", "-5", "abc", "", "60.5"])
def test_poll_interval_garbage_degrades_to_default(monkeypatch, raw):
    """Parse-guarded and positive-clamped: a bad value must not become a hot
    loop (0/negative would make `_poll_loop` a test-only single-shot in prod)."""
    monkeypatch.setenv("SYNC_HEALTH_POLL_INTERVAL_SECONDS", raw)
    assert sync_health_service._poll_interval_seconds() == 60


def test_explicit_constructor_value_wins_over_env(monkeypatch):
    """`poll_interval=0` is how the tests mean 'one cycle then exit' — it must
    survive the env read, which requires an `is not None` check, not truthiness."""
    monkeypatch.setenv("SYNC_HEALTH_POLL_INTERVAL_SECONDS", "300")
    assert _svc(poll_interval=0).poll_interval == 0


class TestPollIntervalReachesTheContainer:
    """The cadence knob is wired into BOTH compose files and `.env.example`.

    The env read above is deliberately at CALL time (a property, not an
    import-time copy) so an operator override is honoured — but a knob the
    container never receives is inert no matter how carefully the code reads
    it, and `/validate-pr` caught exactly that on this branch: all three files
    were missing the var. That is the #1056 class (`VOIP_*`), which
    `test_ent237_skill_source_env_packaging.py` records as having recurred
    seven times. A new backend `os.getenv` is THREE edits, not one.

    Prod compose launches standalone — no base-compose merge and no `env_file:`
    on the backend service — so the explicit `environment:` list is the ONLY
    route in, and dev-only wiring does not carry over.

    The `${VAR:-60}` form is asserted rather than mere presence. There is no
    "disable" sentinel for a cadence: unset and empty must both land on the
    60 s default, which is the documented promise that this issue does not
    move the poll rate.
    """

    COMPOSE_FILES = ("docker-compose.yml", "docker-compose.prod.yml")
    VAR = "SYNC_HEALTH_POLL_INTERVAL_SECONDS"

    @staticmethod
    def _backend_env_lines(compose: str) -> list[str]:
        import re

        repo = Path(__file__).resolve().parents[2]
        text = (repo / compose).read_text()
        m = re.search(r"^  backend:$(.*?)(?=^  \w)", text, re.M | re.S)
        assert m, f"no backend service found in {compose}"
        env = re.search(r"^    environment:$(.*?)(?=^    \w)", m.group(1), re.M | re.S)
        assert env, f"backend has no environment: block in {compose}"
        return [ln.strip() for ln in env.group(1).splitlines() if ln.strip().startswith("- ")]

    @pytest.mark.parametrize("compose", COMPOSE_FILES)
    def test_var_is_passed_through_with_the_default_form(self, compose):
        expected = f"- {self.VAR}=${{{self.VAR}:-60}}"
        lines = self._backend_env_lines(compose)
        matching = [ln for ln in lines if ln.split("=", 1)[0] == f"- {self.VAR}"]
        assert matching, (
            f"{self.VAR} is read by sync_health_service but never reaches the "
            f"backend container in {compose} — the operator's .env lever is inert "
            f"(#1056 packaging class)"
        )
        assert matching[0].split("#", 1)[0].strip() == expected, (
            f"{compose} must pass {self.VAR} through as {expected} so that both "
            f"unset and empty land on the 60 s default; got {matching[0]!r}"
        )

    def test_env_example_documents_the_knob(self):
        repo = Path(__file__).resolve().parents[2]
        text = (repo / ".env.example").read_text()
        assert f"{self.VAR}=60" in text, (
            f"{self.VAR} must be documented in .env.example at its unchanged "
            f"60 s default"
        )

    def test_default_form_matches_the_code_default(self):
        """The compose default and the code default are one number.

        Pinned against the module constant rather than a literal, so a future
        change to one side fails here instead of silently giving a container a
        different cadence from a laptop.
        """
        assert sync_health_service.DEFAULT_POLL_INTERVAL == 60
        for compose in self.COMPOSE_FILES:
            line = [
                ln for ln in self._backend_env_lines(compose)
                if ln.split("=", 1)[0] == f"- {self.VAR}"
            ][0]
            assert f":-{sync_health_service.DEFAULT_POLL_INTERVAL}}}" in line
