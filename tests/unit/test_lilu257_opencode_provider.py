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
    OLLAMA_DEFAULT_PORT,
    OPENAI_COMPATIBLE_NPM,
    OpenCodeProviderConfigError,
    apply_provider_config,
    build_ollama_provider,
    derive_gateway_base_url,
    normalize_base_url,
    read_default_gateway,
    require_ollama_base_url,
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


def test_operator_block_without_a_base_url_and_no_env_is_a_named_failure(tmp_path):
    """No env, no gateway, no declared URL => named failure.

    `route_path` is pinned at a routeless fixture rather than left to the real
    `/proc/net/route`: since Abilityai/lilu#272 the gateway IS a source of base
    URLs, so a test that let the host supply one would assert nothing on a
    machine with a default route (i.e. every container this ever runs in).
    """
    config = dict(_BASE)
    config["provider"] = {"ollama": {"models": {"x:1b": {}}}}
    with pytest.raises(OpenCodeProviderConfigError) as exc:
        apply_provider_config(
            config, model="ollama/x:1b", env={}, route_path=str(_routeless(tmp_path))
        )
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
    # Abilityai/lilu#655: the pair is skipped only for an argument-less
    # `--command`, which sends no message at all.
    assert 'cmd.append("--")\n            cmd.append(prompt)' in body


# ---------------------------------------------------------------------------
# The derived gateway default (Abilityai/lilu#272)
#
# The measured defect: with OPENCODE_OLLAMA_BASE_URL unset and the turn pinned
# to `ollama/…`, no provider block was emitted at all and OpenCode died in
# 1.6s with the opaque ref `err_411e3b55`. The one value that works — the
# container's default gateway, where the host's Ollama answers; localhost is
# refused in 0ms — appeared in no config and no env, so it was unknowable
# without inspecting the host. It is DERIVED per turn rather than stored,
# because it is a property of the network the container is attached to.
# ---------------------------------------------------------------------------

#: A real `/proc/net/route` from a Trinity agent on trinity-agent-network.
#: `Gateway` is a LITTLE-ENDIAN hex word: 01001CAC unpacks to the bytes
#: AC 1C 00 01 = 172.28.0.1. Read in string order it would be 1.0.28.172 —
#: a plausible-looking address that is not the gateway, which is exactly the
#: kind of wrong a fixture has to pin.
_ROUTE_WITH_DEFAULT = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "eth0\t00000000\t01001CAC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    "eth0\t00001CAC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
)

#: Same table with the default route removed — an on-link-only container.
_ROUTE_WITHOUT_DEFAULT = (
    "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
    "eth0\t00001CAC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
)


