"""/edge-cases 2026-10-06 — pull coordination + pull pilot: parametrized edges.

Companion to ``test_ec_pull_coordination_properties.py``. Targets:

* ``services/pull_coordination_service.py`` — ``claim_next_task``,
  ``_turn_limit``, ``_build_claim_response``, ``_context_used``,
  ``apply_task_result`` (status map, CAS outcomes), ``_switch_failure_kind``,
  ``_delivery_metadata``, ``_spawn_post_turn_delivery``,
  ``_spawn_breaker_verdict``.
* ``services/pull_pilot.py`` — the ``PULL_MODE_PILOT_AGENTS`` gate,
  ``pull_queue_allowance``, ``pull_owns_dispatch``.
* The claim/lease SQL these services delegate to (``db/schedules/queue.py``)
  where the edge is a lease *semantic*: the strict ``lease < now`` expiry
  boundary, claim-after-terminal, re-claim after requeue, the #3127
  ``session:`` waiting-conversation prefix, and the one-running-turn
  IntegrityError retry budget.

Each parametrize id carries its matrix row number (the 2026-10-06 /edge-cases
matrix). Rows already covered by the existing suite
(``test_1081_*``, ``test_2842_2843_*``, ``test_1766_*``, ``test_2048_*``,
``test_3127_*``) are NOT repeated here.

Lease renewal ("heartbeat extension") does not exist by design: #2846 clamps
the turn limit to the agent timeout at claim so a healthy turn always ends
inside its lease. That substitute guarantee is asserted as a property in the
companion file; here row E07 pins that nothing a worker reports moves the lease.

Real-DB rows run through ``db_harness`` (SQLite; PostgreSQL too when
``TEST_POSTGRES_URL`` is set). No Docker / network / Redis.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_BACKEND_STR = str(Path(__file__).resolve().parent.parent.parent / "src" / "backend")
if _BACKEND_STR not in sys.path:
    sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun, scalar as _scalar  # noqa: E402,F401

pytestmark = pytest.mark.unit

# sys.modules hygiene (#762) — same churned set as test_1081_pull_endpoints.py.
_STUBBED_MODULE_NAMES = [
    "db.connection",
    "db.schedules",
    "db.agent_settings.resources",
    "database",
]


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
    for mod in ("db.connection", "db.schedules", "db.agent_settings.resources", "database"):
        sys.modules.pop(mod, None)


@pytest.fixture
def tmp_db(db_backend):
    _evict()
    try:
        yield db_backend
    finally:
        _evict()


@pytest.fixture
def ops(tmp_db):
    from db.schedules import ScheduleOperations

    return ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())


def _seed_agent(name="alpha", timeout=600):
    _hrun(
        "INSERT INTO agent_ownership (agent_name, owner_id, execution_timeout_seconds, created_at) "
        "VALUES (:n, 1, :t, '2026-01-01T00:00:00Z')",
        n=name, t=timeout,
    )


_T0 = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)


def _enqueue(agent="alpha", *, eid=None, minute=0, trigger="manual", key=None,
             meta=None, message="do it"):
    eid = eid or f"{trigger}-{minute}-{key or 'nokey'}"
    qa = (_T0 + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    _hrun(
        "INSERT INTO schedule_executions "
        "(id, schedule_id, agent_name, status, started_at, queued_at, message, "
        " triggered_by, conversation_key, backlog_metadata) "
        "VALUES (:id, '__manual__', :a, 'queued', :qa, :qa, :m, :t, :k, :meta)",
        id=eid, a=agent, qa=qa, m=message, t=trigger, k=key,
        meta=None if meta is None else json.dumps(meta),
    )
    return eid


@pytest.fixture
def pcs(tmp_db):
    from services import pull_coordination_service as _pcs

    return _pcs


@pytest.fixture
def pure_pcs():
    """No DB: for helpers that never touch it (or whose ``db`` is patched)."""
    from services import pull_coordination_service as _pcs

    return _pcs


# ===========================================================================
# E — lease semantics (claim / expiry boundary / re-claim / terminal)
# ===========================================================================


class TestLeaseSemantics:
    def _claimed(self, ops, lease_seconds=900):
        eid = _enqueue()
        row = ops.claim_next_queued("alpha", worker_id="w1", lease_seconds=lease_seconds)
        assert row["id"] == eid
        return row

    @pytest.mark.parametrize(
        "delta_us, expired",
        [
            pytest.param(-1, False, id="E01-now-one-us-before-lease"),
            pytest.param(0, False, id="E02-now-exactly-at-lease-is-NOT-expired"),
            pytest.param(1, True, id="E03-now-one-us-after-lease"),
        ],
    )
    def test_expiry_boundary_is_strict(self, ops, delta_us, expired):
        """``lease_expires_at < now`` — strictly. At the boundary instant the
        holder still owns the row; one microsecond later the reaper may take it.
        Both stamps are fixed-width ``%f`` ISO-Z, so the lexicographic SQL
        compare equals the chronological one at microsecond resolution."""
        row = self._claimed(ops)
        lease = datetime.strptime(row["lease_expires_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
        now = (lease + timedelta(microseconds=delta_us)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        found = [c["id"] for c in ops.find_expired_leases(now_iso=now)]
        assert (row["id"] in found) is expired
        # The transition CAS agrees with the finder at the same instant.
        assert ops.requeue_expired_lease(row["id"], now_iso=now) is expired

    def test_E04_lease_is_claim_instant_plus_lease_seconds_exactly(self, ops):
        row = self._claimed(ops, lease_seconds=900)
        started = datetime.strptime(row["started_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
        lease = datetime.strptime(row["lease_expires_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
        assert lease - started == timedelta(seconds=900)

    @pytest.mark.parametrize(
        "terminal",
        [pytest.param(s, id=f"E05-{s}") for s in ("success", "failed", "cancelled", "skipped")],
    )
    def test_claim_after_terminal_finds_nothing(self, ops, terminal):
        eid = _enqueue()
        _hrun("UPDATE schedule_executions SET status = :s WHERE id = :id", s=terminal, id=eid)
        assert ops.claim_next_queued("alpha", worker_id="w1", lease_seconds=900) is None

    def test_E06_reclaim_after_requeue_mints_a_new_token_and_fences_the_old(self, ops, pcs):
        _seed_agent()
        first = self._claimed(ops)
        far = "2999-01-01T00:00:00.000000Z"
        assert ops.requeue_expired_lease(first["id"], now_iso=far)
        second = ops.claim_next_queued("alpha", worker_id="w2", lease_seconds=900)
        assert second["id"] == first["id"]                      # same execution id (#1084)
        assert second["claim_token"] != first["claim_token"]
        assert second["redelivery_count"] == 1
        # The superseded worker's late terminal loses to the live holder.
        late = pcs.apply_task_result(first["id"], first["claim_token"], status="success", content="old")
        assert late.kind == "conflict"
        live = pcs.apply_task_result(second["id"], second["claim_token"], status="success", content="new")
        assert live.kind == "applied"
        assert ops.get_execution(first["id"]).response == "new"

    def test_E07_a_result_never_moves_the_lease(self, ops, pcs):
        """No lease renewal exists (#2846 replaced it with the turn clamp): a
        terminal write leaves ``lease_expires_at`` exactly as the claim set it."""
        _seed_agent()
        row = self._claimed(ops)
        pcs.apply_task_result(row["id"], row["claim_token"], status="failed",
                              content="x", error_code="crash")
        assert _scalar("SELECT lease_expires_at FROM schedule_executions WHERE id=:i",
                       i=row["id"]) == row["lease_expires_at"]

    def test_E08_result_for_a_never_claimed_row_is_conflict(self, ops, pcs):
        """A queued row has a NULL token; no presented token can match it."""
        _seed_agent()
        eid = _enqueue()
        out = pcs.apply_task_result(eid, "anything", status="success", content="x")
        assert out.kind == "conflict"
        assert ops.get_execution(eid).status == "queued"

    @pytest.mark.parametrize(
        "prior, incoming, kind, final",
        [
            pytest.param("cancelled", "success", "replayed", "cancelled", id="E09-success-over-cancelled"),
            pytest.param("skipped", "success", "replayed", "skipped", id="E10-success-over-skipped"),
            pytest.param("success", "failed", "replayed", "success", id="E11-failed-over-success"),
            pytest.param("failed", "failed", "replayed", "failed", id="E12-failed-over-failed"),
            pytest.param("failed", "cancelled", "replayed", "failed", id="E13-cancel-over-failed"),
            pytest.param("failed", "success", "applied", "success", id="E14-late-success-corrects-failed"),
        ],
    )
    def test_terminal_precedence(self, ops, pcs, prior, incoming, kind, final):
        _seed_agent()
        row = self._claimed(ops)
        _hrun("UPDATE schedule_executions SET status=:s WHERE id=:i", s=prior, i=row["id"])
        out = pcs.apply_task_result(row["id"], row["claim_token"], status=incoming,
                                    content="late", error_code=None if incoming == "success" else "crash")
        assert out.kind == kind
        assert ops.get_execution(row["id"]).status == final

    def test_E15_conflict_when_row_vanishes_between_reads(self, pure_pcs):
        """CAS lost and the re-read finds nothing → ``conflict`` with status None."""
        running = SimpleNamespace(status="running", agent_name="alpha", backlog_metadata=None)
        fake = MagicMock()
        fake.get_execution.side_effect = [running, None]
        fake.update_execution_status.return_value = False
        with patch.object(pure_pcs, "db", fake):
            out = pure_pcs.apply_task_result("e1", "tok", status="success", content="x")
        assert (out.kind, out.status) == ("conflict", None)


# ===========================================================================
# C — #3127 waiting-conversation prefix + one-running-turn retry budget
# ===========================================================================


class TestClaimOrder3127:
    def _pull(self, ops):
        from services.pull_pilot import INTERACTIVE_TRIGGERS, WAITING_CONVERSATION_PREFIX

        row = ops.claim_next_queued(
            "alpha", worker_id="w", lease_seconds=900,
            interactive_triggers=INTERACTIVE_TRIGGERS,
            waiting_conversation_prefix=WAITING_CONVERSATION_PREFIX,
        )
        return row["id"] if row else None

    @pytest.mark.parametrize(
        "key, waiting",
        [
            pytest.param("session:chat:s1", True, id="C01-session-prefix"),
            pytest.param("session:", True, id="C02-bare-prefix"),
            pytest.param("session", False, id="C03-no-colon"),
            pytest.param("sessionX:chat", False, id="C04-near-miss"),
            pytest.param("channel:session:x", False, id="C05-substring-not-prefix"),
            pytest.param("room:abc", False, id="C06-other-conversation"),
            pytest.param(None, False, id="C07-keyless"),
        ],
    )
    def test_only_a_true_prefix_jumps_the_batch_queue(self, ops, key, waiting):
        batch = _enqueue(trigger="schedule", minute=0)
        agent_turn = _enqueue(trigger="agent", minute=5, key=key)  # autonomous trigger
        first = self._pull(ops)
        assert first == (agent_turn if waiting else batch)

    def test_C08_like_wildcards_in_a_key_do_not_widen_the_prefix(self, ops):
        """``startswith(autoescape=True)``: a key that would match ``session%``
        or ``session_`` as a LIKE pattern must not count as waiting."""
        batch = _enqueue(trigger="schedule", minute=0)
        _enqueue(trigger="agent", minute=5, key="session_x")
        _enqueue(trigger="agent", minute=6, key="session%x")
        assert self._pull(ops) == batch

    def test_C09_three_lost_races_read_as_an_empty_poll(self, ops):
        from sqlalchemy.exc import IntegrityError
        import db.schedules.queue as q

        calls = {"n": 0}

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, stmt):
                calls["n"] += 1
                raise IntegrityError("UPDATE", {}, Exception("one running turn"))

        engine = MagicMock()
        engine.begin.side_effect = lambda: _Conn()
        engine.dialect.name = "sqlite"
        with patch.object(q, "get_engine", return_value=engine):
            assert ops.claim_next_queued("alpha", worker_id="w", lease_seconds=900) is None
        assert calls["n"] == 3


# ===========================================================================
# T — turn limit (#2846) and claim envelope
# ===========================================================================


class TestTurnLimit:
    @pytest.mark.parametrize(
        "requested, expected",
        [
            pytest.param(None, (600, None), id="T01-absent"),
            pytest.param("", (600, None), id="T02-empty-string"),
            pytest.param("abc", (600, None), id="T03-garbage"),
            pytest.param(0, (600, None), id="T04-zero"),
            pytest.param(-1, (600, None), id="T05-negative"),
            pytest.param(1, (1, None), id="T06-one"),
            pytest.param(599, (599, None), id="T07-cap-minus-1"),
            pytest.param(600, (600, None), id="T08-at-cap-not-shortened"),
            pytest.param(601, (600, 601), id="T09-cap-plus-1-shortened"),
            pytest.param("900", (600, 900), id="T10-numeric-string"),
            pytest.param(599.9, (599, None), id="T11-float-truncates"),
            pytest.param(float("nan"), (600, None), id="T12-nan"),
        ],
    )
    def test_turn_limit(self, pure_pcs, requested, expected):
        assert pure_pcs._turn_limit(requested, 600) == expected

    def test_T13_db_timeout_failure_propagates_from_claim(self, pure_pcs):
        """No fail-safe on the cap read: a DB outage surfaces to the router
        (500) rather than claiming with an invented lease. The poll stamp was
        already attempted."""
        with patch.object(pure_pcs, "record_worker_poll") as poll, \
             patch.object(pure_pcs.db, "get_execution_timeout", side_effect=RuntimeError("db")):
            with pytest.raises(RuntimeError):
                pure_pcs.claim_next_task("alpha", "w")
        poll.assert_called_once_with("alpha")

    @pytest.mark.xfail(
        strict=True,
        reason="BUG: platform prompt advertises the row's stale unclamped timeout, "
               "not the #2846-clamped turn limit — #3321",
    )
    def test_T14_prompt_timeout_equals_the_enforced_limit(self, ops, pcs, monkeypatch):
        """#3114 says the pulled prompt's timeout matches push; push tells the
        model the limit it actually runs under. A row enqueued under a 1800s
        agent timeout that is then lowered to 600s is clamped to 600 at claim,
        but ``_build_claim_response`` composes the prompt BEFORE the clamp, so
        the model is told "Timeout: 1800s — plan to finish well within this
        budget" and is killed at 600s."""
        _seed_agent(timeout=600)
        _enqueue(trigger="schedule", meta={"timeout_seconds": 1800})
        seen = {}
        real = pcs._compose_pull_system_prompt

        def spy(*a, **kw):
            seen["timeout"] = kw.get("timeout_seconds")
            return real(*a, **kw)

        monkeypatch.setattr(pcs, "_compose_pull_system_prompt", spy)
        monkeypatch.setattr(pcs, "_resolve_agent_runtime", lambda _n: "claude-code")
        claim = pcs.claim_next_task("alpha", "w1")
        enforced = claim["envelope"]["payload"]["task_overrides"]["timeout_seconds"]
        assert enforced == 600
        prompt = claim["envelope"]["payload"]["task_overrides"]["system_prompt"] or ""
        assert "1800s" not in prompt, "model is told a budget it will not get"
        assert seen["timeout"] == enforced


class TestBuildClaimResponse:
    @pytest.fixture(autouse=True)
    def _cheap_prompt(self, pure_pcs, monkeypatch):
        monkeypatch.setattr(pure_pcs, "_compose_pull_system_prompt",
                            lambda _a, _t, caller, **kw: caller)

    def _row(self, **kw):
        base = {"id": "e1", "agent_name": "alpha", "message": "hi", "backlog_metadata": None,
                "lease_expires_at": "2026-10-06T10:00:00.000000Z", "claim_token": "tok",
                "claimed_by_worker": "w", "redelivery_count": 0, "triggered_by": "schedule",
                "source_agent_name": None, "source_user_id": None}
        base.update(kw)
        return base

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param(None, id="B01-null-column"),
            pytest.param("", id="B02-empty-string"),
            pytest.param("null", id="B03-json-null"),
            pytest.param("[1,2]", id="B04-json-list"),
            pytest.param("{not json", id="B05-malformed"),
            pytest.param('"a string"', id="B06-json-scalar"),
        ],
    )
    def test_unusable_metadata_degrades_to_defaults(self, pure_pcs, raw):
        c = pure_pcs._build_claim_response(self._row(backlog_metadata=raw))
        env = c["envelope"]
        assert env["id"] == "e1" and env["correlation_id"] == "e1"
        assert env["kind"] == "task" and env["from"] == "system"
        assert env["payload"]["task_overrides"] == {"system_prompt": None}

    @pytest.mark.parametrize(
        "row_kw, meta, expected",
        [
            pytest.param({"source_user_id": 0}, {}, "0", id="B07-user-id-zero-is-a-sender"),
            pytest.param({"source_agent_name": "bob", "source_user_id": 5}, {}, "bob", id="B08-agent-beats-user"),
            pytest.param({"source_agent_name": "bob"}, {"from": ""}, "bob", id="B09-empty-meta-from-falls-back"),
            pytest.param({}, {"from": "carol"}, "carol", id="B10-meta-from-wins"),
        ],
    )
    def test_sender_resolution(self, pure_pcs, row_kw, meta, expected):
        c = pure_pcs._build_claim_response(self._row(backlog_metadata=json.dumps(meta), **row_kw))
        assert c["envelope"]["from"] == expected

    @pytest.mark.parametrize(
        "count, banner",
        [
            pytest.param(None, False, id="B11-null-count"),
            pytest.param(0, False, id="B12-first-delivery"),
            pytest.param(-1, False, id="B13-negative-count"),
            pytest.param(1, True, id="B14-first-redelivery"),
        ],
    )
    def test_redelivery_banner(self, pure_pcs, count, banner):
        from config import MAX_REDELIVERY

        c = pure_pcs._build_claim_response(self._row(redelivery_count=count))
        msg = c["envelope"]["payload"]["message"]
        assert msg.startswith("[Trinity re-delivery notice]") is banner
        assert msg.endswith("hi")
        assert c["redelivery_count"] == (count or 0)
        if banner:
            assert f"attempt {count + 1} of {MAX_REDELIVERY}" in msg

    def test_B15_banner_on_a_null_message_still_frames(self, pure_pcs):
        c = pure_pcs._build_claim_response(self._row(message=None, redelivery_count=2))
        assert c["envelope"]["payload"]["message"].endswith("\n\n")

    @pytest.mark.parametrize(
        "meta, key, present, value",
        [
            pytest.param({"persist_session": False}, "persist_session", True, False, id="B16-false-persist-kept"),
            pytest.param({"persist_session": None}, "persist_session", False, None, id="B17-null-persist-dropped"),
            pytest.param({"images": []}, "images", False, None, id="B18-empty-images-dropped"),
            pytest.param({"file_ids": []}, "file_ids", True, [], id="B19-empty-file-ids-kept"),
        ],
    )
    def test_optional_payload_fields(self, pure_pcs, meta, key, present, value):
        payload = pure_pcs._build_claim_response(self._row(backlog_metadata=json.dumps(meta)))["envelope"]["payload"]
        assert (key in payload) is present
        if present:
            assert payload[key] == value

    @pytest.mark.parametrize(
        "meta, expected",
        [
            pytest.param({"session_id": "s", "resume_session_id": "r"}, "s", id="B20-session-id-first"),
            pytest.param({"resume_session_id": "r", "chat_session_id": "c"}, "r", id="B21-resume-not-chat"),
            pytest.param({"chat_session_id": "c"}, None, id="B22-chat-row-id-never-resumes"),
        ],
    )
    def test_session_identity(self, pure_pcs, meta, expected):
        payload = pure_pcs._build_claim_response(self._row(backlog_metadata=json.dumps(meta)))["envelope"]["payload"]
        assert payload["session_id"] == expected

    @pytest.mark.parametrize(
        "meta, expected",
        [
            pytest.param({"model": "a", "task_overrides": {"model": "b"}}, "b", id="B23-nested-overlays-flat"),
            pytest.param({"model": "a", "task_overrides": {"model": None}}, "a", id="B24-nested-null-ignored"),
            pytest.param({"model": "a", "task_overrides": ["model"]}, "a", id="B25-nested-non-dict-ignored"),
            pytest.param({"model": None}, None, id="B26-flat-null-absent"),
        ],
    )
    def test_override_overlay(self, pure_pcs, meta, expected):
        ov = pure_pcs._build_claim_response(self._row(backlog_metadata=json.dumps(meta)))["envelope"]["payload"]["task_overrides"]
        assert ov.get("model") == expected


