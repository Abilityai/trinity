"""Metric point validation — the pure leaf (trinity-enterprise#478, C3).

`validate_batch` is a leaf over the ent#477 definitions: no DB, no settings, no
HTTP. Every reason code in the frozen contract (§2.4) is proven here against
real definition rows, because the route's job is only to decide what a rejected
batch means at the transport layer — the *why* is decided here.

The value and timestamp cases are not decoration: Pydantic's union coercion was
MEASURED to turn `True` into `1.0` and to accept `NaN`, and
`datetime.fromisoformat` was measured to accept `0999-01-01` (which normalises
to `999-01-01T...` and then sorts as the newest row forever under the
lexicographic ISO index this table is read by).
"""

from __future__ import annotations

import math
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

from services import metric_points_service as svc  # noqa: E402

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)


def _definition(**overrides):
    d = {
        "name": "cycles",
        "type": "counter",
        "status": "active",
        "dimensions": [],
        "values": None,
        "type_conflict": None,
    }
    d.update(overrides)
    return d


def _validate(points, definitions=None, **kwargs):
    return svc.validate_batch(
        definitions if definitions is not None else [_definition()],
        points,
        now=NOW,
        **kwargs,
    )


def _codes(errors):
    return [e["code"] for e in errors]


# ---------------------------------------------------------------------------
# Happy path + identity
# ---------------------------------------------------------------------------

def test_a_declared_numeric_point_becomes_a_row():
    rows, errors = _validate([{"metric": "cycles", "value": 3}])
    assert errors == []
    assert len(rows) == 1
    assert rows[0]["value_numeric"] == 3.0
    assert rows[0]["value_text"] is None
    assert rows[0]["ts"] == "2026-09-22T12:00:00.000000Z"


def test_an_integer_value_is_stored_as_a_float_not_a_string():
    rows, _ = _validate([{"metric": "cycles", "value": 7}])
    assert isinstance(rows[0]["value_numeric"], float)


def test_the_identity_is_stable_across_dimension_key_order():
    """Two clients that spell the same point differently must collide, or the
    row-level dedup — the only guarantee that holds with no client key, no
    Redis and no execution id — is decorative."""
    a = svc.point_identity("cycles", "2026-09-22T12:00:00Z",
                           {"region": "eu", "tier": "pro"})
    b = svc.point_identity("cycles", "2026-09-22T12:00:00Z",
                           {"tier": "pro", "region": "eu"})
    assert a == b
    assert len(a) == 64


def test_the_identity_excludes_the_value():
    """Same observation, different number, one identity: a corrected re-post
    must dedup rather than double-count. A correction is a NEW ts."""
    rows, _ = _validate([{"metric": "cycles", "value": 1,
                          "ts": "2026-09-22T11:00:00Z"}])
    other, _ = _validate([{"metric": "cycles", "value": 999,
                           "ts": "2026-09-22T11:00:00Z"}])
    assert rows[0]["idempotency_key"] == other[0]["idempotency_key"]


def test_the_identity_separates_metrics_and_timestamps():
    keys = {
        svc.point_identity("cycles", "2026-09-22T12:00:00Z", None),
        svc.point_identity("cycle", "s2026-09-22T12:00:00Z", None),
        svc.point_identity("cycles", "2026-09-22T12:00:01Z", None),
        svc.point_identity("cycles", "2026-09-22T12:00:00Z", {"region": "eu"}),
    }
    assert len(keys) == 4, "NUL-joining must keep field boundaries distinct"


def test_no_dims_and_empty_dims_are_the_same_point():
    assert (svc.point_identity("cycles", "2026-09-22T12:00:00Z", None)
            == svc.point_identity("cycles", "2026-09-22T12:00:00Z", {}))


# ---------------------------------------------------------------------------
# Declaration gate
# ---------------------------------------------------------------------------

