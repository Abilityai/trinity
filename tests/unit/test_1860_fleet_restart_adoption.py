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

    monkeypatch.setattr(mod, "assert_admin", lambda user: None)
    monkeypatch.setattr(mod, "db", MagicMock())
    mod.db.get_agent_owner.return_value = {"is_system": False}
    mod.db.get_agent_ephemeral_info.return_value = None
    monkeypatch.setattr(mod, "list_all_agents_fast", lambda: [_Agent("a1")])
    monkeypatch.setattr(mod, "get_agent_container", lambda name: _Container())
    monkeypatch.setattr(
        mod, "restart_agent_internal", AsyncMock(return_value=dict(_START_OK))
    )
    monkeypatch.setattr(mod, "platform_audit_service", MagicMock(log=AsyncMock()))
    monkeypatch.setattr(mod, "get_breaker_redis", lambda: None)  # lock fail-open
    monkeypatch.setattr(mod, "invalidate_context_stats_cache", MagicMock())
    return mod


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
