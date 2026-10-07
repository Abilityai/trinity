"""Property tests for the operator-queue service — invariants over the input space.

/edge-cases run 2026-10-06 (slug `ec_operator_queue`). Companion to
`test_ec_operator_queue_edges.py`, which pins named matrix rows; this file
asserts what must hold for EVERY input. Matrix: the 2026-10-06 /edge-cases matrix.

TWO TIERS

* **Tier 1 — pure** (`max_examples=200`): the clamp, truncation, prefix
  predicates, the #3243 caps (against an independent oracle), the answer
  validator, the hold ranking, the ingestion-marker write and the aging bound.
* **Tier 2 — real SQLite** (`max_examples=60`): the #2915 fingerprint ROUND-TRIP
  — entry → `_clamp_ingested_item` → `create_operator_queue_item_with_outcome`
  → `get_operator_queue_sync_index_for_agent` → `changed_fields(row, entry) == []`.
  The oracle is the production write path itself, so the property is exactly
  "the poller never calls an untouched entry a rewrite". Each example uses a
  FRESH agent name (rows never collide, nothing is deleted); the fixture
  asserts the engine points at the per-process unit-test SQLite.

  The strategy EXCLUDES the inputs of the three strict-xfail bugs in the edges
  file (falsy-but-not-null `options`, non-string `title`/`question`, a null
  `type`) — they are pinned there with reachability evidence; leaving them in
  would make this property permanently red instead of guarding the rest.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

pytest.importorskip("sqlalchemy")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = str(Path(__file__).resolve().parents[2] / "src" / "backend")
while _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)

import services.operator_queue_service as oqs  # noqa: E402
from services.operator_queue_choices import (  # noqa: E402
    SOMETHING_ELSE,
    InstructionRequiredError,
    ReservedValueError,
    ResponseNotOfferedError,
    options_cap_violation,
    reads_as_something_else,
    validate_response_choice,
)

pytestmark = pytest.mark.unit

_seq = count()

json_scalars = st.none() | st.booleans() | st.integers(-10**6, 10**6) | st.text(max_size=40)
json_values = st.recursive(
    json_scalars,
    lambda kids: st.lists(kids, max_size=4) | st.dictionaries(st.text(max_size=12), kids, max_size=4),
    max_leaves=12,
)
hostile = json_values | st.text(max_size=5000) | st.floats(allow_nan=True, allow_infinity=True) \
    | st.just({1, 2}) | st.just(object())


# ===========================================================================
# Tier 1 — pure
# ===========================================================================

@settings(max_examples=200, deadline=None)
@given(text=st.text(max_size=600), max_len=st.integers(len(oqs._TRUNC_MARKER), 500))
@example(text="x" * 13, max_len=12)
@example(text="x" * 12, max_len=12)
def test_truncate_bounds_prefix_and_idempotence(text, max_len):
    out = oqs._truncate_with_marker(text, max_len)
    assert len(out) <= max_len
    if len(text) <= max_len:
        assert out == text
    else:
        assert out.endswith(oqs._TRUNC_MARKER)
        assert text.startswith(out[: len(out) - len(oqs._TRUNC_MARKER)])
    assert oqs._truncate_with_marker(out, max_len) == out


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(req=st.fixed_dictionaries({}, optional={
    "id": hostile, "title": hostile, "question": hostile, "options": hostile,
    # an UNHASHABLE priority is the r145 strict-xfail (the clamp raises) — kept
    # out so this property guards everything else
    "context": hostile, "priority": json_scalars | st.floats(allow_nan=True), "type": hostile,
    "created_at": hostile, "expires_at": hostile,
    "addressed_to_email": st.none() | st.integers() | st.text(max_size=30).filter(lambda s: "@" not in s),
}))
def test_clamp_is_total_non_mutating_and_bounded(req):
    """No-crash-total + bounds over hostile shapes (the #1632 contract: the
    clamp runs inside the create try, but is written to NEVER raise)."""
    before = {k: (id(v), repr(v)) for k, v in req.items()}
    out = oqs._clamp_ingested_item(req, "agent-x")
    assert {k: (id(v), repr(v)) for k, v in req.items()} == before
    assert out["priority"] in oqs._VALID_PRIORITIES
    assert isinstance(out["context"], dict)
    ctx = out["context"]
    assert ctx.get("_truncated") is True or oqs._json_bytes(ctx) <= oqs.OPERATOR_QUEUE_CONTEXT_MAX_BYTES
    if out.get("options") is not None:
        assert (oqs._json_bytes(out["options"]) or 10**9) <= oqs.OPERATOR_QUEUE_OPTIONS_MAX_BYTES
    if isinstance(out.get("title"), str):
        assert len(out["title"]) <= oqs.OPERATOR_QUEUE_TITLE_MAX
    if isinstance(out.get("question"), str):
        assert len(out["question"]) <= oqs.OPERATOR_QUEUE_QUESTION_MAX
    assert out["addressed_to_email"] is None
    for k in oqs._PLATFORM_CONTEXT_KEYS:
        if not ctx.get("_truncated"):
            assert k not in ctx
    assert out["created_at"].endswith("Z")


reserved = st.sampled_from(oqs._RESERVED_ID_PREFIXES)
casing = st.lists(st.booleans(), min_size=40, max_size=40)
pad = st.sampled_from(["", " ", "  ", "\t", "\n "])


@settings(max_examples=200, deadline=None)
@given(prefix=reserved, flips=casing, left=pad, right=pad, tail=st.text(max_size=20))
def test_any_casing_or_padding_of_a_reserved_prefix_is_platform_minted(prefix, flips, left, right, tail):
    """#1632: the fold is what stops ` Poison-x`; `is_platform_minted` keys on
    the same tuple and must agree with the sync-loop guard for every spelling."""
    spelled = "".join(c.upper() if f else c for c, f in zip(prefix, flips))
    rid = left + spelled + tail + right
    assert oqs.is_platform_minted(rid)
    assert oqs.is_platform_minted({"request_id": rid})
    # the sync loop's own guard expression
    assert rid.strip().lower().startswith(oqs._RESERVED_ID_PREFIXES)


@settings(max_examples=200, deadline=None)
@given(rid=st.text(max_size=40) | st.sampled_from(oqs._ABOUT_A_PERSON_ID_PREFIXES).map(lambda p: p + "x"))
def test_about_a_person_implies_platform_minted(rid):
    """#715: no agent can mint a row the machine-key reads hide."""
    if oqs.is_about_a_person({"request_id": rid}):
        assert oqs.is_platform_minted({"request_id": rid})


