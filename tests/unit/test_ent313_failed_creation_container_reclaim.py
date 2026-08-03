"""ent#313 — a creation that fails after `containers.run` must not leak the container.

The defect: `_rollback_failed_creation` rolled back DB/quota handles only and
deferred the container to "the cleanup watchdog". No watchdog covers a
non-ephemeral agent — `cleanup_service._sweep_ephemeral_agents` is gated on the
`trinity.ephemeral` label — so a post-`containers.run` failure left a RUNNING
container with no `agent_ownership` row. Two guards then deadlocked: nothing
removed the container, and because the container kept its workspace volume
mounted, `_sweep_orphan_agent_volumes` could never advance its
`ORPHAN_VOLUME_UNATTACHED_STRIKES` counter (#1581), so the volume was
unreclaimable too. The phantom still listed in the UI, which reads Docker as
truth (Invariant #11). Observed on a real instance: 13+ hours, ~66 MB, an SSH
port and a 2g memory limit held by an agent with zero rows anywhere.

These tests exercise `_reclaim_failed_creation_container` directly rather than
through `create_agent_internal`, because the interesting half is the
**fail-closed provenance gating** on the no-handle path — the shape the reported
failure actually takes (a 60s Docker read timeout: the daemon created the
container, the client never received the handle).

The safety direction that matters: a false negative leaves one orphan for an
operator to `docker rm -f`; a false positive DELETES A RUNNING AGENT — including,
on a shared Docker daemon (git worktrees, a second stack on one host), an agent
belonging to a different install entirely. Every ambiguous case must refuse.
"""

from __future__ import annotations

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


FLOOR = "2026-08-03T10:00:00Z"
AFTER_FLOOR = "2026-08-03T10:00:05Z"
BEFORE_FLOOR = "2026-08-03T09:59:55Z"


def _container(created_label=AFTER_FLOOR, *, name="agent-a1"):
    """A docker-py container double carrying only what the reclaim reads."""
    labels = {} if created_label is None else {"trinity.created": created_label}
    return SimpleNamespace(
        name=name,
        attrs={"Config": {"Labels": labels}},
    )


@pytest.fixture
def crud(monkeypatch):
    """The real module, with its Docker + Redis + DB seams monkeypatched.

    Patching module attributes (never `sys.modules[...] =`) keeps
    tests/lint_sys_modules.py green.
    """
    try:
        from services.agent_service import crud as crud_mod
    except ImportError:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")

    monkeypatch.setattr(crud_mod, "container_remove", AsyncMock())
    monkeypatch.setattr(crud_mod, "clear_agent_runtime_state", AsyncMock())
    monkeypatch.setattr(crud_mod, "get_agent_container", MagicMock(return_value=None))
    monkeypatch.setattr(crud_mod.db, "is_agent_name_reserved", MagicMock(return_value=False))
    return crud_mod


def _handles(crud, name="a1", floor=FLOOR):
    return crud._RollbackHandles(agent_name=name, container_floor_ts=floor)


# ---------------------------------------------------------------------------
# The handle path — the create returned, a later step raised
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_container_returned_by_create_is_removed_and_state_cleared(crud):
    """AC 1 + 3 + 4: ownership is unambiguous, so no provenance check is needed."""
    container = _container()

    await crud._reclaim_failed_creation_container(
        _handles(crud), container, RuntimeError("post-container boom")
    )

    crud.container_remove.assert_awaited_once_with(container, force=True)
    crud.clear_agent_runtime_state.assert_awaited_once_with("a1")
    # The handle was in hand — no re-derivation by name.
    crud.get_agent_container.assert_not_called()


@pytest.mark.asyncio
async def test_a_stale_created_label_does_not_block_the_handle_path(crud):
    """Provenance gating applies ONLY to the re-derived path. With the handle we
    know we created it, whatever its label says."""
    await crud._reclaim_failed_creation_container(
        _handles(crud), _container(BEFORE_FLOOR), RuntimeError("boom")
    )
    crud.container_remove.assert_awaited_once()


# ---------------------------------------------------------------------------
# The no-handle path — the failure happened inside containers.run
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_timeout_inside_create_still_reclaims_by_name(crud):
    """The reported failure: docker created it, the client timed out at 60s."""
    orphan = _container(AFTER_FLOOR)
    crud.get_agent_container.return_value = orphan

    await crud._reclaim_failed_creation_container(
        _handles(crud), None, Exception("UnixHTTPConnectionPool: Read timed out. (read timeout=60)")
    )

    crud.container_remove.assert_awaited_once_with(orphan, force=True)
    crud.clear_agent_runtime_state.assert_awaited_once_with("a1")


