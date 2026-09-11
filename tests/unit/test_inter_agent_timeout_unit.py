"""
Inter-agent timeout unit tests (test_inter_agent_timeout_unit.py)

Issue #418. Verifies that the inter-agent execution path honours the target
agent's configured `execution_timeout_seconds` (TIMEOUT-001) instead of a
hardcoded 600s ceiling. Covers:

- FanOutRequest model accepts omitted / None `timeout_seconds` and validates it.
- FanOutService dispatches each sub-task with `timeout_seconds=None` so the
  TaskExecutionService resolves the per-agent config, regardless of whether
  an outer fan-out deadline is set.
- When no outer deadline is set, the asyncio.timeout() wrap is skipped.
- When an outer deadline is set, the wrap is applied but sub-tasks still
  receive `timeout_seconds=None`.

Runs as a pure unit test — no backend container required.
"""

import asyncio
import importlib.util
import os
import sys
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add backend to path so relative imports inside the target modules resolve.
_backend_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend")
)
if _backend_path not in sys.path:
    sys.path.insert(0, _backend_path)


# #1895: the import-time stubs below must not leak into a LATER unit file's
# import — the conftest's per-test restore runs only once collection has finished,
# too late for a cross-file collection leak. Use the sanctioned
# _STUBBED_MODULE_NAMES/_restore_sys_modules pair (precedent:
# tests/unit/test_telegram_webhook_backfill.py) and call the restore below once the
# collection-time module loads have bound what they need.
_STUBBED_MODULE_NAMES = ["database", "utils.helpers", "services", "services.task_execution_service", "services.fan_out_service", "dependencies", "models", "routers", "routers.fan_out"]
_SAVED_STUBBED_MODULES = {_k: sys.modules.get(_k) for _k in _STUBBED_MODULE_NAMES}


def _restore_sys_modules():
    for _k in _STUBBED_MODULE_NAMES:
        _v = _SAVED_STUBBED_MODULES[_k]
        if _v is not None:
            sys.modules[_k] = _v
        else:
            sys.modules.pop(_k, None)


# Stub database and utils so fan_out_service can be imported without a
# running backend.
_fake_db = MagicMock()
sys.modules.setdefault("database", types.SimpleNamespace(db=_fake_db))

if "utils.helpers" not in sys.modules:
    _helpers = types.ModuleType("utils.helpers")
    _helpers.utc_now = lambda: datetime.utcnow()
    _helpers.utc_now_iso = lambda: datetime.utcnow().isoformat() + "Z"
    _helpers.to_utc_iso = lambda v: str(v)
    _helpers.parse_iso_timestamp = lambda s: datetime.fromisoformat(s.rstrip("Z"))
    sys.modules["utils.helpers"] = _helpers


# Load fan_out_service directly by file path to bypass services/__init__.py
# which imports unrelated modules that need a full backend env.
if "services" not in sys.modules:
    sys.modules["services"] = types.ModuleType("services")

# Stub task_execution_service before fan_out_service imports it.
_fake_tes = types.ModuleType("services.task_execution_service")
_fake_tes.get_task_execution_service = MagicMock()
_fake_tes.TaskExecutionResult = MagicMock  # sentinel, only name is used by type hint
_fake_tes.TaskExecutionErrorCode = MagicMock  # ditto
sys.modules["services.task_execution_service"] = _fake_tes

_fos_path = os.path.join(_backend_path, "services", "fan_out_service.py")
_spec = importlib.util.spec_from_file_location(
    "services.fan_out_service", _fos_path
)
fos = importlib.util.module_from_spec(_spec)
sys.modules["services.fan_out_service"] = fos
_spec.loader.exec_module(fos)  # type: ignore[union-attr]

FanOutService = fos.FanOutService
FanOutTaskInput = fos.FanOutTaskInput


# Load the FanOutRequest pydantic model. Needs `dependencies` and `models`
# stubbed because routers/fan_out.py imports them at module load.
_fake_deps = types.ModuleType("dependencies")
_fake_deps.get_authorized_agent = lambda: None
_fake_deps.get_current_user = lambda: None
sys.modules.setdefault("dependencies", _fake_deps)