# ===========================================================================
# R — result sink (status map, context, error text, side-effect gates)
# ===========================================================================


class TestResultSink:
    def _fake_db(self, won=False, after="running"):
        fake = MagicMock()
        fake.get_execution.side_effect = [
            SimpleNamespace(status="running", agent_name="alpha", backlog_metadata=None, id="e1"),
            SimpleNamespace(status=after, agent_name="alpha", backlog_metadata=None, id="e1"),
        ]
        fake.update_execution_status.return_value = won
        return fake

    @pytest.mark.parametrize(
        "status, code, row_status",
        [
            pytest.param("success", None, "success", id="R01-success"),
            pytest.param("success", "auth", "success", id="R02-success-with-stray-code"),
            pytest.param("cancelled", None, "cancelled", id="R03-clean-cancel"),
            pytest.param("cancelled", "auth", "failed", id="R04-auth-mislabelled-cancel"),
            pytest.param("cancelled", "  AUTH ", "failed", id="R05-auth-case-space"),
            pytest.param("cancelled", "timeout", "cancelled", id="R06-cancel-other-code"),
            pytest.param("failed", None, "failed", id="R07-failed"),
            pytest.param("weird", None, "failed", id="R08-unknown-status-fails-closed"),
        ],
    )
    def test_status_map(self, pure_pcs, status, code, row_status):
        fake = self._fake_db()
        with patch.object(pure_pcs, "db", fake):
            pure_pcs.apply_task_result("e1", "tok", status=status, content="c", error_code=code)
        assert fake.update_execution_status.call_args.kwargs["status"] == row_status

    @pytest.mark.parametrize(
        "content, code, error",
        [
            pytest.param("boom", "crash", "[crash] boom", id="R09-code-folded"),
            pytest.param(None, "crash", "[crash]", id="R10-code-no-content"),
            pytest.param("boom", None, "boom", id="R11-content-no-code"),
            pytest.param(None, None, None, id="R12-neither-is-null-not-empty"),
            pytest.param("", None, None, id="R13-empty-content-is-null"),
        ],
    )
    def test_error_text(self, pure_pcs, content, code, error):
        fake = self._fake_db()
        with patch.object(pure_pcs, "db", fake):
            pure_pcs.apply_task_result("e1", "tok", status="failed", content=content, error_code=code)
        assert fake.update_execution_status.call_args.kwargs["result"].error == error

    @pytest.mark.parametrize(
        "meta, expected_max",
        [
            pytest.param(None, 200000, id="R14-no-metadata"),
            pytest.param({"context_window": 0}, 200000, id="R15-zero-window-defaults"),
            pytest.param({"context_window": 1000000}, 1000000, id="R16-1m-window"),
        ],
    )
    def test_context_max(self, pure_pcs, meta, expected_max):
        fake = self._fake_db()
        with patch.object(pure_pcs, "db", fake):
            pure_pcs.apply_task_result("e1", "tok", status="success", content="c", metadata=meta)
        assert fake.update_execution_status.call_args.kwargs["result"].context_max == expected_max

    @pytest.mark.parametrize(
        "meta, tokens, expected",
        [
            pytest.param({}, None, None, id="R17-nothing-known"),
            pytest.param({}, 0, None, id="R18-zero-tokens-unknown"),
            pytest.param({}, 42, 42, id="R19-tokens-fallback"),
            pytest.param({"input_tokens": 7}, 42, 7, id="R20-input-beats-tokens"),
            pytest.param({"cache_read_tokens": 3, "cache_creation_tokens": 4, "input_tokens": 9}, 42, 7,
                         id="R21-cache-beats-input"),
            pytest.param({"cache_read_tokens": None, "input_tokens": 9}, None, 9, id="R22-null-cache"),
        ],
    )
    def test_context_used(self, pure_pcs, meta, tokens, expected):
        assert pure_pcs._context_used(meta, tokens) == expected

    def test_R23_lost_cas_spawns_no_side_effects(self, pure_pcs):
        fake = self._fake_db(won=False, after="success")
        with patch.object(pure_pcs, "db", fake), \
             patch.object(pure_pcs.event_dispatch_service, "spawn_task_terminal_event") as ev, \
             patch.object(pure_pcs.channel_completion_report, "spawn_completion_report") as rep, \
             patch.object(pure_pcs.activity_service, "spawn_close_execution_activity") as act, \
             patch.object(pure_pcs.subscription_auto_switch, "spawn_subscription_failure") as sw, \
             patch.object(pure_pcs, "_spawn_breaker_verdict") as br, \
             patch.object(pure_pcs, "_spawn_post_turn_delivery") as dl:
            out = pure_pcs.apply_task_result("e1", "tok", status="failed", content="x", error_code="billing")
        assert out.kind == "replayed"
        for m in (ev, rep, act, sw, br, dl):
            m.assert_not_called()

    @pytest.mark.parametrize(
        "status, code, switch",
        [
            pytest.param("failed", "billing", "rate_limit", id="R24-billing-to-rate-limit"),
            pytest.param("cancelled", "auth", "auth", id="R25-auth-cancel-switches"),
            pytest.param("success", "billing", None, id="R26-success-never-switches"),
            pytest.param("failed", "timeout", None, id="R27-unknown-code-no-switch"),
        ],
    )
    def test_switch_only_on_won_failure(self, pure_pcs, status, code, switch):
        fake = self._fake_db(won=True)
        with patch.object(pure_pcs, "db", fake), \
             patch.object(pure_pcs.event_dispatch_service, "spawn_task_terminal_event"), \
             patch.object(pure_pcs.channel_completion_report, "spawn_completion_report"), \
             patch.object(pure_pcs.activity_service, "spawn_close_execution_activity"), \
             patch.object(pure_pcs.subscription_auto_switch, "spawn_subscription_failure") as sw, \
             patch.object(pure_pcs, "_spawn_breaker_verdict"), \
             patch.object(pure_pcs, "_spawn_post_turn_delivery"):
            out = pure_pcs.apply_task_result("e1", "tok", status=status, content="quota", error_code=code)
        assert out.kind == "applied"
        if switch is None:
            sw.assert_not_called()
        else:
            assert sw.call_args.kwargs["failure_kind"] == switch

    @pytest.mark.parametrize(
        "code, text, expected",
        [
            pytest.param("billing", "", "rate_limit", id="R28-billing"),
            pytest.param(" Billing ", "", "rate_limit", id="R29-billing-case-space"),
            pytest.param("auth", "", "auth", id="R30-auth"),
            pytest.param(None, "", None, id="R31-none"),
            pytest.param("", "", None, id="R32-empty"),
            pytest.param("rate_limit", "", None, id="R33-sub003-vocab-not-wire-vocab"),
        ],
    )
    def test_switch_failure_kind(self, pure_pcs, code, text, expected):
        assert pure_pcs._switch_failure_kind(code, text) == expected

    @pytest.mark.parametrize(
        "row_status, is_auth, code",
        [
            pytest.param("success", False, None, id="R34-success-records-none"),
            pytest.param("failed", True, "auth", id="R35-auth-records-auth"),
            pytest.param("failed", False, "SKIP", id="R36-other-failure-records-nothing"),
            pytest.param("cancelled", False, "SKIP", id="R37-cancel-records-nothing"),
        ],
    )
    def test_breaker_verdict(self, pure_pcs, row_status, is_auth, code):
        from services import task_execution_service as tes

        with patch.object(tes, "_spawn_bg") as bg, \
             patch.object(tes, "_record_dispatch_terminal") as rec, \
             patch.object(tes, "dispatch_breaker_active", return_value=False):
            pure_pcs._spawn_breaker_verdict("alpha", row_status, is_auth)
        if code == "SKIP":
            rec.assert_not_called()
            bg.assert_not_called()
        else:
            rec.assert_called_once_with("alpha", False, code)
            bg.assert_called_once()

    def test_R38_breaker_failure_is_swallowed(self, pure_pcs):
        from services import task_execution_service as tes

        with patch.object(tes, "dispatch_breaker_active", side_effect=RuntimeError("redis")):
            pure_pcs._spawn_breaker_verdict("alpha", "success", False)  # must not raise


