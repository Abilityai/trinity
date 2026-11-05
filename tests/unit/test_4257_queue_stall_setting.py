"""The queue-stall threshold: default, bounds, and a named refusal (#4257).

The monitor's window is admin-tunable (`queue_stall_alert_minutes`, the #506
`system_settings` precedent — no migration). Two things must hold: the getter
is a clamp, not a trusting read, because it is the single read-through feeding
every tick; and the generic settings catch-all cannot write it, because an
unvalidated string there either silences the monitor or makes it file on every
tick.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

from services import queue_stall_service as qss  # noqa: E402
from services import settings_service as ss  # noqa: E402


def _stored(value):
    return patch.object(ss.settings_service, "get_setting", return_value=value)


def test_an_unset_threshold_is_thirty_minutes():
    with _stored(None):
        assert ss.get_queue_stall_alert_minutes() == 30


def test_a_configured_value_in_range_is_honoured():
    with _stored("120"):
        assert ss.get_queue_stall_alert_minutes() == 120


def test_a_value_below_the_floor_is_clamped_up_not_defaulted():
    """A stray `0` would make every tick file an alert for a row queued one
    second ago; clamping (not defaulting) keeps a configured-low intent."""
    with _stored("0"):
        assert ss.get_queue_stall_alert_minutes() == ss.QUEUE_STALL_ALERT_MINUTES_MIN


def test_a_value_above_the_ceiling_is_clamped_down():
    """A huge value would silence the monitor entirely."""
    with _stored("999999"):
        assert ss.get_queue_stall_alert_minutes() == ss.QUEUE_STALL_ALERT_MINUTES_MAX


def test_a_garbage_value_falls_back_to_the_default():
    with _stored("soon"):
        assert ss.get_queue_stall_alert_minutes() == 30


def test_a_settings_read_failure_never_breaks_the_sweep():
    with patch.object(ss.settings_service, "get_setting",
                      side_effect=RuntimeError("db down")):
        assert ss.get_queue_stall_alert_minutes() == 30


def test_the_service_reads_the_setting_through_the_getter():
    with patch.object(ss, "get_queue_stall_alert_minutes", return_value=77):
        assert qss.threshold_minutes() == 77


def test_the_service_falls_back_when_settings_is_unavailable():
    with patch.object(ss, "get_queue_stall_alert_minutes",
                      side_effect=RuntimeError("no settings")):
        assert qss.threshold_minutes() == qss.DEFAULT_STALL_MINUTES


def test_the_documented_range_is_five_to_a_day():
    assert (ss.QUEUE_STALL_ALERT_MINUTES_MIN,
            ss.QUEUE_STALL_ALERT_MINUTES_MAX) == (5, 1440)
    assert (qss.MIN_STALL_MINUTES, qss.MAX_STALL_MINUTES) == (5, 1440)


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


def test_the_generic_settings_catch_all_refuses_the_key_with_a_pointer():
    """Without this an admin can PUT `queue_stall_alert_minutes=0` through the
    generic route and the named validation is bypassed entirely."""
    src = (_BACKEND / "routers" / "settings" / "generic.py").read_text()
    tree = ast.parse(src)
    guarded = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name) and node.left.id == "key"
        and any(isinstance(c, ast.Name)
                and c.id == "QUEUE_STALL_ALERT_MINUTES_KEY"
                for c in node.comparators)
    ]
    assert guarded, "the generic PUT no longer blocks the threshold key"
    assert "PUT /api/settings/queue-stall-alert-minutes" in src


def test_the_dedicated_route_is_admin_gated_and_range_validated():
    src = (_BACKEND / "routers" / "settings" / "agent_defaults.py").read_text()
    tree = ast.parse(src)
    handlers = {
        node.name: node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    put = handlers["update_queue_stall_alert_minutes_setting"]
    body = ast.dump(put)

    # Invariant #8: `assert_admin` also rejects agent principals (#1890), which
    # is what keeps an agent's injected key off a fleet-wide knob.
    assert "assert_admin" in body
    assert "QUEUE_STALL_ALERT_MINUTES_MIN" in body
    assert "QUEUE_STALL_ALERT_MINUTES_MAX" in body
    # Named validation, not a generic 500/422 (Product Quality Bar).
    assert "queue_stall_alert_minutes must be an integer between" in src
    assert "update_queue_stall_alert_minutes_setting" in handlers
    assert "get_queue_stall_alert_minutes_setting" in handlers


def test_the_static_routes_precede_the_key_catch_all():
    """Invariant #4: declared after `/{key}`, the GET would answer
    "setting 'queue-stall-alert-minutes' not found"."""
    init = (_BACKEND / "routers" / "settings" / "__init__.py").read_text()
    assert "generic" in init
    # The dedicated routes live on a module included before the generic one.
    agent_defaults_at = init.index("agent_defaults")
    generic_at = init.rindex("generic")
    assert agent_defaults_at < generic_at
