"""OpenCode telemetry under ``part`` (Abilityai/lilu#659) and the per-provider
images capability (Abilityai/lilu#660).

#659: the pinned 1.18.32 binary nests every step/tool payload one level down,
under ``part``. Reading the top level alone reported 0 tokens and a tool named
"Tool". The fixture lines below are REAL — captured from the raw /chat stream
on the DGX — and the legacy flat shape must keep working.

#660: 1.18.32 replaces an image part with an "ERROR: Cannot read …" text part
unless the model's ``capabilities.input.image`` is true, which for a model
declared only in opencode.json (Ollama, any OpenAI-compatible URL) is
``modalities.input`` includes "image", else false. The runtime must never run
that turn as a silent text-only turn.
"""

from __future__ import annotations

import base64
import json
import os
import sys

import pytest

from agent_server.services import opencode_runtime  # noqa: E402
from agent_server.services._runtime_config import opencode_provider_images  # noqa: E402
from agent_server.services.opencode_provider import apply_provider_config  # noqa: E402
from agent_server.services.opencode_runtime import (  # noqa: E402
    OpenCodeRuntime,
    images_deliverable,
    parse_opencode_jsonl,
)

sys.path.insert(0, os.path.dirname(__file__))
from test_lilu257_opencode_runtime import _spawn_capture, _step  # noqa: E402

pytestmark = pytest.mark.unit

# Verbatim from the DGX /chat stream (Abilityai/lilu#659).
REAL_STEP_FINISH = (
    '{"type":"step_finish","part":{"type":"step-finish","tokens":{"total":56976,'
    '"input":56913,"output":63,"reasoning":0,"cache":{"read":0,"write":0}},"cost":0}}'
)


def _part_turn(inputs, *, tools=3, output=10):
    """A multi-step 1.18.32 turn: one bash call per step, text on the last."""
    events = []
    for i, step_input in enumerate(inputs):
        events.append({"type": "step_start", "sessionID": "ses_1", "part": {"type": "step-start"}})
        if i < tools:
            events.append({
                "type": "tool_use", "sessionID": "ses_1",
                "part": {
                    "type": "tool", "id": f"prt_{i}", "callID": f"call_{i}", "tool": "bash",
                    "state": {"status": "completed", "input": {"command": "ls"}, "output": "ok"},
                },
            })
        else:
            events.append({"type": "text", "sessionID": "ses_1", "part": {"type": "text", "text": "done"}})
        events.append({
            "type": "step_finish", "sessionID": "ses_1",
            "part": {
                "type": "step-finish",
                "tokens": {"total": step_input + output, "input": step_input, "output": output,
                           "reasoning": 0, "cache": {"read": 0, "write": 0}},
                "cost": 0,
            },
        })
    return [json.dumps(e) for e in events]


# -- #659 ---------------------------------------------------------------------

def test_real_step_finish_line_is_read_under_part():
    lines = [
        json.dumps({"type": "step_start", "part": {"type": "step-start"}}),
        json.dumps({"type": "text", "part": {"type": "text", "text": "hello"}}),
        REAL_STEP_FINISH,
    ]
    response, _log, metadata, _raw, _outcome = parse_opencode_jsonl(lines)
    assert response == "hello"
    assert metadata.input_tokens == 56913
    assert metadata.output_tokens == 63
    # Decision (#659): a native cost of 0 stays 0.0 — None means "no cost field".
    assert metadata.cost_usd == 0.0


def test_four_step_tool_turn_reports_last_input_and_real_tool_id():
    lines = _part_turn([57148, 57177, 57203, 57229])
    response, log, metadata, _raw, _outcome = parse_opencode_jsonl(lines, model="ollama/gemma4:12b")
    assert response == "done"
    # #655 design kept: LAST step's input, not the ~229K sum; output summed.
    assert metadata.input_tokens == 57229
    assert metadata.output_tokens == 40
    assert metadata.tool_count == 3
    uses = [e for e in log if e.type == "tool_use"]
    assert [e.tool for e in uses] == ["bash", "bash", "bash"]
    assert uses[0].input == {"command": "ls"}
    results = [e for e in log if e.type == "tool_result"]
    assert [e.output for e in results] == ["ok", "ok", "ok"]
    assert all(e.tool != "Tool" for e in log)


