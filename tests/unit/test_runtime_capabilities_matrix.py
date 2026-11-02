"""Cross-runtime capability matrix (#1187 Phase G).

Each runtime declares honest capabilities so callers (Session-tab gate, frontend
tab visibility, cost-label rendering) can gate on a capability instead of
branching on the runtime name. The ABC default is conservative so a future
runtime that forgets to override is treated as the least-capable.
"""

from __future__ import annotations

import inspect

import pytest

from agent_server.services.runtime_adapter import AgentRuntime
from agent_server.services.codex_runtime import CodexRuntime
from agent_server.services.claude_code import ClaudeCodeRuntime
from agent_server.services.gemini_runtime import GeminiRuntime
from agent_server.services.opencode_runtime import OpenCodeRuntime


def test_default_capabilities_are_conservative():
    caps = AgentRuntime.capabilities()
    assert caps.chat_continuity is False
    assert caps.session_tab_resume is False
    assert caps.mcp_support is False
    assert caps.cost_reporting == "estimated"


def test_claude_is_the_reference_runtime():
    caps = ClaudeCodeRuntime.capabilities()
    assert caps.chat_continuity is True
    assert caps.session_tab_resume is True   # the Session tab is Claude's machinery
    assert caps.mcp_support is True
    assert caps.cost_reporting == "native"   # Claude emits total_cost_usd


def test_gemini_has_continuity_but_no_session_resume():
    caps = GeminiRuntime.capabilities()
    assert caps.chat_continuity is True
    assert caps.session_tab_resume is False  # execute_headless ignores resume
    assert caps.cost_reporting == "estimated"


def test_codex_matches_gemini_shape_for_resume_and_cost():
    caps = CodexRuntime.capabilities()
    assert caps.chat_continuity is True       # codex exec resume <thread_id>
    assert caps.session_tab_resume is False   # MVP: Session tab stays Claude/Gemini
    assert caps.mcp_support is True
    assert caps.cost_reporting == "estimated"


def test_opencode_has_continuity_and_native_cost():
    caps = OpenCodeRuntime.capabilities()
    assert caps.chat_continuity is True
    assert caps.session_tab_resume is False
    assert caps.mcp_support is True
    assert caps.cost_reporting == "native"


def test_capabilities_to_dict_is_serializable_for_callers():
    """to_dict() is what the backend/frontend serialize to gate UI — every flag
    must round-trip as a plain JSON-friendly dict."""
    caps = CodexRuntime.capabilities()
    d = caps.to_dict()
    assert d == {
        "chat_continuity": True,
        "session_tab_resume": False,
        "mcp_support": True,
        "cost_reporting": "estimated",
    }


# ---------------------------------------------------------------------------
# ABC-superset contract (Abilityai/lilu#651 — the test that would have caught
# #649 / #651 at sync time)
#
# For every runtime, the ABC's execute / execute_headless parameter set must be
# a SUBSET of the implementation's parameter set.  An implementation that
# accepts fewer parameters breaks any caller that passes the ABC-declared ones.
# ---------------------------------------------------------------------------

_RUNTIMES = [ClaudeCodeRuntime, GeminiRuntime, CodexRuntime, OpenCodeRuntime]
_ABC_METHODS = ["execute", "execute_headless"]


def _param_names(cls, method_name):
    fn = getattr(cls, method_name, None)
    if fn is None:
        return set()
    sig = inspect.signature(fn)
    return {name for name in sig.parameters if name != "self"}


@pytest.mark.parametrize("runtime_cls", _RUNTIMES, ids=[c.__name__ for c in _RUNTIMES])
@pytest.mark.parametrize("method", _ABC_METHODS)
def test_implementation_accepts_all_abc_params(runtime_cls, method):
    """Each implementation must accept every parameter the ABC declares.

    This guards against a runtime silently dropping an ABC parameter (the
    Abilityai/lilu#649/#651 class of bug).
    """
    abc_params = _param_names(AgentRuntime, method)
    impl_params = _param_names(runtime_cls, method)

    missing = abc_params - impl_params
    assert not missing, (
        f"{runtime_cls.__name__}.{method}() is missing ABC-declared params: {missing}"
    )
