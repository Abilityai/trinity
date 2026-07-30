"""Unit tests for #1871: agent container logs are bounded by a json-file
max-size/max-file cap, operator-tunable via AGENT_LOG_MAX_SIZE / AGENT_LOG_MAX_FILE.

Docker's json-file driver ships with no cap, so every container log grew forever
under /var/lib/docker/containers/ until the Docker data root hit 100% and dockerd
wedged (2026-07-27). Compose's `logging:` block covers the platform services;
agent containers are SDK-created and need this constant instead.

The validation is fail-safe in BOTH directions and that is the load-bearing
property here: a malformed value *and* a well-formed but absurd one ("1000g",
max-file "9999") must fall back to the bounded default, because either would
leave the cap effectively disabled — the exact failure the constant prevents.

Loaded by file path (stdlib-only) so the test doesn't drag the
docker / fastapi / database transitive imports of the agent_service package.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_CAPS_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "src" / "backend" / "services" / "agent_service" / "capabilities.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("caps_logcfg_under_test", _CAPS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- _resolve_agent_log_max_size (call-time env read) -------------------

def test_size_default_when_unset(monkeypatch):
    monkeypatch.delenv("AGENT_LOG_MAX_SIZE", raising=False)
    assert _load()._resolve_agent_log_max_size() == "10m"


@pytest.mark.parametrize("value", ["1k", "512k", "10m", "50m", "100m", "1g"])
def test_size_valid_values_pass_through(monkeypatch, value):
    monkeypatch.setenv("AGENT_LOG_MAX_SIZE", value)
    assert _load()._resolve_agent_log_max_size() == value


def test_size_case_folds_and_strips(monkeypatch):
    monkeypatch.setenv("AGENT_LOG_MAX_SIZE", "  20M ")
    assert _load()._resolve_agent_log_max_size() == "20m"


@pytest.mark.parametrize(
    "bad", ["10", "10Mi", "10MB", "0.5g", "m", "abc", "-1m", "", "   ", "10 m"]
)
def test_size_malformed_falls_back_to_default(monkeypatch, bad):
    monkeypatch.setenv("AGENT_LOG_MAX_SIZE", bad)
    assert _load()._resolve_agent_log_max_size() == "10m"


@pytest.mark.parametrize("zero", ["0k", "0m", "0g"])
def test_size_zero_rejected(monkeypatch, zero):
    """A zero cap means "never roll" — it disables the control silently."""
    monkeypatch.setenv("AGENT_LOG_MAX_SIZE", zero)
    assert _load()._resolve_agent_log_max_size() == "10m"


@pytest.mark.parametrize("huge", ["2g", "1000g", "1025m", "9999999k"])
def test_size_over_ceiling_falls_back_to_default(monkeypatch, huge):
    """THE #1871 magnitude guard: these are well-formed and would pass a
    format-only regex, yet they effectively remove the cap. A fat-finger
    ("1000m" for "100m") must not silently disable a disk-safety control."""
    monkeypatch.setenv("AGENT_LOG_MAX_SIZE", huge)
    assert _load()._resolve_agent_log_max_size() == "10m"


def test_size_ceiling_boundary_is_inclusive(monkeypatch):
    """1g is exactly the ceiling and must be accepted (off-by-one guard)."""
    monkeypatch.setenv("AGENT_LOG_MAX_SIZE", "1024m")
    assert _load()._resolve_agent_log_max_size() == "1024m"


# --- _resolve_agent_log_max_file ---------------------------------------

def test_file_default_when_unset(monkeypatch):
    monkeypatch.delenv("AGENT_LOG_MAX_FILE", raising=False)
    assert _load()._resolve_agent_log_max_file() == "3"


@pytest.mark.parametrize("value", ["1", "2", "3", "5", "10"])
def test_file_valid_values_pass_through(monkeypatch, value):
    monkeypatch.setenv("AGENT_LOG_MAX_FILE", value)
    assert _load()._resolve_agent_log_max_file() == value


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "", "3.5", "03x", "  "])
def test_file_malformed_falls_back_to_default(monkeypatch, bad):
    monkeypatch.setenv("AGENT_LOG_MAX_FILE", bad)
    assert _load()._resolve_agent_log_max_file() == "3"


@pytest.mark.parametrize("huge", ["11", "100", "9999"])
def test_file_over_ceiling_falls_back_to_default(monkeypatch, huge):
    monkeypatch.setenv("AGENT_LOG_MAX_FILE", huge)
    assert _load()._resolve_agent_log_max_file() == "3"


# --- warning on an explicitly-set bad value ----------------------------

def test_rejected_value_warns(monkeypatch, capsys):
    """An ignored knob must be discoverable — silently dropping it is the
    inert-by-obscurity class (#1039) this issue itself cites.

    `capsys`, not `caplog`: the rejection is reported with
    `print(..., flush=True)` because AGENT_LOG_CONFIG resolves at IMPORT time,
    before `lifespan` calls `setup_logging()` (#858). A `caplog` assertion would
    pass under pytest's injected handler while the real backend emitted an
    unstructured stderr line — the test would be blind to the thing it checks.
    """
    monkeypatch.setenv("AGENT_LOG_MAX_SIZE", "1000g")
    _load()._resolve_agent_log_max_size()
    out = capsys.readouterr().out
    assert "AGENT_LOG_MAX_SIZE" in out and "1000g" in out, out


def test_unset_value_does_not_warn(monkeypatch, capsys):
    """The common case (variable never set) is not a misconfiguration."""
    monkeypatch.delenv("AGENT_LOG_MAX_SIZE", raising=False)
    monkeypatch.delenv("AGENT_LOG_MAX_FILE", raising=False)
    module = _load()
    module._resolve_agent_log_max_size()
    module._resolve_agent_log_max_file()
    assert "AGENT_LOG_MAX" not in capsys.readouterr().out


def test_unknown_unit_falls_back(monkeypatch):
    """Defence-in-depth for the regex/unit-table coupling: if a future edit
    widens the regex without extending _LOG_SIZE_UNIT_BYTES, the resolver must
    return the bounded default rather than raising KeyError at import time and
    crash-looping backend boot."""
    module = _load()
    monkeypatch.setattr(module, "_AGENT_LOG_MAX_SIZE_RE", __import__("re").compile(r"^(\d+)([kmgt])$"))
    monkeypatch.setenv("AGENT_LOG_MAX_SIZE", "5t")   # 't' matches the regex, absent from the table
    assert module._resolve_agent_log_max_size() == "10m"


# --- AGENT_LOG_CONFIG (import-time spec) -------------------------------

def test_log_config_default_shape(monkeypatch):
    monkeypatch.delenv("AGENT_LOG_MAX_SIZE", raising=False)
    monkeypatch.delenv("AGENT_LOG_MAX_FILE", raising=False)
    assert _load().AGENT_LOG_CONFIG == {
        "type": "json-file",
        "config": {"max-size": "10m", "max-file": "3"},
    }


def test_log_config_honors_env(monkeypatch):
    monkeypatch.setenv("AGENT_LOG_MAX_SIZE", "50m")
    monkeypatch.setenv("AGENT_LOG_MAX_FILE", "5")
    assert _load().AGENT_LOG_CONFIG == {
        "type": "json-file",
        "config": {"max-size": "50m", "max-file": "5"},
    }


def test_log_config_is_never_uncapped(monkeypatch):
    """The security-equivalent of test_1231's "flags always present": whatever
    the operator sets, the emitted config always carries BOTH bounds."""
    for size, count in (("garbage", "garbage"), ("1000g", "0"), ("", ""), ("0m", "99")):
        monkeypatch.setenv("AGENT_LOG_MAX_SIZE", size)
        monkeypatch.setenv("AGENT_LOG_MAX_FILE", count)
        cfg = _load().AGENT_LOG_CONFIG
        assert cfg["type"] == "json-file"
        assert cfg["config"]["max-size"], "max-size must never be empty/absent"
        assert cfg["config"]["max-file"], "max-file must never be empty/absent"
        # and must be within the documented ceilings
        assert cfg["config"]["max-size"].endswith(("k", "m", "g"))
        assert 1 <= int(cfg["config"]["max-file"]) <= 10
