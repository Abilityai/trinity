"""OpenCode controls parity (#2981, checkpoint D).

Read-only mode, the operator's guardrails ∪ platform denials, and the headless
``allowed_tools`` allowlist all land on OpenCode's ``permission:`` block in the
same per-turn ``opencode.json`` that carries the platform prompt. Every control
name with no OpenCode equivalent is surfaced in the turn's execution log — the
operator sees it in the ledger, not only in the container logger.

Verified against the pinned OpenCode v1.18.32 source (cited in
``opencode_runtime.py`` above ``PERMISSION_KEY_FOR_TOOL``): values are
``ask | allow | deny``; under ``--auto`` every ``ask`` is auto-approved, so
``deny`` is the only value that withholds a capability.

Precedence pinned here: read-only deny > guardrail / platform deny >
``allowed_tools`` > the binary default. Nothing turns a deny back into allow.
"""

from __future__ import annotations

import json
import logging

import pytest

# conftest wires the real docker/base-image/agent_server as `agent_server`.
from agent_server.models import ExecutionLogEntry, ExecutionMetadata  # noqa: E402
from agent_server.services import _runtime_config, opencode_runtime, result_callback  # noqa: E402
from agent_server.services.opencode_runtime import (  # noqa: E402
    CONTROL_ALLOWED_TOOLS,
    CONTROL_GUARDRAIL,
    CONTROL_PLATFORM,
    OpenCodeRuntime,
    PERMISSION_KEY_FOR_TOOL,
    READ_ONLY_PERMISSION,
    build_permission_block,
    merge_permission_block,
)

# Reused as-is from checkpoint A's suite: the fake subprocess, the stream
# builders, the custom-provider env and the autouse provider-key fixture
# (a fixture bound into this namespace is collected here too, which is what
# the execute-path tests below need). Loaded by path so the import does not
# depend on which directory pytest was started from.
import importlib.util as _ilu  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_spec = _ilu.spec_from_file_location(
    "_opencode_runtime_suite", _Path(__file__).with_name("test_opencode_runtime.py")
)
_suite = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_suite)
_install_fake_subprocess = _suite._install_fake_subprocess
_lines = _suite._lines
_provider_env = _suite._provider_env
_provider_keys_present = _suite._provider_keys_present
_pstep = _suite._pstep
_step = _suite._step


def _block(**kw):
    kw.setdefault("read_only", False)
    kw.setdefault("guardrails", {})
    kw.setdefault("allowed_tools", None)
    return build_permission_block(**kw)


def _no_platform(unmapped):
    return [(o, n) for o, n in unmapped if o != CONTROL_PLATFORM]


# ---------------------------------------------------------------------------
# the vocabulary table
# ---------------------------------------------------------------------------

def test_vocabulary_is_the_pinned_schema_subset():
    """The plan's set plus `websearch`, which the pinned schema has
    (`packages/core/src/v1/config/permission.ts:29-30`)."""
    assert PERMISSION_KEY_FOR_TOOL == {
        "Write": "edit", "Edit": "edit", "MultiEdit": "edit", "NotebookEdit": "edit",
        "Bash": "bash", "WebFetch": "webfetch", "WebSearch": "websearch",
    }
    assert set(PERMISSION_KEY_FOR_TOOL.values()) <= {
        "read", "edit", "glob", "grep", "list", "bash", "task", "external_directory",
        "todowrite", "question", "webfetch", "websearch", "lsp", "doom_loop", "skill",
    }


def test_block_values_are_in_the_pinned_vocabulary():
    block, _ = _block(read_only=True, guardrails={"disallowed_tools": ["Bash"]},
                      allowed_tools=["Read"])
    assert set(block.values()) <= {"ask", "allow", "deny"}


# ---------------------------------------------------------------------------
# read-only → the exact deny set
# ---------------------------------------------------------------------------

def test_read_only_renders_the_exact_set():
    """Read-only decides `edit` and `bash` only — `webfetch` is ABSENT from
    the block (R18: an explicit allow decided nothing under `--auto` and
    could only widen an operator deny)."""
    block, unmapped = _block(read_only=True)
    assert block == {"edit": "deny", "bash": "deny"}
    assert "webfetch" not in block
    assert block == READ_ONLY_PERMISSION
    assert "allow" not in block.values()
    assert _no_platform(unmapped) == []


def test_not_read_only_renders_nothing_by_itself():
    """Keys the controls do not decide stay at the binary default."""
    block, _ = _block(read_only=False)
    assert block == {}


