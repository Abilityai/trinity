"""
#2670: `fan_out` is the third route of the #914 gateway-timeout class, and the
one that hits it most reliably — a fan-out dispatches N tasks and by
construction runs longer than any single one of them.

The receipt shape does not transfer from #914 / #2661. Those name ONE
`execution_id`; a fan-out creates N rows sharing one `fan_out_id`, and until
this change nothing resolved that id to anything: `ExecutionSummary.fan_out_id`
existed as a field on a list row and no endpoint read it. So the client aborted
into `fetch failed` with nothing to poll while N executions kept running.

Three halves are covered here — the aggregate rule, the read surface's
enumeration safety, and the two backend contracts the MCP client depends on
(the 409 shape, and the batch id being attached to the idempotency claim BEFORE
the batch runs rather than after it finishes).
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The aggregate rule
# ---------------------------------------------------------------------------

def _rows(*statuses):
    return [{"id": f"e{i}", "status": s} for i, s in enumerate(statuses)]


def _fold(*statuses):
    from services.fan_out_service import build_fan_out_batch_status
    return build_fan_out_batch_status("scout", "fo_x", _rows(*statuses))


def test_a_live_batch_is_running_whatever_else_has_happened():
    """`running` outranks every verdict. A batch with a failure and a success
    already in it is still `running` while one row can change — reporting a
    verdict early is what makes a polling caller stop polling."""
    b = _fold("success", "running", "failed")
    assert b.status == "running"
    assert (b.total, b.completed, b.failed, b.running) == (3, 1, 1, 1)


def test_pending_retry_counts_as_live_not_failed():
    """The easy one to miss. A subtask awaiting a #271 retry is neither done nor
    lost; counting it as failed tells the caller the batch is finished while a
    row is about to run again."""
    b = _fold("pending_retry")
    assert b.status == "running"
    assert (b.failed, b.running) == (0, 1)


def test_queued_counts_as_live():
    """A subtask waiting for a slot has not failed."""
    assert _fold("queued", "success").status == "running"


@pytest.mark.parametrize("statuses,expected", [
    (("success", "success"), "completed"),
    (("success", "failed"), "partial"),
    (("failed", "cancelled"), "failed"),
    (("success", "skipped"), "partial"),
])
def test_a_finished_batch_lands_on_one_of_three_verdicts(statuses, expected):
    """`partial` exists because best-effort is the fan-out's default policy: a
    batch where four of five succeeded is neither a success nor a failure, and
    calling it either loses the fact the caller needs."""
    assert _fold(*statuses).status == expected


def test_cancelled_and_skipped_are_failures_not_successes():
    """Only `success` is a success. A terminal row that is anything else did not
    produce the answer the task asked for, whatever the reason."""
    b = _fold("cancelled", "skipped")
    assert (b.completed, b.failed, b.running) == (0, 2, 0)


def test_deadline_exceeded_is_never_reported_here():
    """It is the DISPATCHER's verdict on its own outer deadline, held in memory
    by the call that timed out. It is not a property of any row, so this surface
    cannot observe it — and must not invent it."""
    from services.fan_out_service import build_fan_out_batch_status
    for statuses in [("success",), ("failed",), ("running",), ("success", "failed")]:
        assert build_fan_out_batch_status("a", "fo_x", _rows(*statuses)).status \
            in {"running", "completed", "partial", "failed"}


def test_per_task_status_is_the_execution_status_verbatim():
    """NOT the dispatch response's `completed`/`failed` pair. A poll of a live
    batch has to distinguish "waiting for a slot" from "running", and a
    two-value vocabulary forces a healthy queued subtask to be reported as a
    failure."""
    b = _fold("queued", "running", "success")
    assert [t.status for t in b.results] == ["queued", "running", "success"]


def test_a_row_with_no_id_is_dropped_rather_than_counted():
    from services.fan_out_service import build_fan_out_batch_status
    b = build_fan_out_batch_status("a", "fo_x", [{"id": None, "status": "success"},
                                                 {"id": "e1", "status": "success"}])
    assert b.total == 1


def test_the_fold_is_pure():
    """No database, no clock, no service — every input resolved by the caller, so
    the rule is testable and cannot drift into doing I/O."""
    from services import fan_out_service
    src = inspect.getsource(fan_out_service.build_fan_out_batch_status)
    for forbidden in ("db.", "get_engine", "datetime.now", "await "):
        assert forbidden not in src, f"the fold reached for {forbidden!r}"


# ---------------------------------------------------------------------------
# The read surface
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", [
    "", "not-an-id", "fo_", "../etc/passwd", "fo_" + "x" * 200, "FO_abc", "e_abc",
])
async def test_a_malformed_id_is_the_same_404_as_an_unknown_one(bad_id):
    """Enumeration safety (Invariant #8): malformed, unknown, and belonging-to-
    another-agent are one answer. A 400 for "that isn't an id shape" would be
    free information, and the shape check exists only to keep junk out of the
    query."""
    from fastapi import HTTPException
    from routers import fan_out as router

    with patch.object(router, "db") as fake_db:
        with pytest.raises(HTTPException) as exc:
            await router.get_fan_out_status(fan_out_id=bad_id, name="scout")
    assert exc.value.status_code == 404
    assert exc.value.detail == "Fan-out not found"
    # ...and a malformed id never reaches the database at all.
    fake_db.get_fan_out_executions.assert_not_called()


@pytest.mark.asyncio
async def test_an_unknown_batch_is_404_not_an_empty_aggregate():
    """Reporting `{total: 0, status: "completed"}` for an id that does not exist
    would tell a polling caller their batch finished successfully."""
    from fastapi import HTTPException
    from routers import fan_out as router

    with patch.object(router, "db") as fake_db:
        fake_db.get_fan_out_executions.return_value = []
        with pytest.raises(HTTPException) as exc:
            await router.get_fan_out_status(fan_out_id="fo_abc123", name="scout")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_the_read_is_scoped_by_agent_as_well_as_by_batch_id():
    """The id is server-minted and unguessable, but the query must not be able
    to return another agent's rows even if one were somehow reused — the route
    is agent-gated, so the read has to be too."""
    from routers import fan_out as router

    with patch.object(router, "db") as fake_db:
        fake_db.get_fan_out_executions.return_value = _rows("success")
        await router.get_fan_out_status(fan_out_id="fo_abc123", name="scout")
    args = fake_db.get_fan_out_executions.call_args[0]
    assert args[0] == "scout" and args[1] == "fo_abc123"


def test_the_read_route_is_agent_access_gated():
    """`get_authorized_agent`, the same dependency the POST uses — an agent-scoped
    key reaches its own agent and the ones it is permitted, and nothing else."""
    from routers import fan_out as router
    src = inspect.getsource(router.get_fan_out_status)
    assert "get_authorized_agent" in src


def test_the_route_reads_rows_not_the_idempotency_snapshot():
    """The snapshot is written by `complete()` — only once the whole batch has
    finished — so it cannot answer the question a timed-out caller is asking,
    which is what is happening RIGHT NOW. The rows can."""
    from routers import fan_out as router
    src = inspect.getsource(router.get_fan_out_status)
    assert "get_fan_out_executions" in src
    # The word appears in the docstring explaining WHY it is not read, so this
    # asserts on the code: no idempotency call of any kind on this path.
    body = src.split('"""')[2]
    assert "idempotency" not in body and "snapshot" not in body


# ---------------------------------------------------------------------------
# The two contracts the MCP client depends on
# ---------------------------------------------------------------------------

def test_the_in_flight_409_carries_an_execution_id_like_chat_and_task():
    """It returned a bare string, so the #2661 client — which reads
    `detail.execution_id` off a 409 to answer with a receipt — could not benefit
    from machinery that already existed."""
    from routers import fan_out as router
    src = inspect.getsource(router.fan_out)
    assert '"error": "request_in_progress"' in src
    assert '"execution_id": idem.execution_id' in src


def test_the_batch_id_is_attached_before_the_batch_runs():
    """`complete()` records the id when the batch is already over. A fan-out's
    whole window — the one in which a concurrent duplicate arrives and in which
    this call's own gateway gives up — is while it RUNS, so the id has to be on
    the claim from the first dispatch."""
    from routers import fan_out as router
    src = inspect.getsource(router.fan_out)
    assert "on_started=lambda fid: idempotency_service.attach_execution(idem, fid)" in src


def test_the_started_hook_fires_before_any_dispatch_and_cannot_fail_the_batch():
    from services import fan_out_service
    src = inspect.getsource(fan_out_service.FanOutService.execute)
    minted = src.index('fan_out_id = f"fo_')
    hook = src.index("on_started(fan_out_id)")
    gather = src.index("asyncio.gather") if "asyncio.gather" in src else len(src)
    assert minted < hook < gather, "the hook must fire between minting and dispatch"
    # Bookkeeping must never be able to fail a dispatch that is otherwise fine.
    tail = src[hook - 200:hook + 300]
    assert "try:" in tail and "except Exception" in tail
