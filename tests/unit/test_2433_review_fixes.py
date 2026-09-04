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
async def test_publish_and_check_is_fail_soft_and_the_residual_is_recorded(limiter, monkeypatch):
    """A Redis error at grant reads as 'no cancel', exactly as the bare read it
    replaced did — it must never abort a dispatch (every other Redis touch in
    this subsystem is fail-open; failing closed would fail EVERY dispatch on a
    blip).

    The second half of that is the residual, recorded here as well as in the
    helper's docstring because "fails soft" reads as covering both halves and
    covers only the read: the transition is then never published either, so a
    remote worker with a healthy connection can still read a stale `parked` and
    finalize CANCELLED under this live POST. Bounded by the negative cache plus
    one tick, and unreachable on a HARD outage (a process whose client is None
    never wrote a marker, so the remote routes through the agent)."""
    class _Boom:
        def pipeline(self, *a, **k):
            raise ConnectionError("down")

    monkeypatch.setattr(limiter, "_client_factory", lambda: _Boom())
    async with limiter.track_inflight_dispatch("exec-boom", "agent-a", http_timeout=60):
        async with limiter.acquire_agent_call_slot("agent-a", execution_id="exec-boom"):
            pass  # no raise — the dispatch proceeds, which is the documented direction
    assert limiter.INFLIGHT_REDIS_RETRY_SECONDS > 0, "the residual's bound must exist"


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


AGENT_SERVER = _ROOT / "docker" / "base-image" / "agent_server"


def _is_registry_module(tree: ast.AST, source: str) -> bool:
    """A module is in scope when it drives the process registry — that is what
    makes a live registration reachable from a `stdin.write`. A module that
    writes to a subprocess without ever registering it is a different (and not
    leaky) shape, and requiring `unregister` there would be nonsense.

    Scope is keyed on `register(`, not on `register_pending(` — a chosen
    boundary, not an oversight (#2450 review). `unregister()` discards a
    pending entry too, so the pairing would be meaningful for a module that
    only registers PENDING and writes stdin; no such module exists today
    (every pending registration is promoted by `register()` at spawn, and the
    write follows the spawn). Widen the predicate here if one ever appears."""
    if "process_registry" not in source and "get_process_registry" not in source:
        return False
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "register"
        for n in ast.walk(tree)
    )


def _stdin_writes(node: ast.AST):
    """Every `<something>.stdin.write(...)` call in this subtree."""
    return [
        n for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "write"
        and isinstance(n.func.value, ast.Attribute)
        and n.func.value.attr == "stdin"
    ]


def _unregisters(nodes) -> bool:
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "unregister"
        for node in nodes for n in ast.walk(node)
    )


def _unguarded_stdin_writes(source: str, label: str):
    """Return `(label, lineno)` for every stdin write that no `unregister` will
    cover. Two shapes are accepted, because both occur in the tree:

    * **local** — the write sits in a `try` whose handlers or `finally`
      unregister (`claude_code`, `gemini_runtime` x2 after this PR);
    * **caller-paired** — the write's enclosing function is *referenced* inside
      a `try` whose `finally` unregisters, so an exception propagates to that
      `finally` (`headless_executor._run_headless_subprocess`, handed to
      `run_in_executor` inside `execute_headless_task`'s guarded try).

    Caller pairing is recognised only through a bare NAME reference, never an
    attribute call (#2450 review). Collecting `ast.Attribute` attrs too made
    the exemption fire on any method call in any guarded try, so a plain
    `try: runtime.execute(...) finally: unregister()` silently exempted EVERY
    function named `execute` in the module — and `execute`/`run`/`send` are
    exactly the names a dispatch try calls. That is a false negative landing
    on the case discovery was added for: the four known sites are pinned by
    `test_each_known_site_is_pinned_to_its_pairing_mechanism`, but a NEW
    module has no such pin. The one legitimate caller-paired site passes a
    bare name (`run_in_executor(_HEADLESS_EXECUTOR, _run_headless_subprocess,
    ctx)`), so the attribute half bought nothing and cost the guard its teeth.
    If a future site is genuinely paired through an attribute call it will be
    reported here — fix it by referencing the function by name or by adding a
    justified allowlist entry, never by re-adding the attribute half.

    Cross-MODULE caller pairing is deliberately not modelled either: it would
    fail loudly here rather than pass silently, which is the safe direction
    for a guard. A module with no stdin write is vacuously clean (empty list)
    — never a failure.
    """
    tree = ast.parse(source)
    if not _is_registry_module(tree, source):
        return []

    guarded_nodes = set()
    covered_names = set()
    for t in ast.walk(tree):
        if not isinstance(t, ast.Try):
            continue
        if not _unregisters(list(t.handlers) + list(t.finalbody)):
            continue
        for w in _stdin_writes(ast.Module(body=t.body, type_ignores=[])):
            guarded_nodes.add(w)
        for n in ast.walk(ast.Module(body=t.body, type_ignores=[])):
            if isinstance(n, ast.Name):
                covered_names.add(n.id)

    offenders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        caller_paired = fn.name in covered_names
        for w in _stdin_writes(fn):
            if w in guarded_nodes or caller_paired:
                continue
            offenders.append((label, w.lineno))
    return offenders