@pytest.mark.asyncio
async def test_no_container_exists_still_clears_redis(crud):
    """AC 3: `agent:circuit:{name}` outlived the rollback in the field report.
    Nothing to remove is still a reason to clear the name-keyed keyspace (#1560),
    and the container is provably absent, so the slot clear is safe."""
    crud.get_agent_container.return_value = None

    await crud._reclaim_failed_creation_container(
        _handles(crud), None, Exception("some other failure")
    )

    crud.container_remove.assert_not_awaited()
    crud.clear_agent_runtime_state.assert_awaited_once_with("a1")


# ---------------------------------------------------------------------------
# Fail-closed gates — AC 5, and the cross-install hazard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        Exception('Conflict. The container name "/agent-a1" is already in use'),
        Exception("409 Client Error: Conflict"),
    ],
)
async def test_name_conflict_never_touches_the_incumbent(crud, exc):
    """A 409 means the daemon created NOTHING — so whatever holds the name is
    somebody else's: a live agent here, or another install's agent on a shared
    daemon. This is the case that would destroy a running agent."""
    crud.get_agent_container.return_value = _container(AFTER_FLOOR)

    await crud._reclaim_failed_creation_container(_handles(crud), None, exc)

    crud.container_remove.assert_not_awaited()
    crud.clear_agent_runtime_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_docker_api_error_409_is_recognised_by_status_not_only_text(crud):
    """The string check is a fallback; the typed check is the real one."""
    import docker

    err = docker.errors.APIError("boom", response=SimpleNamespace(status_code=409))
    crud.get_agent_container.return_value = _container(AFTER_FLOOR)

    await crud._reclaim_failed_creation_container(_handles(crud), None, err)

    crud.container_remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_container_predating_this_attempt_is_left_alone(crud):
    """AC 5: a container whose `trinity.created` is older than this attempt was
    not created by it — e.g. a live agent mid-recreate, or a sibling stack's."""
    crud.get_agent_container.return_value = _container(BEFORE_FLOOR)

    await crud._reclaim_failed_creation_container(
        _handles(crud), None, Exception("read timeout")
    )

    crud.container_remove.assert_not_awaited()
    crud.clear_agent_runtime_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("label", [None, "not-a-timestamp"])
async def test_unprovable_provenance_refuses(crud, label):
    """Missing or unparseable label ⇒ cannot prove we created it ⇒ refuse."""
    crud.get_agent_container.return_value = _container(label)

    await crud._reclaim_failed_creation_container(
        _handles(crud), None, Exception("read timeout")
    )

    crud.container_remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_floor_timestamp_refuses(crud):
    """No floor ⇒ no provenance ⇒ refuse (an old `_RollbackHandles` shape)."""
    crud.get_agent_container.return_value = _container(AFTER_FLOOR)

    await crud._reclaim_failed_creation_container(
        _handles(crud, floor=None), None, Exception("read timeout")
    )

    crud.container_remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_registered_agent_keeps_its_container_even_with_the_handle(crud):
    """The regression this gate exists for: `_register_agent` writes the
    ownership row BEFORE the last creation step, so a failure in
    `_materialize_agent_files` arrives here holding the handle of a container
    the DB already considers a created agent. Removing it would turn a
    half-created-but-present agent into a row with no container, still holding
    its name — strictly worse than the leak. Leave it; `DELETE /api/agents/{name}`
    removes both."""
    crud.db.is_agent_name_reserved.return_value = True

    await crud._reclaim_failed_creation_container(
        _handles(crud), _container(), RuntimeError("materialize boom")
    )

    crud.container_remove.assert_not_awaited()
    crud.clear_agent_runtime_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_registered_name_means_a_concurrent_creation_won(crud):
    """Two creations racing on one name: the winner registers its ownership row,
    so the loser's rollback must not remove the winner's container."""
    crud.db.is_agent_name_reserved.return_value = True
    crud.get_agent_container.return_value = _container(AFTER_FLOOR)

    await crud._reclaim_failed_creation_container(
        _handles(crud), None, Exception("read timeout")
    )

    crud.container_remove.assert_not_awaited()
    crud.get_agent_container.assert_not_called()