def test_an_undeclared_metric_is_refused_with_a_reachable_remedy():
    rows, errors = _validate([{"metric": "unknown_thing", "value": 1}])
    assert rows == []
    assert _codes(errors) == ["metric_undeclared"]
    assert "refresh_metric_definitions" in errors[0]["hint"]


def test_a_retired_metric_says_so_rather_than_unknown():
    rows, errors = _validate(
        [{"metric": "cycles", "value": 1}],
        definitions=[_definition(status="retired")])
    assert _codes(errors) == ["metric_retired"]


def test_a_malformed_metric_name_never_reaches_the_registry_lookup():
    rows, errors = _validate([{"metric": "Cycles!", "value": 1}])
    assert _codes(errors) == ["metric_name_invalid"]


def test_one_agents_definitions_do_not_declare_anothers():
    """The validator sees only the definitions handed to it, so a point can
    never be validated against a metric the caller does not own."""
    rows, errors = _validate([{"metric": "cycles", "value": 1}], definitions=[])
    assert _codes(errors) == ["metric_undeclared"]


# ---------------------------------------------------------------------------
# Value typing (S10/E1 — measured coercions)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [True, False])
def test_a_boolean_is_not_a_number(value):
    _, errors = _validate([{"metric": "cycles", "value": value}])
    assert _codes(errors) == ["value_invalid"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_value_is_refused(value):
    _, errors = _validate([{"metric": "cycles", "value": value}])
    assert _codes(errors) == ["value_invalid"]


def test_a_numeric_string_is_not_coerced():
    _, errors = _validate([{"metric": "cycles", "value": "42"}])
    assert _codes(errors) == ["type_mismatch"]


def test_a_number_on_a_status_metric_is_a_type_mismatch():
    _, errors = _validate(
        [{"metric": "mood", "value": 1}],
        definitions=[_definition(name="mood", type="status",
                                 values=[{"value": "ok"}])])
    assert _codes(errors) == ["type_mismatch"]


def test_a_declared_status_value_is_stored_as_text():
    rows, errors = _validate(
        [{"metric": "mood", "value": "ok"}],
        definitions=[_definition(name="mood", type="status",
                                 values=[{"value": "ok"}, {"value": "bad"}])])
    assert errors == []
    assert rows[0]["value_text"] == "ok" and rows[0]["value_numeric"] is None


def test_an_undeclared_status_value_is_refused():
    _, errors = _validate(
        [{"metric": "mood", "value": "elated"}],
        definitions=[_definition(name="mood", type="status",
                                 values=[{"value": "ok"}])])
    assert _codes(errors) == ["status_value_undeclared"]


def test_a_status_metric_with_no_declared_values_refuses_rather_than_widens():
    """ent#477's T5 defect (a refused type change used to wipe
    `status_values_json`) is fixed upstream, but a hand-edited row can still
    reach this state — and accepting any string would silently widen the
    metric's domain to everything."""
    _, errors = _validate(
        [{"metric": "mood", "value": "anything"}],
        definitions=[_definition(name="mood", type="status", values=None)])
    assert _codes(errors) == ["type_mismatch"]
    assert errors[0]["hint"]


def test_the_stored_type_decides_not_the_refused_one():
    """T5: points are validated against the type the store kept, and the hint
    names the author-side remedy, because the VALUE is not what is wrong."""
    rows, errors = _validate(
        [{"metric": "cycles", "value": 5}],
        definitions=[_definition(type="counter", type_conflict="status")])
    assert errors == [] and rows[0]["value_numeric"] == 5.0

    _, errors = _validate(
        [{"metric": "cycles", "value": "ok"}],
        definitions=[_definition(type="counter", type_conflict="status")])
    assert _codes(errors) == ["type_mismatch"]
    assert "rename the metric" in errors[0]["hint"]


def test_a_control_character_in_a_status_value_is_refused():
    """PostgreSQL's JSONB rejects a NUL with a DataError, which the route would
    have to classify as non-retryable — an agent retrying a permanently
    unstorable point forever is the failure this stops at the cheap end."""
    _, errors = _validate(
        [{"metric": "mood", "value": "o\x00k"}],
        definitions=[_definition(name="mood", type="status", values=None)])
    assert errors, "a NUL must not reach the driver"


# ---------------------------------------------------------------------------
# Timestamps (E8/S2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "2026-09-22T11:00:00Z",
    "2026-09-22T11:00:00.123456Z",
    "2026-09-22T13:00:00+02:00",
])
def test_rfc3339_shapes_are_accepted_and_normalised_to_utc_z(raw):
    rows, errors = _validate([{"metric": "cycles", "value": 1, "ts": raw}])
    assert errors == []
    assert rows[0]["ts"].endswith("Z")
    assert rows[0]["ts"].startswith("2026-09-22T11:00:00")


