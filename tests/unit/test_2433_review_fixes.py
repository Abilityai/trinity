"""#2435 review (#2433) — the three blocking findings and the smaller ones.

Reviewer found that #2433's own fix reintroduced the #378 symptom in a
narrower window, and turned a pre-existing registry leak into a permanent one.
Pinned here:

R1 — the cross-worker cancel must never act on a marker phase that predates
     its own cancel write. ``entry.phase`` flipped to ``calling`` in memory
     only, so the marker advertised ``parked`` for up to one 15s refresher
     tick after the POST began; a terminate served by the OTHER uvicorn worker
     then finalized CANCELLED and released the slot under a live POST.
R2 — ``list_recently_completed_ids`` bounds its exited-but-registered set, so
     a LEAKED entry stops blocking that row's orphan recovery forever; and the
     three prompt-writing runtimes pair ``register()`` with the failure path
     of the ``stdin.write`` that follows it (the kill-at-spawn path makes a
     ``BrokenPipeError`` there reachable).
R3 — the dispatch re-stamp is a sync sqlite write and must not run on the
     event loop.
S1/S3 — ``/api/chat`` discards its pending entry even when the lock wait is
     cancelled; Phase 3 reads in-flight verdicts once per cycle, not per row.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import json
import sys
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis
import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent.parent
_BACKEND = _ROOT / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

_STUBBED_MODULE_NAMES = ["database", "db_models"]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {n: sys.modules.get(n) for n in _STUBBED_MODULE_NAMES}
    _database_mod = sys.modules.get("database")
    _saved_db = getattr(_database_mod, "db", None) if _database_mod is not None else None
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
        if _database_mod is not None and _saved_db is not None:
            _database_mod.db = _saved_db


def _install_db_stub() -> None:
    db_stub = sys.modules.get("database")
    if db_stub is None:
        db_stub = types.ModuleType("database")
        sys.modules["database"] = db_stub

    class _Db:
        def get_max_parallel_tasks(self, agent_name: str) -> int:
            raise KeyError(agent_name)

    db_stub.db = _Db()


def _limiter_mod():
    _install_db_stub()
    if "services.agent_call_limiter" in sys.modules:
        return sys.modules["services.agent_call_limiter"]
    return importlib.import_module("services.agent_call_limiter")


@pytest.fixture
def fake_redis():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def limiter(fake_redis, monkeypatch):
    mod = _limiter_mod()
    mod._reset_for_testing(
        global_limit=100, queue_timeout_s=10.0,
        client_factory=lambda: fake_redis,
        slot_renewer=lambda agent, eid: True,
    )
    monkeypatch.setattr(mod, "INFLIGHT_TICK_SECONDS", 0.01)
    monkeypatch.setattr(mod, "INFLIGHT_MARKER_GRACE_SECONDS", 0.0)
    yield mod
    mod._reset_for_testing()


def _phase(fake_redis, eid: str):
    raw = fake_redis.get(f"execution:inflight:{eid}")
    return None if raw is None else json.loads(raw).get("phase")


# ---------------------------------------------------------------------------
# R1 — the marker may only ever be stale in the SAFE direction
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_grant_publishes_calling_before_the_next_refresher_tick(limiter, fake_redis, monkeypatch):
    """The transition is published in the same round-trip that reads the cancel
    key — NOT left for the 15s refresher, which is what made the marker lie."""
    monkeypatch.setattr(limiter, "INFLIGHT_TICK_SECONDS", 3600)  # refresher can't help
    async with limiter.track_inflight_dispatch("exec-pub", "agent-a", http_timeout=60):
        async with limiter.acquire_agent_call_slot("agent-a", execution_id="exec-pub"):
            assert _phase(fake_redis, "exec-pub") == "calling"


@pytest.mark.asyncio
async def test_remote_cancel_cannot_read_a_stale_parked_under_a_live_post(limiter, fake_redis, monkeypatch):
    """The regression itself. A tick publishes `parked`; the entry then grants
    and is mid-POST. A cancel arriving on the OTHER worker must NOT be told
    `parked` — that answer makes it write CANCELLED and release the slot while
    the agent runs the turn to a billed completion (#378)."""
    monkeypatch.setattr(limiter, "INFLIGHT_TICK_SECONDS", 3600)
    async with limiter.track_inflight_dispatch("exec-race", "agent-a", http_timeout=60):
        entry = limiter.inflight_entry("exec-race")
        # A refresher tick caught it parked and published that.
        fake_redis.set("execution:inflight:exec-race", limiter._marker_payload(entry), ex=60)
        assert _phase(fake_redis, "exec-race") == "parked"

        async with limiter.acquire_agent_call_slot("agent-a", execution_id="exec-race"):
            # Mid-POST. The other worker asks.
            phase = await limiter.request_cross_worker_cancel("exec-race", agent_name="agent-a")
            assert phase == "calling", "a live POST must never be reported as parked"


@pytest.mark.asyncio
async def test_remote_cancel_reads_the_phase_after_writing_the_cancel_key(limiter, fake_redis):
    """Ordering: the key must be written BEFORE the deciding read, so an
    observed `parked` guarantees the owner's grant will see the key."""
    mod = limiter
    order: list = []
    real_pipeline = fake_redis.pipeline

    class _Recorder:
        def __init__(self, inner):
            self._inner = inner

        def set(self, key, *a, **k):
            order.append(("set", key))
            self._inner.set(key, *a, **k)
            return self

        def get(self, key):
            order.append(("get", key))
            self._inner.get(key)
            return self

        def execute(self):
            return self._inner.execute()

    fake_redis.pipeline = lambda *a, **k: _Recorder(real_pipeline(*a, **k))
    try:
        async with mod.track_inflight_dispatch("exec-ord", "agent-a", http_timeout=60):
            entry = mod.inflight_entry("exec-ord")
            fake_redis.set("execution:inflight:exec-ord", mod._marker_payload(entry), ex=60)
            assert await mod.request_cross_worker_cancel("exec-ord", agent_name="agent-a") == "parked"
    finally:
        fake_redis.pipeline = real_pipeline

    assert order == [("set", "execution:cancel:exec-ord"), ("get", "execution:inflight:exec-ord")]
    assert fake_redis.get("execution:cancel:exec-ord") == "1"


@pytest.mark.asyncio
async def test_scope_check_runs_before_any_cancel_key_is_written(limiter, fake_redis):
    """The agent scope is decided on the FIRST read — a caller authorised on
    another agent must not leave a cancel key behind."""
    async with limiter.track_inflight_dispatch("exec-scope", "agent-a", http_timeout=60):
        entry = limiter.inflight_entry("exec-scope")
        fake_redis.set("execution:inflight:exec-scope", limiter._marker_payload(entry), ex=60)
        assert await limiter.request_cross_worker_cancel("exec-scope", agent_name="agent-b") is None
    assert fake_redis.get("execution:cancel:exec-scope") is None


@pytest.mark.asyncio
async def test_publish_gates_on_entry_age_not_this_attempts_park(limiter, fake_redis, monkeypatch):
    """`track_inflight_dispatch` wraps the whole retry loop, so a retry can
    grant instantly under a marker a tick left saying `parked`. The gate is the
    ENTRY's age, so the transition is still published."""
    monkeypatch.setattr(limiter, "INFLIGHT_TICK_SECONDS", 3600)
    monkeypatch.setattr(limiter, "INFLIGHT_MARKER_GRACE_SECONDS", 0.05)
    async with limiter.track_inflight_dispatch("exec-retry", "agent-a", http_timeout=60):
        entry = limiter.inflight_entry("exec-retry")
        fake_redis.set("execution:inflight:exec-retry", limiter._marker_payload(entry), ex=60)
        await asyncio.sleep(0.08)  # entry is now older than the grace
        async with limiter.acquire_agent_call_slot("agent-a", execution_id="exec-retry"):
            # This attempt's park was ~0s, well under the grace.
            assert _phase(fake_redis, "exec-retry") == "calling"


@pytest.mark.asyncio
async def test_a_fast_first_call_still_never_touches_redis(limiter, fake_redis, monkeypatch):
    """The hot path stays free: a young entry publishes nothing."""
    monkeypatch.setattr(limiter, "INFLIGHT_TICK_SECONDS", 3600)
    monkeypatch.setattr(limiter, "INFLIGHT_MARKER_GRACE_SECONDS", 300)
    async with limiter.track_inflight_dispatch("exec-fast", "agent-a", http_timeout=60):
        async with limiter.acquire_agent_call_slot("agent-a", execution_id="exec-fast"):
            assert fake_redis.get("execution:inflight:exec-fast") is None


@pytest.mark.asyncio
async def test_publish_and_check_is_fail_soft(limiter, monkeypatch):
    """A Redis error at grant reads as 'no cancel', exactly as the bare read
    it replaced did — it must never abort a dispatch."""
    class _Boom:
        def pipeline(self, *a, **k):
            raise ConnectionError("down")

    monkeypatch.setattr(limiter, "_client_factory", lambda: _Boom())
    async with limiter.track_inflight_dispatch("exec-boom", "agent-a", http_timeout=60):
        async with limiter.acquire_agent_call_slot("agent-a", execution_id="exec-boom"):
            pass  # no raise


# ---------------------------------------------------------------------------
# R3 — the re-stamp is a sync sqlite write; it must not run on the loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restamp_runs_off_the_event_loop(limiter, monkeypatch):
    import threading

    tes = importlib.import_module("services.task_execution_service")
    monkeypatch.setattr(limiter, "DISPATCH_RESTAMP_THRESHOLD_SECONDS", 0.0)
    loop_thread = threading.get_ident()
    seen: dict = {}

    fake_db = MagicMock()

    def _restamp(eid):
        seen["thread"] = threading.get_ident()
        return True

    fake_db.restamp_execution_dispatch.side_effect = _restamp
    slot_mod = MagicMock()
    slot_mod.get_slot_service.return_value = MagicMock(renew_slot=MagicMock(return_value=True))
    monkeypatch.setitem(sys.modules, "services.slot_service", slot_mod)

    class _Resp:
        status_code = 200

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *a, **k):
            return _Resp()

    with patch.object(tes, "db", fake_db), patch.object(tes, "agent_httpx_client", lambda *a, **k: _Client()):
        await tes.agent_post_with_retry(
            "agent-a", "/api/task", {"execution_id": "exec-t"}, timeout=60, execution_id="exec-t"
        )

    assert seen["thread"] != loop_thread, "restamp_execution_dispatch ran on the event loop"