@pytest.mark.asyncio
async def test_ownership_lookup_failure_refuses(crud):
    """A DB error is not evidence of absence."""
    crud.db.is_agent_name_reserved.side_effect = RuntimeError("db down")
    crud.get_agent_container.return_value = _container(AFTER_FLOOR)

    await crud._reclaim_failed_creation_container(
        _handles(crud), None, Exception("read timeout")
    )

    crud.container_remove.assert_not_awaited()


# ---------------------------------------------------------------------------
# Never raises into the creation except-path; never clears state it can't justify
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_removal_failure_is_swallowed_and_leaves_redis_alone(crud):
    """The original creation failure must still be the one the caller reports.
    And with the container still up, the slot ZSET is not provably idle — so the
    keyspace clear is skipped rather than wiping state from under it."""
    crud.container_remove.side_effect = RuntimeError("daemon unreachable")

    await crud._reclaim_failed_creation_container(
        _handles(crud), _container(), RuntimeError("boom")
    )

    crud.clear_agent_runtime_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_redis_clear_failure_is_swallowed(crud):
    crud.clear_agent_runtime_state.side_effect = RuntimeError("redis down")

    await crud._reclaim_failed_creation_container(
        _handles(crud), _container(), RuntimeError("boom")
    )

    crud.container_remove.assert_awaited_once()


@pytest.mark.asyncio
async def test_conflict_check_survives_a_stubbed_docker_module(crud, monkeypatch):
    """The reclaim must not raise even when `docker` is a test double.

    Caught by the full suite: the first version used
    `isinstance(exc, docker.errors.APIError)`, which raises
    `TypeError: isinstance() arg 2 must be a type` wherever a sibling test stubs
    the docker module (test_fork_to_own, test_1484 both do). That TypeError
    propagated out of a function whose contract is "never raises" and REPLACED
    the creation error the caller was trying to report — turning a clear
    "failed to persist per-agent GitHub PAT" into an unrelated TypeError. The
    409 check is duck-typed now; this pins it.
    """
    from types import SimpleNamespace as NS

    monkeypatch.setattr(crud, "docker", NS(errors=NS(APIError=MagicMock())))
    crud.get_agent_container.return_value = _container(AFTER_FLOOR)

    await crud._reclaim_failed_creation_container(
        _handles(crud), None, RuntimeError("failed to persist per-agent GitHub PAT")
    )

    # And the reclaim still did its job rather than bailing out.
    crud.container_remove.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_409_is_detected_by_response_status_on_any_exception_type(crud):
    """Duck-typing keeps the typed signal: a plain object carrying
    `.response.status_code == 409` is still recognised as a name conflict."""
    from types import SimpleNamespace as NS

    crud.get_agent_container.return_value = _container(AFTER_FLOOR)

    await crud._reclaim_failed_creation_container(
        _handles(crud), None, RuntimeError("boom")
    )
    crud.container_remove.assert_awaited_once()   # control: no 409 ⇒ reclaimed
    crud.container_remove.reset_mock()

    exc = RuntimeError("boom")
    exc.response = NS(status_code=409)
    await crud._reclaim_failed_creation_container(_handles(crud), None, exc)
    crud.container_remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_container_lookup_failure_is_swallowed(crud):
    crud.get_agent_container.side_effect = RuntimeError("docker down")

    await crud._reclaim_failed_creation_container(
        _handles(crud), None, Exception("read timeout")
    )

    crud.container_remove.assert_not_awaited()


# ---------------------------------------------------------------------------
# The docstring that justified the leak must not come back
# ---------------------------------------------------------------------------

def test_the_creation_except_path_actually_calls_the_reclaim():
    """The unit tests above drive the reclaim directly, so they stay green even
    if nothing calls it. Pin the wiring: the create except-path must await it,
    and it must be awaited (a bare call would schedule nothing and silently do
    nothing — the reclaim is async)."""
    import ast

    src = (_BACKEND / "services" / "agent_service" / "crud.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "create_agent_internal"
    )
    awaited = {
        node.value.func.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
    }
    assert "_reclaim_failed_creation_container" in awaited, (
        "create_agent_internal must AWAIT the reclaim on its failure path — "
        "otherwise the container leak (ent#313) is back with tests still green"
    )
    # And the DB/quota rollback should point a future reader at it, so the old
    # "left for the cleanup watchdog" conclusion isn't re-derived.
    body = src.split("def _rollback_failed_creation")[1].split("\ndef ")[0]
    assert "_reclaim_failed_creation_container" in body