def _route_file(tmp_path, body: str, name: str = "route"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _routeless(tmp_path):
    return _route_file(tmp_path, _ROUTE_WITHOUT_DEFAULT, name="route-no-default")


def test_default_gateway_is_decoded_little_endian(tmp_path):
    """01001CAC -> 172.28.0.1, and the value is verified against the live
    kernel table's own encoding, not a guess."""
    assert read_default_gateway(str(_route_file(tmp_path, _ROUTE_WITH_DEFAULT))) == "172.28.0.1"


def test_derived_base_url_is_the_gateway_on_ollamas_port(tmp_path):
    derived = derive_gateway_base_url(str(_route_file(tmp_path, _ROUTE_WITH_DEFAULT)))
    assert derived == f"http://172.28.0.1:{OLLAMA_DEFAULT_PORT}/v1"


@pytest.mark.parametrize(
    "body",
    [
        _ROUTE_WITHOUT_DEFAULT,
        # An on-link default route: destination is zero but there is no next
        # hop, so there is no host address to dial.
        "Iface\tDestination\tGateway\n" "eth0\t00000000\t00000000\t0003\t0\t0\t0\n",
        # Header only.
        "Iface\tDestination\tGateway\n",
    ],
)
def test_no_usable_default_route_yields_no_gateway(tmp_path, body):
    assert read_default_gateway(str(_route_file(tmp_path, body))) is None


def test_unreadable_route_file_yields_no_gateway_rather_than_raising(tmp_path):
    """An OSError here must not take the turn down by itself — the caller
    decides, and for a non-ollama turn a missing route file is irrelevant."""
    assert read_default_gateway(str(tmp_path / "does-not-exist")) is None


def test_unset_env_on_an_ollama_turn_derives_the_gateway(tmp_path):
    """The defect, directly: unset env + `ollama/…` used to emit nothing."""
    emitted = apply_provider_config(
        dict(_BASE),
        model="ollama/gemma4:12b",
        env={},
        route_path=str(_route_file(tmp_path, _ROUTE_WITH_DEFAULT)),
    )
    assert emitted["provider"]["ollama"] == {
        "npm": OPENAI_COMPATIBLE_NPM,
        "options": {"baseURL": "http://172.28.0.1:11434/v1"},
        "models": {"gemma4:12b": {}},
    }


def test_the_env_var_still_wins_over_the_derived_gateway(tmp_path):
    """The gateway is a DEFAULT, not a policy: an operator who points the
    agent at a remote DGX must not be silently redirected to their host."""
    emitted = apply_provider_config(
        dict(_BASE),
        model="ollama/gemma4:12b",
        env={ENV_BASE_URL: "http://dgx:11434"},
        route_path=str(_route_file(tmp_path, _ROUTE_WITH_DEFAULT)),
    )
    assert emitted["provider"]["ollama"]["options"]["baseURL"] == "http://dgx:11434/v1"


def test_agent_wide_pin_alone_derives_the_gateway(tmp_path):
    """`runtime: {type: opencode, model: "ollama/gemma4:12b"}` sets
    AGENT_RUNTIME_MODEL and no `-m`; that is the shipped shape."""
    emitted = apply_provider_config(
        dict(_BASE),
        model=None,
        env={"AGENT_RUNTIME_MODEL": "ollama/gemma4:12b"},
        route_path=str(_route_file(tmp_path, _ROUTE_WITH_DEFAULT)),
    )
    assert emitted["provider"]["ollama"]["options"]["baseURL"] == "http://172.28.0.1:11434/v1"


def test_a_non_ollama_turn_with_no_env_still_emits_nothing(tmp_path):
    """Graceful degradation is not traded away for the fix: the
    Anthropic/OpenRouter agent must not acquire a provider key — nor dial the
    host — just because a gateway happens to exist."""
    emitted = apply_provider_config(
        dict(_BASE),
        model="anthropic/claude-sonnet-5",
        env={},
        route_path=str(_route_file(tmp_path, _ROUTE_WITH_DEFAULT)),
    )
    assert "provider" not in emitted


def test_an_ollama_turn_with_no_env_and_no_gateway_is_a_named_failure(tmp_path):
    """Neither path resolves => the operator is told what to set, rather than
    handed OpenCode's `err_*` ref."""
    with pytest.raises(OpenCodeProviderConfigError) as exc:
        apply_provider_config(
            dict(_BASE), model="ollama/gemma4:12b", env={}, route_path=str(_routeless(tmp_path))
        )
    assert ENV_BASE_URL in str(exc.value)


def test_the_derivation_is_logged_at_info_with_the_value(tmp_path, caplog):
    """A failing local-Ollama turn's first question is which host it dialled;
    "DERIVED" vs "set" is the difference between fixing the network and
    fixing the .env."""
    with caplog.at_level("INFO", logger="agent_server.services.opencode_provider"):
        apply_provider_config(
            dict(_BASE),
            model="ollama/gemma4:12b",
            env={},
            route_path=str(_route_file(tmp_path, _ROUTE_WITH_DEFAULT)),
        )
    derived = [r.getMessage() for r in caplog.records if "DERIVED" in r.getMessage()]
    assert derived, caplog.text
    assert "http://172.28.0.1:11434/v1" in derived[0]
    assert ENV_BASE_URL in derived[0]


# ---------------------------------------------------------------------------
# The `-m` boundary guard
# ---------------------------------------------------------------------------

def test_boundary_guard_refuses_an_unrunnable_ollama_pin(tmp_path):
    with pytest.raises(OpenCodeProviderConfigError) as exc:
        require_ollama_base_url(
            "ollama/gemma4:12b", env={}, route_path=str(_routeless(tmp_path))
        )
    assert ENV_BASE_URL in str(exc.value)


@pytest.mark.parametrize(
    "kwargs",
    [
        # nothing to run against Ollama at all
        {"model": "anthropic/claude-sonnet-5", "env": {}},
        # the env names a host
        {"model": "ollama/gemma4:12b", "env": {ENV_BASE_URL: "http://dgx:11434"}},
        # the operator declared one in the shared config
        {
            "model": "ollama/gemma4:12b",
            "env": {},
            "config": {"provider": {"ollama": {"options": {"baseURL": "http://op:11434/v1"}}}},
        },
    ],
)
def test_boundary_guard_is_silent_when_a_url_exists_anywhere(tmp_path, kwargs):
    """It refuses the unrunnable and decides nothing else — in particular it
    must not veto an operator-declared block just because this container has
    no default route."""
    model = kwargs.pop("model")
    require_ollama_base_url(model, route_path=str(_routeless(tmp_path)), **kwargs)


@pytest.mark.asyncio
async def test_an_unrunnable_ollama_turn_fails_with_the_env_var_not_an_err_ref(
    monkeypatch, tmp_path
):
    """End to end at the seam that matters: the turn never reaches OpenCode,
    and the execution's error names OPENCODE_OLLAMA_BASE_URL. Past the `-m`
    boundary the only diagnosis an operator gets is `err_411e3b55`, which
    names nothing they can set."""
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    monkeypatch.setattr(opencode_runtime, "_load_shared_config", lambda: dict(_BASE))
    monkeypatch.delenv(ENV_BASE_URL, raising=False)
    monkeypatch.delenv("AGENT_RUNTIME_MODEL", raising=False)
    # Bind the guard to a container that has no default route; everything else
    # on the path is the real code.
    routeless = str(_routeless(tmp_path))
    monkeypatch.setattr(
        opencode_runtime,
        "require_ollama_base_url",
        lambda model, **kw: require_ollama_base_url(model, route_path=routeless, **kw),
    )

    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    with pytest.raises(HTTPException) as exc:
        await runtime.execute_headless("ping", model="ollama/gemma4:12b")
    assert exc.value.status_code == 400
    assert ENV_BASE_URL in str(exc.value.detail)
    assert "err_" not in str(exc.value.detail)


# ---------------------------------------------------------------------------
# Providers are configuration (Abilityai/lilu#653)
#
# Before: only `provider.ollama` was ever generated, from the agent-server's
# own os.environ — so a `template.yaml runtime.env` declaration reached the
# `opencode` child but NOT the provider block, and OpenRouter worked only by
# accident. Now any provider is `OPENCODE_PROVIDER_<ID>_*` in the SPAWN env.
# No test here touches the network: there is no OpenRouter key in the lab.
# ---------------------------------------------------------------------------

from agent_server.services import opencode_provider  # noqa: E402
from agent_server.services.execution_env import build_execution_env  # noqa: E402
from agent_server.services.opencode_provider import (  # noqa: E402
    ENV_OPENROUTER_API_KEY,
    ENV_PLATFORM_BASE_URL,
    OpenCodeProviderCredentialError,
    configured_models,
    provider_env,
    require_openrouter_key,
)


def _template_env(tmp_path, runtime_env: dict, dotenv: str = ""):
    """The real spawn-env builder over a fixture `.env` + `template.yaml`."""
    env_file = tmp_path / ".env"
    env_file.write_text(dotenv, encoding="utf-8")
    lines = ["runtime:", "  type: opencode", "  env:"]
    lines += [f'    {k}: "{v}"' for k, v in runtime_env.items()]
    template = tmp_path / "template.yaml"
    template.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return build_execution_env(env_file=env_file, template_file=template)


def test_template_runtime_env_reaches_the_provider_block(tmp_path, monkeypatch):
    """The defect, end to end on disk: a base URL declared ONLY in
    `template.yaml runtime.env` (nothing in the agent-server's os.environ)
    lands in the turn's opencode.json."""
    for var in (ENV_BASE_URL, "OPENCODE_PROVIDER_OLLAMA_BASE_URL", ENV_PLATFORM_BASE_URL,
                "AGENT_RUNTIME_MODEL", "OPENCODE_OLLAMA_MODELS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENCODE_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    env = _template_env(tmp_path, {
        "OPENCODE_PROVIDER_OLLAMA_BASE_URL": "http://dgx:11434",
        "OPENCODE_PROVIDER_OLLAMA_MODELS": "ministral-3:14b",
    })

    turn_dir = opencode_runtime._prepare_turn_config(
        "exec-653", None, "ollama/gemma4:12b", env=env
    )
    with open(f"{turn_dir}/opencode/opencode.json", encoding="utf-8") as fh:
        written = json.load(fh)
    assert written["provider"]["ollama"] == {
        "npm": OPENAI_COMPATIBLE_NPM,
        "options": {"baseURL": "http://dgx:11434/v1"},
        "models": {"gemma4:12b": {}, "ministral-3:14b": {}},
    }


