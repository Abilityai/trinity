"""/edge-cases 2026-10-06 — pull coordination + pull pilot: Hypothesis properties.

Companion to ``test_ec_pull_coordination_edges.py`` (named boundaries). This
file asserts the invariants that must hold across the whole input space.

TWO TIERS
---------
* **Pure** (``max_examples=200``) — no DB. ``_turn_limit`` bounds (the #2846
  guarantee that stands in for the never-built lease-renewal heartbeat),
  the result-sink status map's trust boundary, ``_build_claim_response``
  totality over arbitrary ``backlog_metadata``, and oracles for the
  ``PULL_MODE_PILOT_AGENTS`` gate and ``pull_owns_dispatch``.
* **Real DB** (``max_examples=30``) — the lease expiry boundary, the claim
  order (#2842 interactive-first + #3127 ``session:`` prefix + #2843 one
  running turn per conversation), and the result CAS over arbitrary
  sequences of late / duplicate / wrong-token reports. Each is an ORACLE
  (equality with an independent model), not a membership check.

Per-example isolation: a function-scoped ``db_backend`` is built once per test
FUNCTION, so every DB property truncates ``schedule_executions`` at the top of
each example (the function-scoped-fixture health check is suppressed for that
reason only), and asserts ``DATABASE_URL`` is unset so the truncate can only
hit the harness temp file.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

_BACKEND_STR = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
if _BACKEND_STR not in sys.path:
    sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun  # noqa: E402,F401

pytestmark = pytest.mark.unit

PURE = settings(max_examples=200, deadline=None)
DB = settings(max_examples=30, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])

_STUBBED_MODULE_NAMES = ["db.connection", "db.schedules", "db.agent_settings.resources", "database"]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _evict():
    for mod in _STUBBED_MODULE_NAMES:
        sys.modules.pop(mod, None)


@pytest.fixture
def ops(db_backend):
    _evict()
    assert db_backend != "sqlite" or not os.getenv("DATABASE_URL")
    from db.schedules import ScheduleOperations

    yield ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())
    _evict()


def _pcs():
    from services import pull_coordination_service

    return pull_coordination_service


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ===========================================================================
# Pure tier
# ===========================================================================

_requested = st.one_of(
    st.none(),
    st.integers(min_value=-10**12, max_value=10**12),
    st.floats(allow_nan=True, allow_infinity=False),
    st.text(max_size=12),
    st.integers(min_value=-10**6, max_value=10**6).map(str),
)


@PURE
@given(requested=_requested, cap=st.integers(min_value=60, max_value=7200))
@example(requested=None, cap=60)
@example(requested=7200, cap=7200)
@example(requested=7201, cap=7200)
@example(requested=0, cap=600)
def test_turn_limit_always_ends_inside_the_lease(requested, cap):
    """#2846 is the lease-renewal substitute: whatever a row asks for, the
    limit is in ``(0, cap]`` so ``limit + SLOT_TTL_BUFFER <= lease_seconds``;
    ``shortened_from`` is reported iff the ask was clipped."""
    from services.slot_service import SLOT_TTL_BUFFER

    limit, shortened = _pcs()._turn_limit(requested, cap)
    assert 0 < limit <= cap
    assert limit + SLOT_TTL_BUFFER <= cap + SLOT_TTL_BUFFER
    if shortened is not None:
        assert limit == cap and shortened > cap
    else:
        try:
            asked = int(requested)
        except (TypeError, ValueError):
            asked = None
        assert limit == (asked if asked is not None and 0 < asked <= cap else cap)


@PURE
@given(status=st.one_of(st.sampled_from(["success", "failed", "cancelled"]), st.text(max_size=10)),
       code=st.one_of(st.none(), st.sampled_from(["auth", " AUTH", "Auth ", "billing", "timeout"]),
                      st.text(max_size=8)))
def test_status_map_trust_boundary(status, code):
    """Only ``success`` becomes SUCCESS; a cancel becomes CANCELLED unless the
    code says auth (an agent cannot launder an auth failure into a clean
    cancel); everything else fails closed to FAILED."""
    pcs = _pcs()
    fake = MagicMock()
    fake.get_execution.side_effect = [
        SimpleNamespace(status="running", agent_name="a", backlog_metadata=None, id="e"),
        SimpleNamespace(status="running", agent_name="a", backlog_metadata=None, id="e"),
    ]
    fake.update_execution_status.return_value = False
    with patch.object(pcs, "db", fake):
        out = pcs.apply_task_result("e", "tok", status=status, content="c", error_code=code)
    written = fake.update_execution_status.call_args.kwargs["status"]
    is_auth = (code or "").strip().lower() == "auth"
    expected = ("success" if status == "success"
                else "cancelled" if status == "cancelled" and not is_auth
                else "failed")
    assert written == expected
    assert out.kind == "conflict"
    assert fake.update_execution_status.call_args.kwargs["claim_token"] == "tok"


_json = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.floats(allow_nan=False, allow_infinity=False),
              st.text(max_size=8)),
    lambda inner: st.one_of(st.lists(inner, max_size=3),
                            st.dictionaries(st.text(max_size=6), inner, max_size=3)),
    max_leaves=10,
)
_meta_keys = st.sampled_from(["from", "id", "envelope_id", "kind", "session_id", "resume_session_id",
                              "file_ids", "persist_session", "images", "model", "allowed_tools",
                              "max_turns", "timeout_seconds", "system_prompt", "task_overrides",
                              "schedule_context", "attempt", "correlation_id", "causation_id"])
_metadata_text = st.one_of(
    st.none(),
    st.text(max_size=20),
    st.dictionaries(_meta_keys, _json, max_size=8).map(json.dumps),
    _json.map(json.dumps),
)


@PURE
@given(raw=_metadata_text,
       message=st.one_of(st.none(), st.text(max_size=20)),
       count=st.one_of(st.none(), st.integers(min_value=-3, max_value=50)))
@example(raw='{"task_overrides": {"model": null}}', message=None, count=1)
def test_claim_response_is_total_and_frames_redelivery(raw, message, count):
    """Any ``backlog_metadata`` the column can hold yields a well-formed claim;
    the banner is prepended iff ``redelivery_count > 0`` and never replaces
    the stored message; lease/token pass through untouched."""
    pcs = _pcs()
    row = {"id": "e1", "agent_name": "alpha", "message": message, "backlog_metadata": raw,
           "lease_expires_at": "L", "claim_token": "T", "claimed_by_worker": "w",
           "redelivery_count": count, "triggered_by": "schedule",
           "source_agent_name": None, "source_user_id": None}
    with patch.object(pcs, "_compose_pull_system_prompt", lambda _a, _t, caller, **kw: caller):
        c = pcs._build_claim_response(row)
    env = c["envelope"]
    assert c["execution_id"] == "e1" and c["claim_token"] == "T"
    assert c["lease_expires_at"] == "L" == env["deadline"]
    assert env["to"] == "alpha" and env["from"]
    assert c["redelivery_count"] == (count or 0)
    assert isinstance(env["payload"]["task_overrides"], dict)
    msg = env["payload"]["message"]
    if (count or 0) > 0:
        assert msg.startswith("[Trinity re-delivery notice]")
        assert msg.endswith(message or "")
    else:
        assert msg == message


_names = st.from_regex(r"[a-z][a-z0-9-]{0,10}", fullmatch=True)


@PURE
@given(members=st.lists(_names, max_size=5), probe=_names,
       pads=st.lists(st.sampled_from(["", " ", "  ", "\t"]), min_size=12, max_size=12),
       empties=st.integers(min_value=0, max_value=3))
def test_allowlist_oracle(members, probe, pads, empties):
    """Membership is exact-name after strip, regardless of padding and empty
    entries; nothing outside the listed names is ever a pilot."""
    from services.pull_pilot import is_pull_pilot_agent

    parts = [f"{pads[i % 12]}{m}{pads[(i + 1) % 12]}" for i, m in enumerate(members)] + [""] * empties
    with patch.dict(os.environ, {"PULL_MODE_PILOT_AGENTS": ",".join(parts)}):
        assert is_pull_pilot_agent(probe) is (probe in members)
        for m in members:
            assert is_pull_pilot_agent(m)


@PURE
@given(trigger=st.one_of(st.none(), st.text(max_size=12),
                         st.sampled_from(sorted({"agent", "event", "schedule", "webhook", "reminder", "loop",
                                                 "fan_out", "a2a", "operator_response", "operator_ending",
                                                 "retry", "manual", "mcp", "chat", "session", "public",
                                                 "validation", "self_task", "slack", "paid"}))),
       pilot=st.booleans())
def test_pull_owns_dispatch_oracle(trigger, pilot):
    from services.pull_pilot import (PULL_REACHABLE_NON_AUTONOMOUS, PULL_REACHABLE_TRIGGERS,
                                     pull_owns_dispatch)
    from services.task_execution_service import _AUTONOMOUS_TRIGGERS

    with patch.dict(os.environ, {"PULL_MODE_PILOT_AGENTS": "alpha" if pilot else ""}):
        got = pull_owns_dispatch("alpha", trigger)
    expected = pilot and (trigger in PULL_REACHABLE_NON_AUTONOMOUS
                          or trigger in (_AUTONOMOUS_TRIGGERS & PULL_REACHABLE_TRIGGERS))
    assert got is bool(expected)


# ===========================================================================
# Real-DB tier
# ===========================================================================

_T0 = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)


def _truncate():
    _hrun("DELETE FROM schedule_executions")


def _enqueue(eid, minute, trigger="schedule", key=None, agent="alpha"):
    qa = _fmt(_T0 + timedelta(minutes=minute))
    _hrun(
        "INSERT INTO schedule_executions (id, schedule_id, agent_name, status, started_at, queued_at, "
        " message, triggered_by, conversation_key) "
        "VALUES (:id, '__manual__', :a, 'queued', :qa, :qa, 'm', :t, :k)",
        id=eid, a=agent, qa=qa, t=trigger, k=key,
    )


@DB
@given(lease_seconds=st.integers(min_value=0, max_value=10_000),
       delta_us=st.integers(min_value=-2_000_000, max_value=2_000_000))
@example(lease_seconds=900, delta_us=0)
@example(lease_seconds=900, delta_us=1)
@example(lease_seconds=0, delta_us=-1)
def test_lease_expiry_boundary(ops, lease_seconds, delta_us):
    """Expired iff ``now > lease`` (strict), at microsecond resolution, for any
    lease length — and the finder and the requeue CAS agree."""
    _truncate()
    _enqueue("e1", 0)
    row = ops.claim_next_queued("alpha", worker_id="w", lease_seconds=lease_seconds)
    lease = datetime.strptime(row["lease_expires_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
    started = datetime.strptime(row["started_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
    assert lease - started == timedelta(seconds=lease_seconds)
    now = _fmt(lease + timedelta(microseconds=delta_us))
    found = any(c["id"] == "e1" for c in ops.find_expired_leases(now_iso=now))
    assert found is (delta_us > 0)
    assert ops.requeue_expired_lease("e1", now_iso=now) is (delta_us > 0)


_TRIGGERS = ["schedule", "agent", "chat", "webhook", "manual", "event"]
_KEYS = [None, None, "session:chat:a", "session:chat:b", "room:r1", "channel:c1"]


@DB
@given(rows=st.lists(st.tuples(st.sampled_from(_TRIGGERS), st.sampled_from(_KEYS)),
                     min_size=1, max_size=8))
def test_claim_order_matches_oracle(ops, rows):
    """Claim repeatedly (never completing anything) and compare with an
    independent model: interactive trigger OR ``session:`` key first, oldest
    first within a group, skipping a conversation that already has a running
    turn, until nothing is claimable."""
    from services.pull_pilot import INTERACTIVE_TRIGGERS, WAITING_CONVERSATION_PREFIX

    _truncate()
    queued = []
    for i, (trig, key) in enumerate(rows):
        eid = f"r{i}"
        _enqueue(eid, i, trigger=trig, key=key)
        queued.append((eid, i, trig, key))

    def waiting(r):
        return r[2] in INTERACTIVE_TRIGGERS or (r[3] or "").startswith(WAITING_CONVERSATION_PREFIX)

    expected, running_keys, pending = [], set(), sorted(queued, key=lambda r: (not waiting(r), r[1]))
    while True:
        pick = next((r for r in pending if r[3] is None or r[3] not in running_keys), None)
        if pick is None:
            break
        expected.append(pick[0])
        pending.remove(pick)
        if pick[3] is not None:
            running_keys.add(pick[3])

    got = []
    for _ in range(len(rows) + 1):
        row = ops.claim_next_queued("alpha", worker_id="w", lease_seconds=900,
                                    interactive_triggers=INTERACTIVE_TRIGGERS,
                                    waiting_conversation_prefix=WAITING_CONVERSATION_PREFIX)
        if row is None:
            break
        got.append(row["id"])
    assert got == expected


_AUTH_TERMINALS = {"success", "cancelled", "skipped"}


@DB
@given(events=st.lists(st.tuples(st.sampled_from(["mine", "wrong"]),
                                 st.sampled_from(["success", "failed", "cancelled"])),
                       min_size=1, max_size=6))
@example(events=[("mine", "failed"), ("mine", "success"), ("mine", "success")])
@example(events=[("wrong", "success"), ("mine", "cancelled"), ("mine", "success")])
def test_result_cas_matches_oracle(ops, events):
    """Any sequence of reports against one claim: outcomes and the final row
    status equal a model of the CAS contract. In particular a wrong token is
    never ``applied``, an authoritative terminal never changes, and the only
    terminal that may be overwritten is FAILED → SUCCESS."""
    pcs = _pcs()
    _truncate()
    _enqueue("e1", 0, trigger="schedule")
    tok = ops.claim_next_queued("alpha", worker_id="w", lease_seconds=900)["claim_token"]

    state, expected, got = "running", [], []
    for who, status in events:
        if state in _AUTH_TERMINALS:
            kind = "replayed"
        elif who == "wrong":
            kind = "replayed" if state == "failed" else "conflict"
        elif status == "success":
            kind, state = "applied", "success"
        elif state == "failed":
            kind = "replayed"
        else:
            kind, state = "applied", status
        expected.append(kind)

    with patch.object(pcs.event_dispatch_service, "spawn_task_terminal_event"), \
         patch.object(pcs.channel_completion_report, "spawn_completion_report"), \
         patch.object(pcs.activity_service, "spawn_close_execution_activity"), \
         patch.object(pcs.subscription_auto_switch, "spawn_subscription_failure"), \
         patch.object(pcs, "_spawn_breaker_verdict"), \
         patch.object(pcs, "_spawn_post_turn_delivery"), \
         patch.object(pcs, "_shortened_note", return_value=""):
        for who, status in events:
            out = pcs.apply_task_result("e1", tok if who == "mine" else "not-it", status=status,
                                        content="c", error_code=None)
            got.append(out.kind)
    assert got == expected
    assert ops.get_execution("e1").status == state
