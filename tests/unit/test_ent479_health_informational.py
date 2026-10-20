"""`get_agent_health` gains an INFORMATIONAL metrics block (ent#479, C5).

The whole contract of this block is what it does NOT do. A business metric
going stale is the operator's news, not a platform health failure: if it could
move `aggregate_status` or add an `issue`, every fleet dashboard would turn
red for a reason Trinity cannot fix, and the health signal that exists to say
"this container is broken" would stop meaning that.

So the assertions are mostly negative, and the positive one is a byte-identical
comparison of the rest of the response with and without stale metrics.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytest.importorskip("fastapi", reason="backend venv required")

import database as database_mod  # noqa: E402
import routers.monitoring as monitoring  # noqa: E402
from db_models import AgentHealthDetail  # noqa: E402
from services import metric_read_service  # noqa: E402

AGENT = "health-agent"
HOUR = 3600


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _ago(seconds):
    return _iso(datetime.now(timezone.utc) - timedelta(seconds=seconds))


def _definition(name="revenue", **overrides):
    d = {"name": name, "type": "gauge", "status": "active",
         "cadence_seconds": HOUR, "aggregation": "last", "dimensions": [],
         "values": None, "retired_at": None, "type_conflict": None}
    d.update(overrides)
    return d


class _Db:
    def __init__(self, definitions=None, points=None):
        self.definitions = definitions or []
        self.points = points or []
        self.raise_on_read = None

    def list_metric_definitions(self, name, include_retired=False):
        if self.raise_on_read:
            raise self.raise_on_read
        if include_retired:
            return list(self.definitions)
        return [d for d in self.definitions if d.get("status") == "active"]

    def latest_metric_points(self, name, metric_names, per_metric_limit=200):
        wanted = set(metric_names)
        return [p for p in self.points if p["metric"] in wanted]


def _point(metric, ts):
    return {"metric": metric, "ts": ts, "value_numeric": 1.0,
            "value_text": None, "dims": None, "idempotency_key": metric + ts}


@pytest.fixture
def store(monkeypatch):
    fake = _Db()
    monkeypatch.setattr(database_mod, "db", fake)
    return fake


# ---------------------------------------------------------------------------
# The block
# ---------------------------------------------------------------------------

def test_the_block_counts_declared_and_names_what_is_stale(store):
    store.definitions = [_definition("revenue"), _definition("leads")]
    store.points = [_point("revenue", _ago(60)),
                    _point("leads", _ago(10 * HOUR))]

    block = metric_read_service.freshness_summary(AGENT)
    assert block["declared"] == 2
    assert block["with_points"] == 2
    assert block["stale"] == ["leads"]
    assert block["rule"] == "2x cadence"


def test_a_metric_with_no_cadence_is_listed_apart_from_the_stale_ones(store):
    store.definitions = [_definition("revenue", cadence_seconds=None)]
    store.points = [_point("revenue", _ago(400 * 24 * HOUR))]

    block = metric_read_service.freshness_summary(AGENT)
    assert block["no_cadence"] == ["revenue"]
    assert block["stale"] == []


def test_a_metric_with_no_points_is_listed_apart_from_the_stale_ones(store):
    store.definitions = [_definition("revenue")]
    block = metric_read_service.freshness_summary(AGENT)
    assert block["no_points"] == ["revenue"]
    assert block["stale"] == []
    assert block["with_points"] == 0


def test_a_retired_definition_with_points_is_named_separately(store):
    """E-E4: `declared` counts what the template declares TODAY; points under
    a retired name are history and are reported as such, not as a gap."""
    store.definitions = [_definition("legacy", status="retired",
                                     retired_at="2026-08-01T00:00:00Z")]
    store.points = [_point("legacy", _ago(60))]

    block = metric_read_service.freshness_summary(AGENT)
    assert block["declared"] == 0
    assert block["retired_with_points"] == ["legacy"]


def test_the_block_carries_no_values_only_names(store):
    """It is a freshness summary, not a second read: the values live behind
    `GET .../metrics`, which is access-gated per metric."""
    store.definitions = [_definition("revenue")]
    store.points = [_point("revenue", _ago(60))]
    block = metric_read_service.freshness_summary(AGENT)
    assert set(block) == {
        "declared", "with_points", "stale", "no_cadence", "no_points",
        "retired_with_points", "last_point_at", "rule",
    }


# ---------------------------------------------------------------------------
# Informational — the negative contract
# ---------------------------------------------------------------------------

def test_a_store_failure_yields_a_null_block_not_a_failed_health_check(store):
    store.raise_on_read = RuntimeError("store down")
    assert monitoring._declared_metrics_block(AGENT) is None


def test_the_rest_of_the_health_payload_is_identical_with_and_without_stale(store):
    """The block is attached AFTER the model is built, so nothing upstream can
    read it. Proved by construction: the same detail object, serialised before
    and after the attach, differs in exactly one key."""
    detail = AgentHealthDetail(
        agent_name=AGENT, aggregate_status="healthy", issues=[],
        last_check_at="2026-09-22T12:00:00Z")
    before = detail.model_dump()

    store.definitions = [_definition("revenue")]
    store.points = [_point("revenue", _ago(10 * HOUR))]
    detail.metrics = monitoring._declared_metrics_block(AGENT)
    after = detail.model_dump()

    assert detail.metrics["stale"] == ["revenue"]
    assert detail.aggregate_status == "healthy"
    assert detail.issues == []
    assert {k: v for k, v in after.items() if k != "metrics"} == \
           {k: v for k, v in before.items() if k != "metrics"}


def test_every_existing_health_builder_still_validates_without_the_block():
    """E-T2: the field is optional and defaults to `None`, so the fleet
    summary path and every other `AgentHealthDetail(...)` call site keeps
    working without knowing metrics exist."""
    detail = AgentHealthDetail(agent_name=AGENT, aggregate_status="unknown")
    assert detail.metrics is None
    assert "metrics" in detail.model_dump()