@pytest.mark.asyncio
async def test_the_runtime_builds_the_spawn_env_before_the_provider_block(monkeypatch, tmp_path):
    """Wiring: `_execute_opencode` hands the spawn env (not os.environ) to the
    guards and to `_prepare_turn_config`."""
    spawn_env = {"OPENCODE_PROVIDER_OLLAMA_BASE_URL": "http://from-template:11434"}
    monkeypatch.setattr(opencode_runtime, "build_execution_env", lambda extra=None, **kw: dict(spawn_env))
    monkeypatch.setattr(opencode_runtime, "_load_shared_config", lambda: dict(_BASE))
    monkeypatch.setattr(opencode_runtime, "refresh_agents_md", lambda: None)
    monkeypatch.delenv(ENV_BASE_URL, raising=False)
    seen = {}

    class _Stop(Exception):
        pass

    def _capture(execution_id, system_prompt, model=None, env=None, **_kw):
        seen["env"] = env
        raise _Stop()

    monkeypatch.setattr(opencode_runtime, "_prepare_turn_config", _capture)
    with pytest.raises(_Stop):
        await OpenCodeRuntime()._execute_opencode(
            prompt="ping", model="ollama/gemma4:12b", system_prompt=None,
            session_id=None, timeout_seconds=30, execution_id="e-1",
        )
    assert seen["env"]["OPENCODE_PROVIDER_OLLAMA_BASE_URL"] == "http://from-template:11434"