# ---------------------------------------------------------------------------
# R2 — bounded exited-but-registered set + register/stdin-write pairing
# ---------------------------------------------------------------------------

def _registry_mod():
    if "agent_server" not in sys.modules:
        pkg = types.ModuleType("agent_server")
        pkg.__path__ = [str(_ROOT / "docker" / "base-image" / "agent_server")]
        sys.modules["agent_server"] = pkg
    if "agent_server.services" not in sys.modules:
        sub = types.ModuleType("agent_server.services")
        sub.__path__ = [str(_ROOT / "docker" / "base-image" / "agent_server" / "services")]
        sys.modules["agent_server.services"] = sub
    if "agent_server.utils" not in sys.modules:
        u = types.ModuleType("agent_server.utils")
        u.__path__ = [str(_ROOT / "docker" / "base-image" / "agent_server" / "utils")]
        sys.modules["agent_server.utils"] = u
    return importlib.import_module("agent_server.services.process_registry")


class _ExitedProcess:
    pid = 4242

    def poll(self):
        return 0


def test_leaked_exited_entry_stops_being_reported_after_the_ttl(monkeypatch):
    """An entry that leaks (exception between `register()` and
    `finally: unregister()`) used to be reported as agent-known FOREVER, so the
    watchdog never recovered that row — a regression against pre-#2433, where
    `list_running()`'s `poll() is None` filter self-healed it."""
    mod = _registry_mod()
    reg = mod.ProcessRegistry()
    reg.register("leaked-eid", _ExitedProcess(), metadata={"type": "task"})

    assert "leaked-eid" in reg.list_recently_completed_ids()
    assert reg.list_running() == [] or all(
        e.get("execution_id") != "leaked-eid" for e in reg.list_running()
    )

    real_time = time.time
    monkeypatch.setattr(
        mod.time, "time", lambda: real_time() + mod.RECENTLY_COMPLETED_TTL_SECONDS + 1
    )
    assert "leaked-eid" not in reg.list_recently_completed_ids()


