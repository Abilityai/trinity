"""#2981 — runtime seam hygiene, before any OpenCode code depends on it.

Defects that the Codex runtime already has and that a fourth runtime would
inherit verbatim:

  1. ``plugins_reinstall`` ran on EVERY runtime. Every subprocess it runs is
     ``claude plugin ...``; on a non-Claude agent that is two guaranteed-failing
     execs per boot plus a ``withheld:`` state file describing a capability the
     runtime does not have. Gated on ``is_claude_runtime()``.
  2. ``AgentState`` probed ``claude --version`` for every non-Gemini runtime,
     so a Codex (or OpenCode) agent's availability was read off a binary it
     never invokes. Now one probe binary per runtime, keyed on the normalized
     name; an unknown runtime reports unavailable instead of probing Claude.

The predicate itself is pinned too: it must be case-insensitive (the #1187
casing trap) and it must be importable BOTH as a package member and standalone,
because ``plugins_reinstall`` is a ``python3 -m`` boot hook. An import fallback
that degrades to "assume Claude" would be a gate that silently does nothing,
which is why the name sets live in a zero-dependency leaf module.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_AGENT_SERVER = _ROOT / "docker" / "base-image" / "agent_server"



def _load(name: str, path: Path):
    """Load an agent-server module by path with the agent-server dir on
    ``sys.path`` — the ``test_1704_plugins_reinstall.py`` idiom."""
    sys.path.insert(0, str(_AGENT_SERVER))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(_AGENT_SERVER))


@pytest.fixture(scope="module")
def names():
    return _load("_oc2981_runtime_names", _AGENT_SERVER / "runtime_names.py")


@pytest.fixture(scope="module")
def plugins():
    return _load("_oc2981_plugins_reinstall", _AGENT_SERVER / "plugins_reinstall.py")


# --------------------------------------------------------------------------
# 1. the predicate
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        (None, True),          # unset → the historical default
        ("", True),            # crud.py writes "" for "pins no runtime"
        ("claude-code", True),
        ("claude", True),
        ("Claude-Code", True),  # #1187 casing trap
        ("  claude  ", True),
        ("codex", False),
        ("Codex", False),
        ("gemini-cli", False),
        ("gemini", False),
        ("opencode", False),
    ],
)
def test_is_claude_runtime_classification(names, value, expected):
    assert names.is_claude_runtime(value) is expected


def test_is_claude_runtime_reads_env_when_unspecified(names, monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME", "codex")
    assert names.is_claude_runtime() is False
    monkeypatch.setenv("AGENT_RUNTIME", "claude-code")
    assert names.is_claude_runtime() is True
    monkeypatch.delenv("AGENT_RUNTIME")
    assert names.is_claude_runtime() is True


def test_runtime_names_leaf_has_no_package_imports():
    """The whole point of the leaf: a boot hook loaded outside the package
    must still get the REAL predicate, never a fallback stub."""
    src = (_AGENT_SERVER / "runtime_names.py").read_text()
    assert "from ." not in src and "from .." not in src


def test_runtime_adapter_reexports_the_leaf(names):
    """Existing importers of ``runtime_adapter.KNOWN_RUNTIMES`` keep working,
    and there is exactly one definition of the name sets."""
    src = (_AGENT_SERVER / "services" / "runtime_adapter.py").read_text()
    assert "from ..runtime_names import" in src
    # No second definition of the Claude set anywhere in the adapter.
    assert not re.search(r"_CLAUDE_RUNTIMES\s*=\s*frozenset", src)


# --------------------------------------------------------------------------
# 2. the plugin-reinstall gate
# --------------------------------------------------------------------------

def _no_subprocess(monkeypatch, plugins):
    def _boom(*a, **k):  # pragma: no cover — asserts it is never reached
        raise AssertionError(f"ran a subprocess on a non-Claude runtime: {a!r}")

    monkeypatch.setattr(plugins.subprocess, "run", _boom)


@pytest.mark.parametrize("runtime", ["codex", "gemini-cli", "gemini", "opencode"])
def test_reinstall_skips_non_claude_runtimes(plugins, monkeypatch, tmp_path, runtime):
    monkeypatch.setenv("AGENT_RUNTIME", runtime)
    monkeypatch.setattr(plugins, "STATE_FILE", tmp_path / "state.json", raising=False)
    _no_subprocess(monkeypatch, plugins)

    result = plugins.reinstall(manifest={"marketplaces": {"m": "github:o/r"},
                                         "installed": ["p@m"]})

    assert result["status"] == "skipped_runtime"
    assert result["installed"] == []
    assert result["added_marketplaces"] == []
    assert result["withheld"] == {}


def test_reinstall_skip_is_a_named_status_not_a_failure(plugins, monkeypatch, tmp_path):
    """'off because this runtime has no Claude plugins' and 'it broke' must
    not look alike — the #1929 withhold-with-a-reason contract."""
    monkeypatch.setenv("AGENT_RUNTIME", "opencode")
    monkeypatch.setattr(plugins, "STATE_FILE", tmp_path / "state.json", raising=False)
    _no_subprocess(monkeypatch, plugins)

    result = plugins.reinstall(manifest={})
    assert result["status"] == "skipped_runtime"
    assert result["status"] not in ("no_manifest", "ok", "platform_defaults_only")


