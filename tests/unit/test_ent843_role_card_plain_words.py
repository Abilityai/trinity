"""trinity-enterprise#843 — the Workspace role card's objectives, in plain words.

Pins the backend half (the card's own rendering is pinned by
`src/frontend/tests/unit/portalAgentRole.spec.js`):

* a metric always crosses with a readable label: its declared label, else the
  label the owner's other agents' active declarations agree on (so a metric
  another agent tracks shows by THAT agent's label), else the code name turned
  into words — never a raw snake_case name, and a failed lookup still falls
  back to words;
* `declarations_elsewhere` reads only the same owner's other live agents,
  ignores retired rows, and never says which agent declared — an agent of
  another owner cannot put text on this owner's card;
* a supported objective says "tracked elsewhere" ONCE, only when another agent
  really declares one of its numbers, and never names that agent;
* the canon's optional `client_heading` reaches the card through the join.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND_STR = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, seed_agent, seed_user  # noqa: E402,F401

pytest.importorskip("sqlalchemy", reason="backend venv required")

from database import db  # noqa: E402
from client_portal import role_card as rc  # noqa: E402
from services import metric_registry, objective_join_service, template_metrics  # noqa: E402


# --------------------------------------------------------------------------- #
# Readable names
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name,words", [
    ("runway_months", "Runway (months)"),
    ("finance_ar_overdue_usd", "Finance AR overdue (USD)"),
    ("close_rate_pct", "Close rate (%)"),
    ("mrr", "MRR"),
    ("pipeline-coverage", "Pipeline coverage"),
    ("weekly_active_users", "Weekly active users"),
    ("", "Metric"),
])
def test_a_code_name_reads_as_words(name, words):
    assert rc.readable_metric_name(name) == words


def _metric(name, label=None, **over):
    return {"name": name, "label": label, "type": None, "unit": None, "target": None,
            "actual": None, "gap": {}, "finding": None, **over}


def test_a_declared_label_wins_and_the_joins_code_name_fallback_does_not_count():
    assert rc.portal_metric(_metric("close_rate", "Close rate"))["label"] == "Close rate"
    # The join falls back to the NAME when it has a definition without a label.
    assert rc.portal_metric(_metric("close_rate", "close_rate"))["label"] == ""


def test_missing_labels_take_the_agreed_label_else_words():
    objectives = [{"metrics": [rc.portal_metric(_metric("runway_months")),
                               rc.portal_metric(_metric("finance_ar_overdue_usd")),
                               rc.portal_metric(_metric("close_rate", "Close rate"))]}]
    asked = []

    def lookup(names):
        asked.append(names)
        return {"runway_months": "Cash runway", "finance_ar_overdue_usd": None,
                "close_rate": "Someone else's label"}

    rc.apply_declarations_elsewhere(objectives, lookup)
    labels = [m["label"] for m in objectives[0]["metrics"]]
    # agreed label; declared elsewhere but no agreed label → words; own label wins
    assert labels == ["Cash runway", "Finance AR overdue (USD)", "Close rate"]
    assert asked == [["close_rate", "finance_ar_overdue_usd", "runway_months"]]


def test_a_failed_label_lookup_still_never_shows_a_code_name():
    objectives = [{"metrics": [rc.portal_metric(_metric("runway_months"))]}]

    def boom(_names):
        raise RuntimeError("db down")

    objectives[0]["tracked_elsewhere"] = True
    rc.apply_declarations_elsewhere(objectives, boom)
    assert objectives[0]["metrics"][0]["label"] == "Runway (months)"
    assert objectives[0]["tracked_elsewhere"] is False   # no claim without evidence


# --------------------------------------------------------------------------- #
# declarations_elsewhere — real database
# --------------------------------------------------------------------------- #
CARD = "card-agent"


def _declare(agent, *entries):
    return metric_registry.reconcile_declared_metrics(
        agent, template_metrics.normalize_declared_metrics(list(entries)), source="create")


@pytest.fixture
def owners(db_backend):
    """CARD and its siblings belong to owner 1; `stranger` to owner 2."""
    seed_user(1, "owner", role="admin")
    seed_user(2, "someone", role="user")
    for name in (CARD, "finance-agent", "ops-agent", "quiet-agent"):
        seed_agent(name, owner_id=1)
    seed_agent("stranger", owner_id=2)


def test_declarations_elsewhere_agree_or_say_none(owners):
    _declare("finance-agent", {"name": "runway_months", "type": "gauge", "label": "Runway"},
             {"name": "burn_usd", "type": "gauge", "label": "Monthly burn"})
    _declare("ops-agent", {"name": "runway_months", "type": "gauge", "label": "Runway"},
             {"name": "burn_usd", "type": "gauge", "label": "Burn rate"})
    _declare("quiet-agent", {"name": "unlabelled_metric", "type": "gauge"})

    got = db.metric_declarations_elsewhere(
        CARD, ["runway_months", "burn_usd", "unlabelled_metric", "nobody_declares"])
    # agreed; declared but disagreeing; declared without a label; nobody → absent
    assert got == {"runway_months": "Runway", "burn_usd": None, "unlabelled_metric": None}


def test_another_owners_agent_cannot_author_this_card(owners):
    """Merge-train review (2026-10-08): the read was fleet-wide, so any agent
    declaring the same name put up to 120 characters on another owner's
    client-facing card."""
    _declare("stranger", {"name": "runway_months", "type": "gauge", "label": "Buy now at example.com"})
    assert db.metric_declarations_elsewhere(CARD, ["runway_months"]) == {}


def test_the_card_agents_own_declaration_is_not_elsewhere(owners):
    _declare(CARD, {"name": "runway_months", "type": "gauge", "label": "Runway"})
    assert db.metric_declarations_elsewhere(CARD, ["runway_months"]) == {}


def test_an_agent_with_no_ownership_row_matches_nothing(owners):
    _declare("finance-agent", {"name": "runway_months", "type": "gauge", "label": "Runway"})
    assert db.metric_declarations_elsewhere("unowned-agent", ["runway_months"]) == {}


def test_a_retired_declaration_does_not_count(owners):
    _declare("finance-agent", {"name": "runway_months", "type": "gauge", "label": "Runway"})
    _declare("ops-agent", {"name": "runway_months", "type": "gauge", "label": "Old runway"})
    _declare("ops-agent")                             # emptied block retires ops-agent's row
    assert db.metric_declarations_elsewhere(CARD, ["runway_months"]) == {"runway_months": "Runway"}


# --------------------------------------------------------------------------- #
# Tracked elsewhere — once per objective, never named, only when true
# --------------------------------------------------------------------------- #
def _objective(owned, metrics):
    return {"id": "o", "statement": "S", "owned": owned, "metrics": metrics}


def _card_objective(owned, metrics, found):
    obj = rc.portal_objective(_objective(owned, metrics))
    rc.apply_declarations_elsewhere([obj], lambda names: found)
    return obj


def test_a_supported_objective_tracked_elsewhere_says_so_once_without_a_name():
    obj = _card_objective(False, [_metric("a", declared_elsewhere=True),
                                  _metric("b", declared_elsewhere=True)], {"a": "A"})
    assert obj["tracked_elsewhere"] is True
    assert "tracked_by" not in obj


def test_nobody_declaring_the_number_is_not_tracked_elsewhere():
    """The join's `declared_elsewhere` is also true when NO agent declares it."""
    obj = _card_objective(False, [_metric("a", declared_elsewhere=True)], {})
    assert obj["tracked_elsewhere"] is False


