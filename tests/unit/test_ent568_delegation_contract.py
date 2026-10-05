"""trinity-enterprise#568 — the delegation contract, taught where callers read.

A dispatch receipt (`queued_timeout`, `accepted`, `queued`, `fan_out_timeout`)
already exists on every route (#914, #2661, #2670). What was missing is the
caller knowing what it means: in the 2026-09-08 cascade every duplicate
dispatch was an agent re-sending after "could not confirm delivery". The
contract is ONE text, carried verbatim by two programs:

  * the platform prompt, §Agent Collaboration (``DELEGATION_CONTRACT`` spliced
    into ``PLATFORM_INSTRUCTIONS``) — every agent, every turn, no rebuild;
  * the MCP server (``src/mcp-server/src/delegation_contract.ts``) — the
    ``chat_with_agent`` / ``chat_with_<agent>`` descriptions, which are the only
    copy an external MCP client, or an agent at ``PromptTier.MINIMAL``, sees.

This suite pins the prompt half and the parity between the two copies. It
also pins that every name the text teaches is real, because a contract that
names a tool, an argument or a status nobody produces is the
"description promises a mechanism the environment lacks" class (#2454, #2468).
The MCP half — what ``tools/list`` actually publishes, under Claude Code's
2,048-char description cap — is ``src/mcp-server/src/delegation-contract.test.ts``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from services import platform_prompt_service
from services.event_dispatch_service import TASK_COMPLETED_EVENT, TASK_FAILED_EVENT
from services.platform_prompt_service import (
    DELEGATION_CONTRACT,
    PLATFORM_INSTRUCTIONS,
    _MINIMAL_DROP_SECTIONS,
    _iter_sections,
    get_platform_system_prompt,
    render_platform_instructions,
)
from services.prompt_tier import PromptTier

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_SRC = REPO_ROOT / "src" / "mcp-server" / "src"
TS_CONTRACT = MCP_SRC / "delegation_contract.ts"

RUNTIMES = ("claude-code", "codex", "gemini-cli", "")

# Claude Code shows the model only the first 2,048 characters of an MCP tool
# description, and the contract shares them with each tool's own lead (~310
# chars on `chat_with_agent`, one name-only line on a `chat_with_<agent>` tool).
# The MCP suite pins the published descriptions themselves; this keeps the
# shared text from eating that headroom. Raising it is a decision, not an
# accident — the #1535 / ent#536 context-budget discipline.
MAX_CONTRACT_CHARS = 1650

# Every backticked token in the contract, classified. A token outside these
# sets fails the build until someone decides what it is and adds it to the set
# whose check proves it real (a guard that knows only today's names stops
# guarding the day a new one is added).
TOOLS = {
    "fan_out",
    "get_execution_result",
    "get_fan_out_result",
    "list_recent_executions",
    "set_reminder",
    "subscribe_to_event",
}
# `chat_with_*` covers `chat_with_agent` and every dynamic `chat_with_<agent>`
# tool (#846); the glob must still match the real built-in.
TOOL_GLOBS = {"chat_with_*": "chat_with_agent"}
# status -> the files of a DISPATCH route that emit it as a literal. Pinned per
# status on purpose: `ask_operator` answers `"replayed"`, so a repo-wide scan
# would vouch for a status no dispatch route returns.
STATUS_PRODUCERS = {
    "accepted": ["src/backend/services/chat_execution_service.py"],
    "queued": ["src/backend/services/chat_execution_service.py"],
    "queued_timeout": ["src/mcp-server/src/client.ts"],
    "fan_out_timeout": ["src/mcp-server/src/client.ts"],
    "pending_approval": ["src/mcp-server/src/client.ts"],
    "agent_busy": ["src/mcp-server/src/tools/chat.ts"],
    "running": ["src/backend/models.py"],
}
# field -> a file whose answer carries it.
FIELD_PRODUCERS = {
    "execution_id": "src/mcp-server/src/client.ts",
    "fan_out_id": "src/mcp-server/src/client.ts",
    "retry_after_seconds": "src/mcp-server/src/tools/chat.ts",
    "retryable: false": "src/mcp-server/src/client.ts",
    "message": "src/mcp-server/src/client.ts",
}
EVENTS = {TASK_COMPLETED_EVENT, TASK_FAILED_EVENT}
# argument spellings -> (tool, the parameters its schema must declare)
ARGUMENT_FORMS = {"parallel=true, async=true": ("chat_with_agent", ("parallel", "async"))}

_LINES_DECL = re.compile(
    r"^export const DELEGATION_CONTRACT_LINES(?::[^=\n]+)? = \[\n(?P<body>.*?)\n\];$",
    re.M | re.S,
)
_RULE_DECL = re.compile(
    r'^export const DELEGATION_RULE(?::[^=\n]+)? =\s+(?P<lit>"(?:[^"\\\n]|\\.)*");$',
    re.M,
)
_TOOL_NAME = re.compile(r'(?<![\w.])name:\s*"([a-z][a-z0-9_]*)"')


@pytest.fixture(autouse=True)
def _no_custom_prompt(monkeypatch):
    """Pin the operator-configurable ``trinity_prompt`` to empty so these tests
    read only the authored constant, independent of DB state."""
    monkeypatch.setattr(platform_prompt_service.db, "get_setting_value", lambda *a, **k: None)


def _parse_contract_lines(src: str) -> list[str]:
    """``DELEGATION_CONTRACT_LINES`` read the way the TS module declares it.

    Strict on purpose: exactly one declaration, anchored at the start of a line
    (a doc comment naming the array cannot match), a plain array of
    JSON-compatible double-quoted strings. A comment, a template literal or a
    concatenation inside the array makes this raise rather than parse as
    something else.
    """
    assert src.count("export const DELEGATION_CONTRACT_LINES") == 1, (
        "expected exactly one DELEGATION_CONTRACT_LINES declaration"
    )
    m = _LINES_DECL.search(src)
    assert m, "DELEGATION_CONTRACT_LINES is not a plain `= [\\n ... \\n];` array"
    body = re.sub(r",\s*\Z", "", m.group("body").strip())
    lines = json.loads(f"[{body}]")
    assert lines and all(isinstance(x, str) and x for x in lines), (
        "parsed an empty array, or one holding a non-string / empty line"
    )
    return lines


def _parse_rule(src: str) -> str:
    assert src.count("export const DELEGATION_RULE") == 1
    m = _RULE_DECL.search(src)
    assert m, "DELEGATION_RULE is not a single double-quoted string literal"
    return json.loads(m.group("lit"))


def _tool_blocks() -> dict[str, str]:
    """Tool name -> its definition block, from every non-test MCP module."""
    blocks: dict[str, str] = {}
    for path in sorted(MCP_SRC.rglob("*.ts")):
        if path.name.endswith(".test.ts"):
            continue
        src = path.read_text(encoding="utf-8")
        starts = list(_TOOL_NAME.finditer(src))
        for i, m in enumerate(starts):
            end = starts[i + 1].start() if i + 1 < len(starts) else len(src)
            blocks.setdefault(m.group(1), src[m.start():end])
    return blocks


def _backticked() -> list[str]:
    return re.findall(r"`([^`]+)`", DELEGATION_CONTRACT)


# ---------------------------------------------------------------------------
# The prompt carries it — every runtime, once, inside §Agent Collaboration
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("runtime", RUNTIMES)
def test_every_runtime_reads_the_contract_in_agent_collaboration(runtime):
    prompt = get_platform_system_prompt(runtime)
    sections = dict(_iter_sections(prompt))
    assert DELEGATION_CONTRACT in sections["Agent Collaboration"]
    assert prompt.count(DELEGATION_CONTRACT) == 1


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_no_splice_marker_reaches_any_prompt(runtime):
    prompt = get_platform_system_prompt(runtime)
    assert "__DELEGATION_CONTRACT__" not in prompt
    assert not re.search(r"__[A-Z][A-Z_]*__", prompt), "an unspliced __MARKER__ reached the prompt"


def test_the_contract_is_runtime_neutral():
    """Bare tool names only: the same words must work inside an MCP tool
    description (no prefix there) and survive the Codex prefix strip (#1187)
    without forking. No heading either — a `###` line would split
    §Agent Collaboration in two and orphan its tier mapping (ent#243)."""
    assert "mcp__trinity__" not in DELEGATION_CONTRACT
    assert not any(line.lstrip().startswith("#") for line in DELEGATION_CONTRACT.splitlines())


def test_at_minimal_the_tool_description_is_the_copy():
    """ent#243: §Agent Collaboration is tool guidance, so MINIMAL drops it and
    the `chat_with_agent` description carries the contract instead — which is
    why the two copies must be byte-identical (below)."""
    assert "Agent Collaboration" in _MINIMAL_DROP_SECTIONS
    assert DELEGATION_CONTRACT not in render_platform_instructions(PromptTier.MINIMAL)


# ---------------------------------------------------------------------------
# One text: the MCP copy is byte-identical
# ---------------------------------------------------------------------------

def test_the_mcp_copy_is_byte_identical():
    lines = _parse_contract_lines(TS_CONTRACT.read_text(encoding="utf-8"))
    assert "\n".join(lines) == DELEGATION_CONTRACT


def test_the_rule_sentence_is_the_contracts_own_words():
    """`fan_out` and `send_message` repeat DELEGATION_RULE; it must be a
    verbatim sentence of the contract, not a paraphrase that can drift."""
    rule = _parse_rule(TS_CONTRACT.read_text(encoding="utf-8"))
    assert rule.startswith("Never re-send")
    assert rule in DELEGATION_CONTRACT


def test_extraction_raises_on_a_comment_inside_the_array():
    src = TS_CONTRACT.read_text(encoding="utf-8")
    m = _LINES_DECL.search(src)
    assert m
    tampered = src[: m.start("body")] + "  // a reviewer's note\n" + src[m.start("body"):]
    with pytest.raises(json.JSONDecodeError):
        _parse_contract_lines(tampered)


def test_extraction_sees_a_one_character_change():
    src = TS_CONTRACT.read_text(encoding="utf-8")
    m = _LINES_DECL.search(src)
    assert m
    body = m.group("body")
    changed = body.replace("Never re-send", "Never resend", 1)
    assert changed != body
    tampered = src[: m.start("body")] + changed + src[m.end("body"):]
    assert "\n".join(_parse_contract_lines(tampered)) != DELEGATION_CONTRACT


# ---------------------------------------------------------------------------
# It says what the issue asks, and every name it teaches is real
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("term", [
    "`queued_timeout`",
    "`accepted`",
    "`pending_approval`",
    "`get_execution_result(agent_name, execution_id)`",
    "`set_reminder`",
    "`agent.task.completed`",
    "`parallel=true, async=true`",
    "`list_recent_executions(agent_name)`",
    "Never re-send because a call timed out or its delivery could not be confirmed.",
])
def test_the_contract_states_each_acceptance_term(term):
    assert term in DELEGATION_CONTRACT


def test_every_backticked_token_is_classified():
    known = (TOOLS | set(TOOL_GLOBS) | set(STATUS_PRODUCERS) | set(FIELD_PRODUCERS)
             | EVENTS | set(ARGUMENT_FORMS))
    unclassified = []
    for token in _backticked():
        call = re.fullmatch(r"([a-z_]+)\(([^)]*)\)", token)
        name = call.group(1) if call else token
        if name not in known:
            unclassified.append(token)
    assert unclassified == [], (
        f"the contract teaches {unclassified} — classify each in this file so its check runs"
    )


def test_every_tool_and_argument_the_contract_names_exists():
    blocks = _tool_blocks()
    assert len(blocks) >= 50, f"found only {len(blocks)} tool definitions — the scan is broken"
    for token in _backticked():
        call = re.fullmatch(r"([a-z_]+)\(([^)]*)\)", token)
        if call:
            tool, args = call.group(1), [a.strip() for a in call.group(2).split(",") if a.strip()]
        else:
            tool, args = token, []
        if tool in TOOL_GLOBS:
            tool = TOOL_GLOBS[tool]
        elif tool not in TOOLS:
            continue
        assert tool in blocks, f"`{tool}` is taught but no MCP tool has that name"
        for arg in args:
            assert re.search(rf"(?<![\w.]){arg}:\s*z\b", blocks[tool]), (
                f"`{token}`: `{tool}` declares no `{arg}` parameter"
            )
    for form, (tool, params) in ARGUMENT_FORMS.items():
        assert form in DELEGATION_CONTRACT
        for param in params:
            assert re.search(rf"(?<![\w.]){param}:\s*z\b", blocks[tool]), (
                f"`{form}`: `{tool}` declares no `{param}` parameter"
            )


def test_every_status_the_contract_names_is_emitted_by_a_dispatch_route():
    named = {t for t in _backticked() if t in STATUS_PRODUCERS}
    assert {"accepted", "queued", "queued_timeout", "pending_approval"} <= named
    for status in sorted(named):
        for rel in STATUS_PRODUCERS[status]:
            src = (REPO_ROOT / rel).read_text(encoding="utf-8")
            assert f'"{status}"' in src, f"`{status}` is taught but {rel} never emits it"


def test_every_field_the_contract_names_is_carried_by_an_answer():
    for field, rel in FIELD_PRODUCERS.items():
        if f"`{field}`" not in DELEGATION_CONTRACT:
            continue
        key = field.split(":")[0]
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert re.search(rf"\b{re.escape(key)}\b", src), f"`{field}` is taught but {rel} never carries it"


def test_the_events_it_names_are_the_ones_the_backend_emits():
    named = {t for t in _backticked() if t.startswith("agent.task.")}
    assert named == EVENTS


def test_codex_orientation_names_the_contract_tools():
    """#1535 / ent#536 precedent: a tool the prompt teaches is listed in the
    Codex orientation, which tells that runtime to call it by its bare name."""
    orientation = get_platform_system_prompt("codex").split("---", 1)[0]
    for tool in sorted(TOOLS | set(TOOL_GLOBS.values())):
        assert f"`{tool}`" in orientation, f"Codex orientation omits `{tool}`"


# ---------------------------------------------------------------------------
# Size (AC4 + the shared description budget)
# ---------------------------------------------------------------------------

def test_the_contract_stays_inside_its_budget():
    assert len(DELEGATION_CONTRACT) <= MAX_CONTRACT_CHARS, (
        f"the contract grew to {len(DELEGATION_CONTRACT)} chars (cap {MAX_CONTRACT_CHARS}); "
        "it ships in every agent's prompt and inside a 2,048-char tool description"
    )


def test_agent_collaboration_stays_shorter_than_operator_communication():
    """AC4: no new section longer than the operator-communication block."""
    sections = dict(_iter_sections(PLATFORM_INSTRUCTIONS))
    collab, operator = sections["Agent Collaboration"], sections["Operator Communication"]
    assert len(collab) < len(operator)
    assert len(collab.split()) < len(operator.split())
