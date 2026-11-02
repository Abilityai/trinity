"""Backend-side acceptance of the `opencode` runtime (Abilityai/lilu#257 step 1).

The agent-side half is `test_lilu257_opencode_runtime.py`. This file pins the
four places the BACKEND has to know about a fourth runtime, each of which fails
in a different and individually-silent way if missed:

  * `KNOWN_RUNTIME_NAMES` — a missing entry turns a valid `runtime: opencode`
    into a 400 at create time.
  * `validate_runtime_model` — a bare model id would otherwise surface as a
    failed turn INSIDE the container, long after creation reported success.
  * `is_claude_runtime` — a True here would assign a Claude OAuth subscription
    to an OpenCode agent (#1187 decision 7).
  * `RUNTIMES_WITHOUT_SESSION_TAB_RESUME` — OpenCode has no cached-UUID
    `--resume`, so the Session tab must not offer resume and silently start a
    fresh session while the UI claims continuity.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from services.agent_service.helpers import (
    KNOWN_RUNTIME_NAMES,
    is_claude_runtime,
    validate_runtime,
    validate_runtime_model,
)
from services.session_turn_service import RUNTIMES_WITHOUT_SESSION_TAB_RESUME

pytestmark = pytest.mark.unit


# --- runtime acceptance ----------------------------------------------------

def test_opencode_is_accepted():
    assert "opencode" in KNOWN_RUNTIME_NAMES
    validate_runtime("opencode")  # must not raise


def test_opencode_is_accepted_case_insensitively():
    validate_runtime("OpenCode")


def test_unknown_runtime_still_400s():
    with pytest.raises(HTTPException) as exc:
        validate_runtime("opencodex")
    assert exc.value.status_code == 400
    assert "opencodex" in str(exc.value.detail)


# --- subscription gating ---------------------------------------------------

def test_opencode_is_not_claude():
    """The load-bearing assertion: an OpenCode agent must NOT be assigned a
    Claude subscription or injected a Claude OAuth token."""
    assert is_claude_runtime("opencode") is False
    assert is_claude_runtime("OpenCode") is False


# --- Session-tab gate ------------------------------------------------------

def test_opencode_has_session_tab_resume():
    """Abilityai/lilu#655: `resume_session_id` reaches `--session <id>`, so the
    Session tab resumes an OpenCode conversation instead of a stateless turn."""
    assert "opencode" not in RUNTIMES_WITHOUT_SESSION_TAB_RESUME
    # ...and the existing members are unchanged.
    assert "codex" in RUNTIMES_WITHOUT_SESSION_TAB_RESUME
    assert "claude-code" not in RUNTIMES_WITHOUT_SESSION_TAB_RESUME


def test_session_tab_gate_matches_the_agent_side_capability():
    """The backend gate and the runtime's own `capabilities()` must agree —
    two sources for one fact is how they drift."""
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "docker" / "base-image"
    sys.path.insert(0, str(root))
    try:
        from agent_server.services.opencode_runtime import OpenCodeRuntime
        assert OpenCodeRuntime.capabilities().session_tab_resume is True
    finally:
        if str(root) in sys.path:
            sys.path.remove(str(root))


# --- provider/model shape at the create boundary ---------------------------

@pytest.mark.parametrize(
    "model",
    ["anthropic/claude-sonnet-5", "ollama/qwen3.6:27b", "openrouter/meta/llama-4"],
)
def test_valid_opencode_models_pass(model):
    validate_runtime_model("opencode", model)  # must not raise


@pytest.mark.parametrize("model", ["claude-sonnet-5", "qwen3.6:27b", "provider/", "/m"])
def test_bare_model_id_is_a_named_400(model):
    with pytest.raises(HTTPException) as exc:
        validate_runtime_model("opencode", model)
    assert exc.value.status_code == 400
    detail = str(exc.value.detail)
    assert "provider/model" in detail, "the 400 must name what a valid id looks like"
    assert model in detail, "the 400 must name the rejected value"


def test_unset_model_is_valid():
    validate_runtime_model("opencode", None)
    validate_runtime_model("opencode", "")


def test_other_runtimes_are_not_shape_constrained():
    """Only OpenCode takes `provider/model`. Applying the rule to Codex would
    reject every valid `gpt-*` id."""
    validate_runtime_model("codex", "gpt-5.6-sol")
    validate_runtime_model("claude-code", "sonnet")
    validate_runtime_model(None, "sonnet")


def test_shape_rule_is_case_insensitive_on_the_runtime():
    """crud.py lowercases before this runs, but a second caller might not —
    and a rule that silently stops applying is worse than no rule."""
    with pytest.raises(HTTPException):
        validate_runtime_model("OpenCode", "qwen3.6:27b")


def test_backend_and_agent_shape_rules_agree():
    """The backend cannot import the base image, so `validate_runtime_model`
    and `validate_provider_model` are a parity pair. Prove they agree rather
    than assuming it."""
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "docker" / "base-image"
    sys.path.insert(0, str(root))
    try:
        from agent_server.services.opencode_runtime import (
            OpenCodeModelShapeError,
            validate_provider_model,
        )
        cases = [
            "anthropic/claude-sonnet-5", "ollama/qwen3.6:27b", "a/b",
            "claude-sonnet-5", "qwen3.6:27b", "provider/", "/model", "-x/y", "",
        ]
        for model in cases:
            agent_ok = True
            try:
                validate_provider_model(model)
            except OpenCodeModelShapeError:
                agent_ok = False
            backend_ok = True
            try:
                validate_runtime_model("opencode", model)
            except HTTPException:
                backend_ok = False
            assert agent_ok == backend_ok, f"parity broken for {model!r}"
    finally:
        if str(root) in sys.path:
            sys.path.remove(str(root))