@pytest.mark.parametrize("raw", [
    "2026-09-22",            # date only
    "2026-W01-1",            # week date
    "2026-09-22T11:00:00",   # naive — ambiguous, not "UTC by courtesy"
    "2026-09-22 11:00:00Z",  # space separator
    "yesterday",
])
def test_a_shape_fromisoformat_would_swallow_is_refused(raw):
    _, errors = _validate([{"metric": "cycles", "value": 1, "ts": raw}])
    assert _codes(errors) == ["ts_invalid"]


def test_a_year_that_sorts_as_newest_forever_is_refused():
    """`0999-01-01` normalises to `999-01-01T...`, which is lexicographically
    ABOVE every real timestamp — one such row would head the `ts DESC` read
    index permanently."""
    _, errors = _validate(
        [{"metric": "cycles", "value": 1, "ts": "0999-01-01T00:00:00Z"}])
    assert _codes(errors) == ["ts_out_of_range"]


def test_a_future_timestamp_beyond_the_clock_skew_is_refused():
    future = (NOW + timedelta(seconds=svc.TS_FUTURE_SKEW_SECONDS + 60))
    _, errors = _validate([{
        "metric": "cycles", "value": 1,
        "ts": future.strftime("%Y-%m-%dT%H:%M:%SZ")}])
    assert _codes(errors) == ["ts_in_future"]


def test_a_small_clock_skew_is_tolerated():
    near = (NOW + timedelta(seconds=svc.TS_FUTURE_SKEW_SECONDS - 60))
    _, errors = _validate([{
        "metric": "cycles", "value": 1,
        "ts": near.strftime("%Y-%m-%dT%H:%M:%SZ")}])
    assert errors == []


def test_a_backfill_older_than_the_window_is_refused_when_one_is_set():
    """S2: unbounded past points let agent INPUT steer the retention guard —
    100k/day of year-old points push the sweep past its floor, and the refusal
    alarm's acknowledgements are single-use."""
    _, errors = _validate(
        [{"metric": "cycles", "value": 1, "ts": "2024-01-01T00:00:00Z"}],
        retention_days=365)
    assert _codes(errors) == ["ts_before_retention"]
    assert "365" in errors[0]["message"]


def test_backfill_is_unbounded_when_retention_is_disabled():
    rows, errors = _validate(
        [{"metric": "cycles", "value": 1, "ts": "2024-01-01T00:00:00Z"}],
        retention_days=0)
    assert errors == [] and len(rows) == 1


# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------

def test_a_declared_dimension_is_kept():
    rows, errors = _validate(
        [{"metric": "cycles", "value": 1, "dims": {"region": "eu"}}],
        definitions=[_definition(dimensions=["region"])])
    assert errors == [] and rows[0]["dims"] == {"region": "eu"}


def test_an_undeclared_dimension_key_is_refused_and_named():
    _, errors = _validate(
        [{"metric": "cycles", "value": 1, "dims": {"regoin": "eu"}}],
        definitions=[_definition(dimensions=["region"])])
    assert _codes(errors) == ["dimension_undeclared"]
    assert "regoin" in errors[0]["message"], "the KEY is safe to echo"