def test_openrouter_turn_emits_no_block_but_passes_the_key(tmp_path):
    """Built-in provider: no `provider` key is written, the guard passes, and
    the key is in the env the child is spawned with."""
    env = _template_env(
        tmp_path,
        {ENV_OPENROUTER_API_KEY: "${OPENROUTER_API_KEY}"},
        dotenv="OPENROUTER_API_KEY=placeholder-openrouter-key\n",
    )
    assert env[ENV_OPENROUTER_API_KEY] == "placeholder-openrouter-key"
    require_openrouter_key("openrouter/z-ai/glm-5", env=env, config=dict(_BASE))
    emitted = apply_provider_config(dict(_BASE), model="openrouter/z-ai/glm-5", env=env)
    assert "provider" not in emitted


def test_openrouter_turn_without_a_key_is_refused_by_name(tmp_path):
    """Never a silent fallback onto another provider: named, before spawn."""
    with pytest.raises(OpenCodeProviderCredentialError) as exc:
        require_openrouter_key(
            "openrouter/z-ai/glm-5", env={}, config=dict(_BASE),
            auth_path=str(tmp_path / "missing-auth.json"),
        )
    assert ENV_OPENROUTER_API_KEY in str(exc.value)
    assert "openrouter_api_key" in str(exc.value)
    # A subclass, so every existing `except OpenCodeProviderConfigError` 400s it.
    assert isinstance(exc.value, OpenCodeProviderConfigError)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"env": {"OPENCODE_PROVIDER_OPENROUTER_API_KEY": "k"}},
        {"env": {}, "config": {"provider": {"openrouter": {"options": {"apiKey": "k"}}}}},
        {"env": {"AGENT_RUNTIME_MODEL": "anthropic/claude-sonnet-5"}},
    ],
)
def test_openrouter_guard_accepts_every_key_source_and_ignores_other_providers(kwargs):
    model = None if "AGENT_RUNTIME_MODEL" in kwargs.get("env", {}) else "openrouter/x/y"
    require_openrouter_key(model, **kwargs)


def test_openrouter_key_in_opencode_auth_json_counts(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"openrouter": {"type": "api", "key": "k"}}), encoding="utf-8")
    require_openrouter_key("openrouter/x/y", env={}, auth_path=str(auth))


@pytest.mark.asyncio
async def test_a_keyless_openrouter_turn_fails_400_naming_the_key(monkeypatch, tmp_path):
    monkeypatch.setattr(opencode_runtime, "_opencode_root", lambda: str(tmp_path))
    monkeypatch.setattr(opencode_runtime, "_load_shared_config", lambda: dict(_BASE))
    monkeypatch.setattr(opencode_runtime, "build_execution_env", lambda extra=None, **kw: dict(extra or {}))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-config"))
    runtime = OpenCodeRuntime()
    monkeypatch.setattr(runtime, "is_available", lambda: True)
    with pytest.raises(HTTPException) as exc:
        await runtime.execute_headless("ping", model="openrouter/z-ai/glm-5")
    assert exc.value.status_code == 400
    assert ENV_OPENROUTER_API_KEY in str(exc.value.detail)


def test_unseeded_npm_driver_is_refused_by_name():
    env = {
        "OPENCODE_PROVIDER_LMSTUDIO_BASE_URL": "http://lab:1234/v1",
        "OPENCODE_PROVIDER_LMSTUDIO_NPM": "@ai-sdk/openai",
    }
    with pytest.raises(OpenCodeProviderConfigError) as exc:
        apply_provider_config(dict(_BASE), model="lmstudio/qwen", env=env)
    assert "OPENCODE_PROVIDER_LMSTUDIO_NPM" in str(exc.value)
    assert "@ai-sdk/openai'" in str(exc.value)


