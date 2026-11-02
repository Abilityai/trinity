"""#3487 AC5 — a refused pulled turn, end to end through the real claim, the
real sink and the real sync waiter (the `TestTheTaskPathWalksThePool` mirror on
a pilot). Its own module because it borrows #3114's DB fixtures, whose autouse
`sys.modules` guard must not wrap the rest of the #3487 suite."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from db_harness import db_backend  # noqa: F401
from unit.test_3470_subscription_walk import _sub, _switch_db, _wire_db, wired  # noqa: F401

pytestmark = pytest.mark.unit

# ===========================================================================
from unit.test_3114_pull_route_interactive import (  # noqa: E402,F401
    AGENT, _db, _row, _restore_sys_modules, seed_agent, tmp_db,
)


@pytest.mark.asyncio
async def test_a_workspace_caller_on_a_pilot_gets_one_success_after_a_refusal(
    seed_agent, wired, monkeypatch,
):
    """AC5: the pushed `TestTheTaskPathWalksThePool`, on a pilot. A `public` turn
    is claimed, its first attempt is refused, the sink answers `retry` instead
    of a terminal — so the waiting caller is NOT woken — and the re-run on the
    next subscription serves: the caller gets ONE success, carrying the walk."""
    import importlib

    seed_agent()
    _row("w1", trigger="public")
    pcs = importlib.import_module("services.pull_coordination_service")
    sync_waiter = importlib.import_module("services.sync_waiter")
    tes = importlib.import_module("services.task_execution_service")
    import services.platform_audit_service as audit_mod

    _wire_db(wired, _switch_db(viable=[_sub("sub-b")]))
    # Same module object the fixture patched (another test may re-import it).
    monkeypatch.setattr(pcs, "subscription_auto_switch", wired.m)
    monkeypatch.setattr(audit_mod.platform_audit_service, "log", AsyncMock())
    monkeypatch.setattr(pcs, "event_dispatch_service", MagicMock())
    monkeypatch.setattr(pcs, "channel_completion_report", MagicMock())
    monkeypatch.setattr(pcs, "_spawn_breaker_verdict", lambda *a, **k: None)
    monkeypatch.setattr(sync_waiter, "SYNC_WAITER_POLL_INTERVAL", 0.05)
    # #3879: hermetic Redis. The claim stamps the worker poll and the refusal
    # scrubs against the staged runtime secrets, both through a blocking Redis
    # client on the event loop. With no Redis and nothing in this process that
    # had already memoised it as down, the connects outlasted the waiter's 10 s
    # budget, the waiter timed out, and that read as "woken by the refusal".
    monkeypatch.setattr(pcs, "record_worker_poll", lambda *_a: None)
    monkeypatch.setattr(pcs, "get_staged_values", lambda: [])

    claim = pcs.claim_next_task(AGENT, "worker-1")
    waiter = asyncio.create_task(sync_waiter.wait_for_sync_terminal("w1", 10))
    await asyncio.sleep(0)

    refused = await pcs.handle_task_result(
        "w1", claim["claim_token"], status="failed", content="usage limit reached",
        error_code="billing", cost=0.02, metadata={"auth_override_capable": True},
    )
    assert refused.kind == "retry"
    assert refused.retry["auth_override"] == {"oauth_token": "tok-sub-b"}
    await asyncio.sleep(0.2)
    assert not waiter.done(), "a refused attempt must not wake the caller"
    assert _db().get_execution("w1").status == "running"

    served = await pcs.handle_task_result(
        "w1", refused.retry["claim_token"], status="success", content="the answer",
        cost=0.03, metadata={"auth_override_capable": True, "auth_override": "oauth_token"},
    )
    assert served.kind == "applied"
    await asyncio.wait_for(waiter, 2)

    result = tes.result_from_execution_row("w1")
    assert result.status == "success" and result.response == "the answer"
    assert result.cost == pytest.approx(0.05), "the refused attempt's spend is not lost"
    assert result.subscription_switch["switched"] is True
    assert wired.performed == ["sub-b"]
