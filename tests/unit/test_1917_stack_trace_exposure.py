"""#1917 — exception messages must not flow into ops / system-agent responses.

PR #1912 fixed `restart_fleet` (response carries `HTTPException.detail` or the
exception CLASS NAME only; full message + traceback go to the log via
`exc_info=True`). It deliberately left the sibling sites alone under the
minimal-changes rule; this closes them, including open CodeQL alert #231
(`routers/system_agent.py`).

The exposure is bounded — every endpoint here is admin-gated — but exception
messages routinely embed internals: docker/httpx errors lead with the socket
path or the internal container host, and the `learnings.md` 2026-07-14 entry
records git stderr carrying a PAT into operator-visible state. "Admin-only" is
a blast-radius argument, not a reason for the string to be there.

Each test drives the REAL router function with a planted sentinel in the
exception message and asserts (a) the sentinel is absent from the response and
(b) the class name is present — the second half matters, because a response
that dropped the error entirely would pass a sentinel-only check while making
the failure undiagnosable.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

# A string no platform-authored message would ever contain. If it appears in a
# response body, a raw exception message reached the caller.
SENTINEL = "sekrit-internal-host-10.0.0.7:5432"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _human_caller():
    caller = MagicMock()
    caller.agent_name = None
    caller.connector_agent = None
    # #2323: the admin gate allowlists `mcp_scope`, and MagicMock auto-creates a
    # truthy one. None = an interactive human, which is what this stands in for.
    caller.mcp_scope = None
    caller.role = "admin"
    return caller


def _blob(obj) -> str:
    """Whole response as one searchable string — a leak anywhere counts."""
    return json.dumps(obj, default=str)


# ---------------------------------------------------------------------------
# routers/ops.py
# ---------------------------------------------------------------------------

@pytest.fixture
def ops(monkeypatch):
    import routers.ops as mod
    # #1028: the fleet orchestration lives in services/fleet_ops_service; the
    # route keeps the gate. Collaborator patches land on the service, the gate
    # patch on the router, and the fixture hands the SERVICE back with the
    # route entry points attached (the test_1860 shape).
    import services.fleet_ops_service as svc

    monkeypatch.setattr(mod, "assert_admin", lambda user, **kw: None)  # **kw: #2323 added allow_scopes=
    monkeypatch.setattr(svc, "db", MagicMock())
    # Mirrors the test_1860 fixture: a bare MagicMock reads as "this agent is an
    # ephemeral ghost / system agent", which makes the loop SKIP and the test
    # vacuous — the skip path produces no error field at all.
    svc.db.get_agent_owner.return_value = {"is_system": False}
    svc.db.get_agent_ephemeral_info.return_value = None
    monkeypatch.setattr(svc, "platform_audit_service", MagicMock(log=AsyncMock()))
    svc.stop_fleet = mod.stop_fleet
    svc.get_fleet_health = mod.get_fleet_health
    # the cost rollup has its own service (#1028) — attach the route and
    # point per-test patches (httpx, the OTEL url) at that module.
    import services.ops_costs_service as costs_svc
    svc.get_ops_costs = mod.get_ops_costs
    svc.httpx = costs_svc.httpx
    svc.OTEL_COLLECTOR_METRICS_URL = None  # setattr target below is costs_svc
    svc._costs = costs_svc
    return svc


class _Agent:
    def __init__(self, name="a1", status="running"):
        self.name = name
        self.status = status


def test_stop_fleet_per_agent_error_carries_no_raw_message(ops, monkeypatch):
    monkeypatch.setattr(ops, "list_all_agents_fast", lambda: [_Agent()])

    def _boom(name):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(ops, "get_agent_container", _boom)

    # system_prefix must be passed explicitly: calling the endpoint function
    # directly leaves the FastAPI `Query(...)` default object in place, which
    # the prefix-filter arithmetic then trips over.
    out = _run(
        ops.stop_fleet(
            MagicMock(), current_user=_human_caller(), system_prefix=None
        )
    )

    body = _blob(out)
    assert SENTINEL not in body, f"raw exception message leaked: {body}"
    assert "RuntimeError" in body, "the class name must survive for diagnosis"


def test_emergency_stop_helper_error_carries_no_raw_message(ops, monkeypatch):
    def _boom(name):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(ops, "get_agent_container", _boom)

    out = ops._stop_agent_container("a1")

    body = _blob(out)
    assert out["result"] == "error"
    assert SENTINEL not in body
    assert "RuntimeError" in body


def test_fleet_health_probe_error_carries_no_raw_message(ops, monkeypatch):
    """This one was already truncated to 50 chars. Truncation bounds a leak, it
    does not remove one — docker/httpx messages put the host or socket FIRST,
    so the first 50 characters are the sensitive part."""
    monkeypatch.setattr(ops, "list_all_agents_fast", lambda: [_Agent()])
    ops.db.get_agent_owner.return_value = {"is_system": False}

    # The real seam is `get_agent_client(name).get_session()` — an earlier draft
    # patched a `get_agent_context_info` that does not exist (raising=False made
    # that silent), so the probe never raised and the test passed against the
    # UNPATCHED router. That is the #1932/#1951 vacuous-negative class; the
    # pre-fix run below is what caught it.
    client = MagicMock()
    client.get_session = AsyncMock(side_effect=RuntimeError(SENTINEL))
    monkeypatch.setattr(ops, "get_agent_client", lambda name: client)

    out = _run(ops.get_fleet_health(MagicMock(), current_user=_human_caller()))

    assert client.get_session.await_count == 1, (
        "the probe never ran — the test would pass against unpatched code"
    )
    body = _blob(out)
    assert SENTINEL not in body, f"raw exception message leaked: {body}"
    assert "RuntimeError" in body, "the class name must survive for diagnosis"


def test_ops_costs_error_carries_no_raw_message(ops, monkeypatch):
    """The OTel collector URL and its internal host live in this message."""
    monkeypatch.setattr(ops._costs, "OTEL_COLLECTOR_METRICS_URL", "http://otel:8889/metrics", raising=False)

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            raise RuntimeError(SENTINEL)

    monkeypatch.setattr(ops._costs.httpx, "AsyncClient", lambda *a, **k: _Client())

    out = _run(ops.get_ops_costs(MagicMock(), current_user=_human_caller()))

    body = _blob(out)
    assert SENTINEL not in body, f"raw exception message leaked: {body}"


# ---------------------------------------------------------------------------
# routers/system_agent.py — CodeQL alert #231
# ---------------------------------------------------------------------------

def test_system_agent_health_error_carries_no_raw_message(monkeypatch):
    """The open CodeQL alert. An httpx failure here names the internal
    container host (`agent-trinity-system:8000`)."""
    import routers.system_agent as mod

    monkeypatch.setattr(mod, "assert_admin", lambda user, **kw: None, raising=False)  # **kw: #2323 added allow_scopes=
    monkeypatch.setattr(mod, "db", MagicMock())
    monkeypatch.setattr(mod, "get_agent_container", lambda name: MagicMock(status="running"))

    def _boom(*a, **k):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(mod, "agent_httpx_client", _boom)

    out = _run(mod.get_system_agent_status(MagicMock(), current_user=_human_caller()))

    body = _blob(out)
    assert SENTINEL not in body, f"raw exception message leaked: {body}"
    assert "RuntimeError" in body


# ---------------------------------------------------------------------------
# Static backstop across every touched router
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "module",
    ["routers/ops.py", "routers/system_agent.py"],
)
def test_no_raw_str_e_remains_in_these_routers(module):
    """`str(e)` in these two files is, without exception, the defect this issue
    is about — every occurrence was a response field or an HTTPException detail.
    A plain ban is therefore the honest guard, and it fails loudly if a new one
    is added rather than waiting for the next CodeQL run.

    `routers/agents.py` is deliberately NOT in this list: it is a 1000+ line
    router with many `str(e)` uses that never reach a response, so a blanket
    ban there would be false. Its three fixed sites are covered by the
    exception-detail assertions above.
    """
    from pathlib import Path

    backend = Path(__file__).resolve().parents[2] / "src" / "backend"
    src = (backend / module).read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in src.splitlines()
        if "str(e)" in line and not line.strip().startswith("#")
    ]
    assert not offenders, (
        f"{module} reintroduced a raw exception message (#1917): {offenders}"
    )


def test_agents_router_lifecycle_details_use_the_class_name():
    """The three `agents.py` sites named in the issue (start / stop / logs)."""
    from pathlib import Path

    backend = Path(__file__).resolve().parents[2] / "src" / "backend"
    src = (backend / "routers" / "agents.py").read_text(encoding="utf-8")
    for verb in ("Failed to start agent", "Failed to stop agent", "Failed to get logs"):
        idx = src.index(verb)
        window = src[idx: idx + 240]
        assert "e.__class__.__name__" in window, (
            f"{verb!r} no longer reports the exception class name (#1917)"
        )
        assert "{str(e)}" not in window, (
            f"{verb!r} flows the raw exception message into the response (#1917)"
        )