def test_an_owned_objective_is_never_tracked_elsewhere():
    obj = _card_objective(True, [_metric("a", declared_elsewhere=True)], {"a": "A"})
    assert obj["tracked_elsewhere"] is False


# --------------------------------------------------------------------------- #
# The plain heading
# --------------------------------------------------------------------------- #
def test_client_heading_reaches_the_card_through_the_join():
    doc = {"id": "q4-close", "statement": "H10: lift close rate (ADR-0042)",
           "client_heading": "Win more of the deals we pitch",
           "supporting_agents": ["a"],
           "metrics": [{"name": "close_rate", "target": 35}]}
    obj, _findings = objective_join_service.parse_objective(doc, path="canon/objectives/q4-close.yaml")
    assert obj["client_heading"] == "Win more of the deals we pitch"
    joined = objective_join_service.join_objectives([obj], [], {}, agent_name="a", role_id=None)
    assert joined["objectives"][0]["client_heading"] == "Win more of the deals we pitch"
    assert rc.portal_objective(joined["objectives"][0])["client_heading"] == "Win more of the deals we pitch"


def test_an_objective_without_one_keeps_none():
    obj, _ = objective_join_service.parse_objective(
        {"id": "x", "statement": "S", "metrics": [{"name": "m"}]}, path="canon/objectives/x.yaml")
    assert obj["client_heading"] is None
