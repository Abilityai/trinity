"""
#2661: a failed sync ``/task`` must RELEASE its idempotency claim.

Both sync branches call ``_map_task_failure`` after ``idempotency_service.begin()``
and before ``complete()``, and nothing covered the raising path — so every
failed / cancelled / timed-out sync ``/task`` left its claim ``in_flight`` for the
full 24h TTL (``db/idempotency.py`` ttl_hours=24).

The user-visible effect was the exact inverse of what idempotency is for: a
legitimate retry of the same message answered ``409 request_in_progress`` for a
day against a task that had died minutes earlier, while the only way through was
to REWORD the message — which derives a different key (``deriveMcpIdempotencyKey``
hashes the message) and dispatches a genuine duplicate. So the wedge did not just
block retries, it actively selected for the duplicate-dispatch behaviour #2661
exists to stop.

Same class as #946 (T5), one path over: see
``test_946_task_idempotency_on_deny.py`` for the CircuitOpen/CapacityFull siblings.
"""
from __future__ import annotations

import ast
import pathlib
from unittest.mock import MagicMock, patch

import pytest

import services.chat_execution_service as _CE
from services.chat_execution_service import ChatDispatchError, _map_task_failure


def _result(status: str, error: str | None):
    r = MagicMock()
    r.status = status
    r.error = error
    return r


@pytest.mark.parametrize(
    "status,error,expected_code",
    [
        ("failed", "agent is at capacity right now", 429),
        ("failed", "the task timed out after 600s", 504),
        ("failed", "container exited unexpectedly", 503),
        ("failed", None, 503),
        ("cancelled", "operator cancelled", 503),
        # Every terminal failure shape must release — the mapping to 429/504/503
        # is orthogonal to whether the claim is freed, and the original bug was
        # that NONE of them freed it.
    ],
)
def test_failed_sync_task_releases_the_idempotency_claim(status, error, expected_code):
    idem = MagicMock()
    with patch.object(_CE, "idempotency_service") as isvc:
        with pytest.raises(ChatDispatchError) as exc:
            _map_task_failure("agent1", _result(status, error), idem=idem)

    assert exc.value.status_code == expected_code
    isvc.fail.assert_called_once_with(idem)


def test_release_happens_before_the_raise_not_after():
    """The release must not sit after the ``raise`` — an ordering slip is
    invisible to a test that only asserts ``fail`` was called at some point,
    because the exception would abort the function first."""
    idem = MagicMock()
    with patch.object(_CE, "idempotency_service") as isvc:
        # If the release were unreachable (placed after the raise), fail() would
        # never run and this assertion is what catches it.
        with pytest.raises(ChatDispatchError):
            _map_task_failure("agent1", _result("failed", "boom"), idem=idem)
        assert isvc.fail.call_count == 1


def test_successful_task_does_not_release_the_claim():
    """A success must keep the claim so ``complete()`` can store the replay
    snapshot — releasing here would make a duplicate re-execute instead of
    replaying, which is the opposite failure."""
    idem = MagicMock()
    with patch.object(_CE, "idempotency_service") as isvc:
        _map_task_failure("agent1", _result("success", None), idem=idem)  # no raise
    isvc.fail.assert_not_called()


def test_idem_is_required_keyword_only_so_a_new_call_site_cannot_omit_it():
    """A defaulted ``idem`` would let a third sync branch be added later that
    silently reintroduces the 24h wedge — invisible until someone retries a full
    day after a failure. Keep it required."""
    import inspect

    sig = inspect.signature(_map_task_failure)
    param = sig.parameters["idem"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


def test_every_map_task_failure_call_site_passes_idem():
    """AST guard over the real module: the fix is only as good as its call sites.

    The signature test above proves a bare call raises TypeError, but that fires
    at RUNTIME on a failure path that is itself rare — exactly the path nobody
    exercises before shipping. This fails at test time instead.
    """
    src = pathlib.Path(_CE.__file__).read_text()
    tree = ast.parse(src)

    call_sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_map_task_failure"
    ]
    assert call_sites, "no _map_task_failure call sites found — did it get renamed?"

    for call in call_sites:
        kwargs = {kw.arg for kw in call.keywords}
        assert "idem" in kwargs, (
            f"_map_task_failure at line {call.lineno} does not pass idem= — "
            "a failed sync /task there will wedge its idempotency claim for 24h (#2661)"
        )


# --- the two backlog exits the review found still wedged (#2675 review) --------
#
# `_dispatch_sync_backlog` has two exits BETWEEN begin() and complete() that
# `_map_task_failure` never sees: the long-poll timeout and the "row vanished"
# read. Each is driven here with the real function and a fake row.

