"""Unit tests for the #506 max_parallel_tasks ceiling helpers.

Covers the getter's parse/error fallback and the clamp helpers, in isolation
from the settings DB (the `get_setting` call is monkeypatched). Mirrors the
src/backend-on-path bootstrap used by the other unit tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Bootstrap src/backend on sys.path (mirrors test_capacity_manager.py).
_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
for _shadow in ("utils", "utils.api_client", "utils.assertions", "utils.cleanup"):
    sys.modules.pop(_shadow, None)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytestmark = pytest.mark.unit


@pytest.fixture
def ss():
    from services import settings_service as _ss
    return _ss


def _patch_raw(monkeypatch, ss, value):
    """Stub the underlying settings_service.get_setting to return `value`."""
    monkeypatch.setattr(ss.settings_service, "get_setting", lambda *_a, **_kw: value)


class TestCeilingGetter:
    def test_absent_returns_default(self, ss, monkeypatch):
        _patch_raw(monkeypatch, ss, None)
        assert ss.get_max_parallel_tasks_ceiling() == ss.MAX_PARALLEL_TASKS_CEILING_DEFAULT

    def test_valid_string_parsed(self, ss, monkeypatch):
        _patch_raw(monkeypatch, ss, "7")
        assert ss.get_max_parallel_tasks_ceiling() == 7

    def test_garbage_string_falls_back_to_default(self, ss, monkeypatch):
        _patch_raw(monkeypatch, ss, "not-a-number")
        assert ss.get_max_parallel_tasks_ceiling() == ss.MAX_PARALLEL_TASKS_CEILING_DEFAULT

    def test_read_exception_fails_open_to_default(self, ss, monkeypatch):
        def _boom(*_a, **_kw):
            raise RuntimeError("db down")
        monkeypatch.setattr(ss.settings_service, "get_setting", _boom)
        assert ss.get_max_parallel_tasks_ceiling() == ss.MAX_PARALLEL_TASKS_CEILING_DEFAULT

    def test_below_min_clamps_to_min_not_fail_closed(self, ss, monkeypatch):
        """#506 defense-in-depth: a stray persisted '0' must clamp to MIN, never
        return 0 — else min(stored, 0) == 0 fail-closes the whole fleet.
        """
        _patch_raw(monkeypatch, ss, "0")
        assert ss.get_max_parallel_tasks_ceiling() == ss.MAX_PARALLEL_TASKS_CEILING_MIN

    def test_negative_clamps_to_min(self, ss, monkeypatch):
        _patch_raw(monkeypatch, ss, "-5")
        assert ss.get_max_parallel_tasks_ceiling() == ss.MAX_PARALLEL_TASKS_CEILING_MIN

    def test_above_max_clamps_to_max_not_host_saturation(self, ss, monkeypatch):
        """A stray persisted '999' must clamp to MAX so it can't silently defeat
        the host-protection cap.
        """
        _patch_raw(monkeypatch, ss, "999")
        assert ss.get_max_parallel_tasks_ceiling() == ss.MAX_PARALLEL_TASKS_CEILING_MAX


class TestClampHelpers:
    def test_clamp_caps_above_ceiling(self, ss, monkeypatch):
        monkeypatch.setattr(ss, "get_max_parallel_tasks_ceiling", lambda: 2)
        assert ss.clamp_to_ceiling(5) == 2

    def test_clamp_leaves_below_ceiling(self, ss, monkeypatch):
        monkeypatch.setattr(ss, "get_max_parallel_tasks_ceiling", lambda: 10)
        assert ss.clamp_to_ceiling(3) == 3

    def test_effective_uses_stored_clamped(self, ss, monkeypatch):
        monkeypatch.setattr(ss, "get_max_parallel_tasks_ceiling", lambda: 4)
        import database
        monkeypatch.setattr(database.db, "get_max_parallel_tasks", lambda _name: 9)
        assert ss.get_effective_max_parallel_tasks("alice") == 4