def test_legacy_top_level_shape_still_parses():
    lines = _step("legacy", tokens={"input": 100, "output": 7, "cache": {"read": 0, "write": 0}}, cost=0.25)
    lines.insert(1, json.dumps({"type": "tool_use", "id": "t1", "tool": "read",
                                "input": {"path": "x"}, "status": "completed", "output": "y"}))
    response, log, metadata, _raw, _outcome = parse_opencode_jsonl(lines)
    assert response == "legacy"
    assert metadata.input_tokens == 100
    assert metadata.output_tokens == 7
    assert metadata.cost_usd == 0.25
    assert log[0].tool == "read"


def test_cost_absent_stays_none():
    lines = _step("x", tokens={"input": 1, "output": 1})
    _r, _l, metadata, _raw, _o = parse_opencode_jsonl(lines)
    assert metadata.cost_usd is None


def test_model_name_observed_else_the_pinned_model():
    finish = {"type": "step_finish", "part": {"type": "step-finish", "providerID": "ollama",
                                              "modelID": "gemma4:12b", "tokens": {"input": 5, "output": 1}}}
    lines = [json.dumps({"type": "step_start"}), json.dumps({"type": "text", "text": "a"}), json.dumps(finish)]
    _r, _l, metadata, _raw, _o = parse_opencode_jsonl(lines, model="ollama/other")
    assert metadata.model_name == "ollama/gemma4:12b"
    _r, _l, metadata, _raw, _o = parse_opencode_jsonl([REAL_STEP_FINISH], model="ollama/gemma4:12b")
    assert metadata.model_name == "ollama/gemma4:12b"


# -- #660: the capability ---------------------------------------------------

@pytest.mark.parametrize("provider,expected", [
    ("ollama", False), ("openrouter", True), ("anthropic", True), ("openai", True), (None, True),
])
def test_images_defaults(provider, expected):
    assert opencode_provider_images(provider, {}) is expected


def test_url_declared_openai_compatible_provider_defaults_off():
    env = {"OPENCODE_PROVIDER_LMSTUDIO_BASE_URL": "http://127.0.0.1:1234/v1"}
    assert opencode_provider_images("lmstudio", env) is False
    # openrouter behind a proxy URL is still a models.dev provider.
    assert opencode_provider_images("openrouter", {"OPENCODE_PROVIDER_OPENROUTER_BASE_URL": "http://p"}) is True


def test_env_overrides_both_ways():
    assert opencode_provider_images("ollama", {"OPENCODE_PROVIDER_OLLAMA_IMAGES": "true"}) is True
    assert opencode_provider_images("openrouter", {"OPENCODE_PROVIDER_OPENROUTER_IMAGES": "false"}) is False
    # An unparseable value is not a declaration.
    assert opencode_provider_images("ollama", {"OPENCODE_PROVIDER_OLLAMA_IMAGES": "maybe"}) is False


