"""An OpenCode agent refuses a turn it cannot hold to a shared person's reach
(abilityai/trinity-enterprise#1384).

Claude Code holds such a turn with the turn-tier hook. Gemini CLI and Codex run
no hook and refuse it before the CLI starts (ent#926). OpenCode ran no hook and
refused nothing, so the turn ran with the agent's whole shell and file access.
It now asks the same question at the same place: the top of `execute` and
`execute_headless`, which every turn passes on its way to the CLI.

Three things are pinned here:

- the two entry points refuse, and run what the platform allows;
- every route into a runtime hands it the turn's execution id, since a call
  with none is not asked about;
- every runtime the factory can return either asks, or is on the short list of
  runtimes that hold the turn another way. A fifth runtime that does neither
  fails this file.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

# conftest wires the real docker/base-image/agent_server as `agent_server`.
from agent_server.services import opencode_runtime, turn_scope

pytestmark = pytest.mark.unit

AGENT_SERVER = Path(__file__).resolve().parents[2] / "docker" / "base-image" / "agent_server"
ENTRY_POINTS = ("execute", "execute_headless")
GATE = ("turn_scope", "require_owner_tier")

# Runtimes that hold a shared person's turn without `turn_scope`, and how.
HELD_ANOTHER_WAY = {
    "claude_code": "the turn-tier hook holds the turn; managed_hooks.require_hooks_or_owner "
                   "refuses it when the hooks are not running (trinity-enterprise#1176)",
}


# ---- the two entry points -----------------------------------------------------------------

@pytest.fixture()
def runtime(monkeypatch):
    rt = opencode_runtime.OpenCodeRuntime()
    monkeypatch.setattr(rt, "is_available", lambda: True)
    monkeypatch.setattr(turn_scope, "_agent_name", lambda: "agent-a")
    started = []

    async def spawn(**kwargs):
        started.append(kwargs.get("execution_id"))
        raise _Started()
    monkeypatch.setattr(rt, "_execute_with_stall_retry", spawn)

    def no_process(*a, **k):
        raise AssertionError("the CLI was started for a refused turn")
    monkeypatch.setattr(opencode_runtime.subprocess, "Popen", no_process)
    rt.started = started
    return rt


class _Started(Exception):
    """The turn got past the gate and reached the code that starts the CLI."""


def _platform_says(monkeypatch, tier, may_run, kind=None, schedule=None):
    asked = []

    async def answer(agent, run):
        asked.append((agent, run))
        return tier, may_run, kind, schedule
    monkeypatch.setattr(turn_scope, "fetch_turn", answer)
    return asked


def _run(rt, entry, **kwargs):
    return asyncio.run(getattr(rt, entry)(prompt="hi", **kwargs))


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_a_shared_persons_turn_is_refused_before_the_cli_starts(runtime, monkeypatch, entry):
    asked = _platform_says(monkeypatch, "share", False, "person")
    with pytest.raises(HTTPException) as exc:
        _run(runtime, entry, execution_id="exec-1")
    assert exc.value.status_code == 403
    assert exc.value.detail["error_code"] == turn_scope.ERROR_CODE
    assert "OpenCode" in exc.value.detail["message"] and "shared with" in exc.value.detail["message"]
    assert asked == [("agent-a", "exec-1")]
    assert runtime.started == []


def test_the_fresh_session_chat_path_is_refused_too(runtime, monkeypatch):
    """`isolated_session` takes its own branch in `execute`; the gate is above it."""
    _platform_says(monkeypatch, "share", False, "person")
    with pytest.raises(HTTPException) as exc:
        _run(runtime, "execute", execution_id="exec-1", isolated_session=True)
    assert exc.value.status_code == 403
    assert runtime.started == []


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_a_turn_the_platform_cannot_place_is_refused(runtime, monkeypatch, entry):
    _platform_says(monkeypatch, "unknown", False)
    with pytest.raises(HTTPException) as exc:
        _run(runtime, entry, execution_id="exec-1")
    assert exc.value.status_code == 403
    assert runtime.started == []


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_no_answer_refuses_rather_than_running(runtime, monkeypatch, entry):
    _platform_says(monkeypatch, "unreachable", False)
    with pytest.raises(HTTPException) as exc:
        _run(runtime, entry, execution_id="exec-1")
    assert exc.value.status_code == 503
    assert "OpenCode" in exc.value.detail
    assert runtime.started == []


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_a_visitor_the_owner_limited_is_told_so(runtime, monkeypatch, entry):
    _platform_says(monkeypatch, "share", False, "visitor")
    with pytest.raises(HTTPException) as exc:
        _run(runtime, entry, execution_id="exec-1")
    assert exc.value.status_code == 403
    assert "channel or a public link" in exc.value.detail["message"]
    assert runtime.started == []


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_a_schedule_rated_below_the_owner_is_refused_by_name(runtime, monkeypatch, entry):
    """A scheduled run and a wake arrive as a task; the refusal names the schedule."""
    schedule = {"cause": "creator_not_owner_tier", "schedule": "Morning brief", "creator": "Dana",
                "remedy": "recreate"}
    _platform_says(monkeypatch, "share", False, "person", schedule)
    with pytest.raises(HTTPException) as exc:
        _run(runtime, entry, execution_id="exec-1")
    message = exc.value.detail["message"]
    assert "Morning brief" in message and "OpenCode" in message and "did not run" in message
    assert runtime.started == []


@pytest.mark.parametrize("entry", ENTRY_POINTS)
@pytest.mark.parametrize("tier,kind", [("owner", None), ("owner", "member"), ("owner", "delegated"),
                                       ("share", "visitor")])
def test_a_turn_the_platform_allows_reaches_the_cli(runtime, monkeypatch, entry, tier, kind):
    """The owner's turn, an owner-by-rule turn (a member, a delegated turn), and a
    channel visitor while the owner leaves `visitor_shell` on."""
    _platform_says(monkeypatch, tier, True, kind)
    # The runtime maps whatever the spawn raises at its boundary; what matters
    # is that the turn got as far as the spawn.
    with pytest.raises(Exception):
        _run(runtime, entry, execution_id="exec-1")
    assert runtime.started == ["exec-1"]


@pytest.mark.parametrize("entry", ENTRY_POINTS)
def test_a_call_with_no_execution_id_is_no_platform_turn(runtime, monkeypatch, entry):
    asked = _platform_says(monkeypatch, "share", False, "person")
    with pytest.raises(Exception):
        _run(runtime, entry)
    assert asked == []
    assert len(runtime.started) == 1


# ---- every route into a runtime hands it the turn ------------------------------------------

def _trees():
    for path in sorted(AGENT_SERVER.rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _runtime_calls(tree):
    """Calls of `<something>.execute(...)` / `.execute_headless(...)` on a name
    bound from `get_runtime()` in the same module."""
    bound = {t.id for node in ast.walk(tree) if isinstance(node, ast.Assign)
             and isinstance(node.value, ast.Call) and getattr(node.value.func, "id", None) == "get_runtime"
             for t in node.targets if isinstance(t, ast.Name)}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ENTRY_POINTS and isinstance(node.func.value, ast.Name)
                and node.func.value.id in bound):
            yield node


def test_every_route_into_a_runtime_passes_the_turns_execution_id():
    """The gate asks nothing about a call with no execution id, so a route that
    left it out would run every turn unasked."""
    sites, missing = [], []
    for path, tree in _trees():
        for call in _runtime_calls(tree):
            where = f"{path.relative_to(AGENT_SERVER)}:{call.lineno}"
            sites.append(where)
            if "execution_id" not in {kw.arg for kw in call.keywords}:
                missing.append(where)
    assert not missing, f"runtime started without the turn's execution id: {missing}"
    # The sync chat, the task push, a pull-mode claim, and the async result callback.
    assert {s.split(":")[0] for s in sites} == {"routers/chat.py", "services/pull_worker.py",
                                               "services/result_callback.py"}, sites
    assert len(sites) == 4, sites


def test_nothing_outside_the_runtime_reaches_under_its_entry_points():
    private = {"_execute_opencode", "_execute_with_stall_retry"}
    outside = [f"{path.relative_to(AGENT_SERVER)}:{node.lineno}"
               for path, tree in _trees() if path.name != "opencode_runtime.py"
               for node in ast.walk(tree)
               if isinstance(node, ast.Attribute) and node.attr in private]
    assert outside == []


# ---- every runtime the factory can return --------------------------------------------------

def _factory_modules(source=None):
    """Module names `runtime_adapter.get_runtime` imports a runtime from."""
    source = source or (AGENT_SERVER / "services" / "runtime_adapter.py").read_text(encoding="utf-8")
    factory = next(n for n in ast.walk(ast.parse(source))
                   if isinstance(n, ast.FunctionDef) and n.name == "get_runtime")
    return sorted({node.module for node in ast.walk(factory)
                   if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module})


def _is_gate(node) -> bool:
    return (isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == GATE[1]
            and getattr(node.value.func.value, "id", None) == GATE[0])


def _ungated_entry_points(source: str):
    """Entry points of a runtime class that start work before asking, or never ask.

    The gate must be a statement of the method's own body (not inside a branch
    a turn could skip), and nothing before it may await or branch into a
    spawn: only the availability check and plain assignments come first."""
    problems = []
    tree = ast.parse(source)
    found = set()
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        for fn in (n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name in ENTRY_POINTS):
            found.add(fn.name)
            asked = False
            for stmt in fn.body:
                if isinstance(stmt, ast.Expr) and _is_gate(stmt.value):
                    asked = True
                    break
                if any(isinstance(n, ast.Await) for n in ast.walk(stmt)):
                    break
            if not asked:
                problems.append(f"{cls.name}.{fn.name}")
    problems.extend(f"<missing>.{name}" for name in ENTRY_POINTS if name not in found)
    return problems


def test_the_factory_returns_the_four_runtimes_this_file_knows():
    assert _factory_modules() == ["claude_code", "codex_runtime", "gemini_runtime", "opencode_runtime"]


def test_every_runtime_asks_whose_turn_it_is_or_holds_it_another_way():
    ungated = {}
    for module in _factory_modules():
        if module in HELD_ANOTHER_WAY:
            continue
        source = (AGENT_SERVER / "services" / f"{module}.py").read_text(encoding="utf-8")
        problems = _ungated_entry_points(source)
        if problems:
            ungated[module] = problems
    assert ungated == {}, (
        f"{ungated}: a runtime that cannot hold a shared person's turn must call "
        "turn_scope.require_owner_tier at the top of execute and execute_headless, "
        "before anything is awaited, or be listed in HELD_ANOTHER_WAY with how it holds one.")


def test_the_exception_list_names_only_runtimes_that_exist():
    assert set(HELD_ANOTHER_WAY) <= set(_factory_modules())


def test_the_scan_sees_a_runtime_that_does_not_ask():
    """The scan's own check: a runtime shaped like OpenCode before this fix."""
    before = '''
class R:
    async def execute(self, prompt, execution_id=None):
        if not self.is_available():
            raise RuntimeError("x")
        return await self._run(prompt)

    async def execute_headless(self, prompt, execution_id=None):
        if prompt:
            await turn_scope.require_owner_tier("R", execution_id)
        return await self._run(prompt)
'''
    assert _ungated_entry_points(before) == ["R.execute", "R.execute_headless"]
    late = '''
class R:
    async def execute(self, prompt, execution_id=None):
        await self._run(prompt)
        await turn_scope.require_owner_tier("R", execution_id)

    async def execute_headless(self, prompt, execution_id=None):
        await turn_scope.require_owner_tier("R", execution_id)
        return await self._run(prompt)
'''
    assert _ungated_entry_points(late) == ["R.execute"]
    assert _ungated_entry_points("class R:\n    async def execute(self):\n        await turn_scope.require_owner_tier('R', None)\n") \
        == ["<missing>.execute_headless"]
    factory = "def get_runtime():\n    from .fifth_runtime import get_fifth\n    return get_fifth()\n"
    assert _factory_modules(factory) == ["fifth_runtime"]