def test_a_long_turn_entering_its_drain_is_still_reported(monkeypatch):
    """The bound is time-since-EXIT-observed, never time-since-START: an 8-min
    turn that just exited must stay covered, which is the hole #2433 closed."""
    mod = _registry_mod()
    reg = mod.ProcessRegistry()
    reg.register("long-eid", _ExitedProcess(), metadata={"type": "task"})
    entry = reg._processes["long-eid"]
    entry["started_at"] = entry["started_at"].replace(year=entry["started_at"].year - 1)

    assert "long-eid" in reg.list_recently_completed_ids()


def _stdin_write_is_guarded(path: Path) -> bool:
    """True when every `process.stdin.write(...)` in the module sits inside a
    `try` whose handler calls `unregister`."""
    tree = ast.parse(path.read_text())

    def _writes(node) -> bool:
        for n in ast.walk(node):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "write"
                and isinstance(n.func.value, ast.Attribute)
                and n.func.value.attr == "stdin"
            ):
                return True
        return False

    def _unregisters(node) -> bool:
        return any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "unregister"
            for n in ast.walk(node)
        )

    guarded = 0
    total = sum(1 for n in ast.walk(tree) if _writes(n) and isinstance(n, ast.Expr))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        if not any(_writes(stmt) for stmt in node.body):
            continue
        if any(_unregisters(h) for h in node.handlers) or _unregisters(
            ast.Module(body=node.finalbody, type_ignores=[])
        ):
            guarded += sum(1 for stmt in node.body if _writes(stmt))
    return total > 0 and guarded == total


