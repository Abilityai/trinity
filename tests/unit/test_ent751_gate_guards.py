"""
Gated skills — the guards that keep the gate on every road to an agent
(trinity-enterprise#751).

The behaviour is proven by executing it (``test_ent751_gate_entries.py``,
``test_ent751_gate_dispatch.py``, ``test_ent751_gate_callers.py``). These guards
are the other half: they DISCOVER the code that would bypass the gate tomorrow —
a new function that posts to an agent, a new producer that composes a message
without passing the requester's own words, a new ``execute_task`` keyword the
frozen dispatch never classified — and print what they found, so a guard that
went blind shows up as an empty discovery rather than a silent pass.
"""
import ast
import inspect
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"


def _modules():
    for p in sorted(_BACKEND.rglob("*.py")):
        if "enterprise" in p.parts or "__pycache__" in p.parts:
            continue
        try:
            yield p.relative_to(_BACKEND).as_posix(), ast.parse(p.read_text())
        except SyntaxError:
            continue


class _Innermost(ast.NodeVisitor):
    """Yields (function name, node) for every node, keyed to its innermost
    enclosing function."""

    def __init__(self):
        self.stack, self.found = [], []

    def generic_visit(self, node):
        is_fn = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        if is_fn:
            self.stack.append(node.name)
        if self.stack:
            self.found.append((self.stack[-1], node))
        super().generic_visit(node)
        if is_fn:
            self.stack.pop()


def _walk(tree):
    v = _Innermost()
    v.visit(tree)
    return v.found


def _call_name(node):
    f = node.func
    return f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)


# ---------------------------------------------------------------------------
# 1. Every function that posts to an agent sits behind the gate
# ---------------------------------------------------------------------------

def _dispatch_sites():
    sites = {}
    for rel, tree in _modules():
        for fn, node in _walk(tree):
            reason = None
            if isinstance(node, ast.Call):
                name = _call_name(node)
                f = node.func
                if name == "agent_post_with_retry":
                    reason = "agent_post_with_retry"
                elif (isinstance(f, ast.Attribute) and f.attr in ("task", "chat")
                      and isinstance(f.value, ast.Name) and f.value.id.endswith("client")):
                    reason = f"{f.value.id}.{f.attr}"
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and node.value.rstrip("/").endswith(("/api/task", "/api/chat")):
                reason = f"literal {node.value!r}"
            if reason:
                sites.setdefault((rel, fn), set()).add(reason)
    return sites


# Each known dispatching function, and WHY it is behind the gate. A structural
# check below backs every entry that is not a primitive or a fixed prompt.
_DISPATCH_SITES = {
    ("services/task_execution_service.py", "_call_agent_with_retries"):
        "only reached from execute_task, after the 1b skill-gate backstop",
    ("services/chat_execution_service.py", "_walk_chat_dispatch"):
        "the /chat dispatch loop (#3470) — only reached from run_chat_turn, which "
        "routers/chat.py::chat_with_agent calls after admit_chat_request",
    ("services/gemini_voice.py", "_execute_tool"):
        "calls skill_gate_service.enforce(refuse_only=True) before client.task",
    ("routers/public.py", "get_agent_intro"):
        "a fixed platform intro prompt — no requester text reaches it",
    ("services/agent_client/client.py", "chat"): "the primitive itself (callers are discovered)",
    ("services/agent_client/client.py", "task"): "the primitive itself (callers are discovered)",
}


def test_every_function_that_posts_to_an_agent_is_known():
    sites = _dispatch_sites()
    print("discovered dispatch sites:", sorted(sites))
    assert sites, "the discovery found nothing — the guard went blind"
    unknown = sorted(set(sites) - set(_DISPATCH_SITES))
    assert not unknown, (
        f"new code posts to an agent outside the skill gate: {unknown}. Route it through "
        "execute_task / an admission seam, or call skill_gate_service.enforce first and add "
        "it to _DISPATCH_SITES with the reason.")
    dead = sorted(set(_DISPATCH_SITES) - set(sites))
    assert not dead, f"_DISPATCH_SITES names code that no longer dispatches: {dead}"


def _fn(rel, name):
    tree = dict(_modules())[rel]
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _first_line_of_call(fn_node, names):
    lines = [n.lineno for n in ast.walk(fn_node) if isinstance(n, ast.Call) and _call_name(n) in names]
    return min(lines) if lines else None


def test_execute_task_runs_the_backstop_before_admission_and_owns_the_agent_call():
    execute = _fn("services/task_execution_service.py", "execute_task")
    backstop = _first_line_of_call(execute, {"_skill_gate_backstop"})
    admission = _first_line_of_call(execute, {"_admission_gate"})
    assert backstop and admission and backstop < admission
    callers = [fn for rel, tree in _modules() for fn, n in _walk(tree)
               if isinstance(n, ast.Call) and _call_name(n) == "_call_agent_with_retries"]
    assert callers == ["execute_task"], callers