def test_capabilities_surface_image_input(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_MODEL", "ollama/gemma4:12b")
    monkeypatch.delenv("OPENCODE_PROVIDER_OLLAMA_IMAGES", raising=False)
    assert OpenCodeRuntime.capabilities().image_input is False
    assert OpenCodeRuntime.capabilities().to_dict()["image_input"] is False
    monkeypatch.setenv("OPENCODE_PROVIDER_OLLAMA_IMAGES", "true")
    assert OpenCodeRuntime.capabilities().image_input is True
    monkeypatch.setenv("AGENT_RUNTIME_MODEL", "openrouter/google/gemma-3-27b-it:free")
    assert OpenCodeRuntime.capabilities().image_input is True


def test_images_true_writes_modalities_with_text_kept():
    env = {"OPENCODE_PROVIDER_OLLAMA_BASE_URL": "http://127.0.0.1:11434",
           "OPENCODE_PROVIDER_OLLAMA_IMAGES": "true"}
    config = apply_provider_config({}, model="ollama/gemma4:12b", env=env)
    entry = config["provider"]["ollama"]["models"]["gemma4:12b"]
    # "text" must be listed: once `modalities` is set the binary reads text
    # input from it too.
    assert entry == {"modalities": {"input": ["text", "image"], "output": ["text"]}}


def test_images_unset_writes_no_modalities():
    env = {"OPENCODE_PROVIDER_OLLAMA_BASE_URL": "http://127.0.0.1:11434"}
    config = apply_provider_config({}, model="ollama/gemma4:12b", env=env)
    assert config["provider"]["ollama"]["models"]["gemma4:12b"] == {}


def test_declared_modalities_in_opencode_json_count_as_capable():
    config = {"provider": {"ollama": {"models": {"gemma4:12b": {"modalities": {"input": ["text", "image"]}}}}}}
    assert images_deliverable("ollama/gemma4:12b", {}, config) is True
    assert images_deliverable("ollama/qwen3:8b", {}, config) is False
    # Explicit env wins over the declaration.
    assert images_deliverable("ollama/gemma4:12b", {"OPENCODE_PROVIDER_OLLAMA_IMAGES": "false"}, config) is False


# -- #660: the non-silent branch ----------------------------------------------

_PNG = {"media_type": "image/png", "data": base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()}


def _ollama_env(monkeypatch, **extra):
    # The shared harness stubs `_drain_bounded` to a no-op, so the reader
    # thread can still be parsing when the turn is finalised — the first
    # spawn in a process then reads an empty stream (flaky on the base, too).
    # Join the readers instead.
    monkeypatch.setattr(
        opencode_runtime, "_drain_bounded",
        lambda process, out, err, **k: (out.join(5), err.join(5)),
    )
    spawn_env = {"OPENCODE_PROVIDER_OLLAMA_BASE_URL": "http://127.0.0.1:11434", **extra}
    monkeypatch.setattr(
        opencode_runtime, "build_execution_env",
        lambda extra=None, **kw: {**spawn_env, **(extra or {})},
    )


@pytest.mark.asyncio
async def test_undeliverable_images_are_not_a_silent_text_turn(monkeypatch, tmp_path, caplog):
    runtime, seen = _spawn_capture(monkeypatch, tmp_path, _step("I see no image"))
    _ollama_env(monkeypatch)
    caplog.set_level("WARNING")
    response, _log, metadata, _sid = await runtime.execute_headless(
        "describe the image", model="ollama/gemma4:12b", images=[_PNG],
    )
    cmd = seen[0]
    assert "-f" not in cmd
    # The model is told rather than left to improvise about an unseen image.
    assert "could not be delivered to you" in cmd[-1]
    assert cmd[-1].endswith("describe the image")
    assert len(metadata.warnings) == 1
    assert "OPENCODE_PROVIDER_OLLAMA_IMAGES=true" in metadata.warnings[0]
    assert response.startswith(metadata.warnings[0])
    assert response.endswith("I see no image")
    assert any("not delivered" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_images_flow_when_provider_is_declared_capable(monkeypatch, tmp_path):
    runtime, seen = _spawn_capture(monkeypatch, tmp_path, _step("a red square"))
    _ollama_env(monkeypatch, OPENCODE_PROVIDER_OLLAMA_IMAGES="true")
    response, _log, metadata, _sid = await runtime.execute_headless(
        "describe the image", model="ollama/gemma4:12b", images=[_PNG],
    )
    assert response == "a red square"
    assert metadata.warnings == []
    assert seen[0].count("-f") == 1