_fake_models = types.ModuleType("models")
_fake_models.User = MagicMock  # only referenced as a type hint
sys.modules.setdefault("models", _fake_models)

_fo_router_path = os.path.join(_backend_path, "routers", "fan_out.py")
if "routers" not in sys.modules:
    sys.modules["routers"] = types.ModuleType("routers")
_spec2 = importlib.util.spec_from_file_location(
    "routers.fan_out", _fo_router_path
)
fo_router = importlib.util.module_from_spec(_spec2)
sys.modules["routers.fan_out"] = fo_router
_spec2.loader.exec_module(fo_router)  # type: ignore[union-attr]

FanOutRequest = fo_router.FanOutRequest

_restore_sys_modules()


# Override the backend-requiring autouse fixtures from the package conftest.
@pytest.fixture(scope="session")
def api_client():
    yield None


@pytest.fixture(autouse=True)
def cleanup_after_test():
    yield
    # #2524: `_install_rows` swaps the `database` module so the LATE import
    # inside `sync_waiter.wait_for_fan_out_batch` resolves to the fake too.
    # Put it back, per the file's _STUBBED_MODULE_NAMES contract.
    _restore_sys_modules()


# ---------------------------------------------------------------------------
# FanOutRequest model
# ---------------------------------------------------------------------------


def test_fan_out_request_allows_omitted_timeout():
    """Issue #418: timeout_seconds is optional and defaults to None."""
    req = FanOutRequest(tasks=[{"id": "t1", "message": "hi"}])
    assert req.timeout_seconds is None


def test_fan_out_request_accepts_explicit_timeout():
    req = FanOutRequest(
        tasks=[{"id": "t1", "message": "hi"}],
        timeout_seconds=300,
    )
    assert req.timeout_seconds == 300


def test_fan_out_request_rejects_out_of_range_timeout():
    with pytest.raises(Exception):
        FanOutRequest(
            tasks=[{"id": "t1", "message": "hi"}],
            timeout_seconds=5,
        )
    with pytest.raises(Exception):
        FanOutRequest(
            tasks=[{"id": "t1", "message": "hi"}],
            timeout_seconds=10_000,
        )


def test_fan_out_request_accepts_none_explicitly():
    """Explicit None round-trips — no validator error."""
    req = FanOutRequest(
        tasks=[{"id": "t1", "message": "hi"}],
        timeout_seconds=None,
    )
    assert req.timeout_seconds is None


# ---------------------------------------------------------------------------
# FanOutService — per-subtask timeout forwarding
# ---------------------------------------------------------------------------


def _make_success_result():
    """Build a minimal TaskExecutionResult-shaped object."""
    r = MagicMock()
    r.status = "success"
    r.response = "ok"
    r.execution_id = "exec_1"
    r.cost = None
    r.context_used = None
    r.error = None
    r.error_code = None
    return r


class _FanOutRows:
    """The `schedule_executions` surface a fan-out batch needs (#2524).

    The aggregate is a query over `fan_out_id` now, not a dict in the
    dispatching coroutine, so a bare `MagicMock()` db no longer stands in for
    it — `count_fan_out_open` has to return a real integer or the batch never
    completes, and `list_fan_out_executions` has to return real rows or there is
    no aggregate to assert on.
    """

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self._n = 0

    def create_task_execution(self, **kw):
        self._n += 1
        eid = f"exec_{self._n}"
        self.rows[eid] = {
            "id": eid,
            "agent_name": kw.get("agent_name"),
            "fan_out_id": kw.get("fan_out_id"),
            "fan_out_task_id": kw.get("fan_out_task_id"),
            "status": "running",
            "response": None,
            "error": None,
            "cost": None,
            "context_used": None,
            "duration_ms": None,
        }
        return types.SimpleNamespace(id=eid, fan_out_id=kw.get("fan_out_id"))

    def finish(self, execution_id, status="success", response="ok", error=None):
        self.rows[execution_id].update(status=status, response=response, error=error)

    def list_fan_out_executions(self, fan_out_id):
        return [dict(r) for r in self.rows.values() if r["fan_out_id"] == fan_out_id]

    def count_fan_out_open(self, fan_out_id):
        return sum(
            1 for r in self.rows.values()
            if r["fan_out_id"] == fan_out_id
            and r["status"] in ("queued", "running", "pending_retry")
        )

    def get_execution(self, execution_id):
        row = self.rows.get(execution_id)
        return types.SimpleNamespace(**row) if row else None

    def get_execution_timeout(self, agent_name):
        return 600

    def update_execution_status(self, **_kw):
        return True