def test_reinstall_still_runs_on_claude(plugins, monkeypatch, tmp_path):
    """The gate must not disable the feature it guards."""
    monkeypatch.setenv("AGENT_RUNTIME", "claude-code")
    monkeypatch.setattr(plugins, "STATE_FILE", tmp_path / "state.json", raising=False)
    monkeypatch.setattr(plugins, "merge_platform_defaults", lambda m: m)
    seen = []

    def _fake_run(args, **kwargs):
        seen.append(list(args))
        import types
        return types.SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(plugins.subprocess, "run", _fake_run)
    result = plugins.reinstall(manifest={"marketplaces": {"m": "github:o/r"},
                                         "installed": []})
    assert result["status"] != "skipped_runtime"
    assert seen, "Claude runtime must still probe the CLI"


# --------------------------------------------------------------------------
# 3. the availability probe names THIS runtime's CLI
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "runtime,binary",
    [
        ("claude-code", "claude"),
        ("Claude", "claude"),
        ("gemini-cli", "gemini"),
        ("codex", "codex"),
        ("Codex", "codex"),       # #1187 casing trap
        ("opencode", "opencode"),
    ],
)
def test_runtime_probe_runs_the_runtimes_own_cli(monkeypatch, runtime, binary):
    from agent_server.state import AgentState

    probed = []
    monkeypatch.setattr(
        AgentState, "_check_cli", staticmethod(lambda b: probed.append(b) or True)
    )
    state = AgentState.__new__(AgentState)
    state.agent_runtime = runtime
    assert state._check_runtime_available() is True
    assert probed == [binary]


def test_unknown_runtime_is_unavailable_not_probed_as_claude(monkeypatch):
    from agent_server.state import AgentState

    probed = []
    monkeypatch.setattr(
        AgentState, "_check_cli", staticmethod(lambda b: probed.append(b) or True)
    )
    state = AgentState.__new__(AgentState)
    state.agent_runtime = "opencodex"
    assert state._check_runtime_available() is False
    assert probed == []


# --------------------------------------------------------------------------
# 4. the async-dispatch gate shares the casing-safe predicate
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "runtime,expected",
    [("claude-code", True), ("Claude-Code", True), ("claude", True),
     ("codex", False), ("opencode", False)],
)
def test_result_callback_gate_is_case_insensitive(monkeypatch, runtime, expected):
    from agent_server.services import result_callback

    monkeypatch.setattr(result_callback.agent_state, "agent_runtime", runtime)
    assert result_callback.is_claude_runtime() is expected


# --------------------------------------------------------------------------
# 5. review I4: runtime names are compared through the leaf's sets
# --------------------------------------------------------------------------