import asyncio


def _row(status: str, **kw):
    r = MagicMock()
    r.status = status
    r.response = kw.get("response", "done")
    r.cost = kw.get("cost", 0.01)
    r.context_used = 10
    r.context_max = 100
    r.claude_session_id = "sess"
    r.error = kw.get("error")
    return r


def _run_backlog(*, wait, row, idem):
    async def fake_wait(execution_id, timeout):
        if isinstance(wait, Exception):
            raise wait
        return wait

    with patch.object(_CE, "wait_for_sync_terminal", fake_wait), \
            patch.object(_CE, "db") as fake_db, \
            patch.object(_CE, "idempotency_service") as isvc:
        fake_db.get_execution.return_value = row
        try:
            out = asyncio.run(_CE._dispatch_sync_backlog(
                name="agent1", execution_id="ex1", sync_effective_timeout=5, idem=idem,
            ))
        except ChatDispatchError as e:
            return e, isvc
        return out, isvc


def test_long_poll_timeout_on_a_still_running_row_completes_the_claim_with_a_receipt():
    """Still running past the cap: a retry must NOT dispatch a second execution
    (so the claim is not released) and must NOT 409 for a day (so it is not
    left in_flight). It is completed with a receipt naming the execution."""
    idem = MagicMock()
    out, isvc = _run_backlog(wait=asyncio.TimeoutError(), row=_row("running"), idem=idem)

    assert isinstance(out, ChatDispatchError) and out.status_code == 504
    isvc.fail.assert_not_called()
    isvc.complete.assert_called_once()
    _idem, eid, receipt = isvc.complete.call_args.args
    assert _idem is idem and eid == "ex1"
    assert receipt["status"] == "queued_timeout"
    assert receipt["execution_id"] == "ex1" and receipt["task_execution_id"] == "ex1"
    assert "ex1" in out.detail


def test_long_poll_timeout_that_missed_a_success_returns_the_result():
    """The wait lost the wakeup but the row already says SUCCESS: that is a
    result, not a timeout. Reconstruct it and complete the claim normally."""
    idem = MagicMock()
    out, isvc = _run_backlog(wait=asyncio.TimeoutError(), row=_row("success"), idem=idem)

    assert not isinstance(out, ChatDispatchError), out
    assert out["task_execution_id"] == "ex1" and out["response"] == "done"
    isvc.fail.assert_not_called()
    isvc.complete.assert_called_once()
    assert isvc.complete.call_args.args[2].get("status") != "queued_timeout"


def test_long_poll_timeout_that_missed_a_failure_releases_the_claim():
    """Same race, failed row: `_map_task_failure`'s verdict applies — release,
    so the caller may retry."""
    idem = MagicMock()
    out, isvc = _run_backlog(
        wait=asyncio.TimeoutError(), row=_row("failed", error="boom"), idem=idem,
    )
    assert isinstance(out, ChatDispatchError) and out.status_code == 503
    isvc.fail.assert_called_once_with(idem)
    isvc.complete.assert_not_called()


def test_a_vanished_row_releases_the_claim():
    """Nothing is running under this key that anyone can find — hold nothing."""
    idem = MagicMock()
    out, isvc = _run_backlog(wait=None, row=None, idem=idem)
    assert isinstance(out, ChatDispatchError) and out.status_code == 503
    isvc.fail.assert_called_once_with(idem)
    isvc.complete.assert_not_called()


def test_no_raise_sits_between_begin_and_complete_without_settling_the_claim():
    """AST guard over `_dispatch_sync_backlog`: every `raise ChatDispatchError`
    in it must be preceded, in the same block, by an idempotency `fail()` or
    `complete()` call — the review found two that were not (#2675)."""
    src = pathlib.Path(_CE.__file__).read_text()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_dispatch_sync_backlog"
    )

    def settles(stmt):
        return any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in ("fail", "complete")
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "idempotency_service"
            for n in ast.walk(stmt)
        )

    unsettled = []
    for block in (b for n in ast.walk(fn) for b in
                  ([n.body] if hasattr(n, "body") and isinstance(n.body, list) else [])
                  + ([n.orelse] if hasattr(n, "orelse") and isinstance(n.orelse, list) else [])):
        seen = False
        for stmt in block:
            if settles(stmt):
                seen = True
            if isinstance(stmt, ast.Raise) and not seen:
                unsettled.append(stmt.lineno)
    # `_map_task_failure` raises INSIDE itself after releasing, so a bare call to
    # it is a settled exit; the only raises at this level must be settled here.
    assert not unsettled, f"raise without a prior fail()/complete() at lines {unsettled}"
