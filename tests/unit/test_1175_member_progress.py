"""
Tests for member progress card (ent#1175).

The five steps (assistant_ready → trinity_being_prepared → trinity_ready →
first_agent_built → first_run) are stored as an ordered JSON array; each
step carries a key, label, state and optional note. A set_step call updates
state + note for one key and broadcasts a member_progress_update WS event.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


PROGRESS_DDL = """
CREATE TABLE IF NOT EXISTS enterprise_member_progress (
    user_id  INTEGER PRIMARY KEY,
    steps    TEXT    NOT NULL,
    updated_at TEXT  NOT NULL
)
"""


def _init_schema(conn):
    conn.execute(PROGRESS_DDL)
    conn.commit()


# ---------------------------------------------------------------------------
# import the module under test
# ---------------------------------------------------------------------------

# Modules this test registers in sys.modules — restored after each test so the
# path-loaded copy never leaks into other test files.
_STUBBED_MODULE_NAMES = [
    "enterprise.backend.member_agents.progress",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot sys.modules before each test and restore after."""
    import sys

    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _import_progress():
    """Import the enterprise progress module via its path.
    Returns the module, or skips the test when the module does not exist yet.
    """
    import importlib.util, sys
    from pathlib import Path

    # Resolve relative to repo root (two levels up from tests/)
    root = Path(__file__).parent.parent.parent
    path = root / "src/backend/enterprise/backend/member_agents/progress.py"

    spec = importlib.util.spec_from_file_location(
        "enterprise.backend.member_agents.progress",
        str(path),
    )
    if spec is None:
        pytest.skip("progress.py not yet written")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

class TestProgressStepsDefinition:
    """The STEPS constant must carry exactly 5 ordered entries."""

    def test_steps_count(self):
        mod = _import_progress()
        assert len(mod.STEPS) == 5

    def test_step_keys(self):
        mod = _import_progress()
        keys = [s["key"] for s in mod.STEPS]
        assert keys == [
            "assistant_ready",
            "trinity_being_prepared",
            "trinity_ready",
            "first_agent_built",
            "first_run",
        ]

    def test_steps_have_labels(self):
        mod = _import_progress()
        for step in mod.STEPS:
            assert step.get("label"), f"step {step['key']} is missing a label"

    def test_step_labels_are_plain_english(self):
        mod = _import_progress()
        for step in mod.STEPS:
            label = step["label"]
            # No ids, slugs, or underscores in display labels
            assert "_" not in label, f"slug in label: {label!r}"
            assert label[0].isupper(), f"label should start with a capital: {label!r}"


class TestInitProgress:
    """init_progress_sync creates a fresh record with all steps pending."""

    def test_creates_record(self, tmp_path, monkeypatch):
        mod = _import_progress()
        conn = _make_conn()
        _init_schema(conn)

        def _fake_conn():
            return conn

        monkeypatch.setattr(mod, "_get_conn", _fake_conn)
        mod.init_progress_sync(user_id=42)

        row = conn.execute(
            "SELECT steps FROM enterprise_member_progress WHERE user_id = 42"
        ).fetchone()
        assert row is not None
        steps = json.loads(row["steps"])
        assert len(steps) == 5
        assert all(s["state"] == "pending" for s in steps)

    def test_idempotent(self, monkeypatch):
        mod = _import_progress()
        conn = _make_conn()
        _init_schema(conn)
        monkeypatch.setattr(mod, "_get_conn", lambda: conn)

        mod.init_progress_sync(user_id=1)
        mod.init_progress_sync(user_id=1)  # second call must not raise

        count = conn.execute(
            "SELECT COUNT(*) FROM enterprise_member_progress WHERE user_id = 1"
        ).fetchone()[0]
        assert count == 1


