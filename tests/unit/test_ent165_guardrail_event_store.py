"""Guardrail event store and its read seams (trinity-enterprise#165, S1).

Assertions land on REAL rows through the real service and the real `database.db`
facade. `db_backend` parametrizes onto SQLite and, when `TEST_POSTGRES_URL` is
set, real PostgreSQL — the only place the `ON CONFLICT DO NOTHING ... RETURNING`
insert and the JSON details column can be proved against the dialect production
may run on.

What is pinned here is the contract other features consume (plan §6):

- an event is validated on its own: an unknown `kind`/`hook` is kept as
  `other` with the raw value, a malformed event is counted in `rejected`, and
  neither fails the rest of the batch (agent images and backends upgrade
  independently);
- `event_id` makes a resend idempotent, per agent;
- `execution_id` is attribution: an over-long one is nulled, and every count
  filters by the authenticated agent;
- `count_by_execution` / `count_by_agent` return the §6.4 shape, with the
  agent's reporting state beside the counts so "0 events" is distinguishable
  from "not reporting";
- the retention prune and the agent rename / hard-delete follow the rows.
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

from db_harness import db_backend  # noqa: E402,F401

pytest.importorskip("sqlalchemy", reason="backend venv required")
fakeredis = pytest.importorskip("fakeredis")

from sqlalchemy import select  # noqa: E402

from database import db  # noqa: E402
from db.engine import get_engine  # noqa: E402
from db.tables import guardrail_events  # noqa: E402
from services import guardrail_event_service as svc  # noqa: E402

AGENT = "guard-agent"
OTHER = "guard-other"


@pytest.fixture
def redis(monkeypatch):
    """The reporter liveness store. A fresh fakeredis per test, reached through
    the service's own seam (asserted below, so a stub that never lands cannot
    pass as "never reported")."""
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(svc, "_get_redis", lambda: client)
    return client


def _ev(
    event_id="e-1",
    kind="deny",
    hook="turn_tier_guard",
    code="shell_owner_only",
    execution_id="exec-1",
    ts="2026-11-08T10:00:00.000000Z",
    **extra,
):
    ev = {
        "event_id": event_id,
        "ts": ts,
        "kind": kind,
        "hook": hook,
        "tool": "Bash",
        "code": code,
        "execution_id": execution_id,
    }
    ev.update(extra)
    return ev


def _rows(agent=AGENT):
    with get_engine().connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                select(guardrail_events)
                .where(guardrail_events.c.agent_name == agent)
                .order_by(guardrail_events.c.id)
            ).mappings()
        ]


def _ingest(events, agent=AGENT, runtime="claude-code", dropped=0):
    return svc.ingest(agent, events, runtime=runtime, dropped=dropped)


# ---------------------------------------------------------------------------
# Ingest: per-event validation, never per batch
# ---------------------------------------------------------------------------


class TestIngest:
    def test_a_valid_event_becomes_a_row(self, db_backend, redis):
        res = _ingest(
            [_ev(tier="shared", turn_kind="person", path="/home/developer/x")]
        )
        assert res == {"accepted": 1, "duplicates": 0, "rejected": 0}
        [row] = _rows()
        assert row["agent_name"] == AGENT
        assert row["event_id"] == "e-1"
        assert row["kind"] == "deny"
        assert row["hook"] == "turn_tier_guard"
        assert row["code"] == "shell_owner_only"
        assert row["tool"] == "Bash"
        assert row["execution_id"] == "exec-1"
        assert row["occurred_at"] == "2026-11-08T10:00:00.000000Z"
        assert row["received_at"].endswith("Z")
        details = svc.load_details(row["details"])
        assert details["tier"] == "shared"
        assert details["turn_kind"] == "person"
        assert details["path"] == "/home/developer/x"

    def test_an_epoch_ts_is_stored_as_iso_z(self, db_backend, redis):
        """The spool writes `ts` as an epoch float; the projection converts it,
        but a forwarder that forwards it raw must not lose the event."""
        epoch = datetime(2026, 11, 8, 9, 30, tzinfo=timezone.utc).timestamp()
        _ingest([_ev(ts=epoch)])
        assert _rows()[0]["occurred_at"] == "2026-11-08T09:30:00.000000Z"

    def test_an_unknown_kind_and_hook_are_kept_as_other_and_the_batch_lands(
        self, db_backend, redis
    ):
        """A newer agent image than this backend: the event is stored, never refused."""
        res = _ingest(
            [
                _ev(
                    "new-1",
                    kind="egress_refused",
                    hook="network_guard",
                    code="dns_blocked",
                ),
                _ev("old-1"),
            ]
        )
        assert res == {"accepted": 2, "duplicates": 0, "rejected": 0}
        new = next(r for r in _rows() if r["event_id"] == "new-1")
        assert (new["kind"], new["hook"]) == ("other", "other")
        details = svc.load_details(new["details"])
        assert details["kind_raw"] == "egress_refused"
        assert details["hook_raw"] == "network_guard"
        assert new["code"] == "dns_blocked"

    def test_a_new_code_needs_no_backend_change(self, db_backend, redis):
        _ingest([_ev(code="schedule_creator_tier_refused")])
        assert _rows()[0]["code"] == "schedule_creator_tier_refused"

    @pytest.mark.parametrize(
        "bad",
        [
            "not-a-dict",
            {
                "ts": "2026-11-08T10:00:00Z",
                "kind": "deny",
                "hook": "bash_guardrail",
            },  # no event_id
            _ev(event_id=""),
            _ev(event_id="x" * 65),
            _ev(ts="yesterday"),
            _ev(ts=None),
            _ev(ts=True),
            _ev(kind=None),
            _ev(kind="Deny Everything"),
            _ev(hook=7),
            _ev(code="Not A Code"),
            _ev(code="x" * 65),
        ],
    )
    def test_a_malformed_event_is_rejected_and_the_rest_land(
        self, db_backend, redis, bad
    ):
        res = _ingest([bad, _ev("good-1")])
        assert res == {"accepted": 1, "duplicates": 0, "rejected": 1}
        assert [r["event_id"] for r in _rows()] == ["good-1"]

    def test_an_over_long_execution_id_is_nulled_not_rejected(self, db_backend, redis):
        _ingest([_ev(execution_id="x" * 129), _ev("e-2", execution_id="y" * 128)])
        by_id = {r["event_id"]: r for r in _rows()}
        assert by_id["e-1"]["execution_id"] is None
        assert by_id["e-2"]["execution_id"] == "y" * 128

    def test_a_secret_in_path_or_rule_is_sanitized_and_bounded(self, db_backend, redis):
        """Defence in depth: the projection sanitizes too, but the store does not trust it."""
        fake = "ghp_" + "A" * 36
        _ingest(
            [_ev(path=f"/tmp/{fake}/x" + "p" * 600, rule=f"deny {fake}" + "r" * 300)]
        )
        details = svc.load_details(_rows()[0]["details"])
        assert fake not in details["path"] and fake not in details["rule"]
        assert len(details["path"]) <= 512
        assert len(details["rule"]) <= 200

    def test_fields_outside_the_allowlist_are_never_stored(self, db_backend, redis):
        _ingest(
            [
                _ev(
                    command_prefix="curl -H 'Authorization: x'",
                    traceback="Traceback ...",
                    reason="prose",
                    error="boom",
                )
            ]
        )
        details = svc.load_details(_rows()[0]["details"])
        for leaked in ("command_prefix", "traceback", "reason", "error"):
            assert leaked not in details

    def test_an_empty_batch_is_a_keepalive_and_stores_nothing(self, db_backend, redis):
        res = _ingest([])
        assert res == {"accepted": 0, "duplicates": 0, "rejected": 0}
        assert _rows() == []
        assert svc.reporting_state(AGENT)["state"] == "reporting"


class TestIdempotency:
    def test_a_resent_event_is_a_duplicate_not_a_second_row(self, db_backend, redis):
        assert _ingest([_ev()])["accepted"] == 1
        res = _ingest([_ev(), _ev("e-2")])
        assert res == {"accepted": 1, "duplicates": 1, "rejected": 0}
        assert [r["event_id"] for r in _rows()] == ["e-1", "e-2"]

    def test_a_repeat_inside_one_batch_is_a_duplicate(self, db_backend, redis):
        res = _ingest([_ev(), _ev()])
        assert res == {"accepted": 1, "duplicates": 1, "rejected": 0}
        assert len(_rows()) == 1

    def test_the_same_event_id_on_another_agent_is_its_own_row(self, db_backend, redis):
        _ingest([_ev()])
        assert _ingest([_ev()], agent=OTHER)["accepted"] == 1
        assert len(_rows(AGENT)) == 1 and len(_rows(OTHER)) == 1


# ---------------------------------------------------------------------------
# Reporting state
# ---------------------------------------------------------------------------


class TestReporting:
    def test_the_stub_is_the_store_the_service_reads(self, db_backend, redis):
        """Precondition for every "never" assertion below: the fake is reached."""
        _ingest([], runtime="codex", dropped=3)
        keys = list(redis.scan_iter("agent:guardrail_reporter:*"))
        assert keys == [f"agent:guardrail_reporter:{AGENT}"]

    def test_an_agent_that_never_reported_reads_never(self, db_backend, redis):
        assert svc.reporting_state(AGENT) == {
            "state": "never",
            "last_report_at": None,
            "first_report_at": None,
            "runtime": None,
            "dropped_total": 0,
        }

    def test_a_report_records_runtime_and_accumulates_dropped(self, db_backend, redis):
        _ingest([], runtime="codex", dropped=3)
        _ingest([_ev()], runtime="codex", dropped=2)
        state = svc.reporting_state(AGENT)
        assert state["state"] == "reporting"
        assert state["runtime"] == "codex"
        assert state["dropped_total"] == 5
        assert state["last_report_at"].endswith("Z")

    def test_an_unknown_runtime_is_reported_as_other(self, db_backend, redis):
        _ingest([], runtime="some-future-cli")
        assert svc.reporting_state(AGENT)["runtime"] == "other"

    def test_silence_past_the_window_reads_stale(self, db_backend, redis):
        _ingest([])
        later = datetime.now(timezone.utc) + timedelta(
            seconds=svc.STALE_AFTER_SECONDS + 5
        )
        assert svc.reporting_state(AGENT, now=later)["state"] == "stale"

    def test_rows_without_a_redis_record_read_from_the_newest_row_not_never(
        self, db_backend, redis
    ):
        """Redis lost its record (flush, restore) while rows exist: the agent HAS
        reported, so "never" would be a lie. It degrades to the newest row."""
        _ingest([_ev()])
        redis.flushall()
        state = svc.reporting_state(AGENT)
        assert state["state"] in ("reporting", "stale")
        assert state["last_report_at"] is not None

    def test_clear_forgets_the_reporter(self, db_backend, redis):
        _ingest([])
        svc.clear_reporter(AGENT)
        assert svc.reporting_state(AGENT)["state"] == "never"


# ---------------------------------------------------------------------------
# A future `ts` is clamped to the receive time (fix round R3)
# ---------------------------------------------------------------------------


def _z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class TestFutureTimestamp:
    """An agent's clock can run ahead. Its event must still be stored (a skewed
    clock never costs the agent its record) but dated no later than the moment
    the backend received it, or it would sit inside every rolling window until
    it is pruned."""

    def test_a_future_ts_is_stored_at_the_receive_time_with_the_reported_value_kept(
        self, db_backend, redis
    ):
        res = _ingest([_ev(ts="2999-01-01T00:00:00Z")])
        assert res == {"accepted": 1, "duplicates": 0, "rejected": 0}
        [row] = _rows()
        assert row["occurred_at"] == row["received_at"]
        details = svc.load_details(row["details"])
        assert details["occurred_at_reported"] == "2999-01-01T00:00:00.000000Z"

    def test_a_past_ts_is_kept_as_reported(self, db_backend, redis):
        past = _z(datetime.now(timezone.utc) - timedelta(hours=2))
        _ingest([_ev(ts=past)])
        [row] = _rows()
        assert row["occurred_at"] == past
        assert "occurred_at_reported" not in svc.load_details(row["details"])

    def test_a_future_event_leaves_the_window_once_its_receive_time_has(
        self, db_backend, redis, monkeypatch
    ):
        """The behaviour the clamp exists for: an event received two days ago
        with a `ts` still in the future is NOT in today's 24 h counts or a
        `since` read of the last day."""
        now = datetime.now(timezone.utc)
        two_days_ago = now - timedelta(hours=48)
        # A scoped patch: `monkeypatch.undo()` would also revert the fixtures'
        # own patches (the DB and Redis seams) and make every count vacuous.
        with monkeypatch.context() as m:
            m.setattr(svc, "utc_now", lambda: two_days_ago)
            _ingest([_ev("skewed", ts=_z(now + timedelta(days=1)))])
        assert svc.count_by_agent(24, agent_names=[AGENT]) == {"agents": {}}
        page = svc.list_events(AGENT, since=_z(now - timedelta(hours=24)))
        assert page["events"] == []
        assert svc.count_by_agent(72, agent_names=[AGENT]) == {
            "agents": {AGENT: {"by_kind": {"deny": 1}}}
        }


# ---------------------------------------------------------------------------
# Coverage start: when the agent first reported (fix round R4)
# ---------------------------------------------------------------------------


class TestCoverageStart:
    """`reporting.first_report_at` lets a consumer tell "before this agent
    reported at all" from "covered, and clean". First write wins; when the fast
    store lost it, the earliest stored row stands in; a lost fast store with no
    rows resets coverage to the next report (later, never earlier: the safe
    direction)."""

    def test_never_reported_has_no_coverage_start(self, db_backend, redis):
        assert svc.reporting_state(AGENT)["first_report_at"] is None

    def test_the_first_report_sets_it_and_a_later_one_does_not_move_it(
        self, db_backend, redis, monkeypatch
    ):
        t0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(svc, "utc_now", lambda: t0)
        _ingest([])
        monkeypatch.setattr(svc, "utc_now", lambda: t1)
        _ingest([_ev()])
        state = svc.reporting_state(AGENT, now=t1)
        assert state["first_report_at"] == _z(t0)
        assert state["last_report_at"] == _z(t1)

    def test_a_lost_fast_store_falls_back_to_the_earliest_stored_row(
        self, db_backend, redis, monkeypatch
    ):
        t0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(svc, "utc_now", lambda: t0)
        _ingest([_ev("a", ts=_z(t0))])
        monkeypatch.setattr(svc, "utc_now", lambda: t1)
        _ingest([_ev("b", ts=_z(t1))])
        redis.flushall()
        state = svc.reporting_state(AGENT, now=t1)
        assert state["first_report_at"] == _z(t0)
        assert state["last_report_at"] == _z(t1)

    def test_after_a_lost_fast_store_the_next_report_resets_coverage_to_now(
        self, db_backend, redis, monkeypatch
    ):
        """Rows from before the loss do not pull the start back: once the fast
        store holds a start again, it wins (later is the safe direction)."""
        t0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
        t1 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(svc, "utc_now", lambda: t0)
        _ingest([_ev("a", ts=_z(t0))])
        redis.flushall()
        monkeypatch.setattr(svc, "utc_now", lambda: t1)
        _ingest([])
        assert svc.reporting_state(AGENT, now=t1)["first_report_at"] == _z(t1)

    def test_teardown_forgets_it(self, db_backend, redis):
        _ingest([])
        svc.clear_reporter(AGENT)
        assert svc.reporting_state(AGENT)["first_report_at"] is None

    def test_a_record_with_no_start_and_no_rows_starts_at_its_last_report(
        self, db_backend, redis
    ):
        """A reporter hash written before the start was recorded: coverage is
        the last report, never null while the agent reads as reporting."""
        last = _z(datetime.now(timezone.utc))
        redis.hset(f"{svc.REPORTER_KEY_PREFIX}{AGENT}", mapping={"last_report_at": last})
        state = svc.reporting_state(AGENT)
        assert (state["state"], state["first_report_at"]) == ("reporting", last)

    def test_the_execution_count_seam_carries_it(self, db_backend, redis, monkeypatch):
        """trinity#4281 reads coverage through this seam, never the store. Two
        reports, so a seam that passed `last_report_at` off as the start fails."""
        t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
        with monkeypatch.context() as m:
            m.setattr(svc, "utc_now", lambda: t0)
            _ingest([])
        _ingest([_ev()])
        out = svc.count_by_execution(AGENT, ["exec-1"])
        assert out["reporting"]["first_report_at"] == _z(t0)
        assert out["reporting"]["last_report_at"] > _z(t0)
        assert out["executions"]["exec-1"]["by_kind"] == {"deny": 1}

    def test_racing_reports_never_put_the_start_after_the_last_report(
        self, db_backend, redis
    ):
        """A report received at 08:00 that stalls and lands after one received
        at 09:00 leaves first=09:00, last=08:00 in the hash. The agent did
        report at 08:00, so the start reads 08:00: never after the last report."""
        svc.record_report(AGENT, "claude-code", 0, "2026-11-01T09:00:00.000000Z")
        svc.record_report(AGENT, "claude-code", 0, "2026-11-01T08:00:00.000000Z")
        state = svc.reporting_state(AGENT)
        assert state["first_report_at"] == "2026-11-01T08:00:00.000000Z"
        assert state["first_report_at"] <= state["last_report_at"]

    def test_coverage_is_read_before_the_counts(self, db_backend, redis, monkeypatch):
        """The agent's FIRST batch commits while the counts are being read. The
        answer must not pair counts that missed it with the coverage it set up
        (a clean 0 for a run it covered): read coverage first, so the pair is
        `never` with no counts, or coverage with the event counted."""
        real = db.count_guardrail_events_by_execution

        def count_then_first_batch_lands(agent_name, ids):
            groups = real(agent_name, ids)
            _ingest([_ev()])
            return groups

        monkeypatch.setattr(db, "count_guardrail_events_by_execution",
                            count_then_first_batch_lands)
        out = svc.count_by_execution(AGENT, ["exec-1"])
        assert not (out["executions"] == {} and out["reporting"]["first_report_at"])
        assert out["reporting"]["state"] == "never"


# ---------------------------------------------------------------------------
# Per-agent read
# ---------------------------------------------------------------------------


def _seed_page(n=5):
    events = [
        _ev(
            f"e-{i}",
            ts=f"2026-11-08T10:0{i}:00.000000Z",
            kind="deny" if i % 2 else "credential_redacted",
            hook="turn_tier_guard" if i % 2 else "output_scanner",
            code="shell_owner_only" if i % 2 else None,
            execution_id="exec-a" if i < 3 else "exec-b",
        )
        for i in range(n)
    ]
    _ingest(events)


class TestList:
    def test_newest_first_with_a_cursor_that_walks_every_row_once(
        self, db_backend, redis
    ):
        _seed_page(5)
        page1 = svc.list_events(AGENT, limit=2)
        assert [e["event_id"] for e in page1["events"]] == ["e-4", "e-3"]
        assert page1["next_cursor"]
        page2 = svc.list_events(AGENT, limit=2, before=page1["next_cursor"])
        assert [e["event_id"] for e in page2["events"]] == ["e-2", "e-1"]
        page3 = svc.list_events(AGENT, limit=2, before=page2["next_cursor"])
        assert [e["event_id"] for e in page3["events"]] == ["e-0"]
        assert page3["next_cursor"] is None

    def test_filters(self, db_backend, redis):
        _seed_page(5)
        only_deny = svc.list_events(AGENT, kind="deny")["events"]
        assert {e["event_id"] for e in only_deny} == {"e-1", "e-3"}
        only_b = svc.list_events(AGENT, execution_id="exec-b")["events"]
        assert {e["event_id"] for e in only_b} == {"e-3", "e-4"}
        window = svc.list_events(
            AGENT, since="2026-11-08T10:01:00Z", until="2026-11-08T10:03:00Z"
        )["events"]
        assert {e["event_id"] for e in window} == {"e-1", "e-2"}

    def test_another_agents_rows_never_appear(self, db_backend, redis):
        _seed_page(2)
        _ingest([_ev("theirs")], agent=OTHER)
        assert "theirs" not in {e["event_id"] for e in svc.list_events(AGENT)["events"]}

    def test_a_fresh_install_reads_empty_and_never(self, db_backend, redis):
        page = svc.list_events(AGENT)
        assert page == {
            "events": [],
            "next_cursor": None,
            "reporting": {
                "state": "never",
                "last_report_at": None,
                "first_report_at": None,
                "runtime": None,
                "dropped_total": 0,
            },
        }

    def test_a_bad_cursor_is_refused_by_name(self, db_backend, redis):
        with pytest.raises(svc.GuardrailEventQueryError):
            svc.list_events(AGENT, before="not-a-cursor")

    def test_an_unknown_kind_filter_is_refused_by_name(self, db_backend, redis):
        with pytest.raises(svc.GuardrailEventQueryError):
            svc.list_events(AGENT, kind="everything")

    def test_the_page_size_is_capped(self, db_backend, redis):
        with pytest.raises(svc.GuardrailEventQueryError):
            svc.list_events(AGENT, limit=svc.READ_LIMIT_MAX + 1)


# ---------------------------------------------------------------------------
# Counting seams (trinity#4281, S4, ent#141)
# ---------------------------------------------------------------------------


class TestCounts:
    def test_count_by_execution_shape(self, db_backend, redis):
        _seed_page(5)
        out = svc.count_by_execution(AGENT, ["exec-a", "exec-b", "exec-none"])
        assert out["reporting"]["state"] == "reporting"
        assert set(out["executions"]) == {"exec-a", "exec-b"}  # no events -> absent
        a = out["executions"]["exec-a"]  # e-0 redacted, e-1 deny, e-2 redacted
        assert a["by_kind"] == {"credential_redacted": 2, "deny": 1}
        assert a["by_hook_code"] == {"turn_tier_guard:shell_owner_only": 1}
        b = out["executions"]["exec-b"]  # e-3 deny, e-4 redacted
        assert b["by_kind"] == {"deny": 1, "credential_redacted": 1}

    def test_count_by_execution_filters_by_the_authenticated_agent(
        self, db_backend, redis
    ):
        """`execution_id` is attribution, not authority: a forged id on another
        agent's event never counts against this agent's execution."""
        _ingest([_ev("mine")])
        _ingest([_ev("forged-1"), _ev("forged-2")], agent=OTHER)
        out = svc.count_by_execution(AGENT, ["exec-1"])
        assert out["executions"]["exec-1"]["by_kind"] == {"deny": 1}

    def test_count_by_execution_with_no_ids_still_says_whether_it_reports(
        self, db_backend, redis
    ):
        out = svc.count_by_execution(AGENT, [])
        assert out == {"reporting": svc.reporting_state(AGENT), "executions": {}}
        assert out["reporting"]["state"] == "never"

    def test_count_by_agent(self, db_backend, redis):
        now = datetime.now(timezone.utc)
        recent = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        old = (now - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        _ingest(
            [
                _ev("r1", ts=recent),
                _ev(
                    "r2",
                    ts=recent,
                    kind="credential_redacted",
                    hook="output_scanner",
                    code=None,
                ),
                _ev("old", ts=old),
            ]
        )
        _ingest([_ev("o1", ts=recent)], agent=OTHER)
        out = svc.count_by_agent(24)
        assert out == {
            "agents": {
                AGENT: {"by_kind": {"deny": 1, "credential_redacted": 1}},
                OTHER: {"by_kind": {"deny": 1}},
            }
        }
        scoped = svc.count_by_agent(24, agent_names=[OTHER])
        assert scoped == {"agents": {OTHER: {"by_kind": {"deny": 1}}}}
        assert svc.count_by_agent(24, agent_names=[]) == {"agents": {}}


# ---------------------------------------------------------------------------
# Retention and agent lifecycle
# ---------------------------------------------------------------------------


def _age_received(event_id, days):
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    with get_engine().begin() as conn:
        conn.execute(
            guardrail_events.update()
            .where(guardrail_events.c.event_id == event_id)
            .values(received_at=stamp)
        )


class TestRetention:
    def test_the_count_and_the_prune_cover_the_same_rows(self, db_backend, redis):
        _ingest([_ev("old-1"), _ev("old-2"), _ev("new-1")])
        _age_received("old-1", 91)
        _age_received("old-2", 120)
        assert db.count_guardrail_event_candidates(90, 1000) == 2
        assert db.prune_guardrail_events(retention_days=90, chunk_size=1) == 2
        assert [r["event_id"] for r in _rows()] == ["new-1"]

    def test_prune_keys_on_the_server_receive_time_not_the_agent_ts(
        self, db_backend, redis
    ):
        """`occurred_at` is agent-reported: a far-future value must not make a
        row immortal, and an old one must not be swept the moment it lands."""
        _ingest(
            [
                _ev("future", ts="2999-01-01T00:00:00Z"),
                _ev("ancient", ts="2001-01-01T00:00:00Z"),
            ]
        )
        assert db.count_guardrail_event_candidates(90, 1000) == 0
        _age_received("future", 100)
        assert db.prune_guardrail_events(retention_days=90) == 1
        assert [r["event_id"] for r in _rows()] == ["ancient"]

    def test_zero_disables(self, db_backend, redis):
        _ingest([_ev()])
        _age_received("e-1", 400)
        assert db.count_guardrail_event_candidates(0, 1000) == 0
        assert db.prune_guardrail_events(retention_days=0) == 0
        assert len(_rows()) == 1


class TestAgentLifecycle:
    def test_rename_moves_the_rows_and_hard_delete_purges_them(self, db_backend, redis):
        from db.agent_cleanup import cascade_delete, cascade_rename

        _ingest([_ev()])
        _ingest([_ev("keep")], agent=OTHER)
        with get_engine().begin() as conn:
            cascade_rename(conn, AGENT, "guard-renamed")
        assert _rows(AGENT) == []
        assert [r["event_id"] for r in _rows("guard-renamed")] == ["e-1"]
        with get_engine().begin() as conn:
            cascade_delete(conn, "guard-renamed")
        assert _rows("guard-renamed") == []
        assert [r["event_id"] for r in _rows(OTHER)] == ["keep"]


class TestReporterTeardown:
    def test_agent_teardown_forgets_the_reporter(self, db_backend, redis, monkeypatch):
        """Delete / rename / purge run `clear_agent_runtime_state`; a recycled
        name must not read `reporting` on its predecessor's forwarder (#1560)."""
        import asyncio

        from services import agent_runtime_state

        # The sibling clears are fail-open and out of scope here; stub them so
        # the test neither reaches for a real Redis nor depends on their health.
        monkeypatch.setattr(agent_runtime_state, "clear_agent_breakers", lambda name: None)
        _ingest([])
        assert svc.reporting_state(AGENT)["state"] == "reporting"
        asyncio.run(agent_runtime_state.clear_agent_runtime_state(AGENT))
        assert svc.reporting_state(AGENT)["state"] == "never"

    def test_a_start_does_not_forget_the_reporter(self, db_backend, redis, monkeypatch):
        """`clear_agent_breakers` runs on every start: clearing there would make
        a restarted agent read `never` (the older-image state)."""
        from services import agent_runtime_state, heartbeat_service

        monkeypatch.setattr(heartbeat_service, "_get_redis", lambda: None)
        _ingest([])
        agent_runtime_state.clear_agent_breakers(AGENT)
        assert svc.reporting_state(AGENT)["state"] == "reporting"