def test_dimensions_on_a_metric_that_declares_none_are_refused():
    _, errors = _validate(
        [{"metric": "cycles", "value": 1, "dims": {"region": "eu"}}])
    assert _codes(errors) == ["dimension_undeclared"]


@pytest.mark.parametrize("value", [3, True, None, "", "x" * 129, "e\x00u"])
def test_a_dimension_value_that_is_not_bounded_text_is_refused(value):
    _, errors = _validate(
        [{"metric": "cycles", "value": 1, "dims": {"region": value}}],
        definitions=[_definition(dimensions=["region"])])
    assert _codes(errors) == ["dimension_value_invalid"]


def test_more_dimensions_than_the_declaration_reader_allows_is_refused():
    """And under its OWN code (ent#478 I8): "too many" and "not declared" have
    different remedies, so reusing `dimension_undeclared` here would send an
    agent off to declare a dimension that cannot help — every key in this batch
    IS declared."""
    dims = {f"d{i}": "x" for i in range(svc.MAX_DIMENSIONS + 1)}
    _, errors = _validate(
        [{"metric": "cycles", "value": 1, "dims": dims}],
        definitions=[_definition(dimensions=list(dims))])
    assert _codes(errors) == ["dimensions_too_many"]
    assert str(svc.MAX_DIMENSIONS) in errors[0]["message"]


def test_a_text_value_longer_than_the_bound_is_refused_by_name():
    """ent#478 I1: the 2 MiB batch cap is a BATCH bound that one point can
    exhaust. A `value` is a label, so it carries its own per-field bound, and
    the refusal names that rule rather than reading as a type problem."""
    from models import METRIC_VALUE_TEXT_MAX_LEN

    _, errors = _validate(
        [{"metric": "cycles", "value": "x" * (METRIC_VALUE_TEXT_MAX_LEN + 1)}])
    assert _codes(errors) == ["value_too_long"]
    # Never the value itself — this message lands in an LLM's context and logs.
    assert "x" * 50 not in errors[0]["message"]


def test_a_text_value_at_the_bound_is_judged_on_its_type_not_its_length():
    """The boundary is inclusive, and the length check does not shadow the type
    rules: a 1024-character string on a counter is still a `type_mismatch`."""
    from models import METRIC_VALUE_TEXT_MAX_LEN

    _, errors = _validate(
        [{"metric": "cycles", "value": "x" * METRIC_VALUE_TEXT_MAX_LEN}])
    assert _codes(errors) == ["type_mismatch"]


# ---------------------------------------------------------------------------
# Batch shape
# ---------------------------------------------------------------------------

def test_the_same_identity_twice_in_one_batch_names_the_earlier_index():
    point = {"metric": "cycles", "value": 1, "ts": "2026-09-22T11:00:00Z"}
    rows, errors = _validate([point, dict(point, value=2)])
    assert _codes(errors) == ["duplicate_in_batch"]
    assert errors[0]["index"] == 1 and "index 0" in errors[0]["message"]
    assert len(rows) == 1, "the first copy is still a real observation"


def test_every_error_carries_its_batch_index():
    _, errors = _validate([
        {"metric": "cycles", "value": 1},
        {"metric": "nope", "value": 1},
        {"metric": "cycles", "value": "x"},
    ])
    assert [e["index"] for e in errors] == [1, 2]


def test_an_error_message_never_echoes_the_value_or_a_dimension_value():
    """These messages land in MCP tool output — i.e. in an LLM's context — and
    in logs. A dimension value may be a customer name or an email."""
    _, errors = _validate(
        [{"metric": "cycles", "value": "sk-secret-token",
          "dims": {"region": "customer@example.com"}}],
        definitions=[_definition(dimensions=["region"])])
    blob = repr(errors)
    assert "sk-secret-token" not in blob
    assert "customer@example.com" not in blob


