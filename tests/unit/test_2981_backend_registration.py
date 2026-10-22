"""Backend-side acceptance of the `opencode` runtime (#2981, checkpoint G).

The agent-side half is `test_opencode_runtime.py` / `test_opencode_provider.py`.
This file pins the places the BACKEND has to know about a fourth runtime, each
of which fails in a different and individually-silent way if missed:

  * `KNOWN_RUNTIME_NAMES` — a missing entry turns a valid `runtime: opencode`
    into a 400 at create time.
  * `validate_runtime_model` — a bare model id would otherwise surface as a
    failed turn INSIDE the container, long after creation reported success.
  * `validate_runtime_provider` — a malformed `runtime.provider` block is a
    named 400 at create; a block under another runtime is a named 400 too (G1).
  * `render_runtime_provider_env` — THE serializer for `AGENT_RUNTIME_PROVIDER`,
    shared by creation and recovery (G3), and round-tripping through the
    harness's own `load_provider_block`.
  * `is_claude_runtime` — a True here would assign a Claude OAuth subscription
    to an OpenCode agent (#1187 decision 7).
  * `RUNTIMES_WITHOUT_SESSION_TAB_RESUME` — OpenCode has no cached-UUID
    `--resume`, so the Session tab must not offer resume.

The backend cannot import the base image at runtime, so the model regex and
the provider rules are parity pairs with the agent side; both are proven here
(the unit conftest wires the real `docker/base-image/agent_server` as
`agent_server`).
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import sys
import types
from pathlib import Path

import pytest
from fastapi import HTTPException

# Same stub-eviction idiom as test_validate_runtime.py (#1187): sibling unit
# modules replace `services.agent_service[.helpers]` with Mocks in sys.modules
# at collection time; evict only a STAND-IN so we bind the real module.
_STUBBED_MODULE_NAMES = [
    "services.agent_service.helpers",
    "services.agent_service.read_only",
    "services.agent_service.file_sharing",
    "services.agent_service",
]
_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
_REAL_AGENT_SERVICE_DIR = str(_BACKEND / "services" / "agent_service")


def _is_stand_in(module) -> bool:
    path = getattr(module, "__file__", None)
    return not (path and str(Path(path).resolve()).startswith(_REAL_AGENT_SERVICE_DIR))


for _stub in _STUBBED_MODULE_NAMES:  # import-time eviction (monkeypatch can't reach)
    if _stub in sys.modules and _is_stand_in(sys.modules[_stub]):
        sys.modules.pop(_stub, None)

from services.agent_service import helpers  # noqa: E402  (must follow the eviction)
from services.agent_service.helpers import (  # noqa: E402
    KNOWN_RUNTIME_NAMES,
    is_claude_runtime,
    render_runtime_provider_env,
    validate_runtime,
    validate_runtime_model,
    validate_runtime_provider,
)

from agent_server.services import opencode_provider, opencode_runtime  # noqa: E402
from agent_server.services.opencode_runtime import OpenCodeRuntime  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot/restore the stubbed subtree around each test (the sanctioned
    escape hatch the sys.modules lint recognizes; see test_validate_runtime)."""
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _load_turn_service() -> types.ModuleType:
    """Exec the session-turn leaf in isolation (test_session_tab_gate_codex)."""
    path = _BACKEND / "services" / "session_turn_service.py"
    spec = importlib.util.spec_from_file_location("session_turn_service_2981", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


VALID_BLOCK = {
    "id": "local-llm",
    "npm": "@ai-sdk/openai-compatible",
    "base_url": "http://host.docker.internal:11434/v1",
    "api_key_env": "LOCAL_LLM_API_KEY",
    "models": {
        "qwen3-coder:30b": {},
        "llama-4-scout": {"cost": {"input": 0.1, "output": 0.3}},
    },
}


def _block(**overrides):
    block = copy.deepcopy(VALID_BLOCK)
    for key, value in overrides.items():
        if value is _DROP:
            block.pop(key)
        else:
            block[key] = value
    return block


_DROP = object()


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


def test_backend_known_set_matches_agent_side():
    """The backend set mirrors the agent's `runtime_names.KNOWN_RUNTIMES`."""
    from agent_server.runtime_names import KNOWN_RUNTIMES

    assert set(KNOWN_RUNTIME_NAMES) == set(KNOWN_RUNTIMES)


# --- subscription gating ---------------------------------------------------

def test_opencode_is_not_claude():
    """The load-bearing assertion of the subscription gate: an OpenCode agent
    must NOT be assigned a Claude subscription or injected a Claude OAuth token
    (the test_codex_skips_subscription_assign analog)."""
    assert is_claude_runtime("opencode") is False
    assert is_claude_runtime("OpenCode") is False


def test_agent_side_agrees_opencode_takes_no_claude_credential():
    assert OpenCodeRuntime.supports_auth_override is False
    assert OpenCodeRuntime.capabilities().subscription_auth is False


# --- Session-tab gate ------------------------------------------------------

def test_opencode_has_no_session_tab_resume(monkeypatch):
    sessions = _load_turn_service()
    assert "opencode" in sessions.RUNTIMES_WITHOUT_SESSION_TAB_RESUME
    assert "codex" in sessions.RUNTIMES_WITHOUT_SESSION_TAB_RESUME
    assert "claude-code" not in sessions.RUNTIMES_WITHOUT_SESSION_TAB_RESUME
    monkeypatch.setattr(sessions, "get_agent_container", lambda name: object())
    monkeypatch.setattr(
        sessions, "get_agent_status_from_container",
        lambda c: types.SimpleNamespace(runtime="OpenCode"),
    )
    assert sessions.supports_session_resume("a") is False


def test_session_tab_gate_matches_the_agent_side_capability():
    """The backend gate and the runtime's own `capabilities()` must agree."""
    sessions = _load_turn_service()
    assert OpenCodeRuntime.capabilities().session_tab_resume is False
    assert ("opencode" in sessions.RUNTIMES_WITHOUT_SESSION_TAB_RESUME) is (
        not OpenCodeRuntime.capabilities().session_tab_resume
    )


# --- provider/model shape at the create boundary ---------------------------

@pytest.mark.parametrize(
    "model",
    ["anthropic/claude-sonnet-5", "openrouter/qwen/qwen3-coder", "local-llm/qwen3-coder:30b"],
)
def test_valid_opencode_models_pass(model):
    validate_runtime_model("opencode", model)  # must not raise


@pytest.mark.parametrize("model", ["claude-sonnet-5", "qwen3-coder:30b", "provider/", "/m"])
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
    """Only OpenCode takes `provider/model`; Codex `gpt-*` ids must pass."""
    validate_runtime_model("codex", "gpt-5.6-sol")
    validate_runtime_model("claude-code", "sonnet")
    validate_runtime_model(None, "sonnet")


def test_shape_rule_is_case_insensitive_on_the_runtime():
    with pytest.raises(HTTPException):
        validate_runtime_model("OpenCode", "qwen3-coder:30b")


def test_model_regex_is_character_identical_to_the_harness():
    assert helpers._OPENCODE_MODEL_RE.pattern == opencode_runtime._PROVIDER_MODEL_RE.pattern


@pytest.mark.parametrize("model", ["claude-sonnet-5", "qwen3-coder:30b", "/leading-slash", "provider/"])
def test_both_model_rejectors_refuse(model):
    """Sweep m4: the backend's create-time check and the harness's own refuse
    the same bad ids."""
    with pytest.raises(opencode_runtime.OpenCodeModelShapeError):
        opencode_runtime.validate_provider_model(model)
    with pytest.raises(HTTPException) as exc:
        validate_runtime_model("opencode", model)
    assert exc.value.status_code == 400


def test_version_runtimes_are_read_off_the_known_set():
    """Review I4: `/api/version`'s runtimes list is derived from
    KNOWN_RUNTIME_NAMES minus the short aliases, in the payload's historical
    order (the literal test_926 used to pin on the builder)."""
    names = helpers.version_runtime_names()
    assert names == ["claude-code", "gemini-cli", "codex", "opencode"]
    assert set(names) == KNOWN_RUNTIME_NAMES - {"claude", "gemini"}


def test_backend_and_agent_shape_rules_agree():
    cases = [
        "anthropic/claude-sonnet-5", "openrouter/qwen/qwen3-coder", "a/b",
        "claude-sonnet-5", "qwen3-coder:30b", "provider/", "/model", "-x/y", "",
        "a.b_c-d/e@f+g:h", "a b/c", "a/ b",
    ]
    for model in cases:
        agent_ok = True
        try:
            opencode_runtime.validate_provider_model(model)
        except opencode_runtime.OpenCodeModelShapeError:
            agent_ok = False
        backend_ok = True
        try:
            validate_runtime_model("opencode", model)
        except HTTPException:
            backend_ok = False
        assert agent_ok == backend_ok, f"parity broken for {model!r}"


# --- runtime.provider block at the create boundary -------------------------

def test_valid_provider_block_passes():
    validate_runtime_provider("opencode", copy.deepcopy(VALID_BLOCK))
    validate_runtime_provider("OpenCode", copy.deepcopy(VALID_BLOCK))


def test_absent_provider_block_is_valid_for_every_runtime():
    for runtime in (None, "", "claude-code", "codex", "gemini-cli", "opencode"):
        validate_runtime_provider(runtime, None)


@pytest.mark.parametrize("runtime", [None, "", "claude-code", "codex", "gemini-cli"])
def test_provider_block_under_another_runtime_is_a_named_400(runtime):
    """G1: never silently dropped."""
    with pytest.raises(HTTPException) as exc:
        validate_runtime_provider(runtime, copy.deepcopy(VALID_BLOCK))
    assert exc.value.status_code == 400
    assert exc.value.detail == "runtime.provider is only valid with runtime: opencode"


@pytest.mark.parametrize(
    "block, field",
    [
        ("not-an-object", "runtime.provider"),
        (["a"], "runtime.provider"),
        (_block(id=_DROP), "runtime.provider.id"),
        (_block(id="Local LLM"), "runtime.provider.id"),
        (_block(id=7), "runtime.provider.id"),
        (_block(npm=_DROP), "runtime.provider.npm"),
        (_block(npm="@openrouter/ai-sdk-provider"), "runtime.provider.npm"),
        (_block(npm="left-pad"), "runtime.provider.npm"),
        (_block(base_url=_DROP), "runtime.provider.base_url"),
        (_block(base_url="ftp://host/v1"), "runtime.provider.base_url"),
        (_block(base_url="host.docker.internal:11434/v1"), "runtime.provider.base_url"),
        (_block(base_url="http://"), "runtime.provider.base_url"),
        (_block(api_key_env=_DROP), "runtime.provider.api_key_env"),
        (_block(api_key_env="sk-live-abc"), "runtime.provider.api_key_env"),
        (_block(api_key_env="lower_case"), "runtime.provider.api_key_env"),
        (_block(models=_DROP), "runtime.provider.models"),
        (_block(models=["qwen"]), "runtime.provider.models"),
        (_block(models={"-bad id": {}}), "runtime.provider.models"),
        (_block(models={"qwen": "cheap"}), "runtime.provider.models.qwen"),
    ],
)
def test_malformed_provider_block_is_a_400_naming_the_field(block, field):
    with pytest.raises(HTTPException) as exc:
        validate_runtime_provider("opencode", block)
    assert exc.value.status_code == 400
    assert str(exc.value.detail).startswith(field), exc.value.detail


def test_provider_rules_mirror_the_harness():
    """Parity: every block the harness's `validate_provider_block` accepts the
    backend accepts, and every one it rejects the backend rejects."""
    cases = [
        copy.deepcopy(VALID_BLOCK),
        _block(models={}),
        _block(models={"m": None}),
        _block(base_url="https://api.example.com/v1"),
        _block(id="a_b-c9"),
        _block(id="A"),
        _block(npm="@ai-sdk/openai"),
        _block(base_url="file:///etc/passwd"),
        _block(api_key_env="_X1"),
        _block(api_key_env="1X"),
        _block(models={"m": []}),
        _block(models={"a/b:c@d+e": {}}),
        _block(models={".m": {}}),
        _block(id=_DROP),
        _block(models=_DROP),
    ]
    for block in cases:
        agent_ok = True
        try:
            opencode_provider.validate_provider_block(copy.deepcopy(block))
        except opencode_provider.OpenCodeProviderConfigError:
            agent_ok = False
        backend_ok = True
        try:
            validate_runtime_provider("opencode", copy.deepcopy(block))
        except HTTPException:
            backend_ok = False
        assert agent_ok == backend_ok, f"provider-rule parity broken for {block!r}"


def test_provider_constants_are_the_harness_constants():
    assert helpers._OPENCODE_PROVIDER_NPM_ALLOWLIST == opencode_provider.NPM_ALLOWLIST
    assert helpers._OPENCODE_PROVIDER_ID_RE.pattern == opencode_provider._ID_RE.pattern
    assert helpers._OPENCODE_PROVIDER_ENV_NAME_RE.pattern == opencode_provider._ENV_NAME_RE.pattern
    assert helpers._OPENCODE_PROVIDER_MODEL_ID_RE.pattern == opencode_provider._MODEL_ID_RE.pattern


# --- AGENT_RUNTIME_PROVIDER serialization ----------------------------------

def test_env_value_round_trips_through_the_harness_loader():
    value = render_runtime_provider_env(copy.deepcopy(VALID_BLOCK))
    loaded = opencode_provider.load_provider_block({opencode_provider.ENV_PROVIDER: value})
    assert loaded == VALID_BLOCK
    # ...and the harness validates what the backend accepted.
    assert opencode_provider.validate_provider_block(loaded)["id"] == "local-llm"


def test_env_value_is_compact_sorted_json():
    value = render_runtime_provider_env({"b": 1, "a": {"y": 2, "x": 1}})
    assert value == '{"a":{"x":1,"y":2},"b":1}'


def test_absent_block_renders_blank_and_reads_as_no_block():
    assert render_runtime_provider_env(None) == ""
    assert opencode_provider.load_provider_block({opencode_provider.ENV_PROVIDER: ""}) is None


def test_env_name_matches_the_harness():
    assert opencode_provider.ENV_PROVIDER == "AGENT_RUNTIME_PROVIDER"


def test_create_env_bakes_the_provider_block(monkeypatch):
    """Drives the real create-path env builder."""
    pytest.importorskip("docker", reason="backend venv required (crud imports docker)")
    from models import AgentConfig
    from services.agent_service import crud

    monkeypatch.setattr(crud, "get_anthropic_api_key", lambda: "")
    monkeypatch.setattr(crud.db, "get_guardrails_config", lambda name: None)
    config = AgentConfig(
        name="oc", runtime="opencode", runtime_model="local-llm/qwen3-coder:30b",
        runtime_provider=copy.deepcopy(VALID_BLOCK),
    )
    env = crud._build_base_env(config)
    assert env["AGENT_RUNTIME"] == "opencode"
    assert env["AGENT_RUNTIME_PROVIDER"] == render_runtime_provider_env(VALID_BLOCK)
    assert opencode_provider.load_provider_block(env) == VALID_BLOCK

    env_none = crud._build_base_env(AgentConfig(name="cc"))
    assert env_none["AGENT_RUNTIME_PROVIDER"] == ""


# --- wiring pins (source-level, on top of the behavioural tests above) ----

def _function(path: Path, name: str) -> ast.AST:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path.name}")


