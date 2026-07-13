"""#1557 — disabling autonomy must NOT touch the circuit breaker.

Turning an agent's autonomy off used to call
``force_circuit_dormant(reason="autonomy_disabled")``, parking the *transport*
circuit breaker in ``dormant``. The ``execute_task`` gate consults that breaker
for **every** trigger, so a healthy-but-paused agent fast-failed all inbound
chat (manual / Telegram / Slack / public) with "circuit breaker open — agent is
unhealthy" — without ever being contacted. Autonomy governs *proactive* work
only (schedules), so the two must be decoupled.

Two guards here, both of which fail against the pre-#1557 source:

1. **Structural** — ``autonomy.py`` no longer writes the breaker, and still
   disables schedules (the real, and only, proactive-suppression mechanism).
   This is the direct regression guard: the old code contained
   ``force_circuit_dormant`` and the test would fail on it.

2. **Message honesty** — the fast-fail reason now names *which* breaker fired
   (transport = unreachable, dispatch = auth-dead) instead of a blanket
   "unhealthy", while every branch keeps the substring ``circuit breaker open``
   that ``tests/integration/test_1560_breaker_lifecycle.py`` asserts on the
   transport path.
"""

from __future__ import annotations

from pathlib import Path

from services.task_execution_service import _circuit_breaker_error

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_AUTONOMY_SRC = (_BACKEND / "services" / "agent_service" / "autonomy.py").read_text(
    encoding="utf-8"
)


# ---------------------------------------------------------------------------
# Structural: autonomy is decoupled from the breaker (regression guard)
# ---------------------------------------------------------------------------


def test_autonomy_toggle_never_forces_the_circuit_dormant():
    """The #1557 defect: an autonomy-off hook wrote the transport breaker."""
    assert "force_circuit_dormant" not in _AUTONOMY_SRC, (
        "disabling autonomy must not park the circuit breaker — that conflates "
        '"administratively paused" with "transport unhealthy" and fast-fails '
        "all inbound chat on a healthy agent (#1557)"
    )


def test_autonomy_toggle_never_resets_the_circuit_either():
    """The symmetric enable-path reset went too — it would wipe a legitimately
    open breaker protecting a genuinely wedged agent, and #1560's
    ``clear_agent_breakers`` already clears on every real (re)start."""
    assert "reset_circuit" not in _AUTONOMY_SRC


def test_autonomy_still_suppresses_proactive_work_via_schedules():
    """Guard against over-deletion: pausing must still disable schedules — that
    is how proactive work is actually stopped, independent of the breaker."""
    assert "set_schedule_enabled" in _AUTONOMY_SRC


# ---------------------------------------------------------------------------
# Message honesty: name the breaker that fired, keep the pinned substring
# ---------------------------------------------------------------------------


def test_transport_open_message_says_unreachable():
    msg = _circuit_breaker_error(transport_open=True, dispatch_open=False)
    assert "unreachable" in msg.lower()
    assert "transport" in msg.lower()
    assert "unhealthy" not in msg.lower()  # the old lie is gone


def test_dispatch_open_message_says_auth():
    msg = _circuit_breaker_error(transport_open=False, dispatch_open=True)
    assert "auth" in msg.lower()
    assert "dispatch" in msg.lower()


def test_both_open_message_names_both_breakers():
    msg = _circuit_breaker_error(transport_open=True, dispatch_open=True)
    assert "transport" in msg.lower() and "dispatch" in msg.lower()


def test_every_branch_keeps_the_pinned_substring():
    """``tests/integration/test_1560_breaker_lifecycle.py`` asserts
    ``"circuit breaker open" in denied.text.lower()`` on the transport path, and
    consumers may match it — every branch must preserve it."""
    for transport_open, dispatch_open in [(True, False), (False, True), (True, True)]:
        msg = _circuit_breaker_error(transport_open, dispatch_open)
        assert "circuit breaker open" in msg.lower(), (transport_open, dispatch_open)
