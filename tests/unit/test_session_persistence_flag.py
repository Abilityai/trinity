"""
Phase 1.4 / 1.5 regression tests for the ``persist_session`` flag.

Two surfaces verify that flow:

1. ``docker/base-image/agent_server/services/claude_code.py``
   ``execute_headless_task`` must gate ``--no-session-persistence`` on
   ``not persist_session`` so a Session-tab cold turn writes the JSONL the
   next ``--resume`` will need. We pin this with AST/source assertions
   because the function is heavily side-effectful (subprocesses, asyncio
   readers) and the existing pattern in test_chat_wallclock_timeout.py
   does the same.

2. ``src/backend/services/task_execution_service.py`` ``execute_task`` must
   thread ``persist_session`` (default False) into the agent payload — so
   every existing caller (Chat, schedules, MCP, fan-out, webhooks) keeps
   today's stateless behavior, and only routers/sessions.py opts in.
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CLAUDE_CODE_PY = (
    _PROJECT_ROOT / "docker" / "base-image" / "agent_server" / "services" / "claude_code.py"
)
_TASK_EXEC_PY = (
    _PROJECT_ROOT / "src" / "backend" / "services" / "task_execution_service.py"
)
_RUNTIME_ABC_PY = (
    _PROJECT_ROOT / "docker" / "base-image" / "agent_server" / "services" / "runtime_adapter.py"
)
_AGENT_CHAT_ROUTER_PY = (
    _PROJECT_ROOT / "docker" / "base-image" / "agent_server" / "routers" / "chat.py"
)
_PARALLEL_REQ_PY = (
    _PROJECT_ROOT / "docker" / "base-image" / "agent_server" / "models.py"
)


# ---------------------------------------------------------------------------
# Helpers — minimal, mirror test_chat_wallclock_timeout.py
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    assert path.is_file(), f"missing {path}"
    return path.read_text()


def _find_function(tree: ast.Module, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


def _function_source(source: str, func) -> str:
    lines = source.splitlines()
    return "\n".join(lines[func.lineno - 1 : func.end_lineno])


def _has_default(func, arg_name: str, expected_default) -> bool:
    """Return True if `arg_name` is a kwonly/positional with literal default `expected_default`."""
    args = func.args
    # kwonly defaults align 1:1 with kwonly args
    for a, default in zip(args.kwonlyargs, args.kw_defaults):
        if a.arg == arg_name:
            return isinstance(default, ast.Constant) and default.value == expected_default
    # positional defaults align with the trailing positional/kwarg names
    pos_args = args.posonlyargs + args.args
    if pos_args and args.defaults:
        defaults = args.defaults
        offset = len(pos_args) - len(defaults)
        for i, a in enumerate(pos_args):
            if a.arg == arg_name and i >= offset:
                d = defaults[i - offset]
                return isinstance(d, ast.Constant) and d.value == expected_default
    return False


# ---------------------------------------------------------------------------
# 1. Agent-server: execute_headless_task gates --no-session-persistence
# ---------------------------------------------------------------------------


def test_execute_headless_task_accepts_persist_session_default_false():
    tree = ast.parse(_read(_CLAUDE_CODE_PY))
    func = _find_function(tree, "execute_headless_task")
    arg_names = [a.arg for a in (func.args.args + func.args.kwonlyargs)]
    assert "persist_session" in arg_names, "persist_session must be in signature"
    assert _has_default(func, "persist_session", False), \
        "persist_session must default to False so existing callers are unaffected"


def test_execute_headless_task_gates_no_session_persistence():
    """The flag must be added only when persist_session is False — otherwise
    the JSONL stays empty and turn 2's --resume errors with 'No conversation
    found' (the spike's central failure mode)."""
    src = _read(_CLAUDE_CODE_PY)
    tree = ast.parse(src)
    body = _function_source(src, _find_function(tree, "execute_headless_task"))

    # The append must be conditional on `not persist_session`.
    assert re.search(
        r"if\s+not\s+persist_session\s*:\s*\n\s*cmd\.append\(\s*['\"]--no-session-persistence['\"]",
        body,
    ), "--no-session-persistence must be gated on `if not persist_session:`"

    # And there must be no unconditional append left over.
    unconditional = re.findall(r"cmd\.append\(\s*['\"]--no-session-persistence['\"]\)", body)
    # Exactly one occurrence — the gated one above.
    assert len(unconditional) == 1, (
        f"expected a single (gated) --no-session-persistence append, found {len(unconditional)}"
    )

    # --session-id must still be passed even when persist_session=True so
    # cold Session turns get a unique JSONL namespace.
    assert "--session-id" in body, "--session-id must be passed for cold turns"


# ---------------------------------------------------------------------------
# 2. Agent-server: ABC + ParallelTaskRequest + chat router pass it through
# ---------------------------------------------------------------------------


def test_runtime_adapter_abc_includes_persist_session():
    tree = ast.parse(_read(_RUNTIME_ABC_PY))
    func = _find_function(tree, "execute_headless")
    arg_names = [a.arg for a in (func.args.args + func.args.kwonlyargs)]
    assert "persist_session" in arg_names
    assert _has_default(func, "persist_session", False)


def test_parallel_task_request_has_persist_session_field():
    """ParallelTaskRequest is the wire model — without this field, the agent
    server silently drops the flag from the payload."""
    src = _read(_PARALLEL_REQ_PY)
    assert re.search(
        r"persist_session\s*:\s*Optional\[bool\]\s*=\s*False",
        src,
    ), "ParallelTaskRequest must declare persist_session: Optional[bool] = False"


def test_agent_chat_router_forwards_persist_session():
    src = _read(_AGENT_CHAT_ROUTER_PY)
    # The router receives ParallelTaskRequest and must hand the flag to runtime.execute_headless.
    assert "persist_session=" in src and "request.persist_session" in src, (
        "agent_server/routers/chat.py must forward request.persist_session to runtime.execute_headless"
    )


# ---------------------------------------------------------------------------
# 3. Backend: task_execution_service.execute_task plumbs persist_session
# ---------------------------------------------------------------------------


def test_execute_task_accepts_persist_session_default_false():
    tree = ast.parse(_read(_TASK_EXEC_PY))
    # execute_task is an async method on TaskExecutionService — find it inside the class.
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "TaskExecutionService":
            for child in node.body:
                if isinstance(child, ast.AsyncFunctionDef) and child.name == "execute_task":
                    target = child
                    break
    assert target is not None, "TaskExecutionService.execute_task not found"

    arg_names = [a.arg for a in (target.args.args + target.args.kwonlyargs)]
    assert "persist_session" in arg_names
    assert _has_default(target, "persist_session", False), (
        "persist_session must default to False so existing callers (Chat, schedules, "
        "MCP, fan-out, webhooks) remain unchanged"
    )


def test_execute_task_payload_includes_persist_session():
    """The payload posted to the agent is the contract — if persist_session
    isn't in it, the agent server can't gate --no-session-persistence."""
    src = _read(_TASK_EXEC_PY)
    # Must have a literal `"persist_session": persist_session` line inside the payload dict.
    assert re.search(
        r'"persist_session"\s*:\s*persist_session',
        src,
    ), 'payload dict must include "persist_session": persist_session'


def test_execute_task_runtime_signature_inherits_default():
    """Live signature check: importing the service yields a callable whose
    persist_session default is False. Catches accidental signature drift
    that AST parsing alone might miss (e.g. signature wrappers)."""
    import sys
    from pathlib import Path as _P

    backend_str = str(_P(__file__).resolve().parents[2] / "src" / "backend")
    if backend_str not in sys.path:
        sys.path.insert(0, backend_str)

    from services.task_execution_service import TaskExecutionService  # noqa: E402

    sig = inspect.signature(TaskExecutionService.execute_task)
    assert "persist_session" in sig.parameters
    assert sig.parameters["persist_session"].default is False