def _pairing_mechanisms(source: str):
    """`{lineno: "local" | "caller"}` for every stdin write in a registry
    module. Coverage by CALLER is name-based (no call-graph), so a collision
    between a guarded try's identifiers and a function name could in principle
    mask a real offender — a false NEGATIVE. Pinning the mechanism per known
    site is the cheap defence: if a site silently switches from `local` to
    `caller` (or the reverse), the test says so instead of passing on the
    weaker path."""
    tree = ast.parse(source)
    guarded_nodes = set()
    covered_names = set()
    for t in ast.walk(tree):
        if not isinstance(t, ast.Try):
            continue
        if not _unregisters(list(t.handlers) + list(t.finalbody)):
            continue
        body = ast.Module(body=t.body, type_ignores=[])
        guarded_nodes.update(_stdin_writes(body))
        for n in ast.walk(body):
            if isinstance(n, ast.Name):
                covered_names.add(n.id)

    out = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for w in _stdin_writes(fn):
            if w in guarded_nodes:
                out[w.lineno] = "local"
            elif fn.name in covered_names:
                out[w.lineno] = "caller"
    return out


def _agent_server_registry_modules():
    """DISCOVER, never enumerate. A hardcoded list is the failure this guard
    exists to prevent one level up (#1677's caller-parity guard walks every
    call site; Invariant #5: 'a guard that walks only one of the two trees is
    not a guard'). A filename glob is not enough either — `claude_code.py` and
    `headless_executor.py` are both real sites and match no `*_runtime.py`."""
    return sorted(
        p for p in AGENT_SERVER.rglob("*.py")
        if _is_registry_module(ast.parse(p.read_text()), p.read_text())
    )


def test_no_agent_server_module_writes_stdin_without_pairing_unregister():
    """#2448: `register()` SIGKILLs the group when a cancel landed while the
    execution was pending, so the next `stdin.write` can raise BrokenPipeError.
    An unpaired write leaks the registry entry, and a leaked entry blocks that
    row's orphan recovery for a whole RECENTLY_COMPLETED TTL.

    The predecessor of this test ENUMERATED two files and therefore guarded
    nothing (#2435 re-review) — it is the discovery that makes the claim true."""
    modules = _agent_server_registry_modules()
    offenders = []
    for path in modules:
        offenders += _unguarded_stdin_writes(path.read_text(), str(path.relative_to(_ROOT)))
    assert not offenders, (
        "stdin.write not paired with unregister:\n"
        + "\n".join(f"  {f}:{ln}" for f, ln in offenders)
        + "\n\nAccepted shapes: wrap the write in `try/except BaseException: "
          "unregister(); raise`, or call the writing function from inside a try "
          "whose `finally` unregisters."
    )


def test_the_guard_actually_discovers_the_known_sites():
    """A discovery guard that discovers nothing passes vacuously — pin the
    floor so a broken walk (moved tree, renamed package) fails loudly."""
    found = {p.name for p in _agent_server_registry_modules()}
    assert {"claude_code.py", "gemini_runtime.py", "headless_executor.py", "codex_runtime.py"} <= found, found

    writes = sum(
        len(_stdin_writes(ast.parse(p.read_text())))
        for p in _agent_server_registry_modules()
    )
    assert writes >= 4, f"expected the 4 known stdin writes, examined {writes}"