class TestDelivery:
    @pytest.mark.parametrize(
        "raw, wants",
        [
            pytest.param(None, False, id="D01-no-metadata"),
            pytest.param("{bad", False, id="D02-malformed"),
            pytest.param("[1]", False, id="D03-list"),
            pytest.param(json.dumps({"save_to_session": True}), True, id="D04-save"),
            pytest.param(json.dumps({"collaboration_activity_id": "a"}), True, id="D05-collab"),
            pytest.param(json.dumps({"is_self_task": True}), False, id="D06-self-task-without-activity"),
            pytest.param(json.dumps({"is_self_task": True, "self_task_activity_id": "s"}), True, id="D07-self-task"),
            pytest.param(json.dumps({"self_task_activity_id": "s"}), False, id="D08-activity-without-flag"),
        ],
    )
    def test_delivery_metadata(self, pure_pcs, raw, wants):
        got = pure_pcs._delivery_metadata(SimpleNamespace(backlog_metadata=raw))
        assert (got is not None) is wants

    def test_D09_no_running_loop_still_wakes_the_waiter(self, pure_pcs):
        execution = SimpleNamespace(id="e1", agent_name="alpha",
                                    backlog_metadata=json.dumps({"save_to_session": True}))
        with patch("services.sync_waiter.signal_sync_waiter") as sig:
            pure_pcs._spawn_post_turn_delivery(execution, "success")
        sig.assert_called_once_with("e1", None, None)


