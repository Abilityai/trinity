"""#2215 — bounded port-bind-conflict retry in the create path (+ D2b guard).

The D1 reservation (test_2215_port_allocation.py) is fail-open, so a collision
can still surface at `containers.run` — Redis down/restarted, an expired
reservation, a foreign host process. `_run_agent_container_with_port_retry`
converges that within the same create call; the D2b precedence guard makes the
ent#313 reclaim classify a bind conflict BEFORE the name-conflict decline.

The cross-phase theme all three plan reviewers hit independently:
`_is_container_name_conflict`'s text fallback matches the bare substring
"already in use" — which Docker Desktop's bind failure ("… bind: address
already in use") CONTAINS. A bind conflict proves the create SUCCEEDED (the
daemon holds a Created husk of OURS); a name conflict proves it created
NOTHING. Misordering the two strands the husk, 409s every retry of the name,
and lets Cornelius's next-boot 409-convergence burn the durable seed flag on a
dead container (the #1790 latch).

Contracts pinned:
  * classifier vocabulary: narrow two-phrase bind match; the documented
    overlap on the Desktop phrasing; disjointness where it must hold
  * per-attempt order: record port -> cleanup husk -> reallocate(exclude=) ->
    retry; cleanup on EVERY bind attempt, the final one included
  * any cleanup doubt (ownership row, lookup failure, unprovable provenance,
    removal failure) aborts and re-raises the ORIGINAL bind error
  * non-bind errors and caller-pinned ports never retry; <=3 attempts
  * D2b: a bind error reaching the reclaim with no handle re-derives the
    container under the ent#313 fail-closed gates instead of declining
  * wiring: create_agent_internal awaits the wrapper (not the raw create) and
    captures `auto_allocated_port` BEFORE the port mutation (captured after,
    the gate is always-False and the retry ships dead)

Harness mirrors test_ent313_failed_creation_container_reclaim.py: the real
crud module with its Docker/DB seams monkeypatched by object (crud from-imports
at module top, so `crud.<name>` is where the code resolves). No sys.modules
writes (tests/lint_sys_modules.py).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytestmark = pytest.mark.unit

FLOOR = "2026-08-15T10:00:00Z"
AFTER_FLOOR = "2026-08-15T10:00:05Z"
BEFORE_FLOOR = "2026-08-15T09:59:55Z"

# Real daemon phrasings (verbatim shapes, lowercased matching in the code).
BIND_PORT_ALLOCATED = (
    "500 Server Error: driver failed programming external connectivity on "
    "endpoint agent-a1: Bind for 0.0.0.0:2227 failed: port is already allocated"
)
BIND_ADDR_IN_USE = (
    "driver failed programming external connectivity on endpoint agent-a1: "
    "Error starting userland proxy: listen tcp4 0.0.0.0:2227: "
    "bind: address already in use"
)
NAME_CONFLICT = (
    'Conflict. The container name "/agent-a1" is already in use by container '
    '"abc123". You have to remove (or rename) that container to be able to '
    "reuse that name."
)


def _husk(created_label=AFTER_FLOOR):
    labels = {} if created_label is None else {"trinity.created": created_label}
    return SimpleNamespace(attrs={"Config": {"Labels": labels}})


def _not_found():
    exc = Exception("404 Client Error: no such container")
    exc.response = SimpleNamespace(status_code=404)
    return exc


@pytest.fixture
def crud(monkeypatch):
    """The real module with the seams the retry helper touches monkeypatched.

    Defaults: no ownership row; the husk lookup finds a container THIS attempt
    created (provenance passes); removal succeeds; reallocation hands out
    2228, 2229, ... — i.e. the happy retry path. Individual tests override.
    """
    try:
        from services.agent_service import crud as crud_mod
    except ImportError:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")

    monkeypatch.setattr(crud_mod, "container_remove", AsyncMock())
    monkeypatch.setattr(crud_mod, "clear_agent_runtime_state", AsyncMock())
    monkeypatch.setattr(crud_mod, "get_agent_container", MagicMock(return_value=None))
    monkeypatch.setattr(crud_mod.db, "is_agent_name_reserved", MagicMock(return_value=False))
    monkeypatch.setattr(
        crud_mod, "get_next_available_port", MagicMock(side_effect=[2228, 2229, 2230])
    )
    dc = MagicMock()
    dc.containers.get.return_value = _husk(AFTER_FLOOR)
    monkeypatch.setattr(crud_mod, "docker_client", dc)
    return crud_mod


def _config(port=2227):
    return SimpleNamespace(name="a1", port=port)


def _handles(crud, floor=FLOOR):
    return crud._RollbackHandles(agent_name="a1", container_floor_ts=floor)


def _run(crud, config, *, auto=True, handles=None):
    return asyncio.run(
        crud._run_agent_container_with_port_retry(
            config, {}, {}, MagicMock(), None, handles or _handles(crud), auto
        )
    )


def _bind_then_succeed(crud, monkeypatch, fail_times=1, exc_text=BIND_PORT_ALLOCATED):
    """`_create_agent_container` double: bind-fails `fail_times` times, then
    returns a container whose label is built from config.port AT CALL TIME —
    exactly like the real one — so label==port assertions are meaningful."""
    calls = []

    async def fake_create(config, volumes, env_vars, user, eph):
        calls.append(config.port)
        if len(calls) <= fail_times:
            raise Exception(exc_text)
        return SimpleNamespace(labels={"trinity.ssh-port": str(config.port)})

    monkeypatch.setattr(crud, "_create_agent_container", fake_create)
    return calls


# ---------------------------------------------------------------------------
# Classifier vocabulary + the documented overlap
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [BIND_PORT_ALLOCATED, BIND_ADDR_IN_USE])
def test_bind_classifier_matches_both_daemon_phrasings(crud, text):
    assert crud._is_port_bind_conflict(Exception(text)) is True


@pytest.mark.parametrize(
    "text",
    [
        NAME_CONFLICT,
        "409 Client Error: Conflict",
        "UnixHTTPConnectionPool: Read timed out. (read timeout=60)",
        "Cannot connect to the Docker daemon",
    ],
)
def test_bind_classifier_rejects_everything_else(crud, text):
    """Narrow on purpose: daemon-unreachable / timeout / name-conflict errors
    must bubble immediately without burning retries (no generic 'conflict')."""
    assert crud._is_port_bind_conflict(Exception(text)) is False


def test_bind_classifier_walks_the_cause_chain(crud):
    inner = Exception(BIND_PORT_ALLOCATED)
    outer = RuntimeError("creation failed")
    outer.__cause__ = inner
    assert crud._is_port_bind_conflict(outer) is True


def test_classifier_disjointness_and_the_documented_overlap(crud):
    """The pin all three reviewers asked for, fed with real daemon strings:

    * the name-conflict string is a name conflict ONLY (bind says False)
    * "port is already allocated" is a bind conflict ONLY (name says False)
    * the Desktop phrasing matches BOTH — the pre-existing overlap ("address
      already in use" contains "already in use") that the D2b precedence
      guard resolves; pinned so a 'cleanup' of either classifier that
      silently changes the overlap fails here first.
    """
    name_exc = Exception(NAME_CONFLICT)
    assert crud._is_container_name_conflict(name_exc) is True
    assert crud._is_port_bind_conflict(name_exc) is False

    allocated = Exception(BIND_PORT_ALLOCATED)
    assert crud._is_port_bind_conflict(allocated) is True
    assert crud._is_container_name_conflict(allocated) is False

    desktop = Exception(BIND_ADDR_IN_USE)
    assert crud._is_port_bind_conflict(desktop) is True
    assert crud._is_container_name_conflict(desktop) is True  # the overlap


# ---------------------------------------------------------------------------
# The retry loop
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exc_text", [BIND_PORT_ALLOCATED, BIND_ADDR_IN_USE])
def test_bind_conflict_retries_with_fresh_excluded_port(crud, monkeypatch, exc_text):
    calls = _bind_then_succeed(crud, monkeypatch, exc_text=exc_text)
    config = _config(2227)

    result = _run(crud, config)

    assert calls == [2227, 2228]
    assert config.port == 2228
    # No DB copy of the port exists; the container's own label is the truth
    # (Invariant #11) — it must carry the FINAL port.
    assert result.labels["trinity.ssh-port"] == str(config.port)
    crud.get_next_available_port.assert_called_once_with(exclude={2227})
    crud.container_remove.assert_awaited_once()


def test_cleanup_runs_strictly_before_reallocation(crud, monkeypatch):
    order = []
    _bind_then_succeed(crud, monkeypatch)

    async def ordered_remove(*a, **k):
        order.append("cleanup")

    def ordered_alloc(**k):
        order.append("realloc")
        return 2228

    monkeypatch.setattr(crud, "container_remove", ordered_remove)
    monkeypatch.setattr(crud, "get_next_available_port", ordered_alloc)

    _run(crud, _config())

    assert order == ["cleanup", "realloc"]


def test_exhausted_retries_cleanup_every_attempt_and_reraise_bind_error(crud, monkeypatch):
    """The FINAL attempt cleans up too — a bind-classified husk must never be
    handed to the generic reclaim still on the name — and the raised error is
    the bind error, so the outer except path classifies it correctly."""
    calls = _bind_then_succeed(crud, monkeypatch, fail_times=99)
    # Snapshot `exclude` at call time — the helper passes its LIVE accumulator
    # set, so MagicMock's recorded call_args would mutate after the fact.
    excludes_seen = []

    def recording_alloc(*, exclude):
        excludes_seen.append(set(exclude))
        return 2227 + len(excludes_seen)

    monkeypatch.setattr(crud, "get_next_available_port", recording_alloc)

    with pytest.raises(Exception, match="port is already allocated"):
        _run(crud, _config(2227))

    assert calls == [2227, 2228, 2229]          # bounded at 3 attempts
    assert crud.container_remove.await_count == 3  # incl. the final attempt
    # exclude accumulates across attempts.
    assert excludes_seen == [{2227}, {2227, 2228}]


def test_cleanup_removal_failure_aborts_and_reraises_original_bind_error(crud, monkeypatch):
    """Proceeding after a failed removal makes attempt N+1 409 against our OWN
    husk, which the reclaim reads as 'not ours' and strands. Abort instead —
    and report the BIND error, never the removal error (the outer reclaim gets
    its own removal shot via the D2b guard)."""
    calls = _bind_then_succeed(crud, monkeypatch, fail_times=99)
    crud.container_remove.side_effect = RuntimeError("removal boom")

    with pytest.raises(Exception, match="port is already allocated"):
        _run(crud, _config())

    assert calls == [2227]                      # no second attempt
    crud.get_next_available_port.assert_not_called()


def test_ownership_row_aborts_retries(crud, monkeypatch):
    calls = _bind_then_succeed(crud, monkeypatch, fail_times=99)
    crud.db.is_agent_name_reserved.return_value = True

    with pytest.raises(Exception, match="port is already allocated"):
        _run(crud, _config())

    assert calls == [2227]
    crud.container_remove.assert_not_awaited()
    crud.get_next_available_port.assert_not_called()


def test_lookup_failure_aborts_but_lookup_404_means_clear(crud, monkeypatch):
    """`get_agent_container` flattens lookup FAILURE into 'absent'; the cleanup
    deliberately does not — absent (404) means safe to retry, anything else
    must abort."""
    calls = _bind_then_succeed(crud, monkeypatch, fail_times=99)
    crud.docker_client.containers.get.side_effect = RuntimeError("daemon down")

    with pytest.raises(Exception, match="port is already allocated"):
        _run(crud, _config())
    assert calls == [2227]
    crud.get_next_available_port.assert_not_called()

    # 404-shaped NotFound: no husk exists — the name is clear, retry proceeds.
    calls2 = _bind_then_succeed(crud, monkeypatch)
    crud.docker_client.containers.get.side_effect = _not_found()
    result = _run(crud, _config())
    assert calls2 == [2227, 2228]
    assert result.labels["trinity.ssh-port"] == "2228"
    crud.container_remove.assert_not_awaited()  # nothing to remove


def test_unprovable_provenance_aborts_retries(crud, monkeypatch):
    calls = _bind_then_succeed(crud, monkeypatch, fail_times=99)
    crud.docker_client.containers.get.side_effect = None
    crud.docker_client.containers.get.return_value = _husk(BEFORE_FLOOR)

    with pytest.raises(Exception, match="port is already allocated"):
        _run(crud, _config())

    assert calls == [2227]
    crud.container_remove.assert_not_awaited()


@pytest.mark.parametrize(
    "exc_text",
    [
        NAME_CONFLICT,  # contains "already in use" but NOT the bind phrasings
        "UnixHTTPConnectionPool: Read timed out. (read timeout=60)",
        "Cannot connect to the Docker daemon",
    ],
)
def test_non_bind_errors_never_retry(crud, monkeypatch, exc_text):
    calls = _bind_then_succeed(crud, monkeypatch, fail_times=99, exc_text=exc_text)

    with pytest.raises(Exception):
        _run(crud, _config())

    assert calls == [2227]
    crud.container_remove.assert_not_awaited()
    crud.get_next_available_port.assert_not_called()


def test_caller_pinned_port_never_retries(crud, monkeypatch):
    """A published port differing from the requested one is a surprise with
    security texture — the retry is gated on the port having been
    auto-allocated. The husk goes to the outer reclaim (D2b) instead."""
    calls = _bind_then_succeed(crud, monkeypatch, fail_times=99)

    with pytest.raises(Exception, match="port is already allocated"):
        _run(crud, _config(2227), auto=False)

    assert calls == [2227]
    crud.container_remove.assert_not_awaited()
    crud.get_next_available_port.assert_not_called()


# ---------------------------------------------------------------------------
# D2b — the reclaim's bind-before-name precedence guard
# ---------------------------------------------------------------------------

def _run_reclaim(crud, exc, handles=None):
    asyncio.run(
        crud._reclaim_failed_creation_container(handles or _handles(crud), None, exc)
    )


def test_reclaim_bind_error_no_handle_reclaims_under_gates(crud):
    """Pre-D2b: the Desktop bind phrasing matched the name-conflict fallback,
    the reclaim declined ('the existing container is not ours'), the husk
    squatted the name forever. Now: bind classified first -> fall through to
    the lookup + ownership + provenance gates -> removal."""
    crud.get_agent_container.return_value = _husk(AFTER_FLOOR)

    _run_reclaim(crud, Exception(BIND_ADDR_IN_USE))

    crud.container_remove.assert_awaited_once()
    crud.clear_agent_runtime_state.assert_awaited_once_with("a1")


def test_reclaim_pure_name_conflict_still_declines(crud):
    crud.get_agent_container.return_value = _husk(AFTER_FLOOR)

    _run_reclaim(crud, Exception(NAME_CONFLICT))

    crud.container_remove.assert_not_awaited()
    crud.get_agent_container.assert_not_called()


def test_reclaim_bind_error_keeps_the_fail_closed_gates(crud):
    """The guard only reorders classification — a bind error must still pass
    every ent#313 gate before anything is removed."""
    crud.get_agent_container.return_value = _husk(BEFORE_FLOOR)

    _run_reclaim(crud, Exception(BIND_ADDR_IN_USE))

    crud.container_remove.assert_not_awaited()


# ---------------------------------------------------------------------------
# Wiring pins (AST — the 'ships dead' hazards)
# ---------------------------------------------------------------------------

def _create_agent_internal_fn():
    import ast

    src = (_BACKEND / "services" / "agent_service" / "crud.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(src)
    return next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "create_agent_internal"
    )


def test_create_awaits_the_retry_wrapper_not_the_raw_create():
    import ast

    fn = _create_agent_internal_fn()
    awaited = {
        node.value.func.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
    }
    assert "_run_agent_container_with_port_retry" in awaited
    assert "_create_agent_container" not in awaited, (
        "create_agent_internal must go through the retry wrapper — a direct "
        "_create_agent_container call bypasses the #2215 bind-conflict belt"
    )


def test_auto_allocated_port_is_captured_before_the_mutation():
    """Captured AFTER `config.port = get_next_available_port()` the gate is
    always-False and the whole retry ships dead (plan strategy F7)."""
    import ast

    fn = _create_agent_internal_fn()
    capture_line = mutation_line = None
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "auto_allocated_port":
            capture_line = node.lineno
        if (
            isinstance(target, ast.Attribute)
            and target.attr == "port"
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "get_next_available_port"
        ):
            mutation_line = node.lineno
    assert capture_line is not None, "auto_allocated_port capture missing"
    assert mutation_line is not None, "port auto-allocation missing"
    assert capture_line < mutation_line, (
        "auto_allocated_port must be captured BEFORE config.port is mutated"
    )
