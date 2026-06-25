"""Resource normalization for agent container limits (#1197).

A GitHub source repo's template.yaml carrying a fractional / Kubernetes-style
resources block (``cpu: "0.5"``, ``memory: "512Mi"``) used to abort agent
creation deep in container-create with an opaque
``ValueError: invalid literal for int() with base 10: '0.5'``. These tests pin
``normalize_cpu`` / ``normalize_memory`` — the guards now applied at all three
container-create sites — so the failure surfaces early with an actionable
message and the allowed value set can't silently drift.

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
    spec = importlib.util.spec_from_file_location("caps_resnorm_under_test", _CAPS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


caps = _load()


# --- CPU ----------------------------------------------------------------

@pytest.mark.parametrize("value", ["1", "2", "4", "8", "16", 4, 16])
def test_normalize_cpu_accepts_valid(value):
    assert caps.normalize_cpu(value, "2") == str(value)


def test_normalize_cpu_falls_back_to_default_on_empty():
    assert caps.normalize_cpu(None, "2") == "2"
    assert caps.normalize_cpu("", "4") == "4"


@pytest.mark.parametrize("bad", ["0.5", "0", "3", "32", "100m", "half"])
def test_normalize_cpu_rejects_invalid(bad):
    with pytest.raises(ValueError) as ei:
        caps.normalize_cpu(bad, "2")
    msg = str(ei.value)
    assert bad.strip() in msg          # echoes the offending value
    assert "must be one of" in msg     # actionable


def test_normalize_cpu_is_int_castable_after_normalize():
    # The whole point: every value normalize_cpu returns is int()-castable, so
    # the `int(cpu) * 1_000_000_000` NanoCpus line can never raise (#1197).
    for v in caps.VALID_CPU:
        assert isinstance(int(caps.normalize_cpu(v, "2")), int)


# --- Memory -------------------------------------------------------------

@pytest.mark.parametrize("value", ["1g", "2g", "4g", "8g", "16g", "32g"])
def test_normalize_memory_accepts_valid(value):
    assert caps.normalize_memory(value, "4g") == value


def test_normalize_memory_case_folds():
    assert caps.normalize_memory("4G", "4g") == "4g"
    assert caps.normalize_memory("  8G ", "4g") == "8g"


def test_normalize_memory_falls_back_to_default_on_empty():
    assert caps.normalize_memory(None, "4g") == "4g"
    assert caps.normalize_memory("", "8g") == "8g"


@pytest.mark.parametrize("bad", ["512Mi", "512m", "4", "4gb", "0.5g", "huge"])
def test_normalize_memory_rejects_invalid(bad):
    with pytest.raises(ValueError) as ei:
        caps.normalize_memory(bad, "4g")
    msg = str(ei.value)
    assert "must be one of" in msg


# --- drift guard --------------------------------------------------------

def test_value_sets_match_settings_router():
    """The canonical sets here must match what the admin defaults endpoint
    accepts (routers/settings.py reads VALID_CPU/VALID_MEMORY from here)."""
    assert caps.VALID_CPU == ("1", "2", "4", "8", "16")
    assert caps.VALID_MEMORY == ("1g", "2g", "4g", "8g", "16g", "32g")