class TestSetStep:
    """set_step_sync updates exactly one step by key."""

    def test_set_to_done(self, monkeypatch):
        mod = _import_progress()
        conn = _make_conn()
        _init_schema(conn)
        monkeypatch.setattr(mod, "_get_conn", lambda: conn)

        mod.init_progress_sync(user_id=5)
        mod.set_step_sync(user_id=5, step_key="assistant_ready", state="done")

        row = conn.execute(
            "SELECT steps FROM enterprise_member_progress WHERE user_id = 5"
        ).fetchone()
        steps = json.loads(row["steps"])
        step = next(s for s in steps if s["key"] == "assistant_ready")
        assert step["state"] == "done"

    def test_set_with_note(self, monkeypatch):
        mod = _import_progress()
        conn = _make_conn()
        _init_schema(conn)
        monkeypatch.setattr(mod, "_get_conn", lambda: conn)

        mod.init_progress_sync(user_id=7)
        mod.set_step_sync(
            user_id=7, step_key="trinity_being_prepared",
            state="in_progress", note="Taking 30-40 minutes"
        )

        steps = json.loads(
            conn.execute(
                "SELECT steps FROM enterprise_member_progress WHERE user_id = 7"
            ).fetchone()["steps"]
        )
        step = next(s for s in steps if s["key"] == "trinity_being_prepared")
        assert step["state"] == "in_progress"
        assert step["note"] == "Taking 30-40 minutes"

    def test_unknown_key_is_noop(self, monkeypatch):
        mod = _import_progress()
        conn = _make_conn()
        _init_schema(conn)
        monkeypatch.setattr(mod, "_get_conn", lambda: conn)

        mod.init_progress_sync(user_id=9)
        # Must not raise on an unknown key
        mod.set_step_sync(user_id=9, step_key="nonexistent_step", state="done")

    def test_missing_user_is_noop(self, monkeypatch):
        mod = _import_progress()
        conn = _make_conn()
        _init_schema(conn)
        monkeypatch.setattr(mod, "_get_conn", lambda: conn)

        # init not called for user 99
        mod.set_step_sync(user_id=99, step_key="assistant_ready", state="done")

    def test_other_steps_unchanged(self, monkeypatch):
        mod = _import_progress()
        conn = _make_conn()
        _init_schema(conn)
        monkeypatch.setattr(mod, "_get_conn", lambda: conn)

        mod.init_progress_sync(user_id=11)
        mod.set_step_sync(user_id=11, step_key="assistant_ready", state="done")

        steps = json.loads(
            conn.execute(
                "SELECT steps FROM enterprise_member_progress WHERE user_id = 11"
            ).fetchone()["steps"]
        )
        for step in steps:
            if step["key"] == "assistant_ready":
                assert step["state"] == "done"
            else:
                assert step["state"] == "pending"


class TestGetProgress:
    """get_progress returns the current steps list; None when no record."""

    def test_returns_none_when_absent(self, monkeypatch):
        mod = _import_progress()
        conn = _make_conn()
        _init_schema(conn)
        monkeypatch.setattr(mod, "_get_conn", lambda: conn)

        result = mod.get_progress(user_id=100)
        assert result is None

    def test_returns_steps(self, monkeypatch):
        mod = _import_progress()
        conn = _make_conn()
        _init_schema(conn)
        monkeypatch.setattr(mod, "_get_conn", lambda: conn)

        mod.init_progress_sync(user_id=20)
        result = mod.get_progress(user_id=20)
        assert result is not None
        assert len(result["steps"]) == 5
        assert result["all_done"] is False

    def test_all_done_flag(self, monkeypatch):
        mod = _import_progress()
        conn = _make_conn()
        _init_schema(conn)
        monkeypatch.setattr(mod, "_get_conn", lambda: conn)

        mod.init_progress_sync(user_id=21)
        for key in ["assistant_ready", "trinity_being_prepared", "trinity_ready",
                    "first_agent_built", "first_run"]:
            mod.set_step_sync(user_id=21, step_key=key, state="done")

        result = mod.get_progress(user_id=21)
        assert result["all_done"] is True

    def test_failed_step_not_all_done(self, monkeypatch):
        mod = _import_progress()
        conn = _make_conn()
        _init_schema(conn)
        monkeypatch.setattr(mod, "_get_conn", lambda: conn)

        mod.init_progress_sync(user_id=22)
        mod.set_step_sync(user_id=22, step_key="assistant_ready", state="done")
        mod.set_step_sync(user_id=22, step_key="trinity_being_prepared", state="failed",
                          note="Something went wrong. We'll try again shortly.")

        result = mod.get_progress(user_id=22)
        assert result["all_done"] is False


class TestWsEventShape:
    """The WS event emitted by build_progress_event is correct for routing."""

    def test_event_type(self):
        mod = _import_progress()
        steps = [{"key": "assistant_ready", "label": "Ready", "state": "done", "note": None}]
        event = mod.build_progress_event(agent_name="alice-ai", steps=steps)
        assert event["type"] == "member_progress_update"

    def test_event_agent_name(self):
        mod = _import_progress()
        steps = []
        event = mod.build_progress_event(agent_name="alice-ai", steps=steps)
        assert event["agent_name"] == "alice-ai"

    def test_event_steps(self):
        mod = _import_progress()
        steps = [{"key": "assistant_ready", "label": "Your assistant is ready",
                  "state": "done", "note": None}]
        event = mod.build_progress_event(agent_name="x", steps=steps)
        assert event["steps"] == steps