def test_unseeded_npm_driver_in_the_operator_config_is_refused_too():
    config = dict(_BASE)
    config["provider"] = {"vllm": {"npm": "some-unseeded-driver", "options": {"baseURL": "http://v/v1"}}}
    with pytest.raises(OpenCodeProviderConfigError) as exc:
        apply_provider_config(config, model="anthropic/claude-sonnet-5", env={})
    assert "provider.vllm.npm" in str(exc.value)


def test_unseeded_ollama_npm_override_is_refused():
    env = {ENV_BASE_URL: "http://dgx:11434", "OPENCODE_PROVIDER_OLLAMA_NPM": "ollama-ai-provider"}
    with pytest.raises(OpenCodeProviderConfigError) as exc:
        apply_provider_config(dict(_BASE), model="ollama/gemma4:12b", env=env)
    assert "OPENCODE_PROVIDER_OLLAMA_NPM" in str(exc.value)


def test_any_openai_compatible_endpoint_is_a_block():
    env = {
        "OPENCODE_PROVIDER_LMSTUDIO_BASE_URL": "http://lab:1234/v1/",
        "OPENCODE_PROVIDER_LMSTUDIO_API_KEY": "lab-key",
        "OPENCODE_PROVIDER_LMSTUDIO_MODELS": "lmstudio/qwen3:8b, phi-4",
    }
    emitted = apply_provider_config(dict(_BASE), model="lmstudio/qwen3:8b", env=env)
    assert emitted["provider"] == {
        "lmstudio": {
            "npm": OPENAI_COMPATIBLE_NPM,
            "options": {"baseURL": "http://lab:1234/v1", "apiKey": "lab-key"},
            "models": {"qwen3:8b": {}, "phi-4": {}},
        }
    }


def test_a_custom_provider_without_a_base_url_is_a_named_failure():
    with pytest.raises(OpenCodeProviderConfigError) as exc:
        apply_provider_config(
            dict(_BASE), model="lab/x", env={"OPENCODE_PROVIDER_LAB_MODELS": "x"}
        )
    assert "OPENCODE_PROVIDER_LAB_BASE_URL" in str(exc.value)


def test_a_bad_endpoint_url_names_its_variable():
    with pytest.raises(OpenCodeProviderConfigError) as exc:
        apply_provider_config(
            dict(_BASE), model="lab/x", env={"OPENCODE_PROVIDER_LAB_BASE_URL": "lab:1234"}
        )
    assert "OPENCODE_PROVIDER_LAB_BASE_URL" in str(exc.value)


def test_an_optional_openrouter_block_pins_a_proxy_or_declares_models():
    proxied = apply_provider_config(
        dict(_BASE), model="openrouter/z-ai/glm-5",
        env={"OPENCODE_PROVIDER_OPENROUTER_BASE_URL": "https://proxy.example.com/api/v1"},
    )
    assert proxied["provider"]["openrouter"] == {
        "npm": OPENAI_COMPATIBLE_NPM,
        "options": {"baseURL": "https://proxy.example.com/api/v1"},
        "models": {"z-ai/glm-5": {}},
    }
    declared = apply_provider_config(
        dict(_BASE), model="openrouter/z-ai/glm-5",
        env={"OPENCODE_PROVIDER_OPENROUTER_MODELS": "qwen/qwen3-coder"},
    )
    # Built-in: no npm, no baseURL — OpenCode resolves the endpoint itself.
    assert declared["provider"]["openrouter"] == {
        "models": {"z-ai/glm-5": {}, "qwen/qwen3-coder": {}},
    }


def test_canonical_name_wins_over_the_legacy_alias_and_models_union():
    env = {
        "OPENCODE_PROVIDER_OLLAMA_BASE_URL": "http://canonical:11434",
        ENV_BASE_URL: "http://alias:11434",
        "OPENCODE_PROVIDER_OLLAMA_MODELS": "a:1b",
        "OPENCODE_OLLAMA_MODELS": "b:2b",
    }
    entry = apply_provider_config(dict(_BASE), model="ollama/a:1b", env=env)["provider"]["ollama"]
    assert entry["options"]["baseURL"] == "http://canonical:11434/v1"
    assert entry["models"] == {"a:1b": {}, "b:2b": {}}


