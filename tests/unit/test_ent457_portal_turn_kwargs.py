"""Every kwarg a portal turn hands to the resume engine must be one
`execute_task` accepts (ent#457 review).

`run_resumable_turn` names a handful of parameters of its own and splats
**everything else** straight into `TaskExecutionService.execute_task`:

    result = await service.execute_task(..., **execute_kwargs)

That signature has no `**kwargs`. So a keyword the portal adds and the engine
never learned about is not a silently-dropped column — it is a `TypeError` at
call-binding time, raised before the agent is contacted, on EVERY Workspace
turn. It is invisible to the portal suites because they mock the engine, and
invisible to the engine suites because they never call it the way the portal
does; the only place the two signatures meet is here.

Found in review of ent#457, which added `source_channel_client=email` to
`portal_chat`'s call and to `db.create_task_execution`, but not to the
`execute_task` sitting between them — so the sync Workspace chat path raised
`TaskExecutionService.execute_task() got an unexpected keyword argument
'source_channel_client'`.

Derived, never hardcoded: the kwarg names are read out of the actual call so a
future addition is covered without anyone remembering to extend a list.
"""
from __future__ import annotations

import ast
import inspect

import pytest

pytestmark = pytest.mark.unit


def _forwarded_kwargs() -> set[str]:
    """The keywords `portal_chat` passes that `run_resumable_turn` forwards."""
    from client_portal import service as portal_service
    from services import session_turn_service

    tree = ast.parse(inspect.getsource(portal_service.portal_chat).lstrip())
    passed: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name != "run_resumable_turn":
            continue
        passed |= {kw.arg for kw in node.keywords if kw.arg}
    assert passed, "portal_chat no longer calls run_resumable_turn by that name"

    consumed = set(inspect.signature(session_turn_service.run_resumable_turn).parameters)
    return passed - consumed


def test_portal_turn_kwargs_bind_against_execute_task():
    from services.task_execution_service import TaskExecutionService

    forwarded = _forwarded_kwargs()
    assert forwarded, "expected the portal to forward at least one execute_task kwarg"

    sig = inspect.signature(TaskExecutionService.execute_task)
    accepted = set(sig.parameters)
    unknown = sorted(k for k in forwarded if k not in accepted)
    assert not unknown, (
        f"portal_chat forwards {unknown} into execute_task, whose signature does not "
        f"accept them and has no **kwargs — every Workspace turn would raise TypeError"
    )

    # And prove it by actually binding, so a future `**kwargs` on execute_task
    # cannot make the membership check above vacuously pass while the real call
    # still misroutes the value into a dict nobody reads.
    sig.bind_partial(
        None, agent_name="a", message="m", triggered_by="public",
        **{k: None for k in forwarded},
    )


def test_the_client_stamp_is_persisted_not_merely_accepted():
    """Accepting the kwarg without writing it is the same outage one layer on:
    `_resolve_portal` fails CLOSED on a NULL client, so the sync Workspace turn
    would create rows whose delegated children can never report back."""
    from services import task_execution_service

    src = inspect.getsource(task_execution_service.TaskExecutionService.execute_task)
    tree = ast.parse(src.lstrip())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name != "create_task_execution":
            continue
        names = {kw.arg for kw in node.keywords if kw.arg}
        assert "source_channel_client" in names, (
            "execute_task's row-creation branch accepts source_channel_client but "
            "does not persist it"
        )
        return
    pytest.fail("execute_task no longer calls db.create_task_execution")
