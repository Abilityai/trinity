"""`gap()` and the grammar helpers (trinity-enterprise#666, C2 pure half).

A table test, because this is the function every consumer's verdict word comes
out of and the three directions × three positions is exactly nine facts. The
`hold` arm is here in full: it is the one the framework grammar adds and the
one a reader guesses wrong, because `hold` has no good side to be on and so
never produces `behind` or `ahead`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
while _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)

pytest.importorskip("fastapi", reason="backend venv required")

from services import objective_join_service as svc  # noqa: E402


# ---------------------------------------------------------------------------
# The nine facts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("direction,target,actual,status", [
    ("up_good", 35, 30, "behind"),
    ("up_good", 35, 35, "on_target"),
    ("up_good", 35, 40, "ahead"),
    ("down_good", 35, 30, "ahead"),
    ("down_good", 35, 35, "on_target"),
    ("down_good", 35, 40, "behind"),
    ("hold", 35, 30, "off_target"),
    ("hold", 35, 35, "on_target"),
    ("hold", 35, 40, "off_target"),
])
def test_position_for_every_direction(direction, target, actual, status):
    result = svc.gap(target, actual, direction)
    assert result["status"] == status
    assert result["delta"] == actual - target
    assert result["reason"] is None


def test_hold_never_says_behind_or_ahead():
    """`hold` wants a value held, so there is no good side to be on."""
    for actual in (0, 34.9, 35, 35.1, 1000):
        assert svc.gap(35, actual, "hold")["status"] in ("on_target", "off_target")


def test_hold_honours_a_tolerance_band():
    """The framework's metric entry may carry `tolerance`; inside it is on target."""
    assert svc.gap(35, 36, "hold", tolerance=2)["status"] == "on_target"
    assert svc.gap(35, 33, "hold", tolerance=2)["status"] == "on_target"
    assert svc.gap(35, 38, "hold", tolerance=2)["status"] == "off_target"
    # Signed, even inside the band: the consumer may want the drift.
    assert svc.gap(35, 33, "hold", tolerance=2)["delta"] == -2


def test_a_negative_or_unusable_tolerance_is_read_as_exact():
    for tolerance in (None, "wide", float("nan"), True, [2]):
        assert svc.gap(35, 36, "hold", tolerance=tolerance)["status"] == "off_target"
    # A negative band is a typo, not a licence to widen.
    assert svc.gap(35, 36, "hold", tolerance=-2)["status"] == "on_target"


# ---------------------------------------------------------------------------
# Everything that cannot be computed says WHY
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("target,actual,direction,reason", [
    (None, 30, "up_good", "no_target"),
    ("soon", 30, "up_good", "non_numeric"),
    (35, None, "up_good", "no_points"),
    (35, "green", "up_good", "non_numeric"),
    (35, 30, None, "no_direction"),
    (35, 30, "sideways", "no_direction"),
])
def test_not_computable_names_its_reason(target, actual, direction, reason):
    result = svc.gap(target, actual, direction)
    assert result["status"] == "not_computable"
    assert result["reason"] == reason


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nan_and_inf_are_not_numbers(value):
    """YAML accepts `.nan` / `.inf`, the hardened loader passes them through,
    and `json.dumps` emits bare `NaN` — which `JSON.parse` throws on, blanking
    the card. They are refused here, at the only place that compares."""
    assert svc.finite_number(value) is None
    assert svc.gap(value, 30, "up_good")["reason"] == "non_numeric"
    assert svc.gap(35, value, "up_good")["reason"] == "non_numeric"


def test_booleans_are_not_numbers():
    """`isinstance(True, int)` is True, and `true` is not 1."""
    assert svc.finite_number(True) is None
    assert svc.gap(True, 1, "up_good")["reason"] == "non_numeric"


def test_a_list_target_is_not_a_number():
    assert svc.finite_number([1]) is None
    assert svc.gap([1], 30, "up_good")["reason"] == "non_numeric"


# ---------------------------------------------------------------------------
# resolve_direction — TD-2
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("registry,objective,direction,source,mismatch", [
    # The registry speaks → it wins.
    ("up_good", None, "up_good", "registry", False),
    ("down_good", "down", "down_good", "registry", False),
    # `neutral` is the COLUMN DEFAULT, so it is silence, not an opinion.
    ("neutral", "up", "up_good", "objective", False),
    (None, "down", "down_good", "objective", False),
    # `hold` is a declared direction that maps to the registry's `neutral`.
    ("neutral", "hold", "hold", "objective", False),
    ("neutral", "HOLD", "hold", "objective", False),
    # Two declared directions that disagree: registry wins, finding raised.
    ("up_good", "down", "up_good", "registry", True),
    ("up_good", "hold", "up_good", "registry", True),
    # Nobody said.
    ("neutral", None, None, "none", False),
    (None, None, None, "none", False),
    ("neutral", "sideways", None, "none", False),
])
def test_direction_falls_through_to_the_objective(
        registry, objective, direction, source, mismatch):
    assert svc.resolve_direction(registry, objective) == (
        direction, source, mismatch)


def test_a_declared_hold_is_not_the_same_as_silence():
    """Both land on the registry's `neutral` semantics, and they are told apart
    by `direction_source` — which is the whole reason that field exists."""
    held, held_source, _ = svc.resolve_direction("neutral", "hold")
    silent, silent_source, _ = svc.resolve_direction("neutral", None)
    assert (held, held_source) == ("hold", "objective")
    assert (silent, silent_source) == (None, "none")
    assert svc.gap(35, 35, held)["status"] == "on_target"
    assert svc.gap(35, 35, silent)["reason"] == "no_direction"


# ---------------------------------------------------------------------------
# canon_root — author-controlled and it reaches a file read
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("template,expected", [
    ({}, "canon"),
    ({"x-canon": {}}, "canon"),
    ({"x-canon": {"clone_path": "company/canon"}}, "company/canon"),
    ({"x-canon": {"clone_path": "/canon/"}}, "canon"),
    ({"x-canon": {"clone_path": "../../etc"}}, None),
    ({"x-canon": {"clone_path": "canon/../.."}}, None),
    ({"x-canon": {"clone_path": "a b"}}, None),
    ({"x-canon": {"clone_path": "canon/$(whoami)"}}, None),
    ("not a mapping", "canon"),
])
def test_canon_root_refuses_traversal(template, expected):
    assert svc.canon_root(template) == expected


# ---------------------------------------------------------------------------
# objective_concerns / objective_is_active
# ---------------------------------------------------------------------------

def test_an_objective_reaches_this_agent_two_ways():
    owned = {"owner": "role:revenue-lead"}
    supported = {"owner": "role:someone-else",
                 "supporting_agents": ["sales-companion"]}
    foreign = {"owner": "role:someone-else"}

    assert svc.objective_concerns(owned, "revenue-lead", "sales-companion")
    assert svc.objective_concerns(supported, "revenue-lead", "sales-companion")
    assert not svc.objective_concerns(foreign, "revenue-lead", "sales-companion")


def test_an_agent_with_no_role_can_still_support():
    obj = {"owner": "role:revenue-lead", "supporting_agents": ["helper"]}
    assert svc.objective_concerns(obj, None, "helper")
    assert not svc.objective_concerns({"owner": "role:revenue-lead"}, None, "helper")


@pytest.mark.parametrize("status,active", [
    ("active", True),
    (None, True),          # reader tolerance: the oldest canon files predate it
    ("achieved", False),
    ("dropped", False),
    ("paused", False),     # outside the enum is not a licence to keep nagging
])
def test_only_active_objectives_are_joined(status, active):
    assert svc.objective_is_active({"status": status}) is active