def test_each_known_site_is_pinned_to_its_pairing_mechanism():
    """`claude_code` and `gemini_runtime` are paired LOCALLY (the write sits in
    its own try/except that unregisters). `headless_executor` is paired at its
    CALLER — `_run_headless_subprocess` is handed to `run_in_executor` inside
    `execute_headless_task`'s guarded try — which is why an enumerating guard
    (or a `*_runtime.py` glob) never saw it as a site at all."""
    svc = AGENT_SERVER / "services"
    assert set(_pairing_mechanisms((svc / "claude_code.py").read_text()).values()) == {"local"}
    gemini = _pairing_mechanisms((svc / "gemini_runtime.py").read_text())
    assert len(gemini) == 2 and set(gemini.values()) == {"local"}
    headless = _pairing_mechanisms((svc / "headless_executor.py").read_text())
    assert len(headless) == 1 and set(headless.values()) == {"caller"}


def test_the_guard_catches_an_unpaired_new_runtime():
    """#2448 — the reviewer's PoC: a new runtime dropped into the tree with a
    bare write must be caught. The enumerating guard answered '2 passed, 13
    deselected' to exactly this."""
    src = """
from ..services.process_registry import get_process_registry

def execute(prompt, process, execution_id):
    get_process_registry().register(execution_id, process, metadata={})
    process.stdin.write(prompt)
    process.stdin.close()
"""
    assert _unguarded_stdin_writes(src, "mistral_runtime.py") == [("mistral_runtime.py", 6)]


def test_a_method_call_in_a_guarded_try_does_not_exempt_a_same_named_function():
    """#2450 review — the reachable false negative. Collecting `ast.Attribute`
    attrs into the caller-pairing set made ANY method call inside ANY guarded
    try exempt every same-named function in the module, so this ordinary shape
    hid a real leak. `execute` / `run` / `send` are precisely the names a
    dispatch try calls."""
    planted = """
from ..services.process_registry import get_process_registry

class Runtime:
    def execute(self, prompt, process, execution_id):
        get_process_registry().register(execution_id, process, metadata={})
        process.stdin.write(prompt)
        process.stdin.close()

    def dispatch(self, runtime, execution_id, prompt, process):
        try:
            return runtime.execute(prompt, process, execution_id)
        finally:
            get_process_registry().unregister(execution_id)
"""
    assert _unguarded_stdin_writes(planted, "planted.py") == [("planted.py", 7)]


def test_the_guard_accepts_both_pairing_shapes():
    local = """
from ..services.process_registry import get_process_registry

def execute(prompt, process, eid):
    get_process_registry().register(eid, process, metadata={})
    try:
        process.stdin.write(prompt)
    except BaseException:
        get_process_registry().unregister(eid)
        raise
"""
    caller = """
from ..services.process_registry import get_process_registry

def _run(ctx, process):
    get_process_registry().register(ctx.eid, process, metadata={})
    process.stdin.write(ctx.prompt)

async def execute(ctx):
    registry = get_process_registry()
    try:
        await loop.run_in_executor(None, _run, ctx)
    finally:
        registry.unregister(ctx.eid)
"""
    assert _unguarded_stdin_writes(local, "local.py") == []
    assert _unguarded_stdin_writes(caller, "caller.py") == []


def test_a_registry_module_with_no_stdin_write_is_vacuously_clean():
    """`codex_runtime` registers but uses `stdin=DEVNULL`. The old helper
    returned `total > 0 and guarded == total`, so a discovered file with no
    write would have read as a FAILURE the moment the guard stopped
    enumerating."""
    src = """
from ..services.process_registry import get_process_registry

def execute(process, eid):
    get_process_registry().register(eid, process, metadata={})
"""
    assert _unguarded_stdin_writes(src, "codex_like.py") == []
    codex = AGENT_SERVER / "services" / "codex_runtime.py"
    assert _unguarded_stdin_writes(codex.read_text(), "codex_runtime.py") == []


def test_a_non_registry_module_is_out_of_scope():
    """A module that writes to a subprocess without registering it is a
    different shape; demanding `unregister` there would be nonsense."""
    src = """
def run(process, payload):
    process.stdin.write(payload)
    process.stdin.close()
"""
    assert _unguarded_stdin_writes(src, "plain.py") == []


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