@pytest.mark.parametrize(
    "rel",
    [
        "docker/base-image/agent_server/services/gemini_runtime.py",
        "docker/base-image/agent_server/services/claude_code.py",
    ],
)
def test_register_is_paired_with_the_stdin_write_failure_path(rel):
    """`register()` SIGKILLs the group when a cancel landed while the execution
    was pending, so the very next `stdin.write` can raise BrokenPipeError. The
    `finally: unregister()` guards only the reader further down — without this
    pairing the entry leaks, and a leaked entry blocks orphan recovery for a
    whole RECENTLY_COMPLETED TTL."""
    assert _stdin_write_is_guarded(_ROOT / rel), f"{rel}: stdin.write is not paired with unregister"


# ---------------------------------------------------------------------------
# S1 — /api/chat discards its pending entry even if the lock wait is cancelled
# ---------------------------------------------------------------------------

def _chat_mod():
    _registry_mod()
    return importlib.import_module("agent_server.routers.chat")


def test_chat_discards_pending_when_the_lock_wait_is_cancelled():
    chat_mod = _chat_mod()
    registry = MagicMock()

    class _Lock:
        async def __aenter__(self):
            raise asyncio.CancelledError()

        async def __aexit__(self, *exc):
            return False

    request = types.SimpleNamespace(
        message="hi", model=None, stream=False, system_prompt=None, execution_id="exec-cancelled"
    )
    with (
        patch.object(chat_mod, "get_execution_lock", return_value=_Lock()),
        patch.object(chat_mod, "get_process_registry", return_value=registry),
    ):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(chat_mod.chat(request))

    registry.discard_pending.assert_called_once_with("exec-cancelled")


def test_chat_pending_window_covers_the_agent_timeout_ceiling():
    """`ChatRequest` carries no timeout, and a chat can wait on the execution
    lock for up to the agent's `execution_timeout_seconds` (ceiling 7200s).
    Sizing the entry off the /api/task default evicted it mid-wait, so
    `pending_ids` silently stopped covering the wait it exists for."""
    mod = _registry_mod()
    assert mod.PENDING_CHAT_TIMEOUT_SECONDS >= 7200
    assert mod.PENDING_CHAT_TIMEOUT_SECONDS > mod.PENDING_DEFAULT_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# S3 — Phase 3 reads in-flight verdicts once per cycle, not once per row
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stale_slot_reclaims_batch_the_verdict_read(monkeypatch):
    cleanup = importlib.import_module("services.cleanup_service")
    calls: list = []

    async def _fake_verdicts(ids):
        calls.append(list(ids))
        return {eid: "alive" for eid in ids}

    monkeypatch.setattr(cleanup, "_inflight_verdicts", _fake_verdicts, raising=False)
    monkeypatch.setattr(cleanup, "_inflight_verdict_map", _fake_verdicts, raising=False)

    svc = cleanup.CleanupService()
    report = cleanup.CleanupReport()
    reclaimed = {"agent-a": ["e1", "e2", "e3"], "agent-b": ["e4", "e5"]}

    async def _running_ids(client, name):
        return set()

    monkeypatch.setattr(svc, "_get_agent_running_ids", _running_ids)
    await svc._process_stale_slot_reclaims(reclaimed, set(), report)

    assert len(calls) == 1, f"one batched read per cycle, got {len(calls)}"
    assert sorted(calls[0]) == ["e1", "e2", "e3", "e4", "e5"]
    assert report.dispatch_inflight_skipped == 5