# ---------------------------------------------------------------------------
# guardrails + platform denials land on their keys
# ---------------------------------------------------------------------------

# A literal, not `PERMISSION_KEY_FOR_TOOL.items()`: a row deleted from the
# table must fail HERE on behaviour, not vanish from the parametrization.
@pytest.mark.parametrize("name,key", [
    ("Write", "edit"), ("Edit", "edit"), ("MultiEdit", "edit"), ("NotebookEdit", "edit"),
    ("Bash", "bash"), ("WebFetch", "webfetch"), ("WebSearch", "websearch"),
])
def test_each_mapped_guardrail_name_lands_on_its_key(name, key):
    block, unmapped = _block(guardrails={"disallowed_tools": [name]})
    assert block == {key: "deny"}
    assert _no_platform(unmapped) == []


def test_platform_denials_go_through_the_single_builder(monkeypatch):
    """A platform-denied name with an OpenCode equivalent is denied the same
    way as an operator guardrail — via `merged_disallowed_tools`, never a
    second reader of the platform list."""
    monkeypatch.setattr(_runtime_config, "PLATFORM_DENIED_TOOLS", ("WebFetch", "Workflow"))
    block, unmapped = _block()
    assert block == {"webfetch": "deny"}
    assert unmapped == [(CONTROL_PLATFORM, "Workflow")]


def test_platform_denials_are_the_real_list_and_all_unmapped_today():
    """Pinned so a future addition to PLATFORM_DENIED_TOOLS that does map
    (e.g. `Bash`) is seen here, not discovered in a ledger."""
    _, unmapped = _block()
    assert [n for o, n in unmapped if o == CONTROL_PLATFORM] == list(
        _runtime_config.PLATFORM_DENIED_TOOLS
    )


def test_malformed_guardrails_degrade_to_platform_denials_only():
    block, unmapped = _block(guardrails={"disallowed_tools": "Bash"})
    assert block == {}
    assert all(o == CONTROL_PLATFORM for o, _ in unmapped)
    block, _ = _block(guardrails=None)
    assert block == {}


# ---------------------------------------------------------------------------
# allowed_tools: deny what the (non-empty) allowlist omits
# ---------------------------------------------------------------------------

def test_allowed_tools_denies_mapped_tools_outside_the_list():
    block, unmapped = _block(allowed_tools=["Read", "Bash"])
    assert block == {"edit": "deny", "webfetch": "deny", "websearch": "deny"}
    assert "bash" not in block, "a listed tool stays at the binary default"
    assert _no_platform(unmapped) == [(CONTROL_ALLOWED_TOOLS, "Read")]


@pytest.mark.parametrize("allowed", [None, []])
def test_empty_allowlist_restricts_nothing(allowed):
    """None/[] means "no restriction", exactly as the Claude runtime."""
    block, unmapped = _block(allowed_tools=allowed)
    assert block == {}
    assert _no_platform(unmapped) == []


def test_any_listed_edit_tool_keeps_the_shared_edit_key():
    """write/edit/patch collapse onto ONE `edit` key in OpenCode
    (config.ts:567-578), so listing any one of them keeps the key."""
    block, _ = _block(allowed_tools=["Edit"])
    assert "edit" not in block
    assert block == {"bash": "deny", "webfetch": "deny", "websearch": "deny"}


# ---------------------------------------------------------------------------
# precedence
# ---------------------------------------------------------------------------

def test_read_only_beats_an_allowlist_that_names_bash():
    block, _ = _block(read_only=True, allowed_tools=["Bash", "Edit"])
    assert block["bash"] == "deny"
    assert block["edit"] == "deny"


def test_guardrail_deny_survives_an_allowlist():
    block, _ = _block(guardrails={"disallowed_tools": ["Bash"]}, allowed_tools=["Bash"])
    assert block["bash"] == "deny"


def test_read_only_allow_never_overrides_a_guardrail_deny():
    """Nothing turns a deny back into allow. There is no read-only allow any
    more (R18); this pins that a guardrail deny on WebFetch coexists with
    read-only — the `webfetch` key carries the guardrail's deny, untouched."""
    block, _ = _block(read_only=True, guardrails={"disallowed_tools": ["WebFetch"]})
    assert block["webfetch"] == "deny"
    assert block["edit"] == "deny" and block["bash"] == "deny"