# ===========================================================================
# P — pilot gate (PULL_MODE_PILOT_AGENTS) + queue allowance + ownership
# ===========================================================================


class TestPilotGate:
    @pytest.mark.parametrize(
        "raw, name, pilot",
        [
            pytest.param(None, "alpha", False, id="P01-unset"),
            pytest.param("", "alpha", False, id="P02-empty"),
            pytest.param("  ,  , ", "alpha", False, id="P03-only-separators"),
            pytest.param(" alpha , ,beta,, ", "alpha", True, id="P04-padded-and-empty-entries"),
            pytest.param(" alpha , ,beta,, ", "beta", True, id="P05-last-entry"),
            pytest.param("alpha", "Alpha", False, id="P06-case-sensitive"),
            pytest.param("alpha", "alph", False, id="P07-no-prefix-match"),
            pytest.param("alpha-1", "alpha", False, id="P08-no-substring-match"),
            pytest.param("*", "alpha", False, id="P09-star-is-not-a-wildcard"),
            pytest.param("true", "alpha", False, id="P10-truthy-is-not-all-on"),
            pytest.param("1", "alpha", False, id="P11-one-is-not-all-on"),
            pytest.param("alpha,", "", False, id="P12-empty-name-never-pilot"),
            pytest.param("alpha;beta", "beta", False, id="P13-semicolon-is-not-a-separator"),
        ],
    )
    def test_allowlist(self, monkeypatch, raw, name, pilot):
        from services.pull_pilot import is_pull_pilot_agent

        if raw is None:
            monkeypatch.delenv("PULL_MODE_PILOT_AGENTS", raising=False)
        else:
            monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", raw)
        assert is_pull_pilot_agent(name) is pilot

    def test_P14_flag_is_read_live_not_cached(self, monkeypatch):
        from services.pull_pilot import is_pull_pilot_agent

        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "alpha")
        assert is_pull_pilot_agent("alpha")
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "")
        assert not is_pull_pilot_agent("alpha")

    @pytest.mark.parametrize(
        "pilot, timeout, expected",
        [
            pytest.param(False, 600, 0, id="P15-non-pilot-zero"),
            pytest.param(True, 600, 600, id="P16-pilot-timeout"),
            pytest.param(True, -5, 0, id="P17-negative-clamped"),
            pytest.param(True, 0, 0, id="P18-zero"),
            pytest.param(True, "900", 900, id="P19-numeric-string"),
            pytest.param(True, "abc", 0, id="P20-garbage-fails-safe"),
            pytest.param(True, RuntimeError("db"), 0, id="P21-db-error-fails-safe"),
        ],
    )
    def test_queue_allowance(self, monkeypatch, pilot, timeout, expected):
        import database
        from services.pull_pilot import pull_queue_allowance

        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "alpha" if pilot else "")
        fake = MagicMock()
        if isinstance(timeout, Exception):
            fake.get_execution_timeout.side_effect = timeout
        else:
            fake.get_execution_timeout.return_value = timeout
        monkeypatch.setattr(database, "db", fake)
        assert pull_queue_allowance("alpha") == expected

    @pytest.mark.parametrize(
        "trigger, owned",
        [
            pytest.param(None, False, id="P22-null-trigger"),
            pytest.param("", False, id="P23-empty-trigger"),
            pytest.param("validation", True, id="P24-validation"),
            pytest.param("chat", True, id="P25-chat-3127"),
            pytest.param("CHAT", False, id="P26-trigger-case-sensitive"),
            pytest.param("chat ", False, id="P27-trigger-not-stripped"),
            pytest.param("retry", True, id="P28-retry"),
        ],
    )
    def test_owns_dispatch(self, monkeypatch, trigger, owned):
        from services.pull_pilot import pull_owns_dispatch

        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "alpha")
        assert pull_owns_dispatch("alpha", trigger) is owned
