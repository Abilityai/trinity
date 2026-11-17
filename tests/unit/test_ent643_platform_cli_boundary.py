"""The `### Stay inside Trinity's execution ledger` section (trinity-enterprise#643).

An agent that runs a coding-agent CLI (`claude`, `codex`, `gemini`) as a
subprocess steps outside the execution ledger: no cost attribution, no autonomy
gate, no permission enforcement, no audit trail. The platform prompt now says
so at every tier and on every runtime, gives the reason, and names an
alternative the agent can actually use.

What the suite pins:

* the section reaches every render (both tiers, every runtime spelling) and
  every production delivery helper, exactly once;
* "turn-scoped by design" is stated as authorization, never as a claim about
  where credentials are stored or whether a nested CLI can authenticate (a
  claim an agent can disprove with one command invites the exception hunt);
* each runtime is offered only what it has: Claude Code the `Agent` subagent
  tool (also named `Task`), every other runtime its own tools;
* every tool and argument the section names exists where it claims to, read
  from its owner without importing it.

Every helper that reads the section requires exactly one match before it
checks anything, so no check here can pass on a prompt that lacks it.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services import platform_prompt_service, prompt_tier
from services.platform_prompt_service import (
    _ALWAYS_SECTIONS,
    _KNOWN_SECTION_HEADINGS,
    _MINIMAL_DROP_SECTIONS,
    _iter_sections,
    compose_system_prompt,
    get_platform_system_prompt,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "src/backend"
CHAT_TS = REPO_ROOT / "src/mcp-server/src/tools/chat.ts"
HELPERS_PY = BACKEND / "services/agent_service/helpers.py"
SKILL_GATE_PY = REPO_ROOT / "docker/base-image/hooks/_skill_gate.py"
RUNTIME_CONFIG_PY = REPO_ROOT / "docker/base-image/agent_server/services/_runtime_config.py"

HEADING = "Stay inside Trinity's execution ledger"
# Ships on every turn of every agent: heading included.
MAX_SECTION_CHARS = 1000

CLAUDE_RUNTIMES = ("claude-code", "claude", "", None, "CLAUDE-CODE")
OTHER_RUNTIMES = ("codex", "CODEX", "gemini", "gemini-cli", "some-future-runtime")
ALL_RUNTIMES = CLAUDE_RUNTIMES + OTHER_RUNTIMES
CODEX_RUNTIMES = ("codex", "CODEX")

# A model id only this suite uses, so MINIMAL is reached through the real
# resolver rather than by calling the renderer with a tier.
MINIMAL_SENTINEL = "ent643-sentinel-minimal"
TIERS = ("verbose", "minimal")

EXECUTABLES = ("claude", "codex", "gemini")
LAUNCHERS = ("Agent", "Task")
MCP_TOOLS = ("chat_with_agent", "fan_out")
ARGUMENT_FORMS = {"parallel=true": "parallel", "async=true": "async", "model": "model"}


@pytest.fixture(autouse=True)
def _no_custom_prompt(monkeypatch):
    monkeypatch.setattr(platform_prompt_service.db, "get_setting_value", lambda *a, **k: None)


@pytest.fixture
def minimal_enabled(monkeypatch):
    monkeypatch.setattr(prompt_tier, "_MINIMAL_PREFIXES", (MINIMAL_SENTINEL,))


def _model_for(tier: str):
    return MINIMAL_SENTINEL + "-1" if tier == "minimal" else None


def _prompt(runtime, tier: str = "verbose") -> str:
    return get_platform_system_prompt(runtime=runtime, model=_model_for(tier))


def _section(prompt: str) -> str:
    """The rendered section, `### ` heading included. Exactly one, or fail."""
    found = [chunk for heading, chunk in _iter_sections(prompt) if heading == HEADING]
    assert len(found) == 1, (
        f"expected exactly one '### {HEADING}' section, found {len(found)}"
    )
    return "### " + found[0]


def _is_claude(runtime) -> bool:
    return runtime in CLAUDE_RUNTIMES


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.;])\s+", " ".join(text.split())) if s]


def _backticked(text: str) -> list[str]:
    return re.findall(r"`([^`]+)`", text)


def _bare(token: str) -> str:
    return token.removeprefix("mcp__trinity__")


# ---------------------------------------------------------------------------
# Owner reads, without importing the owner
# ---------------------------------------------------------------------------

def _literal(node: ast.AST):
    """A literal, or `frozenset(<literal>)` / `tuple(<literal>)`."""
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in ("frozenset", "tuple", "set") and len(node.args) == 1
            and not node.keywords):
        return {"frozenset": frozenset, "tuple": tuple, "set": set}[node.func.id](
            ast.literal_eval(node.args[0])
        )
    return ast.literal_eval(node)


def _module_constant(path: Path, name: str):
    assert path.is_file(), f"owner of {name} moved: {path}"
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                return _literal(node.value)
    pytest.fail(f"{name} is no longer a module-level constant in {path}")


_TOOL_NAME = re.compile(r'(?<![\w.])name:\s*"([a-z][a-z0-9_]*)"')


def _chat_ts_blocks() -> dict[str, str]:
    assert CHAT_TS.is_file(), f"MCP chat tools moved: {CHAT_TS}"
    src = CHAT_TS.read_text(encoding="utf-8")
    starts = list(_TOOL_NAME.finditer(src))
    blocks = {}
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(src)
        blocks.setdefault(m.group(1), src[m.start():end])
    return blocks


def _declares(block: str, param: str) -> bool:
    return re.search(rf"(?<![\w.]){param}:\s*z\b", block) is not None


# ---------------------------------------------------------------------------
# 1. Composition matrix: every tier, every runtime, every composition shape
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("minimal_enabled")
class TestCompositionMatrix:
    @pytest.mark.parametrize("tier", TIERS)
    @pytest.mark.parametrize("runtime", ALL_RUNTIMES)
    def test_platform_prompt_carries_it_once(self, runtime, tier):
        prompt = _prompt(runtime, tier)
        _section(prompt)
        # The MINIMAL render is real, not assumed: a droppable section is gone.
        assert ("### Agent Collaboration" in prompt) is (tier == "verbose")

    @pytest.mark.parametrize("tier", TIERS)
    @pytest.mark.parametrize("runtime", ALL_RUNTIMES)
    def test_composer_carries_it_with_context_and_caller_prompt(self, runtime, tier):
        ctx = platform_prompt_service.ExecutionContext(
            agent_name="agent-ent643", triggered_by="schedule", model=_model_for(tier),
            collaborators=[], platform_url="", ended_asks=[], pending_asks=[],
            pending_asks_total=0, primary_user_display="", role_id="", stakeholders=[],
        )
        prompt = compose_system_prompt(
            execution_context=ctx, caller_prompt="CALLER-SUFFIX-643", runtime=runtime,
        )
        section = _section(prompt)
        assert prompt.index(section) < prompt.index("CALLER-SUFFIX-643")

    @pytest.mark.parametrize("runtime", ALL_RUNTIMES)
    def test_composer_carries_it_with_execution_context_disabled(self, runtime):
        prompt = compose_system_prompt(
            platform_prompt_service.ExecutionContext(agent_name="agent-ent643"),
            include_execution_context=False, runtime=runtime,
        )
        _section(prompt)
        assert "## Execution Context" not in prompt

    @pytest.mark.parametrize("runtime", ("claude-code", "codex", "gemini"))
    def test_custom_instructions_do_not_displace_it(self, runtime, monkeypatch):
        monkeypatch.setattr(
            platform_prompt_service.db, "get_setting_value",
            lambda key, default=None: "OPERATOR-CUSTOM-643" if key == "trinity_prompt" else default,
        )
        prompt = get_platform_system_prompt(runtime=runtime)
        section = _section(prompt)
        assert prompt.index(section) < prompt.index("## Custom Instructions")

    def test_heading_is_registered_always_class(self):
        _section(_prompt("claude-code"))
        assert HEADING in _KNOWN_SECTION_HEADINGS
        assert HEADING in _ALWAYS_SECTIONS
        assert HEADING not in _MINIMAL_DROP_SECTIONS


# ---------------------------------------------------------------------------
# 2. The boundary and its reason
# ---------------------------------------------------------------------------

class TestBoundaryAndReason:
    @pytest.mark.parametrize("runtime", ALL_RUNTIMES)
    def test_names_the_executables_and_the_class(self, runtime):
        section = _section(_prompt(runtime))
        for exe in EXECUTABLES:
            assert f"`{exe}`" in section, f"`{exe}` not named for runtime={runtime!r}"
        assert "coding-agent CLI" in section

    @pytest.mark.parametrize("runtime", ALL_RUNTIMES)
    def test_prohibits_direct_script_and_wrapper_subprocesses(self, runtime):
        prohibition = next(
            (s for s in _sentences(_section(_prompt(runtime))) if s.startswith("Do not run")), None
        )
        assert prohibition, "no 'Do not run ...' sentence"
        for clause in ("subprocess", "directly", "script", "wrapper"):
            assert clause in prohibition, f"the prohibition does not cover {clause!r}"

    @pytest.mark.parametrize("runtime", ALL_RUNTIMES)
    def test_states_the_ledger_reason(self, runtime):
        section = _section(_prompt(runtime))
        for reason in ("execution ledger", "cost attribution", "autonomy gates",
                       "permission enforcement", "audit trail"):
            assert reason in section, f"reason {reason!r} missing"


# ---------------------------------------------------------------------------
# 3. Credential honesty: authorization, never storage or certain failure
# ---------------------------------------------------------------------------

# Claims an agent could disprove from inside its own container, on at least one
# runtime or auth mode: credentials are readable from the environment, Codex
# keeps an auth file, and a `.env` key can make a nested CLI authenticate.
_FALSE_CLAIMS = (
    r"never (?:written|stored|saved|kept) (?:to|on) disk",
    r"not (?:written|stored|saved|kept) (?:to|on) disk",
    r"\bnot available\b",
    r"\bunavailable\b",
    r"\binaccessible\b",
    r"\bcannot (?:read|see|access|authenticate|log ?in)\b",
    r"\bcan't (?:read|see|access|authenticate|log ?in)\b",
    r"\bwill (?:always )?fail\b",
    r"\balways fails?\b",
    r"\bnot logged in\b",
    r"\bno credentials\b",
    r"\bdo(?:es)? not have (?:any )?credentials\b",
    r"\b(?:are|is) not (?:in|on) (?:your|the) (?:environment|container|disk)\b",
)


class TestCredentialHonesty:
    @pytest.mark.parametrize("runtime", ALL_RUNTIMES)
    def test_turn_scope_is_explained_as_authorization(self, runtime):
        section = _section(_prompt(runtime))
        assert "turn-scoped by design" in section
        scope = next(s for s in _sentences(section) if "turn-scoped by design" in s)
        assert "authoriz" in scope, "turn scope is not explained as authorization"
        assert "Trinity runs" in scope

    @pytest.mark.parametrize("runtime", ALL_RUNTIMES)
    def test_visible_credentials_and_working_auth_do_not_permit_it(self, runtime):
        prohibition = next(
            s for s in _sentences(_section(_prompt(runtime))) if s.startswith("Do not run")
        )
        assert "visible" in prohibition
        assert "authentication succeeds" in prohibition

    @pytest.mark.parametrize("runtime", ALL_RUNTIMES)
    def test_makes_no_storage_or_failure_claim(self, runtime):
        section = _section(_prompt(runtime)).lower()
        hits = [p for p in _FALSE_CLAIMS if re.search(p, section)]
        assert not hits, f"section claims what an agent can disprove: {hits}"


# ---------------------------------------------------------------------------
# 4. Runtime honesty: never offer a launcher the runtime does not have
# ---------------------------------------------------------------------------

class TestRuntimeHonesty:
    @pytest.mark.parametrize("runtime", CLAUDE_RUNTIMES)
    def test_claude_is_offered_the_subagent_tool_conditionally(self, runtime):
        section = _section(_prompt(runtime))
        sentence = next(s for s in _sentences(section) if "`Agent`" in s)
        assert "`Task`" in sentence, "the `Task` alias is not named"
        assert "where you have it" in sentence, "launcher availability is not qualified"
        assert "tools directly" in sentence, "direct tools are not offered as an alternative"

    @pytest.mark.parametrize("runtime", OTHER_RUNTIMES)
    def test_other_runtimes_are_offered_their_own_tools_only(self, runtime):
        section = _section(_prompt(runtime))
        assert "For work inside this turn, use your available tools directly" in section
        assert "delegate" in section
        for launcher in LAUNCHERS:
            assert f"`{launcher}`" not in section, (
                f"runtime={runtime!r} is offered `{launcher}`, a Claude Code tool it does not have"
            )


# ---------------------------------------------------------------------------
# 5. Actionable delegation
# ---------------------------------------------------------------------------

class TestActionableDelegation:
    @pytest.mark.parametrize("runtime", ALL_RUNTIMES)
    def test_self_target_and_model_go_through_parallel_mode(self, runtime):
        section = _section(_prompt(runtime))
        chat = next((s for s in _sentences(section) if "chat_with_agent`" in s), None)
        assert chat, "chat_with_agent is not named"
        assert "parallel=true" in chat
        assert "yourself" in chat, "self-targeting is not taught"
        assert "`model`" in chat
        assert "async=true" in chat

    @pytest.mark.parametrize("runtime", ALL_RUNTIMES)
    def test_fan_out_carries_model_and_self_target(self, runtime):
        section = _section(_prompt(runtime))
        fan = next((s for s in _sentences(section) if "fan_out`" in s), None)
        assert fan, "fan_out is not named"
        assert "`model`" in fan
        assert re.search(r"\byou(?:rself)?\b", fan.split("fan_out`", 1)[1]), (
            "fan_out self-targeting is not taught"
        )

    @pytest.mark.parametrize("runtime", ALL_RUNTIMES)
    def test_agrees_with_the_delegation_contract(self, runtime):
        section = _section(_prompt(runtime))
        assert "receipt" in section
        assert "never re-send on silence" in section


# ---------------------------------------------------------------------------
# 6. Every tool, argument and runtime class the section names has an owner
# ---------------------------------------------------------------------------

class TestOwners:
    @pytest.mark.parametrize("runtime", ("claude-code", "codex", "gemini"))
    def test_every_backticked_reference_is_classified_and_exists(self, runtime):
        section = _section(_prompt(runtime))
        blocks = _chat_ts_blocks()
        unclassified = []
        for token in _backticked(section):
            name = _bare(token)
            if name in EXECUTABLES or name in LAUNCHERS:
                continue
            if name in MCP_TOOLS:
                assert name in blocks, f"`{name}` is taught but chat.ts defines no such tool"
                continue
            if name in ARGUMENT_FORMS:
                continue
            unclassified.append(token)
        assert not unclassified, f"classify these so their checks run: {unclassified}"

    def test_arguments_exist_on_the_tools_that_take_them(self):
        section = _section(_prompt("claude-code"))
        blocks = _chat_ts_blocks()
        chat = next(s for s in _sentences(section) if "chat_with_agent`" in s)
        fan = next(s for s in _sentences(section) if "fan_out`" in s)
        for form, param in ARGUMENT_FORMS.items():
            if form in chat:
                assert _declares(blocks["chat_with_agent"], param), (
                    f"`{form}`: chat_with_agent declares no `{param}`"
                )
        assert _declares(blocks["fan_out"], "model"), "fan_out declares no `model`"
        assert "`model`" in fan
        # Self-targeting is allowed on both (SELF-EXEC-001; fan_out "typically yourself").
        assert "Self: Always allowed" in CHAT_TS.read_text(encoding="utf-8")
        assert "yourself" in blocks["fan_out"]

    def test_launcher_names_are_the_runtime_s_kept_tools(self):
        _section(_prompt("claude-code"))
        agent_tools = _module_constant(SKILL_GATE_PY, "_AGENT_TOOLS")
        kept = _module_constant(RUNTIME_CONFIG_PY, "PLATFORM_KEPT_TOOLS")
        denied = _module_constant(RUNTIME_CONFIG_PY, "PLATFORM_DENIED_TOOLS")
        assert set(LAUNCHERS) <= set(agent_tools), "`Agent`/`Task` are no longer the subagent launcher"
        assert "Task" in kept, "the subagent launcher is no longer kept"
        assert not set(LAUNCHERS) & set(denied), "the subagent launcher is denied"

    def test_subagent_runtimes_match_the_backend_claude_runtimes(self):
        _section(_prompt("claude-code"))
        owner = _module_constant(HELPERS_PY, "CLAUDE_RUNTIME_NAMES")
        here = getattr(platform_prompt_service, "_CLAUDE_SUBAGENT_RUNTIMES", None)
        assert here == frozenset(owner) | {""}, (
            f"_CLAUDE_SUBAGENT_RUNTIMES={here!r} drifted from CLAUDE_RUNTIME_NAMES={owner!r} + default"
        )


# ---------------------------------------------------------------------------
# 7. Budget and substitution
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("minimal_enabled")
class TestBudgetAndSubstitution:
    @pytest.mark.parametrize("tier", TIERS)
    @pytest.mark.parametrize("runtime", ALL_RUNTIMES)
    def test_within_budget_and_fully_substituted(self, runtime, tier):
        section = _section(_prompt(runtime, tier))
        assert len(section) <= MAX_SECTION_CHARS, (
            f"{len(section)} chars for runtime={runtime!r}, cap {MAX_SECTION_CHARS}"
        )
        assert not re.findall(r"__[A-Z][A-Z_]+__", section), "an unfilled marker shipped"

    @pytest.mark.parametrize("runtime", CODEX_RUNTIMES)
    def test_codex_names_the_tools_bare(self, runtime):
        section = _section(_prompt(runtime))
        assert "mcp__trinity__" not in section
        assert "`chat_with_agent`" in section and "`fan_out`" in section

    @pytest.mark.parametrize("runtime", ("claude-code", "gemini"))
    def test_other_runtimes_keep_the_canonical_names(self, runtime):
        section = _section(_prompt(runtime))
        assert "`mcp__trinity__chat_with_agent`" in section
        assert "`mcp__trinity__fan_out`" in section

    def test_rendering_one_runtime_never_changes_another(self):
        before = platform_prompt_service.PLATFORM_INSTRUCTIONS
        claude_first = _section(_prompt("claude-code"))
        for runtime in OTHER_RUNTIMES:
            _section(_prompt(runtime))
        assert platform_prompt_service.PLATFORM_INSTRUCTIONS == before
        assert _section(_prompt("claude-code")) == claude_first
        assert "`Agent`" in claude_first


# ---------------------------------------------------------------------------
# 8. Production delivery helpers, with the real composer
# ---------------------------------------------------------------------------

def _fake_prompt_db():
    fake = MagicMock()
    fake.get_setting_value = MagicMock(side_effect=lambda key, default=None: default)
    fake.get_permitted_agents = MagicMock(return_value=[])
    fake.list_recent_operator_queue_endings = MagicMock(return_value=[])
    fake.list_pending_operator_queue_asks = MagicMock(return_value=[])
    fake.count_operator_queue_pending_for_agent = MagicMock(return_value=0)
    return fake


def _route_prompt_db(monkeypatch, helper):
    """Point the composer the helper ACTUALLY calls at a fake db.

    Resolved through the helper's own globals, so a module re-imported by an
    earlier test (a different module object under the same name) cannot leave
    this patch on a copy the helper never reads."""
    composer = helper.__globals__["compose_system_prompt"]
    monkeypatch.setitem(composer.__globals__, "db", _fake_prompt_db())


def _boom(*a, **k):
    raise RuntimeError("context build failed (ent#643 test)")


class TestDeliveryHelpers:
    @pytest.fixture
    def chat(self, monkeypatch):
        import services.chat_execution_service as ce
        import services.docker_service as ds

        _route_prompt_db(monkeypatch, ce.build_chat_payload)
        monkeypatch.setitem(ce.build_chat_payload.__globals__, "is_execution_context_enabled",
                            lambda: True)

        def build(runtime):
            monkeypatch.setattr(ds, "get_agent_runtime", lambda name: runtime)
            from models import ChatMessageRequest
            return ce.build_chat_payload(
                name="agent-ent643", request=ChatMessageRequest(message="hello"),
                triggered_by="chat",
                current_user=SimpleNamespace(email="user@example.com", username="user",
                                             mcp_key_name=None),
                x_source_agent=None, task_execution_id=None,
            )["system_prompt"]
        return SimpleNamespace(module=ce, build=build)

    @pytest.fixture
    def task(self, monkeypatch):
        import services.task_execution_service as tes

        method = tes.TaskExecutionService._compose_effective_system_prompt
        _route_prompt_db(monkeypatch, method)
        monkeypatch.setitem(method.__globals__, "is_execution_context_enabled", lambda: True)

        def build(runtime, caller=None):
            monkeypatch.setitem(method.__globals__, "_resolve_agent_runtime", lambda name: runtime)
            svc = tes.TaskExecutionService.__new__(tes.TaskExecutionService)
            return svc._compose_effective_system_prompt(
                agent_name="agent-ent643", triggered_by="schedule", source_user_email=None,
                source_agent_name=None, source_mcp_key_name=None, model=None,
                timeout_seconds=None, attempt=None, schedule_context=None, execution_id=None,
                system_prompt=caller,
            )
        return SimpleNamespace(globals=method.__globals__, build=build)

    @pytest.fixture
    def pull(self, monkeypatch):
        import services.pull_coordination_service as pcs

        fn = pcs._compose_pull_system_prompt
        _route_prompt_db(monkeypatch, fn)
        monkeypatch.setitem(fn.__globals__, "is_execution_context_enabled", lambda: True)

        def build(runtime, caller=None):
            monkeypatch.setitem(fn.__globals__, "_resolve_agent_runtime", lambda name: runtime)
            return fn("agent-ent643", "schedule", caller, execution_id=None)
        return build

    @staticmethod
    def _assert_runtime_alternative(prompt, runtime):
        section = _section(prompt)
        assert ("`Agent`" in section) is _is_claude(runtime)

    @pytest.mark.parametrize("runtime", ("claude-code", "codex", "gemini"))
    def test_chat_payload_carries_it(self, chat, runtime):
        self._assert_runtime_alternative(chat.build(runtime), runtime)

    @pytest.mark.parametrize("runtime", ("claude-code", "codex"))
    def test_chat_context_error_fallback_carries_it(self, chat, monkeypatch, runtime):
        monkeypatch.setitem(chat.module.build_chat_payload.__globals__, "compose_system_prompt", _boom)
        prompt = chat.build(runtime)
        assert "## Execution Context" not in prompt  # the fallback really ran
        self._assert_runtime_alternative(prompt, runtime)

    @pytest.mark.parametrize("runtime", ("claude-code", "codex", "gemini"))
    def test_push_task_prompt_carries_it(self, task, runtime):
        prompt = task.build(runtime, caller="CALLER-643")
        self._assert_runtime_alternative(prompt, runtime)
        assert prompt.rstrip().endswith("CALLER-643")

    @pytest.mark.parametrize("runtime", ("claude-code", "codex"))
    def test_push_task_context_error_fallback_carries_it(self, task, monkeypatch, runtime):
        monkeypatch.setitem(task.globals, "compose_system_prompt", _boom)
        prompt = task.build(runtime, caller="CALLER-643")
        assert "## Execution Context" not in prompt  # the fallback really ran
        self._assert_runtime_alternative(prompt, runtime)
        assert prompt.endswith("CALLER-643")

    @pytest.mark.parametrize("runtime", ("claude-code", "codex", "gemini"))
    def test_pull_claim_prompt_carries_it(self, pull, runtime):
        prompt = pull(runtime, caller="CALLER-643")
        self._assert_runtime_alternative(prompt, runtime)
        assert prompt.rstrip().endswith("CALLER-643")
