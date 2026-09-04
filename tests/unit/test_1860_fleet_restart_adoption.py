"""#1860 — fleet restart routes through the canonical lifecycle path.

`POST /api/ops/fleet/restart` used to stop/start each agent with raw Docker
calls, bypassing `start_agent_internal()` — so none of the config-drift
predicates ran and a rebuilt base image was never adopted (#1809's cold-start
gate never fired). The fix delegates the loop body to
`lifecycle.restart_agent_internal` (explicit stop → full start path) and adds:
per-agent `recreated`/`recreate_reason` in the results (explicit allowlist
copy), an ephemeral-ghost skip, a single-flight SETNX lock, a partial-safe
audit entry, and the `reject_agent_principal` human-only gate.

The live suite (`tests/test_ops.py`) exercises only response *structure* with
every agent skipped via a nonexistent prefix — the loop body's behavior is
covered HERE, mocked, per the `test_1816_system_agent_adoption.py` pattern.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _human_caller():
    """A JWT/user-scoped principal. `User.agent_name` is set only for
    scope="agent" keys, and `reject_agent_principal` keys off exactly that — so
    a bare MagicMock (truthy `.agent_name`) would read as an agent key and 403."""
    caller = MagicMock()
    caller.agent_name = None
    return caller


def _agent_caller():
    caller = MagicMock()
    caller.agent_name = "parent-agent"
    return caller


class _Agent:
    def __init__(self, name, status="running"):
        self.name = name
        self.status = status


class _Container:
    def __init__(self, status="running"):
        self.status = status
        self.short_id = "cafe1234"


_START_OK = {
    "recreated": False,
    "recreate_reason": None,
    "credentials_injection": "skipped",
    "skills_injection": "skipped",
}

_START_RECREATED = {
    "recreated": True,
    "recreate_reason": "image_drift",
    "credentials_injection": "success",
    "skills_injection": "success",
}


@pytest.fixture
def ops(monkeypatch):
    """The real ``routers.ops`` with its auth + Docker + Redis surface patched.

    ``reject_agent_principal`` is deliberately NOT stubbed — the human-only
    gate is exercised for real. Callers pass ``_human_caller()``.
    """
    import routers.ops as mod
    # #1028: the restart orchestration lives in services/fleet_ops_service now;
    # the route keeps only its auth gate and a thin delegate. Collaborator
    # patches land on the service module — the one whose bindings the moved
    # body actually reads — while the gate patch stays on the router.
    import services.fleet_ops_service as svc

    monkeypatch.setattr(mod, "assert_admin", lambda user, **kw: None)
    monkeypatch.setattr(svc, "db", MagicMock())
    svc.db.get_agent_owner.return_value = {"is_system": False}
    svc.db.get_agent_ephemeral_info.return_value = None
    monkeypatch.setattr(svc, "list_all_agents_fast", lambda: [_Agent("a1")])
    monkeypatch.setattr(svc, "get_agent_container", lambda name: _Container())
    monkeypatch.setattr(
        svc, "restart_agent_internal", AsyncMock(return_value=dict(_START_OK))
    )
    monkeypatch.setattr(svc, "platform_audit_service", MagicMock(log=AsyncMock()))
    monkeypatch.setattr(svc, "get_breaker_redis", lambda: None)  # lock fail-open
    monkeypatch.setattr(svc, "invalidate_context_stats_cache", MagicMock())
    # The fixture hands back the SERVICE module — that is where every
    # collaborator mock lives now — with the route entry point attached, so
    # `ops.restart_fleet(...)` still drives the real gate + delegate while
    # `ops.restart_agent_internal` / `ops.db` read the mocks that the moved
    # body actually consults.
    svc.restart_fleet = mod.restart_fleet
    return svc


def _call(mod, caller=None, **query):
    return _run(
        mod.restart_fleet(
            MagicMock(),
            current_user=caller if caller is not None else _human_caller(),
            filter_status=query.get("filter_status"),
            system_prefix=query.get("system_prefix"),
        )
    )


# ---------------------------------------------------------------------------
# Adoption + result surfacing
# ---------------------------------------------------------------------------


def test_drift_recreate_is_surfaced_per_agent_and_in_summary(ops, monkeypatch):
    """The whole point of #1860: the fleet loop reaches the image-drift
    predicate, and the router explicitly copies the recreate fields into its
    own result dicts (they do NOT flow through on their own — #1809 learning)."""
    monkeypatch.setattr(
        ops, "list_all_agents_fast", lambda: [_Agent("a1"), _Agent("a2")]
    )
    monkeypatch.setattr(
        ops,
        "restart_agent_internal",
        AsyncMock(return_value=dict(_START_RECREATED)),
    )

    out = _call(ops)

    assert out["summary"] == {
        "total": 2, "successes": 2, "failures": 0, "skipped": 0, "recreated": 2,
        "processed": 2, "stopped_early": None,  # #1919 partial-run honesty keys
    }
    for entry, name in zip(out["results"], ("a1", "a2")):
        assert entry["agent"] == name
        assert entry["result"] == "success"
        assert entry["previous_status"] == "running"
        assert entry["recreated"] is True
        assert entry["recreate_reason"] == "image_drift"
        assert entry["credentials_injection"] == "success"
        assert entry["skills_injection"] == "success"
    assert [c.args for c in ops.restart_agent_internal.await_args_list] == [
        ("a1",), ("a2",),
    ]


def test_no_drift_plain_restart_reports_recreated_false(ops):
    out = _call(ops)

    assert out["summary"]["recreated"] == 0
    (entry,) = out["results"]
    assert entry["result"] == "success"
    assert entry["recreated"] is False
    assert entry["recreate_reason"] is None


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


def test_one_agent_failing_never_aborts_the_loop_and_errors_are_legible(
    ops, monkeypatch
):
    """An `except HTTPException: raise` copied from the single-agent endpoint
    would abort the fleet loop — pin that both exception shapes are recorded
    (with non-empty, useful text) and later agents still restart."""
    monkeypatch.setattr(
        ops,
        "list_all_agents_fast",
        lambda: [_Agent("a1"), _Agent("a2"), _Agent("a3")],
    )
    monkeypatch.setattr(
        ops,
        "restart_agent_internal",
        AsyncMock(
            side_effect=[
                HTTPException(status_code=404, detail="Agent not found"),
                RuntimeError("docker exploded"),
                dict(_START_OK),
            ]
        ),
    )

    out = _call(ops)

    assert out["summary"]["failures"] == 2
    assert out["summary"]["successes"] == 1
    r1, r2, r3 = out["results"]
    assert r1["result"] == "failed" and r1["error"] == "404: Agent not found"
    # py/stack-trace-exposure (CodeQL, PR #1912): an unexpected exception's
    # raw message must NEVER reach the response — class name only, full
    # detail goes to the server log.
    assert r2["result"] == "failed"
    assert "RuntimeError" in r2["error"]
    assert "docker exploded" not in r2["error"]
    assert r3["result"] == "success", "the loop must continue past failures"


def test_containerless_failure_names_the_1559_recovery_path(ops, monkeypatch):
    """A recreate that dies after removing the old container leaves NO
    container — the agent vanishes from Docker-as-truth fleet listings. The
    error must name the recovery (single-agent start → #1559)."""
    handles = [_Container(), None]  # pre-try lookup, then the hint re-check
    monkeypatch.setattr(ops, "get_agent_container", lambda name: handles.pop(0))
    monkeypatch.setattr(
        ops,
        "restart_agent_internal",
        AsyncMock(side_effect=RuntimeError("containers_run failed")),
    )

    out = _call(ops)

    (entry,) = out["results"]
    assert entry["result"] == "failed"
    # Class name + recovery hint surface; the raw exception message does not
    # (py/stack-trace-exposure — see the failure-isolation test above).
    assert "RuntimeError" in entry["error"]
    assert "containers_run failed" not in entry["error"]
    assert "POST /api/agents/a1/start" in entry["error"]


def test_container_gone_before_restart_is_a_failed_row_not_1559(ops, monkeypatch):
    """The pre-existing `container is None` guard is kept: an agent deleted
    mid-loop records "container not found" rather than being routed into
    #1559 missing-container recovery."""
    monkeypatch.setattr(ops, "get_agent_container", lambda name: None)

    out = _call(ops)

    (entry,) = out["results"]
    assert entry == {"agent": "a1", "result": "failed", "error": "container not found"}
    ops.restart_agent_internal.assert_not_awaited()


# ---------------------------------------------------------------------------
# Skips
# ---------------------------------------------------------------------------


def test_system_agent_is_skipped(ops):
    ops.db.get_agent_owner.return_value = {"is_system": True}

    out = _call(ops)

    (entry,) = out["results"]
    assert entry["result"] == "skipped" and entry["reason"] == "system agent"
    ops.restart_agent_internal.assert_not_awaited()


def test_ephemeral_ghost_is_skipped(ops):
    """Config-drift predicates are NOT ephemeral-gated in the lifecycle — a
    cold start could recreate a drifted ghost and destroy its volume-less
    workspace mid-budget (ent#69). The fleet loop must not cold-start ghosts."""
    ops.db.get_agent_ephemeral_info.return_value = {"is_ephemeral": True}

    out = _call(ops)

    (entry,) = out["results"]
    assert entry["result"] == "skipped" and entry["reason"] == "ephemeral"
    ops.restart_agent_internal.assert_not_awaited()


def test_ephemeral_lookup_failure_fails_open_to_restart(ops):
    """A broken ephemeral accessor must not stop a durable agent's restart."""
    ops.db.get_agent_ephemeral_info.side_effect = RuntimeError("db down")

    out = _call(ops)

    assert out["summary"]["successes"] == 1
    ops.restart_agent_internal.assert_awaited_once()


def test_stopped_agent_is_skipped(ops, monkeypatch):
    monkeypatch.setattr(
        ops, "list_all_agents_fast", lambda: [_Agent("a1", status="exited")]
    )

    out = _call(ops)

    (entry,) = out["results"]
    assert entry["result"] == "skipped" and entry["reason"] == "not running"
    ops.restart_agent_internal.assert_not_awaited()


# ---------------------------------------------------------------------------
# Principal gate
# ---------------------------------------------------------------------------


def test_agent_scoped_principal_is_rejected_403(ops):
    """`assert_admin` is a role gate, not a human gate — an agent-scoped key
    carries its owner's role. Container replacement at fleet scale is
    operator-only (Invariant #8; real `reject_agent_principal`)."""
    with pytest.raises(HTTPException) as exc:
        _call(ops, caller=_agent_caller())

    assert exc.value.status_code == 403
    ops.restart_agent_internal.assert_not_awaited()


# ---------------------------------------------------------------------------
# Single-flight lock
# ---------------------------------------------------------------------------


def _lock_client(set_result=True):
    client = MagicMock()
    store = {}

    def _set(key, val, nx=None, ex=None):
        if set_result:
            store[key] = val
        return set_result

    client.set.side_effect = _set
    client.get.side_effect = lambda key: store.get(key)
    # #1919: the backing dict, exposed so tests can simulate a takeover /
    # expiry between iterations (store[key] = "foreign", store.pop(key)).
    client.store = store
    return client


def test_concurrent_fleet_restart_is_refused_409(ops, monkeypatch):
    client = _lock_client(set_result=False)
    monkeypatch.setattr(ops, "get_breaker_redis", lambda: client)

    with pytest.raises(HTTPException) as exc:
        _call(ops)

    assert exc.value.status_code == 409
    assert exc.value.detail == "fleet_restart_in_progress"
    ops.restart_agent_internal.assert_not_awaited()
    # A refused request did no work — it must not write a misleading audit row.
    ops.platform_audit_service.log.assert_not_awaited()


def test_lock_is_acquired_refreshed_and_released_compare_and_delete(
    ops, monkeypatch
):
    client = _lock_client(set_result=True)
    monkeypatch.setattr(ops, "get_breaker_redis", lambda: client)

    _call(ops)

    client.set.assert_called_once()
    assert client.set.call_args.kwargs.get("nx") is True
    client.expire.assert_called()  # own-lease refresh inside the loop
    client.delete.assert_called_once_with(ops._FLEET_RESTART_LOCK_KEY)


def test_redis_down_fails_open_and_still_restarts(ops, monkeypatch):
    client = MagicMock()
    client.set.side_effect = RuntimeError("redis down")
    monkeypatch.setattr(ops, "get_breaker_redis", lambda: client)

    out = _call(ops)

    assert out["summary"]["successes"] == 1
    client.delete.assert_not_called()


# ---------------------------------------------------------------------------
# #1919 — ownership-checked lease refresh + loss detection
# ---------------------------------------------------------------------------


def _two_agents(ops, monkeypatch):
    monkeypatch.setattr(
        ops, "list_all_agents_fast", lambda: [_Agent("a1"), _Agent("a2")]
    )


def test_1919_two_agent_happy_path_refreshes_own_lease_each_iteration(
    ops, monkeypatch
):
    """Baseline that makes a spurious break detectable: every prior lock test
    ran a 1-agent fleet, so 'loop completed' and 'loop broke after agent 1'
    were indistinguishable. An implementation classifying every GET as foreign
    passes all loss tests — but not this one."""
    _two_agents(ops, monkeypatch)
    client = _lock_client(set_result=True)
    monkeypatch.setattr(ops, "get_breaker_redis", lambda: client)

    out = _call(ops)

    assert out["summary"]["successes"] == 2
    assert out["summary"]["processed"] == 2
    assert out["summary"]["stopped_early"] is None
    assert client.expire.call_count == 2  # ownership-checked refresh per agent
    client.set.assert_called_once()       # no re-acquire on the happy path
    client.delete.assert_called_once_with(ops._FLEET_RESTART_LOCK_KEY)


def test_1919_foreign_token_stops_loop_and_release_leaves_it(ops, monkeypatch):
    """A concurrent caller took the lock after our lease lapsed: the loop must
    stop BEFORE the next destructive restart, report the partial run honestly,
    and the compare-and-delete release must not touch the foreign token."""
    _two_agents(ops, monkeypatch)
    client = _lock_client(set_result=True)
    monkeypatch.setattr(ops, "get_breaker_redis", lambda: client)

    def _restart_then_takeover(name):
        # Simulate TTL lapse + second caller acquiring while a1 restarts.
        client.store[ops._FLEET_RESTART_LOCK_KEY] = "intruder-token"
        return dict(_START_OK)

    monkeypatch.setattr(
        ops, "restart_agent_internal", AsyncMock(side_effect=_restart_then_takeover)
    )

    out = _call(ops)

    assert out["summary"]["successes"] == 1          # a1 done, a2 never touched
    assert out["summary"]["processed"] == 1
    assert out["summary"]["total"] == 2
    assert out["summary"]["stopped_early"] == "lease_lost_foreign"
    assert [r["agent"] for r in out["results"]] == ["a1"]
    ops.restart_agent_internal.assert_awaited_once()
    client.delete.assert_not_called()                # foreign token untouched
    details = ops.platform_audit_service.log.await_args.kwargs["details"]
    assert details["stopped_early"] == "lease_lost_foreign"
    assert details["processed"] == 1


def test_1919_absent_token_reacquires_and_continues(ops, monkeypatch):
    """Lease lapsed but NOBODY took it: nothing is racing us, so the run
    re-acquires with its own token and completes instead of aborting a
    half-done destructive operation."""
    _two_agents(ops, monkeypatch)
    client = _lock_client(set_result=True)
    monkeypatch.setattr(ops, "get_breaker_redis", lambda: client)

    def _restart_then_expiry(name):
        if name == "a1":  # the lease lapses once, while a1 restarts
            client.store.pop(ops._FLEET_RESTART_LOCK_KEY, None)
        return dict(_START_OK)

    monkeypatch.setattr(
        ops, "restart_agent_internal", AsyncMock(side_effect=_restart_then_expiry)
    )

    out = _call(ops)

    assert out["summary"]["successes"] == 2
    assert out["summary"]["stopped_early"] is None
    # initial acquire + one re-acquire, both SETNX
    assert client.set.call_count == 2
    assert all(c.kwargs.get("nx") is True for c in client.set.call_args_list)
    details = ops.platform_audit_service.log.await_args.kwargs["details"]
    assert details["lease_reacquired"] is True
    client.delete.assert_called_once_with(ops._FLEET_RESTART_LOCK_KEY)


def test_1919_absent_token_losing_the_reacquire_race_stops(ops, monkeypatch):
    """Absent at GET, but a second caller wins the SETNX between our read and
    our re-acquire: that is a foreign holder — stop."""
    _two_agents(ops, monkeypatch)
    client = _lock_client(set_result=True)
    monkeypatch.setattr(ops, "get_breaker_redis", lambda: client)

    set_calls = {"n": 0}

    def _set_then_lose(key, val, nx=None, ex=None):
        set_calls["n"] += 1
        if set_calls["n"] == 1:      # initial acquire wins
            client.store[key] = val
            return True
        return False                 # re-acquire loses the race

    client.set.side_effect = _set_then_lose

    def _restart_then_expiry(name):
        client.store.pop(ops._FLEET_RESTART_LOCK_KEY, None)
        return dict(_START_OK)

    monkeypatch.setattr(
        ops, "restart_agent_internal", AsyncMock(side_effect=_restart_then_expiry)
    )

    out = _call(ops)

    assert out["summary"]["successes"] == 1
    assert out["summary"]["stopped_early"] == "lease_lost_foreign"
    client.delete.assert_not_called()


def test_1919_refresh_redis_error_fails_open_and_release_still_runs(
    ops, monkeypatch, caplog
):
    """A Redis blip during the refresh is NOT lease loss (fail-open, AC4) —
    and the error must be scoped to the refresh: the release at the end still
    sees Redis healthy and MUST delete, or a green test certifies a TTL-long
    lock leak. Exactly one throttled warning for the whole run."""
    _two_agents(ops, monkeypatch)
    client = _lock_client(set_result=True)
    monkeypatch.setattr(ops, "get_breaker_redis", lambda: client)

    gate_gets = {"n": 0}

    def _get_blipping(key):
        gate_gets["n"] += 1
        if gate_gets["n"] <= 2:      # the two in-loop gate reads fail
            raise RuntimeError("redis blip")
        return client.store.get(key)  # the release read succeeds

    client.get.side_effect = _get_blipping

    with caplog.at_level(logging.WARNING, logger="routers.ops"):
        out = _call(ops)

    assert out["summary"]["successes"] == 2
    assert out["summary"]["stopped_early"] is None
    client.delete.assert_called_once_with(ops._FLEET_RESTART_LOCK_KEY)
    warns = [r for r in caplog.records if "failing open" in r.message]
    assert len(warns) == 1  # throttled: one warning, not one per agent


def test_1919_expire_returning_zero_is_treated_as_absent(ops, monkeypatch):
    """Key vanished in the GET→EXPIRE sliver: EXPIRE returns 0 and creates
    nothing — the gate must not sail on believing it holds a lock. Routed to
    the absent path: re-acquire and continue."""
    client = _lock_client(set_result=True)
    monkeypatch.setattr(ops, "get_breaker_redis", lambda: client)
    client.expire.return_value = 0

    out = _call(ops)

    assert out["summary"]["successes"] == 1
    assert client.set.call_count == 2  # initial + re-acquire
    details = ops.platform_audit_service.log.await_args.kwargs["details"]
    assert details["lease_reacquired"] is True


def test_1919_expire_raising_fails_open(ops, monkeypatch):
    _two_agents(ops, monkeypatch)
    client = _lock_client(set_result=True)
    monkeypatch.setattr(ops, "get_breaker_redis", lambda: client)
    client.expire.side_effect = RuntimeError("redis blip")

    out = _call(ops)

    assert out["summary"]["successes"] == 2
    assert out["summary"]["stopped_early"] is None
    client.delete.assert_called_once_with(ops._FLEET_RESTART_LOCK_KEY)


def test_1919_foreign_token_on_first_iteration_processes_nothing(
    ops, monkeypatch
):
    client = _lock_client(set_result=True)
    monkeypatch.setattr(ops, "get_breaker_redis", lambda: client)
    client.get.side_effect = lambda key: "intruder-token"

    out = _call(ops)

    assert out["results"] == []
    assert out["summary"]["processed"] == 0
    assert out["summary"]["total"] == 1
    assert out["summary"]["stopped_early"] == "lease_lost_foreign"
    ops.restart_agent_internal.assert_not_awaited()
    client.delete.assert_not_called()


def test_1919_bytes_token_from_an_undecoded_client_still_matches(
    ops, monkeypatch
):
    """get_breaker_redis sets decode_responses=True, so bytes is not a live
    path — but the shared helper keeps the shipped release comparison's
    belt-and-braces bytes branch, and it must not be untested dead code."""
    client = _lock_client(set_result=True)
    monkeypatch.setattr(ops, "get_breaker_redis", lambda: client)
    client.get.side_effect = lambda key: (
        v.encode() if isinstance(v := client.store.get(key), str) else v
    )

    out = _call(ops)

    assert out["summary"]["successes"] == 1
    assert out["summary"]["stopped_early"] is None
    client.expire.assert_called_once()
    client.delete.assert_called_once_with(ops._FLEET_RESTART_LOCK_KEY)


def test_1919_loss_warning_names_state_not_token_values(ops, monkeypatch, caplog):
    client = _lock_client(set_result=True)
    monkeypatch.setattr(ops, "get_breaker_redis", lambda: client)
    client.get.side_effect = lambda key: "intruder-secret-value"

    with caplog.at_level(logging.WARNING, logger="routers.ops"):
        _call(ops)

    loss_warns = [r for r in caplog.records if "lease lost" in r.message]
    assert len(loss_warns) == 1
    rendered = loss_warns[0].getMessage()
    assert "token state: foreign" in rendered
    assert "intruder-secret-value" not in rendered


def test_1919_listing_failure_inside_try_still_releases_lock(ops, monkeypatch):
    """list_all_agents_fast cannot raise today (it swallows to []) — this
    guards the acquire-inside-try move: any future raise between acquire and
    the loop must release the lock and write a distinguishable audit row, not
    leak a TTL-long fleet lockout."""
    client = _lock_client(set_result=True)
    monkeypatch.setattr(ops, "get_breaker_redis", lambda: client)
    monkeypatch.setattr(
        ops, "list_all_agents_fast", MagicMock(side_effect=RuntimeError("boom"))
    )

    with pytest.raises(RuntimeError):
        _call(ops)

    client.delete.assert_called_once_with(ops._FLEET_RESTART_LOCK_KEY)
    ops.invalidate_context_stats_cache.assert_called_once()
    details = ops.platform_audit_service.log.await_args.kwargs["details"]
    assert details["total"] == 0
    assert details["stopped_early"] == "error"
    assert details["error"] == "RuntimeError"


# ---------------------------------------------------------------------------
# Audit + cache invalidation
# ---------------------------------------------------------------------------


def test_audit_carries_per_agent_recreate_map_and_failed_names(ops, monkeypatch):
    monkeypatch.setattr(
        ops, "list_all_agents_fast", lambda: [_Agent("a1"), _Agent("a2")]
    )
    monkeypatch.setattr(
        ops,
        "restart_agent_internal",
        AsyncMock(
            side_effect=[dict(_START_RECREATED), RuntimeError("kaput")]
        ),
    )

    _call(ops, system_prefix=None)

    ops.platform_audit_service.log.assert_awaited_once()
    kwargs = ops.platform_audit_service.log.await_args.kwargs
    assert kwargs["event_action"] == "fleet_restart"
    details = kwargs["details"]
    assert details["recreated"] == {"a1": "image_drift"}
    assert details["failed_agents"] == ["a2"]
    assert details["successes"] == 1 and details["failures"] == 1


def test_audit_failure_never_fails_the_endpoint(ops):
    ops.platform_audit_service.log.side_effect = RuntimeError("audit down")

    out = _call(ops)

    assert out["summary"]["successes"] == 1


def test_context_stats_cache_invalidated_once(ops, monkeypatch):
    monkeypatch.setattr(
        ops, "list_all_agents_fast", lambda: [_Agent("a1"), _Agent("a2")]
    )

    _call(ops)

    ops.invalidate_context_stats_cache.assert_called_once()