@settings(max_examples=200, deadline=None)
@given(token=st.one_of(st.none(), st.integers(), st.text(max_size=200)))
def test_fold_helpers_stay_in_their_closed_vocabularies(token):
    d = oqs._detail(token)
    assert d == "other" or oqs._DETAIL_RE.match(d)
    assert "\n" not in d and len(d) <= 96
    s = oqs._fold_agent_status(token)
    assert s == "other" or oqs._AGENT_STATUS_RE.match(s)
    assert "\n" not in s and len(s) <= 32


# --- #3243 caps against an independent oracle ------------------------------

def _oracle_cap(options, max_options, max_chars):
    if not isinstance(options, list):
        return None
    counted = [o for o in options if o != SOMETHING_ELSE]
    if len(counted) > max_options:
        return "too_many_options"
    for o in options:
        if o != SOMETHING_ELSE and isinstance(o, str):
            core = " ".join(
                __import__("unicodedata").normalize("NFKC", o.translate(
                    dict.fromkeys(map(ord, "​‌‍‎‏⁠﻿"))))
                .strip().strip("()").strip().split()).casefold()
            if core == "something else":
                return "invalid_options"
    for o in options:
        if isinstance(o, str) and len(o) > max_chars:
            return "option_too_long"
    return None


option_text = st.one_of(
    st.text(max_size=80),
    st.sampled_from([SOMETHING_ELSE, "Something else", "(SOMETHING ELSE)", "approve", "x" * 61]),
)


@settings(max_examples=200, deadline=None)
@given(options=st.one_of(st.lists(st.one_of(option_text, st.none(), st.integers()), max_size=9),
                         st.text(max_size=5), st.none()),
       max_options=st.integers(2, 7), max_chars=st.integers(16, 70))
