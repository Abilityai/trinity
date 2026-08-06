"""/edge-cases — boundary + property analysis of `ExecutionOrigin.from_payload` (#1970).

`from_payload` is the validation boundary for the scheduler's manual-trigger
endpoint: an unauthenticated (platform-network-only) HTTP route that accepts an
arbitrary JSON body, whose parsed output is written straight into
`schedule_executions`' five audit columns. It is the only thing between a caller
and those columns, which makes it exactly the surface where "validated the type"
and "validated the value" get confused.

Enumerated via boundary-value analysis over each field's domain, plus properties
over the whole payload space. **Two real bugs surfaced, both since fixed:**

* out-of-int64 `source_user_id` reached the INSERT and raised `OverflowError`
  (`test_source_user_id_is_bounded_to_what_the_column_can_hold`);
* `is_empty()` used truthiness, so `user_id=0` read as no attribution
  (`test_is_empty_agrees_with_the_fields`, Hypothesis-shrunk).

Worth recording how they hid: `from_payload` already had **100% statement and
branch coverage** from `test_1970_execution_origin.py` before this file existed.
Full coverage of a validator says every line ran, not that every *value class*
was tried — which is the whole reason this analysis is a separate pass.

Method: /edge-cases (BVA + equivalence partitioning + Hypothesis).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings, strategies as st  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.append(str(_REPO))

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

from src.scheduler.models import ExecutionOrigin  # noqa: E402

# SQLite stores INTEGER as a signed 64-bit value; anything outside this range
# raises OverflowError at INSERT time rather than being silently truncated.
_SQLITE_INT_MIN = -(2 ** 63)
_SQLITE_INT_MAX = 2 ** 63 - 1

_STRING_FIELDS = (
    "source_user_email",
    "source_agent_name",
    "source_mcp_key_id",
    "source_mcp_key_name",
)

_FIELD_TO_ATTR = {
    "source_user_email": "user_email",
    "source_agent_name": "agent_name",
    "source_mcp_key_id": "mcp_key_id",
    "source_mcp_key_name": "mcp_key_name",
}


# ---------------------------------------------------------------------------
# Deterministic boundary rows.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "body"),
    [
        pytest.param(1, None, id="r1-none"),
        pytest.param(2, [], id="r2-list"),
        pytest.param(3, "str", id="r3-str"),
        pytest.param(4, 0, id="r4-int"),
        pytest.param(5, {}, id="r5-empty-dict"),
        pytest.param(6, {"unrelated": "x"}, id="r6-no-known-keys"),
    ],
)
def test_non_payloads_yield_an_empty_origin(row, body):
    """Rows 1-6: anything that is not a dict carrying known keys is blank, and
    blank must never raise — the run must not depend on its audit metadata
    being well-formed."""
    origin = ExecutionOrigin.from_payload(body)
    assert origin.is_empty() is True


@pytest.mark.parametrize(
    ("row", "value", "expected"),
    [
        pytest.param(7, "", None, id="r7-empty"),
        pytest.param(8, "   ", None, id="r8-whitespace-only"),
        pytest.param(9, "\t\n", None, id="r9-tab-newline-only"),
        pytest.param(10, " a ", "a", id="r10-trimmed"),
        pytest.param(11, "x" * 255, "x" * 255, id="r11-at-cap"),
        pytest.param(12, "x" * 256, "x" * 255, id="r12-one-over-cap"),
        pytest.param(13, "0", "0", id="r13-falsy-looking-but-valid"),
        pytest.param(14, 123, None, id="r14-int-in-string-field"),
        pytest.param(15, ["a"], None, id="r15-list-in-string-field"),
        pytest.param(16, None, None, id="r16-explicit-null"),
        pytest.param(17, True, None, id="r17-bool-in-string-field"),
    ],
)
def test_string_field_boundaries(row, value, expected):
    """Rows 7-17, applied to every string field.

    Row 13 is the interesting one: `"0"` is a legitimate identifier that a naive
    falsiness check would drop. Row 12 pins truncation rather than rejection —
    an over-long name costs its tail, not the trigger.
    """
    for field in _STRING_FIELDS:
        origin = ExecutionOrigin.from_payload({field: value})
        assert getattr(origin, _FIELD_TO_ATTR[field]) == expected, field


@pytest.mark.parametrize(
    ("row", "value", "expected"),
    [
        pytest.param(18, 0, 0, id="r18-zero"),
        pytest.param(19, 1, 1, id="r19-one"),
        pytest.param(20, -1, -1, id="r20-negative"),
        pytest.param(21, True, None, id="r21-bool-true-is-not-user-1"),
        pytest.param(22, False, None, id="r22-bool-false"),
        pytest.param(23, "7", None, id="r23-numeric-string"),
        pytest.param(24, 7.0, None, id="r24-float"),
        pytest.param(25, None, None, id="r25-null"),
        pytest.param(26, {"id": 7}, None, id="r26-object"),
    ],
)
def test_user_id_type_boundaries(row, value, expected):
    """Rows 18-26. Row 21 is the load-bearing one: `bool` IS an `int` in Python,
    so an unguarded `isinstance(v, int)` would persist `True` as user 1 — a real
    account attributed to a run it had nothing to do with."""
    assert ExecutionOrigin.from_payload({"source_user_id": value}).user_id == expected


@pytest.mark.parametrize(
    ("row", "value"),
    [
        pytest.param(27, _SQLITE_INT_MAX, id="r27-max-int64"),
        pytest.param(28, _SQLITE_INT_MIN, id="r28-min-int64"),
    ],
)
def test_user_id_at_the_column_limits_is_accepted(row, value):
    """Rows 27-28: the extremes the column CAN hold must pass through."""
    assert ExecutionOrigin.from_payload({"source_user_id": value}).user_id == value


@pytest.mark.parametrize(
    ("row", "value"),
    [
        pytest.param(29, _SQLITE_INT_MAX + 1, id="r29-one-over-max"),
        pytest.param(30, _SQLITE_INT_MIN - 1, id="r30-one-under-min"),
        pytest.param(31, 10 ** 40, id="r31-absurd"),
    ],
)
def test_source_user_id_is_bounded_to_what_the_column_can_hold(row, value):
    """Rows 29-31 — regression guard for a bug this analysis found.

    `source_user_id` lands in an INTEGER column. SQLite stores signed 64-bit and
    raises `OverflowError: Python int too large to convert to SQLite INTEGER`
    for anything wider; it does NOT truncate. Verified end-to-end against a real
    `create_execution`.

    Consequence depends on the caller:
      * via `_execute_schedule_with_lock` (#1970) — the exception escapes into
        `_execute_manual_trigger`'s blanket `except`, so the run is logged and
        silently lost AFTER the endpoint already answered `"triggered"`;
      * via `_trigger_handler` (#1968) — caught, so the trigger 500s.

    Either way a payload field decides whether the schedule runs. The whole
    point of this function is to make that impossible, and it already drops
    wrong-typed values for exactly this reason — the range check is the missing
    sibling of a guard that is otherwise present.

    Fixed by dropping out-of-range values to None, like every other unusable
    input here — attribution is never worth a failed run.
    """
    assert ExecutionOrigin.from_payload({"source_user_id": value}).user_id is None


# ---------------------------------------------------------------------------
# Properties.
# ---------------------------------------------------------------------------

_json_scalars = st.one_of(
    st.none(), st.booleans(), st.integers(), st.floats(allow_nan=True, allow_infinity=True),
    st.text(),
)
_payloads = st.dictionaries(
    keys=st.one_of(
        st.sampled_from(list(_STRING_FIELDS) + ["source_user_id", "triggered_by"]),
        st.text(max_size=20),
    ),
    values=st.one_of(_json_scalars, st.lists(_json_scalars, max_size=3)),
    max_size=8,
)


@given(body=st.one_of(_payloads, _json_scalars, st.lists(_json_scalars, max_size=3)))
@settings(max_examples=400, deadline=None)
def test_never_raises_on_any_json_shaped_input(body):
    """Total function. The endpoint parses an untrusted body; a raise here is a
    failed trigger caused by audit metadata, which inverts the priority."""
    ExecutionOrigin.from_payload(body)


@given(body=_payloads)
@settings(max_examples=400, deadline=None)
def test_output_types_are_exactly_what_the_columns_accept(body):
    """The columns are four TEXT and one INTEGER. Anything else is a bug in the
    parser, not in the DB layer."""
    origin = ExecutionOrigin.from_payload(body)
    for attr in _FIELD_TO_ATTR.values():
        value = getattr(origin, attr)
        assert value is None or isinstance(value, str)
    assert origin.user_id is None or (
        isinstance(origin.user_id, int) and not isinstance(origin.user_id, bool)
    )


@given(body=_payloads)
@settings(max_examples=400, deadline=None)
def test_strings_are_stripped_capped_and_never_blank(body):
    """One rule, three ways to break it: an untrimmed value, an over-long value,
    and `""` — which must not become a second spelling of "unknown" alongside
    None, or a query for unattributed rows has to know about both."""
    origin = ExecutionOrigin.from_payload(body)
    for attr in _FIELD_TO_ATTR.values():
        value = getattr(origin, attr)
        if value is not None:
            assert value == value.strip() != ""
            assert len(value) <= 255


@given(body=_payloads)
@settings(max_examples=400, deadline=None)
def test_is_empty_agrees_with_the_fields(body):
    """`is_empty()` claims to answer "is there any attribution here", so it
    drifting from the actual fields would silently drop or invent attribution.

    Was a REAL BUG (latent), found by this analysis and fixed: the implementation was
    `not any((self.user_id, ...))`, and `0` is falsy — so an origin carrying
    user id 0 reports itself empty. `users.id` is AUTOINCREMENT so 0 does not
    arise naturally, but this parser's input is an arbitrary JSON body, and the
    method is public on a value object. Nothing in `src/` calls it yet, which is
    exactly why it is worth pinning now: the first caller to gate on it inherits
    a helper that lies about a valid value.

    Fixed by comparing against None instead of truthiness.
    """
    origin = ExecutionOrigin.from_payload(body)
    populated = any(
        getattr(origin, a) is not None
        for a in list(_FIELD_TO_ATTR.values()) + ["user_id"]
    )
    assert origin.is_empty() is (not populated)


@given(body=_payloads)
@settings(max_examples=300, deadline=None)
def test_parsing_is_idempotent_and_pure(body):
    """Same payload → same origin, and the caller's dict is not mutated (the
    handler reads `triggered_by` from the same object)."""
    before = dict(body)
    first = ExecutionOrigin.from_payload(body)
    second = ExecutionOrigin.from_payload(body)
    assert first == second
    assert body == before