def test_read_only_allow_never_overrides_an_allowlist_deny():
    """Same for the allowlist: its deny on `webfetch` coexists with read-only
    (no read-only allow exists to override it, R18)."""
    block, _ = _block(read_only=True, allowed_tools=["Read"])
    assert block["webfetch"] == "deny"
    assert block["edit"] == "deny" and block["bash"] == "deny"


def test_merge_keeps_an_operator_deny_and_replaces_a_pattern_map():
    """Deny is sticky in both directions against the operator's own block."""
    merged = merge_permission_block(
        {"webfetch": "deny", "bash": {"git *": "allow"}, "glob": "allow"},
        {"webfetch": "allow", "bash": "deny"},
    )
    assert merged == {"webfetch": "deny", "bash": "deny", "glob": "allow"}


def test_merge_normalises_a_scalar_operator_block():
    assert merge_permission_block("allow", {"edit": "deny"}) == {"*": "allow", "edit": "deny"}
    assert merge_permission_block(None, {"edit": "deny"}) == {"edit": "deny"}


@pytest.mark.parametrize("existing", [
    pytest.param("deny", id="bare-deny-block"),
    pytest.param({"*": "deny"}, id="wildcard-deny"),
    pytest.param({"webfetch": {"*": "deny"}}, id="pattern-map-deny"),
])
def test_merge_keeps_a_wildcard_and_pattern_deny(existing):
    """The validator's finding (PR #3888 review, R18): deny was sticky only
    for a scalar `"deny"` on the key, so a computed `webfetch: allow` flattened
    an operator's bare `"deny"` block, `{"*": "deny"}` and
    `{"webfetch": {"*": "deny"}}` into `webfetch: allow`. Every deny shape the
    schema accepts must survive a computed non-deny; a computed deny still
    lands."""
    merged = merge_permission_block(existing, {"webfetch": "allow", "edit": "deny"})
    assert merged["edit"] == "deny"
    assert merged.get("webfetch") != "allow"
    if isinstance(existing, dict) and "webfetch" in existing:
        # (c) the operator's finer pattern map is kept, not flattened
        assert merged["webfetch"] == {"*": "deny"}
        assert merged == {"webfetch": {"*": "deny"}, "edit": "deny"}
    else:
        # (a) a bare string normalises to the wildcard; (b) the wildcard deny
        # covers an absent key, so no `webfetch` entry is written at all
        assert merged["*"] == "deny"
        assert "webfetch" not in merged
        assert merged == {"*": "deny", "edit": "deny"}


# ---------------------------------------------------------------------------
# unmapped names are surfaced, never dropped
# ---------------------------------------------------------------------------

def test_every_unmapped_name_is_returned_and_nothing_else_is_dropped():
    names = ["Task", "Glob", "Grep", "Read", "mcp__trinity__chat_with_agent", "Bash"]
    block, unmapped = _block(guardrails={"disallowed_tools": names},
                             allowed_tools=["Skill", "WebFetch"])
    assert block == {"bash": "deny", "edit": "deny", "websearch": "deny"}
    assert [n for o, n in unmapped if o == CONTROL_GUARDRAIL] == [
        "Task", "Glob", "Grep", "Read", "mcp__trinity__chat_with_agent",
    ]
    assert [n for o, n in unmapped if o == CONTROL_ALLOWED_TOOLS] == ["Skill"]


def test_control_log_entries_carry_the_ledger_text():
    entries = opencode_runtime._control_log_entries(
        [(CONTROL_GUARDRAIL, "Task"), (CONTROL_ALLOWED_TOOLS, "Glob")]
    )
    assert [e.output for e in entries] == [
        "guardrail not mapped: Task", "allowed_tools not mapped: Glob",
    ]
    assert all(isinstance(e, ExecutionLogEntry) for e in entries)
    assert all(e.tool == "trinity-control" for e in entries)
    assert len({e.id for e in entries}) == 2


# ---------------------------------------------------------------------------
# the seam: the rendered opencode.json carries the block
# ---------------------------------------------------------------------------

def _config_of(turn_dir):
    with open(f"{turn_dir}/opencode/opencode.json", encoding="utf-8") as fh:
        return json.load(fh)


def test_rendered_config_carries_the_block(monkeypatch, tmp_path):
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    turn_dir = opencode_runtime._prepare_turn_config(
        "exec-ro", "PROMPT", permission=READ_ONLY_PERMISSION
    )
    rendered = _config_of(turn_dir)["permission"]
    assert rendered == {"edit": "deny", "bash": "deny"}
    assert "webfetch" not in rendered