def _install_mock_task_service(call_log: list, rows: "_FanOutRows"):
    """Install a mock task execution service that records each call.

    #2524: it also writes the terminal onto the row, because the batch's outcome
    is read from the rows — a fake that only returned a result would exercise a
    path production does not have.
    """
    async def _execute_task(**kwargs):
        call_log.append(kwargs)
        rows.finish(kwargs["execution_id"])
        return _make_success_result()

    svc = MagicMock()
    svc.execute_task = AsyncMock(side_effect=_execute_task)
    fos.get_task_execution_service = lambda: svc
    return svc


def _install_rows() -> "_FanOutRows":
    """Point every `db` a fan-out batch reaches at a fresh row store.

    Two bindings, because there are two import styles in play: `fan_out_service`
    binds `db` at module import (`from database import db`), while
    `sync_waiter.wait_for_fan_out_batch` late-imports it per call to keep that
    module dependency-light. Patching only one leaves the batch wait talking to
    the real singleton, which is how this harness first failed — the service
    stopped being db-free the moment the aggregate became a query (#2524).
    """
    rows = _FanOutRows()
    fos.db = rows
    sys.modules["database"] = types.SimpleNamespace(db=rows)
    return rows


def test_fan_out_service_forwards_none_per_subtask_without_outer_deadline():
    """Issue #418: each sub-task is dispatched with timeout_seconds=None so
    TaskExecutionService resolves the agent's configured timeout."""
    calls: list = []
    rows = _install_rows()
    _install_mock_task_service(calls, rows)

    service = FanOutService()
    tasks = [FanOutTaskInput(id=f"t{i}", message=f"task {i}") for i in range(3)]

    result = asyncio.run(
        service.execute(
            agent_name="delegate-1",
            tasks=tasks,
            max_concurrency=3,
            timeout_seconds=None,
        )
    )

    assert result.total == 3
    assert result.completed == 3
    assert len(calls) == 3
    for call in calls:
        assert call["timeout_seconds"] is None, (
            "Per-subtask timeout must be None so the agent's configured "
            "execution_timeout_seconds is used (TIMEOUT-001)"
        )
        assert call["agent_name"] == "delegate-1"


def test_fan_out_service_forwards_none_per_subtask_with_outer_deadline():
    """Outer fan-out deadline wraps the gather, but each sub-task is still
    dispatched with timeout_seconds=None (per-agent config applies)."""
    calls: list = []
    rows = _install_rows()
    _install_mock_task_service(calls, rows)

    service = FanOutService()
    tasks = [FanOutTaskInput(id="t1", message="one")]

    result = asyncio.run(
        service.execute(
            agent_name="delegate-2",
            tasks=tasks,
            max_concurrency=1,
            timeout_seconds=300,
        )
    )

    assert result.total == 1
    assert result.completed == 1
    assert len(calls) == 1
    assert calls[0]["timeout_seconds"] is None