def test_an_echoed_metric_name_is_charset_bounded():
    _, errors = _validate([{"metric": "<script>alert(1)</script>", "value": 1}])
    assert "<" not in errors[0]["metric"] and ">" not in errors[0]["metric"]


def test_an_empty_batch_is_simply_empty_not_an_error():
    rows, errors = _validate([])
    assert rows == [] and errors == []


def test_validation_never_reaches_the_database_layer():
    """A leaf, proven by running it: with `database` evicted from
    `sys.modules` and poisoned so any import raises, a full batch still
    validates. A route that later hands in stale definitions is a route bug —
    this layer cannot make a query at all.
    """
    import importlib

    saved = sys.modules.pop("database", None)
    sys.modules["database"] = None  # any `import database` now raises
    try:
        importlib.reload(svc)
        rows, errors = svc.validate_batch(
            [_definition(dimensions=["region"])],
            [{"metric": "cycles", "value": 1, "dims": {"region": "eu"}},
             {"metric": "nope", "value": 1}],
            now=NOW,
        )
        assert len(rows) == 1 and _codes(errors) == ["metric_undeclared"]
    finally:
        del sys.modules["database"]
        if saved is not None:
            sys.modules["database"] = saved
        importlib.reload(svc)


# ---------------------------------------------------------------------------
# The wire model (C5) — the outer gate the validator assumes has already run
# ---------------------------------------------------------------------------

class TestWireModel:
    """`MetricPointIn` is the frozen shape ent#479 reads back and #536 binds a
    canvas chart to, so its coercions are a contract, not an implementation
    detail. Pydantic was MEASURED to turn `True` into `1.0` under
    `Union[float, str]` and to accept `NaN`; both are rejected explicitly."""

    @staticmethod
    def _model():
        from models import MetricPointIn
        return MetricPointIn

    def test_a_number_and_a_label_are_both_valid_values(self):
        m = self._model()
        assert m(metric="cycles", value=3).value == 3
        assert m(metric="mood", value="ok").value == "ok"

    @pytest.mark.parametrize("value", [True, False])
    def test_a_boolean_never_becomes_a_number(self, value):
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            self._model()(metric="cycles", value=value)

    @pytest.mark.parametrize("value", [float("nan"), float("inf")])
    def test_a_non_finite_float_is_refused_before_the_dialect_can_disagree(self, value):
        """SQLite stores NaN as NULL and PostgreSQL stores it as NaN — the same
        batch would mean two different things per backend."""
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            self._model()(metric="cycles", value=value)

    def test_the_metric_name_charset_is_enforced_at_the_edge(self):
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            self._model()(metric="Cycles", value=1)

    def test_a_batch_is_bounded_at_both_ends(self):
        import pydantic
        from models import METRIC_BATCH_MAX_POINTS, MetricPointsBatch

        one = {"metric": "cycles", "value": 1}
        assert len(MetricPointsBatch(points=[one]).points) == 1
        with pytest.raises(pydantic.ValidationError):
            MetricPointsBatch(points=[])
        with pytest.raises(pydantic.ValidationError):
            MetricPointsBatch(points=[one] * (METRIC_BATCH_MAX_POINTS + 1))

    def test_a_point_cap_is_not_a_size_cap(self):
        """1000 points x 10 dims x 128 chars is ~1.4 MB of LEGAL input, which
        is why the route carries a byte cap as well — and why that cap is 2 MiB
        rather than the 1 MiB the sibling `report` path uses."""
        from models import (METRIC_BATCH_MAX_BYTES, METRIC_BATCH_MAX_POINTS,
                            METRIC_DIM_VALUE_MAX_LEN)
        worst = METRIC_BATCH_MAX_POINTS * 10 * METRIC_DIM_VALUE_MAX_LEN
        assert worst > 1024 * 1024
        assert METRIC_BATCH_MAX_BYTES >= worst