def test_platform_base_url_sits_below_every_agent_declaration_and_above_the_gateway(tmp_path):
    route = str(_route_file(tmp_path, _ROUTE_WITH_DEFAULT))
    platform_only = apply_provider_config(
        dict(_BASE), model="ollama/g:1b", env={ENV_PLATFORM_BASE_URL: "http://platform:11434"},
        route_path=route,
    )
    assert platform_only["provider"]["ollama"]["options"]["baseURL"] == "http://platform:11434/v1"
    agent_wins = apply_provider_config(
        dict(_BASE), model="ollama/g:1b",
        env={ENV_PLATFORM_BASE_URL: "http://platform:11434", ENV_BASE_URL: "http://agent:11434"},
        route_path=route,
    )
    assert agent_wins["provider"]["ollama"]["options"]["baseURL"] == "http://agent:11434/v1"
    # And the boundary guard counts the platform tier as a URL.
    require_ollama_base_url(
        "ollama/g:1b", env={ENV_PLATFORM_BASE_URL: "http://platform:11434"},
        route_path=str(_routeless(tmp_path)),
    )


def test_provider_env_grammar():
    assert provider_env({
        "OPENCODE_PROVIDER_MY_LAB_BASE_URL": "http://x/v1",
        "OPENCODE_PROVIDER_MY_LAB_API_KEY": "k",
        "OPENCODE_PROVIDER_BLANK_BASE_URL": "  ",
        "OPENCODE_PROVIDER_TYPO_BASEURL": "http://x",
        "UNRELATED": "1",
    }) == {"my_lab": {"BASE_URL": "http://x/v1", "API_KEY": "k"}}


def test_configured_models_lists_every_provider():
    config = {"provider": {"ollama": {"models": {"g:1b": {}}}, "lmstudio": {"models": {"q": {}}}}}
    assert configured_models(config) == ["lmstudio/q", "ollama/g:1b"]
    assert configured_models(dict(_BASE)) == []


def test_get_model_lists_the_configured_models(monkeypatch):
    """`GET /api/model` for an OpenCode agent returns the generated provider
    map, not `[]` (Abilityai/lilu#653 item 3)."""
    from agent_server.routers import chat
    from agent_server.services import execution_env

    monkeypatch.setattr(execution_env, "build_execution_env", lambda *a, **kw: {
        "AGENT_RUNTIME_MODEL": "openrouter/z-ai/glm-5",
        ENV_BASE_URL: "http://dgx:11434",
        "OPENCODE_OLLAMA_MODELS": "gemma4:12b",
    })
    monkeypatch.setattr(opencode_runtime, "_load_shared_config", lambda: dict(_BASE))
    monkeypatch.setattr(chat.agent_state, "current_model", None, raising=False)
    surface = chat._model_surface("opencode")
    assert surface["available_models"] == ["openrouter/z-ai/glm-5", "ollama/gemma4:12b"]


def test_get_model_degrades_to_empty_on_an_unbuildable_provider(monkeypatch):
    from agent_server.routers import chat
    from agent_server.services import execution_env

    monkeypatch.setattr(execution_env, "build_execution_env", lambda *a, **kw: {
        "OPENCODE_PROVIDER_LAB_MODELS": "x",
    })
    monkeypatch.setattr(opencode_runtime, "_load_shared_config", lambda: dict(_BASE))
    monkeypatch.setattr(chat.agent_state, "current_model", None, raising=False)
    assert chat._model_surface("opencode")["available_models"] == []


# ===========================================================================
# Platform OpenCode provider tier — backend half (Abilityai/lilu#653)
#
# * `openrouter_api_key` is an ent#435 secret: encrypted at rest, refused in
#   cleartext at the sink, written only through its dedicated route.
# * The platform OpenRouter key and default Ollama base URL are injected into
#   the container env of `runtime == opencode` agents ONLY, by ONE writer
#   (`settings_service.apply_opencode_provider_env`) that create and both
#   rebuild paths call.
# * A Claude (or Gemini/Codex) agent's env is BYTE-IDENTICAL with the feature
#   configured or not.
# No network, no Docker: the conftest's throwaway SQLite only.
# ===========================================================================

import ast  # noqa: E402
import copy  # noqa: E402
import importlib  # noqa: E402
import os  # noqa: E402
import types  # noqa: E402
from pathlib import Path  # noqa: E402

os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", "ab" * 32)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_KEYS = ("openrouter_api_key", "opencode_default_provider_base_url")


def _live(name):
    """Resolve at call time — sibling harnesses re-import these modules."""
    return importlib.import_module(name)


@pytest.fixture()
def _platform_clean(monkeypatch):
    from services.secret_settings import encrypted_key_for

    def purge():
        db = _live("database").db
        for key in _KEYS:
            db.delete_setting(key)
            db.delete_setting(encrypted_key_for(key))
    for env in ("OPENROUTER_API_KEY", "OPENCODE_DEFAULT_PROVIDER_BASE_URL"):
        monkeypatch.delenv(env, raising=False)
    purge()
    yield
    purge()