def test_rendered_config_has_no_block_when_nothing_was_decided(monkeypatch, tmp_path):
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    turn_dir = opencode_runtime._prepare_turn_config("exec-open", "PROMPT", permission={})
    assert "permission" not in _config_of(turn_dir)


def test_rendered_config_merges_over_the_operator_block(monkeypatch, tmp_path):
    shared = tmp_path / "shared" / "opencode"
    shared.mkdir(parents=True)
    (shared / "opencode.json").write_text(json.dumps({"permission": {"webfetch": "deny"}}))
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    monkeypatch.setattr(opencode_runtime, "_shared_config_dir", lambda: str(shared))
    turn_dir = opencode_runtime._prepare_turn_config(
        "exec-m", None, permission=READ_ONLY_PERMISSION
    )
    assert _config_of(turn_dir)["permission"] == {
        "webfetch": "deny", "edit": "deny", "bash": "deny",
    }


@pytest.mark.parametrize("operator_block,expected", [
    pytest.param(
        "deny",
        {"*": "deny", "edit": "deny", "bash": "deny"},
        id="bare-deny-block",
    ),
    pytest.param(
        {"webfetch": {"*": "deny"}},
        {"webfetch": {"*": "deny"}, "edit": "deny", "bash": "deny"},
        id="pattern-map-deny",
    ),
])
def test_read_only_never_widens_an_operator_deny(monkeypatch, tmp_path, operator_block, expected):
    """End to end on the rendered config (R18): a read-only turn over an
    operator's `permission: "deny"` renders `{"*": "deny", edit, bash}` with no
    `webfetch` key at all, and over `{"webfetch": {"*": "deny"}}` the pattern
    map survives. Read-only withholds; it never widens."""
    shared = tmp_path / "shared" / "opencode"
    shared.mkdir(parents=True)
    (shared / "opencode.json").write_text(json.dumps({"permission": operator_block}))
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    monkeypatch.setattr(opencode_runtime, "_shared_config_dir", lambda: str(shared))
    turn_dir = opencode_runtime._prepare_turn_config(
        "exec-ro-deny", None, permission=READ_ONLY_PERMISSION
    )
    rendered = _config_of(turn_dir)["permission"]
    assert rendered == expected
    assert rendered.get("webfetch") != "allow"
    if operator_block == "deny":
        assert "webfetch" not in rendered
    else:
        assert rendered["webfetch"] == {"*": "deny"}


def _read_only(monkeypatch, tmp_path, enabled: bool):
    root = tmp_path / "read-only.json"
    root.write_text(json.dumps({"enabled": enabled}))
    monkeypatch.setattr(opencode_runtime, "_READ_ONLY_CONFIG", root)
    monkeypatch.setattr(opencode_runtime, "_LEGACY_READ_ONLY_CONFIG", tmp_path / "none")


def _capture_turn_config(monkeypatch):
    """Snapshot the rendered opencode.json before the turn's `finally` removes it."""
    rendered = {}
    real = opencode_runtime._prepare_turn_config

    def _spy(*a, **k):
        turn_dir = real(*a, **k)
        rendered.update(_config_of(turn_dir))
        return turn_dir

    monkeypatch.setattr(opencode_runtime, "_prepare_turn_config", _spy)
    return rendered


@pytest.mark.asyncio
async def test_read_only_turn_renders_the_block_through_execute(monkeypatch, tmp_path):
    """End to end on the stubbed subprocess: the live read-only file → the
    permission block in the config the spawn reads."""
    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    monkeypatch.setattr(opencode_runtime, "_load_guardrails", lambda: {})
    _read_only(monkeypatch, tmp_path, True)
    rendered = _capture_turn_config(monkeypatch)
    _install_fake_subprocess(monkeypatch, stdout_lines=_step("done"))

    _text, log, _meta, _sid = await runtime.execute_headless("task", model="openrouter/x/y")
    assert rendered["permission"] == {"edit": "deny", "bash": "deny"}
    assert "webfetch" not in rendered["permission"]
    assert [e for e in log if e.tool == "trinity-control"] == []


