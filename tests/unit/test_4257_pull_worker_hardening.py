"""The two branches a pull worker used to stay silent in (#4257).

trinity#4257 — an idle pilot's queue did not move for hours and the logs from
the incident could not separate the candidate causes, because neither of these
branches said anything:

* `_claim_once` treats ANY non-200 as "queue empty" and sleeps. A 403 from a
  `PULL_MODE_PILOT_AGENTS` / `TRINITY_PULL_MODE` disagreement is therefore
  indistinguishable, from inside the worker, from a drained queue — and it was
  logged at debug.
* `_deliver_result` retried until the lease deadline (turn timeout + 300 s),
  holding one of N workers with no runtime process running. Three workers
  parked that way is a pool that looks alive and claims nothing.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

_AGENT_SERVER = (
    Path(__file__).resolve().parent.parent.parent
    / "docker" / "base-image" / "agent_server"
)
if str(_AGENT_SERVER.parent) not in sys.path:
    sys.path.insert(0, str(_AGENT_SERVER.parent))

pytestmark = pytest.mark.unit

pull_worker = pytest.importorskip("agent_server.services.pull_worker")


class _Resp:
    def __init__(self, status_code: int, payload: Any = None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Client:
    """A stub httpx client: scripted GET answers, counted POSTs."""

    def __init__(self, get_answers=(), post_answers=()):
        self._get = list(get_answers)
        self._post = list(post_answers)
        self.post_calls = 0

    async def get(self, url, **kwargs):
        return self._get.pop(0) if self._get else _Resp(200, {"envelope": None})

    async def post(self, url, **kwargs):
        self.post_calls += 1
        return self._post.pop(0) if self._post else _Resp(503)


@pytest.fixture(autouse=True)
def _reset_noted():
    pull_worker._NOTED_CLAIM_STATUS.clear()
    delivering = getattr(pull_worker, "_DELIVERING_NOW", None)
    if delivering is not None:
        delivering.clear()
    yield
    pull_worker._NOTED_CLAIM_STATUS.clear()
    if delivering is not None:
        delivering.clear()


def _claim(client, worker="alpha#w0"):
    return asyncio.run(
        pull_worker._claim_once(client, "http://backend", "key", "alpha", worker)
    )


# ---------------------------------------------------------------------------
# A refused claim is logged, once, and still backs off
# ---------------------------------------------------------------------------


def test_a_403_claim_warns_with_the_status_and_the_likely_cause(caplog):
    client = _Client([_Resp(403, {"detail": "denied"})])

    with caplog.at_level(logging.WARNING, logger=pull_worker.__name__):
        assert _claim(client) is None

    warnings = [r.getMessage() for r in caplog.records
                if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "403" in warnings[0]
    assert "TRINITY_PULL_MODE" in warnings[0]


def test_the_refusal_warns_once_per_status_per_process(caplog):
    with caplog.at_level(logging.WARNING, logger=pull_worker.__name__):
        for _ in range(5):
            _claim(_Client([_Resp(403)]))

    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


def test_a_second_distinct_status_gets_its_own_line(caplog):
    with caplog.at_level(logging.WARNING, logger=pull_worker.__name__):
        _claim(_Client([_Resp(403)]))
        _claim(_Client([_Resp(500)]))

    warnings = [r.getMessage() for r in caplog.records
                if r.levelno >= logging.WARNING]
    assert len(warnings) == 2
    assert any("403" in w for w in warnings) and any("500" in w for w in warnings)


def test_a_pool_of_workers_logs_one_line_not_one_per_worker(caplog):
    """The set is module-level for this reason."""
    with caplog.at_level(logging.WARNING, logger=pull_worker.__name__):
        for worker in ("alpha#w0", "alpha#w1", "alpha#w2"):
            _claim(_Client([_Resp(403)]), worker=worker)

    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


def test_the_bearer_token_is_never_logged(caplog):
    with caplog.at_level(logging.DEBUG, logger=pull_worker.__name__):
        _claim(_Client([_Resp(403)]))

    assert all("key" not in r.getMessage() for r in caplog.records)


def test_an_empty_queue_is_still_silent_and_still_empty(caplog):
    """§3.2: a drained poll is normal and must not warn."""
    with caplog.at_level(logging.WARNING, logger=pull_worker.__name__):
        assert _claim(_Client([_Resp(200, {"envelope": None})])) is None
        assert _claim(_Client([_Resp(204)])) is None

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_a_real_claim_still_comes_back():
    envelope = {"envelope": {"payload": {}}, "claim_token": "t"}

    assert _claim(_Client([_Resp(200, envelope)])) == envelope


# ---------------------------------------------------------------------------
# The delivery window is bounded
# ---------------------------------------------------------------------------


def _deliver(client, deadline_offset):
    return asyncio.run(
        pull_worker._deliver_result(
            client, "e1", {"status": "success"}, "http://backend", "key",
            time.monotonic() + deadline_offset,
        )
    )


def test_delivery_gives_up_at_its_deadline_and_reports_undelivered():
    """False is the signal the caller needs to leave the envelope on disk."""
    client = _Client(post_answers=[_Resp(503), _Resp(503), _Resp(503)])

    with patch.object(pull_worker.asyncio, "sleep", new=_no_sleep):
        assert _deliver(client, 0.0) is False

    assert client.post_calls == 1


def test_delivery_still_succeeds_on_a_2xx_inside_the_window():
    client = _Client(post_answers=[_Resp(200, {})])

    assert _deliver(client, 90.0) is True


def test_the_in_loop_window_defaults_to_ninety_seconds():
    assert pull_worker._DELIVERY_IN_LOOP_SECONDS == 90.0


def test_a_long_lease_is_cut_to_the_window():
    """The real computation, on a fixed clock: 10 min of lease left, the
    attempt still stops 90 s in, so the worker goes back to claiming."""
    now = 1000.0
    lease_deadline = now + 600.0

    bound = pull_worker._delivery_deadline(
        lease_deadline, persisted=True, now=now
    )

    assert bound == now + pull_worker._DELIVERY_IN_LOOP_SECONDS
    assert bound < lease_deadline


def test_a_lease_shorter_than_the_window_is_the_bound():
    """The cap only ever SHORTENS. A 10 s lease must not be extended to 90 s —
    delivering past the lease is writing against a claim someone else may
    already hold."""
    now = 1000.0
    lease_deadline = now + 10.0

    bound = pull_worker._delivery_deadline(
        lease_deadline, persisted=True, now=now
    )

    assert bound == lease_deadline


def test_the_two_bounds_do_not_coincide_at_the_values_tested():
    """Guard against the one-value trap: a test where lease == window would
    pass under either `min` or `max`."""
    assert pull_worker._DELIVERY_IN_LOOP_SECONDS not in (10.0, 600.0)


def test_an_unpersisted_terminal_keeps_the_full_lease_window():
    """Nothing on disk means giving up early would DROP spent work, so that
    case is deliberately unbounded — asserted on the returned value, not on
    the source text."""
    now = 1000.0
    lease_deadline = now + 600.0

    assert pull_worker._delivery_deadline(
        lease_deadline, persisted=False, now=now
    ) == lease_deadline


def test_the_delivery_call_site_uses_the_bound():
    """The helper is only worth having if `_run_and_report` calls it: delete
    the call and this goes red."""
    import inspect

    src = inspect.getsource(pull_worker._run_and_report)
    assert "_delivery_deadline(deadline, persisted=persisted)" in src


def test_the_resend_sweep_runs_periodically_not_only_at_boot():
    """A bounded delivery that gives up needs something other than the next
    restart to finish it."""
    import ast

    src = (_AGENT_SERVER / "services" / "pull_worker.py").read_text()
    tree = ast.parse(src)
    names = {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert "_resend_pull_periodically" in names
    assert "_stop_pull_resend_tick" in names
    assert pull_worker._RESEND_TICK_SECONDS == 60.0


async def _no_sleep(_seconds):
    return None


# ---------------------------------------------------------------------------
# The resend tick, executed (review I2): the name pin above cannot tell a
# `while True: sleep` from a tick that runs the sweep, survives a failing
# sweep, and dies on cancel.
# ---------------------------------------------------------------------------


class _FakeApp:
    """Captures the `@app.on_event` handlers the way FastAPI would."""

    def __init__(self):
        self.handlers: Dict[str, Any] = {}

    def on_event(self, name):
        def _register(fn):
            self.handlers[name] = fn
            return fn
        return _register


def test_the_resend_tick_sweeps_repeatedly_survives_a_blip_and_stops_on_shutdown(monkeypatch):
    calls: list = []

    async def _sweep():
        calls.append(len(calls))
        if len(calls) == 2:          # the first TICK (index 1) blows up
            raise RuntimeError("backend blip")

    monkeypatch.setattr(pull_worker, "resend_pending_pull_results", _sweep)
    monkeypatch.setattr(pull_worker, "_RESEND_TICK_SECONDS", 0.0)
    monkeypatch.setattr(pull_worker, "_worker_creds_present", lambda: True)
    pull_worker._RESEND_TASKS.clear()

    app = _FakeApp()
    pull_worker.schedule_pending_pull_result_resend(app)
    assert {"startup", "shutdown"} <= set(app.handlers)

    async def _drive():
        await app.handlers["startup"]()            # boot sweep + spawns the tick
        assert len(pull_worker._RESEND_TASKS) == 1
        task = pull_worker._RESEND_TASKS[0]
        for _ in range(200):
            await asyncio.sleep(0)
            if len(calls) >= 4:
                break
        assert not task.done(), "a failing sweep must not kill the tick"
        await app.handlers["shutdown"]()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert pull_worker._RESEND_TASKS == []

    asyncio.run(_drive())
    # boot (0), the failing tick (1), and at least two more ticks after it
    assert len(calls) >= 4


def test_the_tick_is_not_installed_without_worker_credentials(monkeypatch):
    """No worker, no pending files, nothing to re-send — and no stray task."""
    monkeypatch.setattr(pull_worker, "_worker_creds_present", lambda: False)
    app = _FakeApp()
    pull_worker.schedule_pending_pull_result_resend(app)
    assert app.handlers == {}


# ---------------------------------------------------------------------------
# #4635: the resend tick never pre-empts the worker's own in-loop delivery.
# A resend says `auth_override_capable: False` (right after a restart: the
# worker no longer holds the claim), and the backend answers that with a
# terminal FAILED — so a sweep landing inside the 90 s window threw away the
# retry a refused credential was owed (#3487).
# ---------------------------------------------------------------------------


import json  # noqa: E402
import types  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402


@pytest.fixture
def pull_env(monkeypatch, tmp_path):
    """A pending dir in tmp, no jitter, a refused-credential runtime, a real
    process registry. `deliveries` records every POST as
    `(execution_id, auth_override_capable, "worker" | "sweep")`."""
    from fastapi import HTTPException

    import agent_server.services.process_registry as pr

    pending = tmp_path / "pending-pull-results"
    monkeypatch.setattr(pull_worker, "_PENDING_PULL_DIR", pending)
    monkeypatch.setattr(pull_worker, "_worker_creds_present", lambda: True)
    monkeypatch.setattr(pull_worker, "_SWEEP_INITIAL_JITTER_SECONDS", 0)
    monkeypatch.setattr(pull_worker, "_SWEEP_PER_ENVELOPE_JITTER_SECONDS", 0)
    monkeypatch.setenv("TRINITY_BACKEND_URL", "http://backend")
    monkeypatch.setenv("TRINITY_MCP_API_KEY", "key")
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "get_process_registry", lambda: registry)
    runtime = SimpleNamespace(
        supports_auth_override=True,
        execute_headless=AsyncMock(side_effect=HTTPException(status_code=429, detail="limit")),
    )
    stub = types.ModuleType("agent_server.services.runtime_adapter")
    stub.get_runtime = lambda: runtime
    monkeypatch.setitem(sys.modules, "agent_server.services.runtime_adapter", stub)

    env = SimpleNamespace(pending=pending, deliveries=[], on_worker=None, on_sweep=None)

    async def _deliver(client, execution_id, body, backend_url, mcp_key, deadline,
                       reply_sink=None):
        by = "worker" if reply_sink is not None else "sweep"
        env.deliveries.append((execution_id, body["metadata"]["auth_override_capable"], by))
        hook = env.on_worker if by == "worker" else env.on_sweep
        if hook is not None:
            return await hook(execution_id, reply_sink)
        return True

    monkeypatch.setattr(pull_worker, "_deliver_result", _deliver)
    return env


def _leftover(env, execution_id, claim_token="t-old"):
    env.pending.mkdir(parents=True, exist_ok=True)
    (env.pending / f"{execution_id}.json").write_text(json.dumps({
        "execution_id": execution_id,
        "body": {"claim_token": claim_token, "status": "failed", "error_code": "billing",
                 "metadata": {"auth_override_capable": True}},
    }))


def _claim_for(execution_id="exec-w"):
    return {"execution_id": execution_id, "claim_token": "t0",
            "envelope": {"payload": {"message": "hi", "task_overrides": {"timeout_seconds": 600}}}}


def _report(claim):
    return asyncio.run(pull_worker._run_and_report(claim, None, "http://b", "k", "alpha"))


def test_a_resend_inside_the_in_loop_window_leaves_the_result_to_the_worker(pull_env):
    """The tick fires while the worker is still reporting a refused attempt.
    The leftover from an earlier crash is the control: the sweep did run."""
    _leftover(pull_env, "exec-other")

    async def _tick_fires_now(execution_id, reply_sink):
        await pull_worker.resend_pending_pull_results()
        reply_sink.append({"applied": True})
        return True

    pull_env.on_worker = _tick_fires_now

    _report(_claim_for("exec-w"))

    assert pull_env.deliveries == [
        ("exec-w", True, "worker"),
        ("exec-other", False, "sweep"),
    ]
    assert not (pull_env.pending / "exec-w.json").exists(), "the worker's own delivery removes it"


def test_once_the_worker_gives_up_the_next_tick_sends_it(pull_env):
    async def _gave_up(execution_id, reply_sink):
        return False

    pull_env.on_worker = _gave_up

    _report(_claim_for("exec-w"))
    assert (pull_env.pending / "exec-w.json").exists()
    assert "exec-w" not in pull_worker._DELIVERING_NOW

    asyncio.run(pull_worker.resend_pending_pull_results())

    assert pull_env.deliveries[-1] == ("exec-w", False, "sweep")
    assert not (pull_env.pending / "exec-w.json").exists()


def test_a_cancelled_delivery_leaves_the_file_and_takes_the_id_off_the_list(pull_env):
    async def _shutdown(execution_id, reply_sink):
        raise asyncio.CancelledError()

    pull_env.on_worker = _shutdown

    with pytest.raises(asyncio.CancelledError):
        _report(_claim_for("exec-w"))

    assert (pull_env.pending / "exec-w.json").exists(), "B6: the boot resend still has it"
    assert "exec-w" not in pull_worker._DELIVERING_NOW


def test_the_sweep_never_unlinks_a_result_rewritten_while_it_was_sending(pull_env):
    """The sweep's own delivery can retry for minutes. A worker that re-claimed
    the requeued row (same execution id) writes a fresh terminal under the
    same name meanwhile; unlinking it would lose that turn's work."""
    _leftover(pull_env, "exec-x", claim_token="t-stale")

    async def _rewritten_meanwhile(execution_id, reply_sink):
        pull_worker._persist_pull_result(execution_id, {
            "execution_id": execution_id,
            "body": {"claim_token": "t-fresh", "status": "success", "metadata": {}},
        })
        return True  # the stale token: 409, permanent

    pull_env.on_sweep = _rewritten_meanwhile

    asyncio.run(pull_worker.resend_pending_pull_results())

    left = json.loads((pull_env.pending / "exec-x.json").read_text())
    assert left["body"]["claim_token"] == "t-fresh"


def test_a_delivered_resend_still_removes_its_file(pull_env):
    _leftover(pull_env, "exec-y")

    asyncio.run(pull_worker.resend_pending_pull_results())

    assert pull_env.deliveries == [("exec-y", False, "sweep")]
    assert not (pull_env.pending / "exec-y.json").exists()


def test_an_id_two_workers_deliver_stays_listed_until_both_end():
    """Counted, not a set: the design does not lean on "never two in-loop
    holders of one id" (the lease arithmetic) to stay exact."""
    pull_worker._mark_delivering("e1")
    pull_worker._mark_delivering("e1")
    pull_worker._unmark_delivering("e1")
    assert pull_worker._DELIVERING_NOW.get("e1")

    pull_worker._unmark_delivering("e1")
    assert "e1" not in pull_worker._DELIVERING_NOW