def test_the_chat_route_admits_through_the_gate_before_it_runs_the_turn():
    route = _fn("routers/chat.py", "chat_with_agent")
    admit = _first_line_of_call(route, {"admit_chat_request"})
    run = _first_line_of_call(route, {"run_chat_turn"})
    assert admit and run and admit < run
    admit_fn = _fn("services/dispatch_admission_service.py", "admit_chat_request")
    gate = _first_line_of_call(admit_fn, {"enforce"})
    claim = _first_line_of_call(admit_fn, {"begin"})
    assert gate and claim and gate < claim, "the gate must run before the idempotency claim"
    callers = {(rel, fn) for rel, tree in _modules() for fn, n in _walk(tree)
               if isinstance(n, ast.Call) and _call_name(n) == "run_chat_turn"}
    assert callers == {("routers/chat.py", "chat_with_agent")}, callers


def test_the_task_seam_runs_the_gate_before_the_claim_the_upload_and_the_row():
    fn = _fn("services/chat_execution_service.py", "dispatch_parallel_task")
    gate = _first_line_of_call(fn, {"enforce"})
    claim = _first_line_of_call(fn, {"begin_task_idempotency"})
    row = _first_line_of_call(fn, {"create_task_execution_and_activities"})
    assert gate and claim and row and gate < claim < row


def test_the_voice_tool_refuses_before_it_calls_the_agent():
    fn = _fn("services/gemini_voice.py", "_execute_tool")
    gate = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and _call_name(n) == "enforce"]
    assert gate and any(k.arg == "refuse_only" and getattr(k.value, "value", None) is True
                        for k in gate[0].keywords)
    task = _first_line_of_call(fn, {"task"})
    assert gate[0].lineno < task


# ---------------------------------------------------------------------------
# 2. Every producer hands the gate the requester's own words
# ---------------------------------------------------------------------------

# Producers whose `message` IS the requester's own text (or a platform-composed
# frame that carries no request), so the gate may read it as is. Everything
# else must pass `request_text=`, or `gate_checked=True` (an admission seam ran
# the gate), or forward `**kwargs` (a pass-through wrapper).
_UNWRAPPED = {
    ("client_portal/service.py", "dispatch_capture_feedback"): "fixed platform prose, no requester text",
    ("routers/a2a.py", "_run_a2a_task"): "the A2A caller's text as sent",
    ("routers/internal.py", "execute_task_internal"): "the schedule message as its owner wrote it",
    ("routers/internal.py", "_execute_task_internal_background"): "the schedule message as its owner wrote it",
    # ent#679 moved the paid dispatch into a closure the shared paid turn runs.
    ("routers/paid.py", "_execute"): "the payer's message as sent",
    ("routers/a2a.py", "_run_a2a_paid_execution"): "the paying A2A caller's text as sent",
    ("routers/sessions.py", "send_session_message"): "the person's message (resumed session, no prefix)",
    ("services/brain_orb_postprocess.py", "_run"): "a platform frame around a voice transcript",
    ("services/loop_service.py", "_run_and_advance"): "the rendered iteration is what an approval runs and what the approver sees",
    ("services/mcp_auth_service.py", "dispatch_chat"): "the message as sent",
    ("services/skill_gate_service.py", "_run"): "the gate's own notice (trigger skill_gate)",
    ("services/voip_service.py", "process_call_transcript"): "a platform frame around a call transcript",
}


def _producers():
    found = {}
    for rel, tree in _modules():
        for fn, node in _walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in (
                    "execute_task", "dispatch_and_await_terminal", "run_resumable_turn"):
                kws = {k.arg for k in node.keywords}
                gate_checked = any(k.arg == "gate_checked" and getattr(k.value, "value", None) is True
                                   for k in node.keywords)
                found[(rel, fn)] = "request_text" in kws or gate_checked or None in kws
    return found


def test_every_producer_passes_the_requesters_own_words_or_is_named_unwrapped():
    producers = _producers()
    print("discovered producers:", sorted(producers))
    assert len(producers) > 10, "the discovery found almost nothing — the guard went blind"
    missing = sorted(k for k, ok in producers.items() if not ok and k not in _UNWRAPPED)
    assert not missing, (
        f"producers that compose a message without `request_text=` and are not named "
        f"unwrapped: {missing}. Pass the requester's own words, or add the producer to "
        "_UNWRAPPED with the reason its message is that text.")
    dead = sorted(set(_UNWRAPPED) - set(producers))
    assert not dead, f"_UNWRAPPED names code that no longer dispatches: {dead}"


# ---------------------------------------------------------------------------
# 3. The frozen dispatch classifies every execute_task keyword
# ---------------------------------------------------------------------------

def test_every_execute_task_keyword_has_a_fate_in_the_frozen_dispatch():
    from services.skill_gate_service import DISPATCH_FIELDS
    from services.task_execution_service import TaskExecutionService

    params = set(inspect.signature(TaskExecutionService.execute_task).parameters) - {"self"}
    assert set(DISPATCH_FIELDS) == params, {
        "unclassified": sorted(params - set(DISPATCH_FIELDS)),
        "stale": sorted(set(DISPATCH_FIELDS) - params),
    }


def test_frozen_dispatch_refuses_an_unclassified_keyword():
    from services.skill_gate_service import frozen_dispatch
    with pytest.raises(ValueError):
        frozen_dispatch(model="sonnet", brand_new_kwarg=1)
    assert frozen_dispatch(model="sonnet", resume_session_id="s", execution_id="e") == {"model": "sonnet"}