@pytest.mark.asyncio
async def test_headless_allowed_tools_reach_the_block_and_the_ledger(monkeypatch, tmp_path, caplog):
    """`allowed_tools` is threaded from execute_headless to the config; the
    unmapped names come back in the execution log the turn returns AND as a
    WARNING line."""
    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    monkeypatch.setattr(opencode_runtime, "_load_guardrails", lambda: {"disallowed_tools": ["Task"]})
    _read_only(monkeypatch, tmp_path, False)
    rendered = _capture_turn_config(monkeypatch)
    _install_fake_subprocess(monkeypatch, stdout_lines=_step("done"))

    with caplog.at_level(logging.WARNING, logger=opencode_runtime.logger.name):
        _text, log, _meta, _sid = await runtime.execute_headless(
            "task", model="openrouter/x/y", allowed_tools=["Bash", "Glob"]
        )
    assert rendered["permission"] == {"edit": "deny", "webfetch": "deny", "websearch": "deny"}
    surfaced = [e.output for e in log if e.tool == "trinity-control"]
    assert surfaced == ["allowed_tools not mapped: Glob", "guardrail not mapped: Task"]
    warned = [r.getMessage() for r in caplog.records if "not mapped" in r.getMessage()]
    assert len(warned) == 1
    assert "allowed_tools not mapped: Glob" in warned[0]
    assert "guardrail not mapped: Task" in warned[0]
    # Platform denials are logged, not written to the ledger per turn.
    assert "platform denial not mapped: ScheduleWakeup" in warned[0]
    assert not any("ScheduleWakeup" in s for s in surfaced)


@pytest.mark.asyncio
async def test_chat_path_passes_no_allowlist(monkeypatch, tmp_path):
    """`execute` (chat) has no allowlist parameter; it must not restrict."""
    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    monkeypatch.setattr(opencode_runtime, "_load_guardrails", lambda: {})
    _read_only(monkeypatch, tmp_path, False)
    rendered = _capture_turn_config(monkeypatch)
    _install_fake_subprocess(monkeypatch, stdout_lines=_step("done"))

    await runtime.execute("hi", model="openrouter/x/y")
    assert "permission" not in rendered


# ---------------------------------------------------------------------------
# ledger parity through the result-callback envelope
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_result_callback_envelope_keeps_none_cost_and_model_name(monkeypatch, tmp_path):
    """An OpenCode turn on a custom provider with no declared cost serializes
    `cost_usd: null` (T6 — never 0) and `model_name` from the resolved `-m`
    through the SAME builder `/api/task` async results use."""
    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    monkeypatch.setattr(opencode_runtime, "_load_guardrails", lambda: {})
    _read_only(monkeypatch, tmp_path, False)
    _provider_env(monkeypatch, {"qwen3:27b": {}})
    _install_fake_subprocess(
        monkeypatch,
        stdout_lines=_lines(*_pstep("ok", cost=0.0, tokens={
            "input": 10, "output": 5, "cache": {"read": 4, "write": 1},
        })),
    )

    text, log, metadata, session_id = await runtime.execute_headless(
        "task", model="local/qwen3:27b"
    )
    envelope = result_callback._success_envelope(text, log, metadata, session_id)
    payload = json.loads(json.dumps(envelope))  # what goes on the wire
    assert payload["status"] == "success"
    assert payload["metadata"]["cost_usd"] is None
    assert payload["metadata"]["model_name"] == "local/qwen3:27b"
    assert payload["metadata"]["input_tokens"] == 10
    assert payload["metadata"]["output_tokens"] == 5
    assert payload["metadata"]["cache_read_tokens"] == 4


def test_envelope_builder_does_not_coerce_none_cost():
    """The shared builder is a plain `model_dump`: None stays None."""
    metadata = ExecutionMetadata(cost_usd=None, model_name="local/m")
    envelope = result_callback._success_envelope("t", [], metadata, None)
    assert envelope["metadata"]["cost_usd"] is None
    assert envelope["metadata"]["model_name"] == "local/m"


def test_turn_dir_name_is_generated_never_the_execution_id(monkeypatch, tmp_path):
    """No path derives from a request value (ruling R9, CodeQL path-injection):
    the same execution id twice yields two distinct turn dirs under
    `<root>/turns/`, and neither path carries the id."""
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    execution_id = "exec-r9-request-value"
    a = opencode_runtime._prepare_turn_config(execution_id, None)
    b = opencode_runtime._prepare_turn_config(execution_id, None)
    try:
        assert a != b
        for turn_dir in (a, b):
            assert _Path(turn_dir).parent == tmp_path / "turns"
            assert execution_id not in turn_dir
    finally:
        opencode_runtime._cleanup_turn_dir(a)
        opencode_runtime._cleanup_turn_dir(b)