@pytest.mark.parametrize("rel", ["routers/chat.py", "state.py"])
def test_no_second_literal_for_a_runtime_name(rel):
    """Pin on top of the behaviour tests above (the probe table and the
    /api/model surfaces below): a `== "codex"` beside `normalize_runtime` is a
    second spelling a new alias can miss."""
    src = (_AGENT_SERVER / rel).read_text(encoding="utf-8")
    hits = re.findall(r"""[=!]=\s*["'](?:opencode|codex)["']""", src)
    assert not hits, f"{rel} compares a runtime name by literal: {hits}"


# --------------------------------------------------------------------------
# 6. review I9: the /api/model Claude and Gemini surfaces are unchanged
# --------------------------------------------------------------------------

# PRE-BRANCH TRUTH: copied by hand from `git show
# cd1548c1c2aa:docker/base-image/agent_server/routers/chat.py` (get_model /
# set_model before #2981 refactored them into `_model_surface`). Hard-coded on
# purpose — reading the live module would compare it with itself.
_PRE_GEMINI_MODELS = ["gemini-3-pro", "gemini-3-flash", "gemini-2.5-pro", "gemini-2.5-flash"]
_PRE_GEMINI_NOTE = "Gemini models. 3-pro is the most capable; 3-flash is the fast default."
_PRE_GEMINI_REJECT = (
    "Invalid Gemini model: {m}. Use: gemini-3-pro, gemini-3-flash, gemini-2.5-pro, gemini-2.5-flash."
)
_PRE_CLAUDE_MODELS = ["sonnet", "opus", "haiku", "fable"]
_PRE_CLAUDE_NOTE = (
    "Claude model aliases (Anthropic API): each resolves to the current generation of "
    "its family — today sonnet (Sonnet 5), opus (Opus 5.5), haiku (Haiku 4.5), fable "
    "(Fable 5.1). Add [1m] suffix for the 1M extended-context beta (e.g. sonnet[1m])."
)
_PRE_CLAUDE_REJECT = (
    "Invalid Claude model: {m}. Use aliases (sonnet, opus, haiku, fable) or full model names."
)
_PRE = {
    "gemini": (_PRE_GEMINI_MODELS, _PRE_GEMINI_NOTE, _PRE_GEMINI_REJECT,
               _PRE_GEMINI_MODELS + ["gemini-9-ultra"],
               ["sonnet", "gpt-5", "claude-sonnet-5", "Gemini-3-pro", ""]),
    "claude": (_PRE_CLAUDE_MODELS, _PRE_CLAUDE_NOTE, _PRE_CLAUDE_REJECT,
               ["sonnet", "opus", "haiku", "fable", "sonnet[1m]", "opus[1m]", "haiku[1m]",
                "fable[1m]", "claude-sonnet-5", "claude-x"],
               ["gemini-3-pro", "Sonnet", "sonnet[2m]", "gpt-5", "anthropic/claude", ""]),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime,family",
    [("claude-code", "claude"), ("claude", "claude"), ("gemini-cli", "gemini"), ("gemini", "gemini")],
)
async def test_model_surface_matches_the_pre_branch_literals(monkeypatch, runtime, family):
    from fastapi import HTTPException

    import agent_server.routers.chat as chat_mod
    from agent_server.models import ModelRequest

    models, note, reject, accepted, rejected = _PRE[family]
    monkeypatch.setattr(chat_mod.agent_state, "agent_runtime", runtime)
    monkeypatch.setattr(chat_mod.agent_state, "current_model", None)

    got = await chat_mod.get_model()
    assert got["runtime"] == runtime
    assert got["available_models"] == models
    assert got["note"] == note

    for model in accepted:
        result = await chat_mod.set_model(ModelRequest(model=model))
        assert result["status"] == "success" and result["model"] == model, model
    for model in rejected:
        with pytest.raises(HTTPException) as exc:
            await chat_mod.set_model(ModelRequest(model=model))
        assert exc.value.status_code == 400
        assert exc.value.detail == reject.format(m=model), model
