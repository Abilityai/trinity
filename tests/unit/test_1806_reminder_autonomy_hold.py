"""
Regression for #1806 — a reminder held by the autonomy gate must say so.

The scheduler's `get_active_reminders` filters `ao.autonomy_enabled = 1`, so a
reminder on a paused agent is never armed: no job, therefore no skip line, and
the API reported a bare `pending` next to a `fire_at` that quietly slid into the
past. These tests pin the derived `autonomy_hold` flag that makes the state
legible.

The gate itself is deliberate (a paused agent must not wake itself) and is NOT
changed here — only its visibility.

`routers.reminders` is imported lazily inside each test, matching the
convention in `tests/unit/test_deploy_writable_templates.py`: importing the
router chain at collection time trips the documented
tests/utils-shadows-backend-utils sys.modules race. Monkeypatching module
attributes (never `sys.modules[...] =`) keeps `tests/lint_sys_modules.py` green.
"""

from __future__ import annotations

import pytest


def _row(status: str = "pending", agent: str = "a1") -> dict:
    return {
        "id": "rem_x",
        "agent_name": agent,
        "message": "check the PR",
        "fire_at": "2026-07-27T10:43:51Z",
        "status": status,
        "created_at": "2026-07-27T10:42:51Z",
    }


@pytest.mark.parametrize("status", ["pending", "firing"])
def test_live_reminder_on_paused_agent_is_flagged(monkeypatch, status):
    """The whole point: live reminder + autonomy off ⇒ autonomy_hold True."""
    from routers import reminders

    monkeypatch.setattr(reminders.db, "get_autonomy_enabled", lambda name: False)
    assert reminders._autonomy_hold("a1", status) is True


@pytest.mark.parametrize("status", ["pending", "firing"])
def test_live_reminder_on_active_agent_is_not_flagged(monkeypatch, status):
    from routers import reminders

    monkeypatch.setattr(reminders.db, "get_autonomy_enabled", lambda name: True)
    assert reminders._autonomy_hold("a1", status) is False


@pytest.mark.parametrize("status", ["fired", "cancelled", "failed"])
def test_terminal_reminder_is_never_flagged(monkeypatch, status):
    """A terminal reminder is not waiting on anything — flagging it would be a lie.

    Also asserts the autonomy lookup is skipped entirely for terminal rows.
    """
    from routers import reminders

    def _boom(name):  # pragma: no cover - must not be reached
        raise AssertionError("autonomy must not be resolved for a terminal reminder")

    monkeypatch.setattr(reminders.db, "get_autonomy_enabled", _boom)
    assert reminders._autonomy_hold("a1", status) is False


def test_autonomy_read_failure_fails_open(monkeypatch):
    """A settings-read failure must not invent a warning on a healthy reminder."""
    from routers import reminders

    def _boom(name):
        raise RuntimeError("redis down")

    monkeypatch.setattr(reminders.db, "get_autonomy_enabled", _boom)
    assert reminders._autonomy_hold("a1", "pending") is False


def test_precomputed_autonomy_is_used_without_a_lookup(monkeypatch):
    """The list path resolves autonomy once per page, not once per row."""
    from routers import reminders

    def _boom(name):  # pragma: no cover - must not be reached
        raise AssertionError("precomputed autonomy should short-circuit the lookup")

    monkeypatch.setattr(reminders.db, "get_autonomy_enabled", _boom)
    assert reminders._autonomy_hold("a1", "pending", autonomy_enabled=False) is True
    assert reminders._autonomy_hold("a1", "pending", autonomy_enabled=True) is False


def test_response_model_carries_the_flag(monkeypatch):
    """_reminder_response is the shared JSON shaper — it must include the flag."""
    from routers import reminders

    monkeypatch.setattr(reminders.db, "get_autonomy_enabled", lambda name: False)
    payload = reminders._reminder_response(_row())
    assert payload["autonomy_hold"] is True


def test_model_defaults_to_not_held():
    """Any construction path that omits the flag must default to the safe value."""
    from models import Reminder, ReminderSummary

    assert ReminderSummary(**_row()).autonomy_hold is False
    assert Reminder(**_row()).autonomy_hold is False
