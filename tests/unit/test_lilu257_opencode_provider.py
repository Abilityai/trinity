"""OpenCode custom-provider block + turn watchdog (Abilityai/lilu#257 step 6).

Two things ship together here because they are the two halves of "an agent can
actually run on a local DGX Ollama":

  * **the provider block** — ``runtime: {type: opencode, model:
    "ollama/gemma4:12b"}`` must work with NOTHING hand-edited inside the
    container. The emitted ``opencode.json`` is asserted verbatim, because the
    failure mode is silent: a missing or misshapen ``provider.ollama`` does not
    crash, it makes OpenCode fall back to its own default model.
  * **the turn watchdog** — measured on a real DGX (F6): the window before the
    first ``step_start`` is SILENT and ranged 7.2s → 59.3s for byte-identical
    configs, and a wedged turn emits zero bytes forever. Zero bytes must be a
    504, never a success; a stall is relaunched on a FRESH session up to a
    configurable limit (default 2 retries) bounded by the turn's own deadline,
    and a recovered turn is recorded as such so an affected host is visible.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time

import pytest
from fastapi import HTTPException

# conftest wires the real docker/base-image/agent_server as `agent_server`.
from agent_server.services import opencode_runtime  # noqa: E402
from agent_server.services.opencode_provider import (  # noqa: E402
    ENV_BASE_URL,
    OPENAI_COMPATIBLE_NPM,
    OpenCodeProviderConfigError,
    apply_provider_config,
    build_ollama_provider,
    normalize_base_url,
    resolve_models,
)
from agent_server.models import ExecutionMetadata  # noqa: E402
from agent_server.services.opencode_runtime import (  # noqa: E402
    OpenCodeRuntime,
    OpenCodeStallError,
    _stall_timeout_seconds,
)

_BASE = {
    "$schema": "https://opencode.ai/config.json",
    "share": "disabled",
    "autoupdate": False,
}


# ---------------------------------------------------------------------------
# The emitted JSON — the deliverable, asserted verbatim
# ---------------------------------------------------------------------------

def test_emitted_config_for_a_pinned_ollama_model_is_exact():
    """`runtime: {type: opencode, model: "ollama/gemma4:12b"}` + a base URL.

    Verbatim rather than key-by-key: the shape is a contract with OpenCode's
    config loader, and a spelling drift in `npm`/`options.baseURL`/`models`
    degrades to "unknown provider" without any error we would see.
    """
    env = {
        ENV_BASE_URL: "http://100.93.125.91:11434",
        "AGENT_RUNTIME_MODEL": "ollama/gemma4:12b",
        "OPENCODE_OLLAMA_MODELS": "ministral-3:14b, ollama/qwen3.6:27b",
    }
    emitted = apply_provider_config(dict(_BASE), model="ollama/gemma4:12b", env=env)

    assert emitted == {
        "$schema": "https://opencode.ai/config.json",
        "share": "disabled",
        "autoupdate": False,
        "provider": {
            "ollama": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": "http://100.93.125.91:11434/v1"},
                "models": {
                    "gemma4:12b": {},
                    "ministral-3:14b": {},
                    "qwen3.6:27b": {},
                },
            }
        },
    }
    # Must survive a JSON round-trip — it is written with json.dump.
    assert json.loads(json.dumps(emitted)) == emitted


def test_pinned_model_alone_is_enough():
    """No model list configured: the pinned model still gets declared.

    Otherwise the operator has to maintain a parallel `OPENCODE_OLLAMA_MODELS`
    list just to use the one model they already pinned.
    """
    env = {ENV_BASE_URL: "http://dgx:11434"}
    emitted = apply_provider_config(dict(_BASE), model="ollama/gemma4:12b", env=env)
    assert emitted["provider"]["ollama"]["models"] == {"gemma4:12b": {}}


# ---------------------------------------------------------------------------
# Base URL resolution
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://dgx:11434", "http://dgx:11434/v1"),
        ("http://dgx:11434/", "http://dgx:11434/v1"),
        ("https://dgx.example:443/v1", "https://dgx.example:443/v1"),
        ("  http://dgx:11434  ", "http://dgx:11434/v1"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_base_url(raw, expected):
    assert normalize_base_url(raw) == expected


def test_normalize_base_url_is_idempotent():
    """An operator who already wrote /v1 must not get /v1/v1."""
    once = normalize_base_url("http://dgx:11434")
    assert normalize_base_url(once) == once


@pytest.mark.parametrize("bad", ["dgx:11434", "ftp://dgx", "not a url", "http://"])
def test_bad_base_url_fails_with_the_env_var_name(bad):
    """Quality bar 6: a named reason, not a generic 500 and not a silent skip."""
    with pytest.raises(OpenCodeProviderConfigError) as exc:
        normalize_base_url(bad)
    assert ENV_BASE_URL in str(exc.value)


# ---------------------------------------------------------------------------
# Graceful degradation + operator overrides
# ---------------------------------------------------------------------------

def test_no_base_url_emits_no_provider_key():
    """An agent that never asks for ollama/* must be untouched."""
    emitted = apply_provider_config(dict(_BASE), model="anthropic/claude-sonnet-5", env={})
    assert "provider" not in emitted


def test_foreign_provider_models_never_leak_into_the_ollama_map():
    env = {ENV_BASE_URL: "http://dgx:11434", "AGENT_RUNTIME_MODEL": "anthropic/claude-sonnet-5"}
    emitted = apply_provider_config(dict(_BASE), model="ollama/gemma4:12b", env=env)
    assert emitted["provider"]["ollama"]["models"] == {"gemma4:12b": {}}


def test_operator_declaration_wins_on_options_and_models_are_unioned():
    """The env is a default, not an override — but a pinned model is always
    declared, so a customised config can never be missing the model the turn
    was launched with."""
    config = dict(_BASE)
    config["provider"] = {
        "ollama": {
            "npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": "http://operator-host:11434/v1"},
            "models": {"custom:7b": {}},
        }
    }
    env = {ENV_BASE_URL: "http://env-host:11434"}
    emitted = apply_provider_config(config, model="ollama/gemma4:12b", env=env)

    entry = emitted["provider"]["ollama"]
    assert entry["options"]["baseURL"] == "http://operator-host:11434/v1"
    assert entry["models"] == {"custom:7b": {}, "gemma4:12b": {}}


def test_operator_block_without_a_base_url_and_no_env_is_a_named_failure():
    config = dict(_BASE)
    config["provider"] = {"ollama": {"models": {"x:1b": {}}}}
    with pytest.raises(OpenCodeProviderConfigError) as exc:
        apply_provider_config(config, model="ollama/x:1b", env={})
    assert ENV_BASE_URL in str(exc.value)


def test_other_providers_are_preserved():
    config = dict(_BASE)
    config["provider"] = {"openrouter": {"models": {"z-ai/glm-5": {}}}}
    emitted = apply_provider_config(
        config, model="ollama/gemma4:12b", env={ENV_BASE_URL: "http://dgx:11434"}
    )
    assert "openrouter" in emitted["provider"]
    assert "ollama" in emitted["provider"]


def test_resolve_models_dedupes_and_keeps_the_turn_model_first():
    env = {
        "AGENT_RUNTIME_MODEL": "ollama/gemma4:12b",
        "OPENCODE_OLLAMA_MODELS": "gemma4:12b,ministral-3:14b,,  ",
    }
    assert resolve_models(model="ollama/qwen3.6:27b", env=env) == [
        "qwen3.6:27b", "gemma4:12b", "ministral-3:14b",
    ]


def test_build_ollama_provider_pins_the_openai_compatible_driver():
    """The package is pre-seeded into the image; OpenCode does NOT fetch it on
    demand (measured), so the name must match what the image installs."""
    entry = build_ollama_provider("http://dgx:11434/v1", ["gemma4:12b"])
    assert entry["npm"] == OPENAI_COMPATIBLE_NPM == "@ai-sdk/openai-compatible"


# ---------------------------------------------------------------------------
# The provider block reaches the per-turn config on disk
# ---------------------------------------------------------------------------

def test_prepare_turn_config_writes_the_provider_block(tmp_path, monkeypatch):
    """The wiring, not just the pure function: a turn's opencode.json is what
    OpenCode actually reads."""
    monkeypatch.setenv("OPENCODE_ROOT", str(tmp_path))
    monkeypatch.setenv(ENV_BASE_URL, "http://dgx:11434")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("AGENT_RUNTIME_MODEL", raising=False)
    monkeypatch.delenv("OPENCODE_OLLAMA_MODELS", raising=False)

    turn_dir = opencode_runtime._prepare_turn_config("exec-1", None, "ollama/gemma4:12b")
    with open(f"{turn_dir}/opencode/opencode.json", encoding="utf-8") as fh:
        written = json.load(fh)

    assert written["provider"]["ollama"]["options"]["baseURL"] == "http://dgx:11434/v1"
    assert written["provider"]["ollama"]["models"] == {"gemma4:12b": {}}
    # The privacy defaults are not collateral damage.
    assert written["share"] == "disabled" and written["autoupdate"] is False


# ---------------------------------------------------------------------------
# Turn watchdog (F6)
# ---------------------------------------------------------------------------

def test_stall_window_defaults_and_is_clamped_to_the_turn(monkeypatch):
    monkeypatch.delenv("OPENCODE_STALL_TIMEOUT_SEC", raising=False)
    assert _stall_timeout_seconds(3600) == 300
    # Never longer than the turn itself — a watchdog that fires after the turn
    # timeout is not a watchdog.
    assert _stall_timeout_seconds(60) == 60


@pytest.mark.parametrize("bad", ["0", "-5", "abc", " "])
def test_bad_stall_override_falls_back_rather_than_disabling(monkeypatch, bad):
    """'0 means off' would let a typo silently remove the protection."""
    monkeypatch.setenv("OPENCODE_STALL_TIMEOUT_SEC", bad)
    assert _stall_timeout_seconds(3600) == 300


def test_stall_override_is_honoured(monkeypatch):
    monkeypatch.setenv("OPENCODE_STALL_TIMEOUT_SEC", "45")
    assert _stall_timeout_seconds(3600) == 45


@pytest.mark.asyncio
async def test_a_stall_is_retried_on_a_fresh_session_and_recorded():
    """The measured F6 shape: attempt 1 wedges, an identical relaunch works.

    The recovery must be VISIBLE. A retried turn that eventually succeeds is
    otherwise recorded as a plain success, which is exactly how an affected
    host hides as slow-but-fine (Abilityai/lilu#257 F6 is host-dependent:
    16/16 on one container, ~13% per attempt on another)."""
    runtime = OpenCodeRuntime()
    calls = []

    async def fake(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise OpenCodeStallError("no step_start within 300s")
        return ("PONG", [], ExecutionMetadata(), [], "ses_new")

    runtime._execute_opencode = fake  # type: ignore[assignment]
    result = await runtime._execute_with_stall_retry(
        prompt="ping", model="ollama/gemma4:12b", system_prompt=None,
        session_id="ses_wedged", timeout_seconds=600, execution_id="exec-9",
    )

    assert result[0] == "PONG"
    assert len(calls) == 2
    # The wedged session is NOT resumed — resuming it would reproduce the wedge.
    assert calls[0]["session_id"] == "ses_wedged"
    assert calls[1]["session_id"] is None
    # One execution in the ledger, not a phantom pair.
    assert calls[1]["execution_id"] == "exec-9"
    # ...and the ledger can tell this apart from a healthy turn.
    assert result[2].stall_retries == 1


@pytest.mark.asyncio
async def test_a_healthy_turn_records_no_retries():
    """The field is meaningless unless 0 really means "nothing went wrong"."""
    runtime = OpenCodeRuntime()

    async def fine(**kwargs):
        return ("PONG", [], ExecutionMetadata(), [], "ses_1")

    runtime._execute_opencode = fine  # type: ignore[assignment]
    result = await runtime._execute_with_stall_retry(
        prompt="ping", model=None, system_prompt=None, session_id=None,
        timeout_seconds=600, execution_id="exec-9",
    )
    assert result[2].stall_retries == 0


@pytest.mark.asyncio
async def test_stalls_all_the_way_down_are_a_504_never_a_success(monkeypatch):
    """Empty stdout stays a failure however many attempts it took — the retry
    count is raised to survive a flaky host, NOT to launder a dead turn."""
    monkeypatch.delenv("OPENCODE_STALL_RETRIES", raising=False)
    runtime = OpenCodeRuntime()
    calls = []

    async def always_stall(**kwargs):
        calls.append(kwargs)
        raise OpenCodeStallError("no step_start within 300s")

    runtime._execute_opencode = always_stall  # type: ignore[assignment]
    with pytest.raises(HTTPException) as exc:
        await runtime._execute_with_stall_retry(
            prompt="ping", model="ollama/gemma4:12b", system_prompt=None,
            session_id=None, timeout_seconds=600, execution_id="exec-9",
        )
    assert exc.value.status_code == 504
    assert "stalled" in str(exc.value.detail).lower()
    # Default is 2 retries → 3 attempts, and the give-up line is greppable.
    assert len(calls) == 3
    assert opencode_runtime.STALL_RETRY_MARKER in str(exc.value.detail)


@pytest.mark.parametrize(
    "raw,expected",
    [(None, 2), ("0", 0), ("4", 4), ("99", 5), ("-3", 0), ("abc", 2), (" ", 2)],
)
def test_retry_limit_is_configurable_and_bounded(monkeypatch, raw, expected):
    """A knob an affected host can raise without a rebuild — bounded, because a
    20th relaunch is not a strategy. A junk value falls back to the default; a
    negative one means "stop retrying", the only non-surprising reading."""
    monkeypatch.delenv("OPENCODE_STALL_RETRIES", raising=False)
    if raw is not None:
        monkeypatch.setenv("OPENCODE_STALL_RETRIES", raw)
    assert opencode_runtime._stall_retry_limit() == expected


@pytest.mark.asyncio
async def test_retries_are_capped_by_the_turn_timeout_not_added_to_it(monkeypatch):
    """The ceiling is the turn's own deadline. Each attempt is launched with the
    time actually LEFT, and no retry starts once the remainder is below the
    floor an attempt needs — otherwise N retries would silently multiply a
    turn's worst-case latency by N and blow every caller's timeout."""
    monkeypatch.setenv("OPENCODE_STALL_RETRIES", "5")
    runtime = OpenCodeRuntime()
    calls = []

    async def always_stall(**kwargs):
        calls.append(kwargs)
        raise OpenCodeStallError("no step_start")

    runtime._execute_opencode = always_stall  # type: ignore[assignment]
    with pytest.raises(HTTPException) as exc:
        await runtime._execute_with_stall_retry(
            prompt="ping", model=None, system_prompt=None, session_id=None,
            # Below the retry floor from the start: one attempt, then stop.
            timeout_seconds=30, execution_id="exec-9",
        )
    assert exc.value.status_code == 504
    assert len(calls) == 1, "a retry was started with no budget left for it"
    assert calls[0]["timeout_seconds"] <= 30
    assert "budget" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_a_non_stall_failure_is_not_retried():
    """Only a stall is retried. Re-running a 500 would double every real error."""
    runtime = OpenCodeRuntime()
    calls = []

    async def fake(**kwargs):
        calls.append(kwargs)
        raise HTTPException(status_code=500, detail="empty turn")

    runtime._execute_opencode = fake  # type: ignore[assignment]
    with pytest.raises(HTTPException) as exc:
        await runtime._execute_with_stall_retry(
            prompt="ping", model=None, system_prompt=None, session_id=None,
            timeout_seconds=600, execution_id="exec-9",
        )
    assert exc.value.status_code == 500
    assert len(calls) == 1


def test_step_start_is_the_progress_signal_the_watchdog_waits_for():
    """Pins the event name against the real v1.18.32 stream (measured:
    step_start → text → step_finish). If OpenCode renames it, the watchdog
    would kill every healthy turn — so the name is asserted, not assumed."""
    source = opencode_runtime.__file__
    with open(source, encoding="utf-8") as fh:
        body = fh.read()
    assert 'event.get("type") == "step_start"' in body


def test_watchdog_kills_a_silent_process_and_lets_a_talking_one_through():
    """Behavioural, over the real primitive: a process that never emits
    `step_start` must be abandoned inside the window, while one that does must
    be governed by the ordinary turn timeout."""
    step_seen = threading.Event()
    proc = subprocess.Popen(["sleep", "30"], stdout=subprocess.DEVNULL)
    try:
        started = time.monotonic()
        with pytest.raises(OpenCodeStallError):
            _tiny_watchdog(proc, step_seen, stall_after=1, timeout_seconds=30)
        assert time.monotonic() - started < 10
    finally:
        proc.kill()
        proc.wait()

    proc2 = subprocess.Popen(["sleep", "0.2"], stdout=subprocess.DEVNULL)
    step_seen.set()
    assert _tiny_watchdog(proc2, step_seen, stall_after=1, timeout_seconds=30) == 0


def _tiny_watchdog(process, step_seen, *, stall_after, timeout_seconds):
    """The watchdog's loop, mirrored so it can be exercised without spawning a
    full turn. Kept textually parallel to
    ``opencode_runtime._execute_opencode.wait_with_watchdog``; the test above
    pins the event name that couples them."""
    started = time.monotonic()
    while True:
        remaining = timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise subprocess.TimeoutExpired("opencode", timeout_seconds)
        if step_seen.is_set():
            return process.wait(timeout=remaining)
        try:
            return process.wait(timeout=min(0.2, remaining))
        except subprocess.TimeoutExpired:
            pass
        if not step_seen.is_set() and (time.monotonic() - started) > stall_after:
            raise OpenCodeStallError(
                f"OpenCode produced no output within {stall_after}s "
                f"(no `step_start` event). Turn abandoned."
            )


# ---------------------------------------------------------------------------
# `--` end-of-options separator (Abilityai/lilu#257 step 6 follow-up)
# ---------------------------------------------------------------------------

def test_prompt_is_passed_after_an_end_of_options_separator():
    """A prompt beginning with '-' must not be parsed as a flag.

    Verified live against v1.18.32 before pinning it here:
    `opencode run -- "Reply with exactly the word ZEBRA."` returned `ZEBRA`,
    so the text after `--` reaches the message rather than yargs' `argv['--']`.
    """
    runtime = OpenCodeRuntime()
    cmd = runtime._build_command(
        model="ollama/gemma4:12b", session_id=None, directory="/home/developer"
    )
    cmd.append("--")
    cmd.append("-v, what changed?")
    assert cmd[-2:] == ["--", "-v, what changed?"]

    source = opencode_runtime.__file__
    with open(source, encoding="utf-8") as fh:
        body = fh.read()
    # The separator is appended by `_execute_opencode`, immediately before the
    # prompt — assert the adjacency, not just that '--' appears somewhere.
    assert 'cmd.append("--")\n        cmd.append(prompt)' in body