@example(options=["a"] * 5, max_options=5, max_chars=60)
@example(options=["a"] * 6, max_options=5, max_chars=60)
@example(options=["x" * 60], max_options=5, max_chars=60)
@example(options=["x" * 61], max_options=5, max_chars=60)
def test_options_cap_matches_the_oracle(options, max_options, max_chars):
    got = options_cap_violation(options, max_options=max_options, max_chars=max_chars)
    assert (got[0] if got else None) == _oracle_cap(options, max_options, max_chars)


zw = st.sampled_from(list("​‌‍‎‏⁠﻿"))


@settings(max_examples=200, deadline=None)
@given(base=st.sampled_from(["something else", "Something Else", "(something else)", " SOMETHING  ELSE "]),
       inserts=st.lists(st.tuples(st.integers(0, 30), zw), max_size=5))
def test_listed_zero_width_chars_never_hide_the_chip(base, inserts):
    s = base
    for pos, ch in inserts:
        p = min(pos, len(s))
        s = s[:p] + ch + s[p:]
    assert reads_as_something_else(s)


# --- answers ---------------------------------------------------------------

@settings(max_examples=200, deadline=None)
@given(options=st.lists(st.text(min_size=1, max_size=20), min_size=1, max_size=5),
       response=st.text(min_size=1, max_size=20), text=st.one_of(st.none(), st.text(max_size=10)))
@example(options=["Approve"], response=SOMETHING_ELSE, text=" ")
@example(options=[SOMETHING_ELSE], response=SOMETHING_ELSE, text="x")
def test_an_approval_accepts_exactly_its_options_plus_a_reasoned_off_menu(options, response, text):
    item = {"type": "approval", "options": options}
    if response == SOMETHING_ELSE:
        expect = None if (text or "").strip() else InstructionRequiredError
    elif response in [o for o in options if o != oqs.OPTIONS_DROPPED_MARKER]:
        expect = None
    elif all(o == oqs.OPTIONS_DROPPED_MARKER for o in options):
        expect = None
    else:
        expect = ResponseNotOfferedError
    if expect is None:
        validate_response_choice(item, response, response_text=text)
    else:
        with pytest.raises(expect):
            validate_response_choice(item, response, response_text=text)


@settings(max_examples=200, deadline=None)
@given(kind=st.one_of(st.none(), st.text(max_size=10).filter(lambda k: k != "approval")),
       options=st.one_of(st.none(), st.lists(st.text(max_size=5), max_size=3)))
def test_the_reserved_answer_is_refused_off_approvals(kind, options):
    with pytest.raises(ReservedValueError):
        validate_response_choice({"type": kind, "options": options}, SOMETHING_ELSE, response_text="x")


# --- hold ranking + marker -------------------------------------------------

holds = st.sampled_from(list(oqs._HOLD_RANK))


@settings(max_examples=200, deadline=None)
@given(seq=st.lists(holds, min_size=1, max_size=8))
def test_the_strongest_hold_wins_regardless_of_order(seq):
    cur = None
    for h in seq:
        cur = oqs._stronger_hold(cur, h)
    assert oqs._HOLD_RANK[cur] == max(oqs._HOLD_RANK[h] for h in seq)
    rcur = None
    for h in reversed(seq):
        rcur = oqs._stronger_hold(rcur, h)
    assert oqs._HOLD_RANK[rcur] == oqs._HOLD_RANK[cur]


markers = st.one_of(
    st.none(),
    st.builds(lambda r, a, b: oqs._ingestion_marker(r, a, b), holds,
              st.lists(st.text(max_size=5), max_size=12), st.lists(st.text(max_size=5), max_size=12)),
)
file_platform = st.one_of(
    st.just({}),
    st.builds(lambda b: {"platform": b}, st.one_of(st.text(max_size=3), st.none(), st.dictionaries(
        st.sampled_from(["ingestion", "aging_since", "other"]),
        st.one_of(json_scalars, st.fixed_dictionaries({"reason": holds}, optional={"since": json_scalars})),
        max_size=3))),
)