def _configure_platform_tier():
    ss = _live("services.settings_service")
    ss.set_secret_setting("openrouter_api_key", "placeholder-openrouter-key")
    _live("database").db.set_setting("opencode_default_provider_base_url", "http://ollama-host:11434")


# ---------------------------------------------------------------------------
# The secret
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_platform_clean")
def test_openrouter_key_is_an_encrypted_secret_setting():
    from services.secret_settings import SECRET_SETTING_KEYS, encrypted_key_for, looks_like_envelope

    assert "openrouter_api_key" in SECRET_SETTING_KEYS
    _configure_platform_tier()
    db = _live("database").db
    assert db.get_setting_value("openrouter_api_key", None) is None
    assert looks_like_envelope(db.get_setting_value(encrypted_key_for("openrouter_api_key")))
    assert _live("services.settings_service").settings_service.get_openrouter_api_key() == "placeholder-openrouter-key"


@pytest.mark.usefixtures("_platform_clean")
def test_openrouter_key_is_refused_in_cleartext_at_the_sink():
    from services.secret_settings import SecretSettingWriteError

    with pytest.raises(SecretSettingWriteError):
        _live("database").db.set_setting("openrouter_api_key", "cleartext")


@pytest.mark.usefixtures("_platform_clean")
def test_env_is_the_fallback_tier(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    monkeypatch.setenv("OPENCODE_DEFAULT_PROVIDER_BASE_URL", "http://env-host:11434")
    assert _live("services.settings_service").get_opencode_provider_env() == {
        "OPENROUTER_API_KEY": "from-env",
        "OPENCODE_DEFAULT_PROVIDER_BASE_URL": "http://env-host:11434",
    }


# ---------------------------------------------------------------------------
# Injection — opencode only, and nothing else moves
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_platform_clean")
def test_opencode_agent_gets_the_platform_tier():
    _configure_platform_tier()
    env = {"AGENT_NAME": "a1", "AGENT_RUNTIME": "opencode"}
    _live("services.settings_service").apply_opencode_provider_env(env, "opencode")
    assert env["OPENROUTER_API_KEY"] == "placeholder-openrouter-key"
    assert env["OPENCODE_DEFAULT_PROVIDER_BASE_URL"] == "http://ollama-host:11434"


@pytest.mark.usefixtures("_platform_clean")
def test_unset_settings_plant_no_empty_key():
    env = {"AGENT_RUNTIME": "opencode"}
    _live("services.settings_service").apply_opencode_provider_env(env, "opencode")
    assert "OPENROUTER_API_KEY" not in env
    assert "OPENCODE_DEFAULT_PROVIDER_BASE_URL" not in env


@pytest.mark.usefixtures("_platform_clean")
def test_a_removed_setting_stops_applying_on_recreate():
    """Recreate replays the OLD container env; the tier is re-derived, so a
    deleted platform key does not survive in a rebuilt opencode container."""
    env = {"AGENT_RUNTIME": "opencode", "OPENROUTER_API_KEY": "stale",
           "OPENCODE_DEFAULT_PROVIDER_BASE_URL": "http://stale:11434"}
    _live("services.settings_service").apply_opencode_provider_env(env, "opencode")
    assert "OPENROUTER_API_KEY" not in env
    assert "OPENCODE_DEFAULT_PROVIDER_BASE_URL" not in env


@pytest.mark.usefixtures("_platform_clean")
@pytest.mark.parametrize("runtime", [None, "", "claude-code", "claude", "gemini-cli", "codex"])
def test_non_opencode_env_is_byte_identical(runtime):
    """The dispatch's assertion: with the platform tier FULLY configured, a
    Claude agent's env (and every other non-OpenCode runtime's) is identical,
    key for key and byte for byte — including a pre-existing value under one
    of the tier's own names, which is not this writer's to touch."""
    _configure_platform_tier()
    env = {
        "AGENT_NAME": "a1",
        "ANTHROPIC_API_KEY": "sk-ant-api-placeholder",
        "AGENT_RUNTIME": runtime or "claude-code",
        "AGENT_RUNTIME_MODEL": "sonnet",
        "OPENROUTER_API_KEY": "agent-own-value",
    }
    before = copy.deepcopy(env)
    _live("services.settings_service").apply_opencode_provider_env(env, runtime)
    assert env == before
    assert repr(sorted(env.items())).encode() == repr(sorted(before.items())).encode()


@pytest.mark.usefixtures("_platform_clean")
def test_build_base_env_for_claude_is_unchanged_by_the_feature(monkeypatch):
    """Create path, Claude: identical env with the tier configured vs not."""
    crud = _live("services.agent_service.crud")
    models = _live("models")
    config = models.AgentConfig(name="claude-a", runtime="claude-code")

    without = crud._build_base_env(copy.deepcopy(config))
    _configure_platform_tier()
    with_tier = crud._build_base_env(copy.deepcopy(config))
    assert with_tier == without
    assert "OPENROUTER_API_KEY" not in with_tier
    assert "OPENCODE_DEFAULT_PROVIDER_BASE_URL" not in with_tier


@pytest.mark.usefixtures("_platform_clean")
def test_build_base_env_for_opencode_carries_the_tier():
    crud = _live("services.agent_service.crud")
    models = _live("models")
    _configure_platform_tier()
    env = crud._build_base_env(models.AgentConfig(name="oc-a", runtime="opencode"))
    assert env["OPENROUTER_API_KEY"] == "placeholder-openrouter-key"
    assert env["OPENCODE_DEFAULT_PROVIDER_BASE_URL"] == "http://ollama-host:11434"


def _calls_in(path: Path, function: str) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function:
            return {
                c.func.id for c in ast.walk(node)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            }
    raise AssertionError(f"{function} not found in {path}")


@pytest.mark.usefixtures("_platform_clean")
@pytest.mark.parametrize("path,function", [
    ("services/agent_service/crud.py", "_build_base_env"),
    ("services/agent_service/lifecycle.py", "recreate_container_with_updated_config"),
    ("services/agent_service/lifecycle.py", "_apply_persisted_auth_env"),
])
def test_every_container_env_writer_re_derives_the_tier(path, function):
    """Create, recreate and rebuild-recovery all call the one writer — the
    same three seams the platform ANTHROPIC_API_KEY follows."""
    assert "apply_opencode_provider_env" in _calls_in(_BACKEND / path, function)


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------

def _client(role="admin"):
    pk = _live("routers.settings.provider_keys")
    app = FastAPI()
    app.include_router(pk.router, prefix="/api/settings")
    app.dependency_overrides[pk.get_current_user] = lambda: types.SimpleNamespace(
        id=1, username=role, role=role, email=f"{role}@example.com",
        agent_name=None, connector_agent=None, mcp_scope=None,
    )
    return TestClient(app, raise_server_exceptions=True)


@pytest.mark.usefixtures("_platform_clean")
@pytest.mark.parametrize("method,path,body", [
    ("get", "/api/settings/opencode-provider", None),
    ("put", "/api/settings/api-keys/openrouter", {"api_key": "k"}),
    ("delete", "/api/settings/api-keys/openrouter", None),
    ("put", "/api/settings/opencode-provider/base-url", {"base_url": "http://h:11434"}),
])
def test_endpoints_are_admin_only(method, path, body):
    kwargs = {"json": body} if body is not None else {}
    assert getattr(_client("user"), method)(path, **kwargs).status_code == 403


@pytest.mark.usefixtures("_platform_clean")
def test_put_get_delete_roundtrip_masks_the_key():
    client = _client()
    assert client.put("/api/settings/api-keys/openrouter",
                      json={"api_key": "placeholder-openrouter-key"}).status_code == 200
    assert client.put("/api/settings/opencode-provider/base-url",
                      json={"base_url": "http://ollama-host:11434"}).status_code == 200
    status = client.get("/api/settings/opencode-provider").json()
    assert status["openrouter"]["configured"] is True
    assert status["openrouter"]["source"] == "settings"
    assert "placeholder-openrouter-key" not in str(status)
    assert status["default_provider_base_url"] == "http://ollama-host:11434"

    assert client.delete("/api/settings/api-keys/openrouter").json()["deleted"] is True
    assert client.put("/api/settings/opencode-provider/base-url",
                      json={"base_url": ""}).json()["default_provider_base_url"] is None
    status = client.get("/api/settings/opencode-provider").json()
    assert status["openrouter"]["configured"] is False
    assert status["default_provider_base_url"] is None


@pytest.mark.usefixtures("_platform_clean")
@pytest.mark.parametrize("bad", ["ollama-host:11434", "ftp://h", "not a url"])
def test_bad_base_url_is_a_named_400(bad):
    resp = _client().put("/api/settings/opencode-provider/base-url", json={"base_url": bad})
    assert resp.status_code == 400
    assert "opencode_default_provider_base_url" in resp.json()["detail"]


@pytest.mark.usefixtures("_platform_clean")
def test_whitespace_key_is_a_named_400():
    resp = _client().put("/api/settings/api-keys/openrouter", json={"api_key": "a b"})
    assert resp.status_code == 400
    assert "openrouter_api_key" in resp.json()["detail"]