def test_fan_out_service_outer_deadline_actually_applies():
    """The outer deadline still ends the WAIT — but no longer the work (#2524).

    It used to wrap the `gather` in `asyncio.timeout`, cancelling in-flight
    subtasks and reporting them `failed`/`timeout`. A queued or claimed row is
    not the backend's to cancel, and on push that cancellation was always
    half-illusory: it abandoned the HTTP call while the agent kept running (and
    billing for) the turn.

    So the batch still reports `deadline_exceeded`, and the subtask now reports
    `running` — the honest answer, since nothing stopped it. Reporting `failed`
    would be a lie the moment it succeeds, which it usually does. The status
    endpoint is the source of truth after a deadline.
    """
    rows = _install_rows()
    started = asyncio.Event()

    async def _slow_task(**kwargs):
        started.set()
        await asyncio.sleep(30)  # outlives the deadline; never completes here
        rows.finish(kwargs["execution_id"])
        return _make_success_result()

    svc = MagicMock()
    svc.execute_task = AsyncMock(side_effect=_slow_task)
    fos.get_task_execution_service = lambda: svc

    service = FanOutService()
    tasks = [FanOutTaskInput(id="t1", message="slow")]

    result = asyncio.run(
        service.execute(
            agent_name="delegate-3",
            tasks=tasks,
            max_concurrency=1,
            timeout_seconds=1,
        )
    )

    assert result.status == "deadline_exceeded"
    assert result.total == 1
    assert result.completed == 0
    assert result.failed == 0, "an unfinished subtask is not a failed one (#2524)"
    assert result.results[0].status == "running"
    # The row is still open — the deadline did not touch the execution.
    assert rows.count_fan_out_open(result.fan_out_id) == 1


def test_fan_out_async_mode_returns_a_receipt_without_waiting():
    """#2524: `async_mode` returns as soon as the rows exist. The whole point is
    that the batch outlives the request that started it, which is also what a
    pull-claimed subtask needs — its turn runs later, in the agent's worker."""
    rows = _install_rows()

    async def _never_finishes(**kwargs):
        await asyncio.sleep(30)
        return _make_success_result()

    svc = MagicMock()
    svc.execute_task = AsyncMock(side_effect=_never_finishes)
    fos.get_task_execution_service = lambda: svc

    service = FanOutService()
    tasks = [FanOutTaskInput(id=f"t{i}", message=f"task {i}") for i in range(3)]

    async def _drive():
        return await asyncio.wait_for(
            service.execute(
                agent_name="delegate-4",
                tasks=tasks,
                max_concurrency=3,
                async_mode=True,
            ),
            timeout=2,  # must NOT block on the subtasks
        )

    result = asyncio.run(_drive())

    assert result.status == "accepted"
    assert result.fan_out_id.startswith("fo_")
    assert result.total == 3
    assert result.results == []
    # The rows exist before the caller is answered, so the batch is already
    # discoverable by `fan_out_id` — a status poll can never 404 a live batch.
    assert len(rows.list_fan_out_executions(result.fan_out_id)) == 3


def test_fan_out_status_rebuilds_the_aggregate_from_the_rows():
    """The join, and the reason `fan_out_task_id` had to become a column: the
    caller's own subtask ids have to survive into an aggregate assembled long
    after the dispatching coroutine is gone."""
    rows = _install_rows()
    service = FanOutService()

    fan_out_id = "fo_status_test"
    for task_id, status, response in (
        ("alpha", "success", "A"),
        ("beta", "failed", None),
        ("gamma", "running", None),
    ):
        execution = rows.create_task_execution(
            agent_name="delegate-5", fan_out_id=fan_out_id, fan_out_task_id=task_id,
        )
        if status != "running":
            rows.finish(execution.id, status=status, response=response,
                        error=None if status == "success" else "boom")

    result = service.get_status(fan_out_id)
    assert result is not None
    assert result.total == 3
    assert result.completed == 1
    assert result.failed == 1
    assert result.status == "running"  # one subtask still open
    by_id = {r.id: r for r in result.results}
    assert by_id["alpha"].status == "completed" and by_id["alpha"].response == "A"
    assert by_id["beta"].status == "failed" and by_id["beta"].error == "boom"
    assert by_id["gamma"].status == "running"

    assert service.get_status("fo_does_not_exist") is None
    assert service.batch_belongs_to(fan_out_id, "delegate-5") is True
    assert service.batch_belongs_to(fan_out_id, "someone-else") is False