@settings(max_examples=200, deadline=None)
@given(data=file_platform, wanted=markers, wanted2=markers)
def test_marker_apply_converges_and_is_idempotent(data, wanted, wanted2):
    data = {"requests": [], **copy.deepcopy(data)}
    oqs._apply_ingestion_marker(data, wanted, "T1")
    assert not oqs._marker_differs(data, wanted)
    snapshot = json.dumps(data, sort_keys=True, default=str)
    assert oqs._apply_ingestion_marker(data, wanted, "T2") is False
    assert json.dumps(data, sort_keys=True, default=str) == snapshot   # since kept
    # a continuing hold keeps its start; a new reason restarts it
    oqs._apply_ingestion_marker(data, wanted2, "T3")
    cur = oqs._current_marker(data)
    if wanted2 is None:
        assert cur is None
    elif wanted is not None and wanted["reason"] == wanted2["reason"]:
        assert cur["since"] == "T1"
    else:
        assert cur["since"] == "T3"


# --- aging is monotone in time ---------------------------------------------

@settings(max_examples=200, deadline=None)
@given(age_s=st.integers(0, 10 * 86400), hours=st.integers(-2, 72), later=st.integers(0, 86400))
def test_once_aged_always_aged(age_s, hours, later):
    now = datetime(2026, 10, 6, tzinfo=timezone.utc)
    item = {"status": "pending", "created_at": oqs.to_utc_iso(now - timedelta(seconds=age_s))}
    a = oqs.is_aged(item, hours, now)
    b = oqs.is_aged(item, hours, now + timedelta(seconds=later))
    assert (not a) or b
    assert a == (hours > 0 and age_s >= hours * 3600)


# ===========================================================================
# Tier 2 — real SQLite: the #2915 fingerprint round-trip
# ===========================================================================

@pytest.fixture(scope="module")
def real_db():
    from db.engine import resolve_database_url
    url = resolve_database_url()
    assert url.startswith("sqlite:///") and "trinity-unit-tests" in url, url
    from database import db
    return db


short = st.text(max_size=150)
entries = st.fixed_dictionaries(
    {"status": st.just("pending")},
    optional={
        "title": st.one_of(st.none(), short, st.text(min_size=290, max_size=320)),
        "question": st.one_of(st.none(), short),
        # falsy-non-null options are the r88/r89 xfail — excluded here
        "options": st.one_of(st.none(), st.lists(st.text(max_size=20), min_size=1, max_size=5),
                             st.lists(st.dictionaries(st.text(max_size=5), st.integers(), max_size=2),
                                      min_size=1, max_size=3),
                             st.just(["x" * 5000])),
        "context": st.one_of(st.none(), st.text(max_size=10), st.lists(st.integers(), max_size=3),
                             st.dictionaries(st.text(max_size=10), json_scalars, max_size=4),
                             st.just({"blob": "y" * 9000, "execution_id": "e-1"})),
        "priority": st.one_of(st.sampled_from(sorted(oqs._VALID_PRIORITIES)), st.text(max_size=8)),
        # a null type is the r92 xfail — excluded here
        "type": st.one_of(st.sampled_from(["question", "approval", "alert", ""]), st.text(max_size=12)),
        "expires_at": st.one_of(st.none(), st.just(""), st.text(max_size=12),
                                st.datetimes(min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1))
                                .map(lambda d: d.isoformat() + "Z"),
                                st.datetimes(min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1),
                                             timezones=st.timezones())
                                .map(lambda d: d.isoformat())),
    },
)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(entry=entries)
@example(entry={"status": "pending", "options": None})
@example(entry={"status": "pending", "context": {"workspace_session_id": "forged", "k": 1}})
@example(entry={"status": "pending", "context": {"blob": "y" * 9000, "execution_id": "e-1"}})
@example(entry={"status": "pending", "title": "t" * 310})
def test_an_untouched_entry_never_reads_as_changed(real_db, entry):
    agent = f"ec-oq-prop-{next(_seq)}-{os.getpid()}"
    entry = {"id": "p-1", **entry}
    clamped = oqs._clamp_ingested_item(entry, agent)
    real_db.create_operator_queue_item_with_outcome(agent, clamped, channel="file", raised_by="agent")
    rows = real_db.get_operator_queue_sync_index_for_agent(agent)["open"]
    assert len(rows) == 1
    assert oqs.changed_fields(rows[0], entry) == []