def _provider_env_value(func: ast.AST) -> ast.AST:
    for node in ast.walk(func):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "AGENT_RUNTIME_PROVIDER":
                    return v
    raise AssertionError("AGENT_RUNTIME_PROVIDER not set")


def test_create_and_recovery_use_the_same_serializer():
    """G3 / no-drift: both baked env sets call the ONE helper."""
    agent_service = _BACKEND / "services" / "agent_service"
    for path, fn in (
        (agent_service / "crud.py", "_build_base_env"),
        (agent_service / "lifecycle.py", "recreate_missing_container"),
    ):
        value = _provider_env_value(_function(path, fn))
        assert isinstance(value, ast.Call) and getattr(value.func, "id", None) == (
            "render_runtime_provider_env"
        ), f"{path.name}::{fn} must serialize with render_runtime_provider_env"


def test_recovery_reads_provider_from_template_without_revalidating():
    func = _function(_BACKEND / "services" / "agent_service" / "lifecycle.py",
                     "recreate_missing_container")
    src = ast.unparse(func)
    assert "runtime_cfg.get('provider')" in src
    assert "validate_runtime_provider" not in src


def test_create_validates_model_and_provider_after_normalization():
    func = _function(_BACKEND / "services" / "agent_service" / "crud.py",
                     "create_agent_internal")
    src = ast.unparse(func)
    lower = src.index("config.runtime = config.runtime.lower()")
    model = src.index("validate_runtime_model(config.runtime, config.runtime_model)")
    provider = src.index("validate_runtime_provider(config.runtime, config.runtime_provider)")
    assert lower < model < provider


def test_template_resolution_reads_the_provider_block():
    func = _function(_BACKEND / "services" / "agent_service" / "crud.py",
                     "_resolve_local_template")
    assert "runtime_config.get('provider', config.runtime_provider)" in ast.unparse(func)


@pytest.mark.parametrize("model", ["claude-sonnet-5", "qwen3-coder:30b"])
def test_missing_provider_prefix_is_named_as_such(model):
    """The two rejections have different fixes, so the bare-id case names the
    missing prefix rather than the generic shape rule."""
    with pytest.raises(HTTPException) as exc:
        validate_runtime_model("opencode", model)
    assert "missing a provider prefix" in str(exc.value.detail)
