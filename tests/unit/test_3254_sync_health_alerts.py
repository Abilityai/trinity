"""#3254 S2: the git sync health alerts on the platform-alert seam.

The sync-health poller raises four conditions: sync failing, sync diverged, a
`.git` over its size ceiling and a failing-maintenance streak. Each used to be
one row per crossing, filed directly and never ended. On the seam
(`services.platform_alerts`) each is one pending row per `(agent, kind)`:

* the row keeps the `type` its readers filter on (D2: `Kind.row_type`);
* two readings are one row; a falling edge ends it as the platform;
* a person's ending stands, and a re-reading inside the snooze files nothing;
* a condition that persists is re-read at most every 6 hours (R9), so its
  deadline moves while it holds, and only a condition nobody re-read (a clear
  lost to a fault) ends on its 30-day lifetime;
* git bloat is two conditions, two subjects (AC 5);
* the divergence row ends only when the `diverged_since` episode ends, never
  when sync failing takes precedence as the freeze cause;
* R1: the second upgrade sweep derives the subject `observe` uses from the
  pre-move ids (git bloat is exempt, residual stated in its test).

Driven through the REAL `SyncHealthService._poll_cycle` against a real DB
(db_harness); only the agent's `/api/git/status` response is faked. The clock
is pinned (T0) and moved by the test, never compared to the wall clock.
"""
from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

import pytest

from db_harness import db_backend, run as _hrun  # noqa: F401

pytestmark = pytest.mark.unit


@pytest.fixture
def pa(monkeypatch):
    from services import operator_resume_service, platform_alerts
    # The seam's follow-up (a thin broadcast) needs no loop here.
    monkeypatch.setattr(operator_resume_service, "spawn_on_loop", lambda factory: None)
    return platform_alerts


@pytest.fixture
def db(db_backend):  # noqa: F811
    from database import db as database
    return database


# --------------------------------------------------------------------------
# D2: the row type survives the move
# --------------------------------------------------------------------------

_ROW_TYPES = {
    "sync_failing": "sync_failing",
    "sync_diverged": "sync_diverged",
    "git_bloat_size": "git_bloat",
    "git_bloat_maintenance": "git_bloat",
}


@pytest.mark.parametrize("kind", sorted(_ROW_TYPES))
def test_the_seam_writes_the_kinds_row_type(db, pa, kind):
    """The `?type=` list filter, `/stats` by_type, the MCP list tool and the UI
    label read these types; the move must not turn them into `alert`."""
    assert pa.observe("alpha", kind, "alpha", title="t", question="q") == pa.OBSERVED_CREATED
    (row,) = db.list_operator_queue_items(agent_name="alpha", status="pending")
    assert row["type"] == _ROW_TYPES[kind]
    assert row["subject"] == f"{kind}:alpha"


@pytest.mark.parametrize("kind, row_type", [
    ("circuit_dormant", "alert"),      # no row_type, unbudgeted: unchanged
    ("queue_flood", "queue_flood"),    # no row_type, budgeted: its budgeted type
])
def test_a_kind_without_a_row_type_keeps_the_old_type(db, pa, kind, row_type):
    pa.observe("alpha", kind, "oversize_file", title="t", question="q")
    (row,) = db.list_operator_queue_items(agent_name="alpha", status="pending")
    assert row["type"] == row_type


def test_an_explicit_row_type_must_match_the_budgeted_type(pa):
    """The #1677 budget counts pending rows BY TYPE, so a budgeted kind that
    wrote another type would never meet its own cap."""
    with pytest.raises(ValueError, match="row_type"):
        pa.Kind("x_kind", "x-", pa.LIFETIME_DEFAULT, pa.CLEARS_HOOK,
                budgeted_type="x_budget", row_type="x_other")
    same = pa.Kind("x_kind", "x-", pa.LIFETIME_DEFAULT, pa.CLEARS_HOOK,
                   budgeted_type="x_budget", row_type="x_budget")
    assert same.row_type == "x_budget"
    unbudgeted = pa.Kind("x_kind", "x-", pa.LIFETIME_DEFAULT, pa.CLEARS_HOOK,
                         row_type="x_other")
    assert unbudgeted.row_type == "x_other"


# --------------------------------------------------------------------------
# The poller harness: a pinned clock, the real poller, a real DB
# --------------------------------------------------------------------------

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
_DENIED = "remote: Write access to repository not granted."


class _Clock:
    """One clock for the poller, the seam and the queue. Each reader is
    patched in the globals of the code that runs it (R6), so a sibling suite
    that re-imported a module cannot leave this test on the wall clock.

    Like real time it moves between two reads (1 µs). The seam's request ids
    carry a timestamp, and a frozen instant would hand the two git-bloat kinds
    (one prefix, one key) the same id, which the seam reports as `created`
    while filing nothing: a pre-existing seam defect, drafted as a follow-up."""

    def __init__(self):
        self.now = T0

    def advance(self, **delta):
        self.now += timedelta(**delta)

    def read(self):
        self.now += timedelta(microseconds=1)
        return self.now

    def iso(self):
        return self.read().strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def monotonic(self):
        return (self.read() - T0).total_seconds() + 10_000.0


@pytest.fixture
def service(db, pa, monkeypatch):
    import services.sync_health_service as shs
    # Fail-open lease: no Redis, so every test polls (see test_sync_health_service).
    monkeypatch.setattr(shs, "get_breaker_redis", lambda: None)
    # The poller writes through the SAME db object the test reads.
    monkeypatch.setattr(shs, "db", db)
    # So do the answer and ending sinks: a sibling suite that swapped
    # `database` while `ask_service` was first imported leaves it holding an
    # orphaned db whose queue module no clock below reaches (R6).
    from services import ask_service
    monkeypatch.setitem(ask_service.answer.__globals__, "db", db)
    return shs.SyncHealthService(poll_interval=0)


@pytest.fixture
def clock(service, db, monkeypatch):
    c = _Clock()

    class _Datetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return c.read() if tz is None else c.read().astimezone(tz)

    import utils.helpers
    poller = type(service)._sync_agent.__globals__
    monkeypatch.setitem(poller, "utc_now_iso", c.iso)
    monkeypatch.setitem(poller, "datetime", _Datetime)
    monkeypatch.setitem(poller, "time", types.SimpleNamespace(monotonic=c.monotonic))
    monkeypatch.setitem(poller["platform_alerts"].observe.__globals__, "datetime", _Datetime)
    monkeypatch.setitem(poller["sync_view"].__globals__, "datetime", _Datetime)
    monkeypatch.setitem(type(db._operator_queue_ops).mark_expired.__globals__, "utc_now_iso", c.iso)
    monkeypatch.setattr(utils.helpers, "utc_now_iso", c.iso)  # the seam's function-local import
    return c


@pytest.fixture
def seed_agent(db):
    def _seed(name="alpha"):
        _hrun("INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
              "VALUES (:n, 1, '2026-01-01T00:00:00Z')", n=name)
        _hrun("INSERT INTO agent_git_config "
              "(id, agent_name, github_repo, working_branch, instance_id, created_at, "
              " sync_enabled, source_mode, auto_sync_enabled, freeze_schedules_if_sync_failing) "
              "VALUES (:gid, :n, :repo, :wb, 'abc123', '2026-01-01T00:00:00Z', 1, 0, 1, 1)",
              gid=name + "-git", n=name, repo=f"org/{name}", wb=f"trinity/{name}/abc123")
        return name
    return _seed


def _payload(*, status="success", error=None, ahead=0, behind=0, **sync_extra):
    sync_state = {
        "last_sync_status": status,
        "last_sync_at": "2026-11-01T09:00:00+00:00",
        "last_error_summary": error,
        "consecutive_failures": 0,
        **sync_extra,
    }
    return {
        "git_enabled": True,
        "branch": "trinity/alpha/abc123",
        "last_commit": {"sha": "deadbeef"},
        "changes_count": 0,
        "ahead_main": 0,
        "behind_main": 0,
        "ahead_working": ahead,
        "behind_working": behind,
        "sync_state": sync_state,
    }


async def _poll(service, payload):
    from unittest.mock import AsyncMock, patch
    with patch.object(service, "_fetch_git_status", AsyncMock(return_value=payload)):
        await service._poll_cycle()


def _rows(db, prefix, agent="alpha"):
    return [r for r in db.list_operator_queue_items(agent_name=agent, limit=1000)
            if r["request_id"].startswith(prefix)]


def _pending(db, prefix, agent="alpha"):
    return [r for r in _rows(db, prefix, agent) if r["status"] == "pending"]


def _ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _about(a, b):
    """Equal up to the clock's per-read tick (two writes, a few µs apart)."""
    return abs(a - b) < timedelta(seconds=1)


def _person_ends(db, row):
    """A person acknowledges the row through the answer sink (AC 7, #2372)."""
    from services import ask_service
    ask_service.answer(db.get_operator_queue_item(row["id"]), response="acknowledged",
                       response_text=None, actor=ask_service.Actor(email="op@example.com"))


_FAIL = _payload(status="failed", error="boom")
_OK = _payload()
_MIN = {"minutes": 1}
_AHEAD = _payload(ahead=7)
_BIG, _SMALL = 2**40, 2**20  # either side of the 10 GiB GIT_DIR_ALERT_BYTES default

# How each condition is raised, kept, and recovered through the real poller:
# `raise_` is (time to wait before, payload) per poll.
SCENARIOS = {
    "sync_failing": {
        "prefix": "sync-failing-", "row_type": "sync_failing",
        "raise_": [({}, _FAIL), (_MIN, _FAIL), (_MIN, _FAIL)],
        "persist": _FAIL, "recover": _OK,
    },
    # A divergence freezes schedules once the episode is over 24 h old (#706).
    "sync_diverged": {
        "prefix": "sync-diverged-", "row_type": "sync_diverged",
        "raise_": [({}, _AHEAD), ({"hours": 25}, _AHEAD)],
        "persist": _AHEAD, "recover": _OK,
    },
    # Two conditions under the one `git-bloat-` prefix, two subjects (AC 5).
    "git_bloat_size": {
        "prefix": "git-bloat-", "row_type": "git_bloat",
        "raise_": [({}, _payload(git_dir_bytes=_BIG))],
        "persist": _payload(git_dir_bytes=_BIG), "recover": _payload(git_dir_bytes=_SMALL),
    },
    "git_bloat_maintenance": {
        "prefix": "git-bloat-", "row_type": "git_bloat",
        "raise_": [({}, _payload(maintenance_failures=3))],
        "persist": _payload(maintenance_failures=4), "recover": _payload(maintenance_failures=0),
    },
}


async def _raise(service, clock, scenario):
    for before, payload in scenario["raise_"]:
        clock.advance(**before)
        await _poll(service, payload)


# --------------------------------------------------------------------------
# Per kind: the seam's properties, two readings, a person's ending
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(SCENARIOS))
async def test_the_alert_is_a_condition_on_the_seam(kind, service, seed_agent, db, clock):
    """The seam-property test (the universal mutation: the old direct create
    turns it red). The row carries the subject; a re-reading bumps
    `seen_count` and moves `last_seen_at` and `expires_at`; the falling edge
    ends it as the platform."""
    sc = SCENARIOS[kind]
    seed_agent()
    await _raise(service, clock, sc)
    (row,) = _rows(db, sc["prefix"])
    assert row["subject"] == f"{kind}:alpha"
    assert row["type"] == sc["row_type"]
    assert row["context"]["seen_count"] == 1
    assert _about(_ts(row["expires_at"]), _ts(row["last_seen_at"]) + timedelta(days=30))

    # R9's "at most": a poll inside the 6 hours touches nothing.
    clock.advance(hours=1)
    await _poll(service, sc["persist"])
    assert db.get_operator_queue_item(row["id"])["context"]["seen_count"] == 1

    # ...and once they are up, the persisting condition is re-read in place.
    clock.advance(hours=5, minutes=1)
    await _poll(service, sc["persist"])
    (again,) = _rows(db, sc["prefix"])
    assert again["id"] == row["id"]
    assert again["context"]["seen_count"] == 2
    assert _ts(again["last_seen_at"]) - _ts(row["last_seen_at"]) >= timedelta(hours=6)
    assert _ts(again["expires_at"]) - _ts(row["expires_at"]) >= timedelta(hours=6)

    clock.advance(**_MIN)
    await _poll(service, sc["recover"])
    ended = db.get_operator_queue_item(row["id"])
    assert (ended["status"], ended["disposed_by"], ended["disposition_reason"]) == (
        "cancelled", "platform", "condition_cleared")
    assert _pending(db, sc["prefix"]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(SCENARIOS))
async def test_two_readings_are_one_pending_row(kind, service, seed_agent, db, clock):
    """A restarted process (or a second fail-open leader) has no memo and
    re-reads the persisting condition at once; the subject keeps it one row."""
    sc = SCENARIOS[kind]
    seed_agent()
    await _raise(service, clock, sc)
    restarted = type(service)(poll_interval=0)
    clock.advance(**_MIN)
    await _poll(restarted, sc["persist"])
    (row,) = _rows(db, sc["prefix"])
    assert row["status"] == "pending"
    assert row["context"]["seen_count"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(SCENARIOS))
async def test_a_persons_ending_stands(kind, service, seed_agent, db, clock):
    """Acknowledged through the answer sink, the row lands `acknowledged`
    (#2372); a re-reading inside the 7-day snooze files nothing, and the
    platform's clear on recovery never overwrites the person's ending."""
    sc = SCENARIOS[kind]
    seed_agent()
    await _raise(service, clock, sc)
    (row,) = _pending(db, sc["prefix"])
    _person_ends(db, row)
    assert db.get_operator_queue_item(row["id"])["status"] == "acknowledged"

    clock.advance(hours=6, minutes=1)  # due for a re-read: it is snoozed
    await _poll(service, sc["persist"])
    assert _pending(db, sc["prefix"]) == []

    clock.advance(**_MIN)
    await _poll(service, sc["recover"])
    ended = db.get_operator_queue_item(row["id"])
    assert (ended["status"], ended["disposed_by"]) == ("acknowledged", "person")
    assert len(_rows(db, sc["prefix"])) == 1


# --------------------------------------------------------------------------
# The other shape: the clock 30 days ahead
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_push_denied_failure_that_lasts_30_days_keeps_one_row(
        service, seed_agent, db, clock):
    """A refused push fails identically every cycle (#2107). Without R9 its row
    (a 30-day net lifetime, measured from the reading) expired on day 30 while
    the agent was still unable to save its work."""
    seed_agent()
    denied = _payload(status="failed", error=_DENIED)
    expired, first_id = [], None
    for _ in range(0, 31 * 24 + 1, 2):  # a poll every 2 hours for 31 days
        await _poll(service, denied)
        expired += [r for r in db.mark_operator_queue_expired() if r["agent_name"] == "alpha"]
        if first_id is None and _rows(db, "sync-failing-"):
            first_id = _rows(db, "sync-failing-")[0]["id"]
        clock.advance(hours=2)
    assert expired == []
    (row,) = _rows(db, "sync-failing-")
    assert row["id"] == first_id and row["status"] == "pending"
    assert row["title"] == "Git token can't push"
    assert clock.now - _ts(row["last_seen_at"]) <= timedelta(hours=6, minutes=1)
    assert _ts(row["expires_at"]) > clock.now + timedelta(days=29)
    assert row["context"]["seen_count"] > 100  # re-read about four times a day


@pytest.mark.asyncio
async def test_a_row_whose_recovery_edge_was_lost_expires_30_days_after_its_last_reading(
        service, seed_agent, db, clock, monkeypatch):
    """The lifetime is the net under a lost clear: the recovery poll's clear
    fails (a DB fault), nothing re-reads a condition that ended, and the row
    expires exactly 30 days after the last reading, not before."""
    seed_agent()
    await _raise(service, clock, SCENARIOS["sync_failing"])
    clock.advance(hours=6, minutes=1)
    await _poll(service, _FAIL)  # the last reading
    (row,) = _pending(db, "sync-failing-")
    last = _ts(row["last_seen_at"])
    assert _about(_ts(row["expires_at"]), last + timedelta(days=30))

    real_lookup = db.find_pending_operator_queue_by_subject
    lost = {"on": True}

    def lookup(*args, **kwargs):
        if lost["on"]:
            raise RuntimeError("the database went away mid-clear")
        return real_lookup(*args, **kwargs)

    monkeypatch.setattr(db, "find_pending_operator_queue_by_subject", lookup)
    clock.advance(**_MIN)
    await _poll(service, _OK)  # recovery seen; its clear is lost
    lost["on"] = False
    assert db.get_sync_state("alpha")["consecutive_failures"] == 0
    assert _pending(db, "sync-failing-")[0]["id"] == row["id"]

    while clock.now < last + timedelta(days=30) - timedelta(hours=6):
        clock.advance(hours=6)
        await _poll(service, _OK)
        db.mark_operator_queue_expired()
    clock.now = last + timedelta(days=30) - timedelta(minutes=1)
    db.mark_operator_queue_expired()
    assert db.get_operator_queue_item(row["id"])["status"] == "pending"

    clock.now = last + timedelta(days=30, minutes=1)
    db.mark_operator_queue_expired()
    ended = db.get_operator_queue_item(row["id"])
    assert (ended["status"], ended["disposed_by"]) == ("expired", "timeout")


# --------------------------------------------------------------------------
# R1: the second upgrade sweep derives the subject observe uses
# --------------------------------------------------------------------------

@pytest.mark.parametrize("agent", ["alpha", "my-agent-2"])
def test_r1_a_pre_move_sync_failing_row_derives_the_seam_subject(pa, agent):
    # The pre-move id: f"sync-failing-{agent}-{utc_now_iso()}".
    old_id = f"sync-failing-{agent}-2026-10-20T08:15:30.123456Z"
    match = pa.derive_legacy_subject(old_id, {"consecutive_failures": 3})
    assert match.kind == "sync_failing"
    assert match.subject == pa.subject_for("sync_failing", agent)


@pytest.mark.parametrize("agent", ["alpha", "my-agent-2"])
def test_r1_a_pre_move_sync_diverged_row_derives_the_seam_subject(pa, agent):
    # The pre-move id, one per episode: f"sync-diverged-{agent}-{diverged_since}".
    old_id = f"sync-diverged-{agent}-2026-10-19T07:00:00.000000Z"
    match = pa.derive_legacy_subject(old_id, {"diverged_since": "2026-10-19T07:00:00.000000Z"})
    assert match.kind == "sync_diverged"
    assert match.subject == pa.subject_for("sync_diverged", agent)


# --------------------------------------------------------------------------
# Divergence: the freeze cause moving is not the episode ending
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_divergence_outlives_a_sync_failing_precedence_and_ends_with_its_episode(
        service, seed_agent, db, clock):
    """Sync failing takes precedence over divergence as the freeze cause
    (sync_freeze_policy). While it does, the divergence episode still holds:
    its row is neither ended nor duplicated, it is still re-read (R9, with the
    divergence text, not the failure's), and when divergence is the cause again
    the same row carries on. Only `diverged_since` clearing ends it."""
    import services.sync_health_service as shs
    seed_agent()
    await _poll(service, _AHEAD)
    clock.advance(hours=25)
    await _poll(service, _AHEAD)
    (row,) = _rows(db, "sync-diverged-")
    episode = db.get_sync_state("alpha")["diverged_since"]

    failing = _payload(status="failed", error="boom", ahead=7)
    for _ in range(3):
        clock.advance(**_MIN)
        await _poll(service, failing)
    clock.advance(hours=7)  # past the re-read window, sync failing still the cause
    await _poll(service, failing)
    (config,) = db.list_git_enabled_agents()
    assert shs.sync_view(db.get_sync_state("alpha"), config)["freeze_cause"] == "sync_failing"
    assert db.get_sync_state("alpha")["diverged_since"] == episode
    assert len(_pending(db, "sync-failing-")) == 1
    held = db.get_operator_queue_item(row["id"])
    assert held["status"] == "pending" and held["context"]["seen_count"] == 2
    assert "diverged 0 behind / 7 ahead" in held["question"]
    assert "boom" not in held["question"]

    clock.advance(**_MIN)
    await _poll(service, _AHEAD)  # the sync recovers; divergence is the cause again
    assert _pending(db, "sync-failing-") == []
    (again,) = _rows(db, "sync-diverged-")
    assert again["id"] == row["id"] and again["status"] == "pending"
    assert again["context"]["seen_count"] == 2  # re-read 7 min ago: not due

    clock.advance(**_MIN)
    await _poll(service, _OK)  # the episode ends
    assert db.get_sync_state("alpha")["diverged_since"] is None
    ended = db.get_operator_queue_item(row["id"])
    assert (ended["status"], ended["disposed_by"]) == ("cancelled", "platform")
    assert len(_rows(db, "sync-diverged-")) == 1


# --------------------------------------------------------------------------
# Git bloat: two conditions, two subjects (AC 5); R1 exempt
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ac5_size_and_maintenance_are_two_subjects(service, seed_agent, db, clock):
    """`.git` over its ceiling and a failing-maintenance streak need different
    operator action, so each is its own row, and one ending leaves the other."""
    seed_agent()
    await _poll(service, _payload(git_dir_bytes=_BIG, maintenance_failures=3))
    rows = {r["subject"]: r for r in _pending(db, "git-bloat-")}
    assert set(rows) == {"git_bloat_size:alpha", "git_bloat_maintenance:alpha"}
    assert "GiB" in rows["git_bloat_size:alpha"]["question"]
    assert "maintenance has failed 3" in rows["git_bloat_maintenance:alpha"]["question"]
    assert {r["type"] for r in rows.values()} == {"git_bloat"}

    clock.advance(**_MIN)  # the repack succeeds while .git stays over the ceiling
    await _poll(service, _payload(git_dir_bytes=_BIG, maintenance_failures=0))
    size = db.get_operator_queue_item(rows["git_bloat_size:alpha"]["id"])
    maintenance = db.get_operator_queue_item(rows["git_bloat_maintenance:alpha"]["id"])
    assert (maintenance["status"], maintenance["disposed_by"]) == ("cancelled", "platform")
    assert size["status"] == "pending"


def test_r1_git_bloat_is_exempt_and_a_legacy_row_gets_a_lifetime_stamp_only(pa):
    """R1 exemption, with its residual. A pre-move `git-bloat-{agent}-{ts}` row
    told size from maintenance only in its prose, so the sweep's parser is
    `_no_subject` on purpose and never guesses which condition it was. The
    second upgrade sweep therefore stamps such a row with a lifetime only: it
    sits beside the seam's row for up to 30 days and then expires."""
    old_id = "git-bloat-alpha-2026-10-20T08:15:30.123456Z"
    assert pa.derive_legacy_subject(old_id, {"git_dir_bytes": _BIG}) == \
        pa.LegacyMatch("git_bloat_size", None)
    plan = pa.plan_sweep([{
        "id": "g1", "agent_name": "alpha", "request_id": old_id, "status": "pending",
        "created_at": "2026-10-20T08:15:30.123456Z", "expires_at": None, "context": None,
        "subject": None, "disposed_by": None, "disposed_at": None, "raised_by": None,
    }], now=T0)
    assert plan.survivor_stamps == () and plan.ended_ids == ()
    (stamp,) = plan.lifetime_stamps
    assert _ts(stamp.expires_at) == T0 + timedelta(days=30)


# --------------------------------------------------------------------------
# /cso: an agent cannot silence the alarm about itself
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_oversized_agent_error_cannot_keep_the_sync_failing_alert_out(
        service, seed_agent, db, clock):
    """`last_error_summary` comes from the agent-writable `sync-state.json`. A
    compromised agent writing one past the queue's 64 KiB context belt made
    every write of its own sync_failing alert fail, and the seam's retry turned
    that into a loop. The alert carries the agent writer's own 240-char excerpt
    (`_summarize_git_error`), so it lands; the stored sync state is untouched."""
    seed_agent()
    huge = "remote: Write access to repository not granted. " + "x" * (70 * 1024)
    failing = _payload(status="failed", error=huge)
    for _ in range(3):
        await _poll(service, failing)
        clock.advance(**_MIN)
    (row,) = _pending(db, "sync-failing-")
    assert row["title"] == "Git token can't push"
    assert row["context"]["last_error_summary"] == huge[:240]
    assert db.get_sync_state("alpha")["last_error_summary"] == huge


@pytest.mark.asyncio
async def test_a_divergence_under_a_31_day_sync_failing_precedence_keeps_its_row(
        service, seed_agent, db, clock):
    """The other shape for the precedence: a divergence reported, then sync
    failing for 31 days while `diverged_since` holds. Its row is re-read under
    the precedence, so its 30-day deadline keeps moving and it never expires."""
    seed_agent()
    await _poll(service, _AHEAD)
    clock.advance(hours=25)
    await _poll(service, _AHEAD)
    (row,) = _rows(db, "sync-diverged-")
    failing = _payload(status="failed", error="boom", ahead=7)
    expired = []
    for _ in range(0, 31 * 24 + 1, 2):
        clock.advance(hours=2)
        await _poll(service, failing)
        expired += [r for r in db.mark_operator_queue_expired() if r["agent_name"] == "alpha"]
    assert expired == []
    (again,) = _rows(db, "sync-diverged-")
    assert again["id"] == row["id"] and again["status"] == "pending"
    assert _ts(again["expires_at"]) > clock.now + timedelta(days=29)
    assert len(_pending(db, "sync-failing-")) == 1


@pytest.mark.asyncio
async def test_a_sync_failing_precedence_never_raises_a_divergence_alert(
        service, seed_agent, db, clock):
    """A refused push leaves commits unpushed, so the agent diverges while sync
    failing is already the freeze cause. Sync failing has its own row; the
    divergence underneath it is re-read only for an episode already reported."""
    seed_agent()
    failing = _payload(status="failed", error=_DENIED, ahead=7)
    for _ in range(3):
        await _poll(service, failing)
        clock.advance(**_MIN)
    clock.advance(hours=25)
    for _ in range(2):
        await _poll(service, failing)
        clock.advance(hours=7)
    assert db.get_sync_state("alpha")["diverged_since"] is not None
    assert len(_pending(db, "sync-failing-")) == 1
    assert _rows(db, "sync-diverged-") == []

    restarted = type(service)(poll_interval=0)  # a restart does not raise one either
    for _ in range(2):
        await _poll(restarted, failing)
        clock.advance(hours=7)
    assert _rows(db, "sync-diverged-") == []


async def _report_then_precede(service, clock):
    """A divergence reported while it is the cause, then sync failing on top."""
    await _poll(service, _AHEAD)
    clock.advance(hours=25)
    await _poll(service, _AHEAD)
    failing = _payload(status="failed", error="boom", ahead=7)
    for _ in range(3):
        clock.advance(**_MIN)
        await _poll(service, failing)
    return failing


@pytest.mark.asyncio
async def test_a_failed_re_read_under_a_precedence_is_retried_on_the_next_poll(
        service, seed_agent, db, clock, monkeypatch):
    seed_agent()
    failing = await _report_then_precede(service, clock)
    (row,) = _rows(db, "sync-diverged-")

    real_create = db.create_platform_operator_queue_item
    fail = {"on": False}

    def create(*args, **kwargs):
        if fail["on"]:
            raise RuntimeError("the database went away mid-write")
        return real_create(*args, **kwargs)

    monkeypatch.setattr(db, "create_platform_operator_queue_item", create)
    for seen in (2, 3):  # twice in one episode: a retry must not depend on a one-off lookup
        fail["on"] = True
        clock.advance(hours=7)
        await _poll(service, failing)  # the re-read is due and its write fails
        fail["on"] = False
        assert db.get_operator_queue_item(row["id"])["context"]["seen_count"] == seen - 1
        clock.advance(**_MIN)
        await _poll(service, failing)  # retried on the next poll, not in 6 h or never
        assert db.get_operator_queue_item(row["id"])["context"]["seen_count"] == seen


@pytest.mark.asyncio
async def test_a_restarted_poller_re_reads_a_divergence_under_a_precedence(
        service, seed_agent, db, clock, monkeypatch):
    """A restart or a leadership hand-off empties the memo; the divergence row
    that exists is still re-read under the precedence (a restart re-reads once),
    for 31 days, and the seam's row is looked up once, not on every poll."""
    seed_agent()
    failing = await _report_then_precede(service, clock)
    (row,) = _rows(db, "sync-diverged-")

    real_lookup = db.find_pending_operator_queue_by_subject
    lookups = []

    def lookup(agent_name, subject):
        lookups.append(subject)
        return real_lookup(agent_name, subject)

    monkeypatch.setattr(db, "find_pending_operator_queue_by_subject", lookup)
    restarted = type(service)(poll_interval=0)
    clock.advance(**_MIN)
    await _poll(restarted, failing)  # re-read at once
    assert db.get_operator_queue_item(row["id"])["context"]["seen_count"] == 2
    before = len(lookups)
    for _ in range(10):  # inside the 6 hours: no lookup, no write
        clock.advance(minutes=30)
        await _poll(restarted, failing)
    assert len(lookups) == before

    expired = []
    for _ in range(0, 31 * 24 + 1, 2):
        clock.advance(hours=2)
        await _poll(restarted, failing)
        expired += [r for r in db.mark_operator_queue_expired() if r["agent_name"] == "alpha"]
    assert expired == []
    again = db.get_operator_queue_item(row["id"])
    assert again["status"] == "pending"
    assert _ts(again["expires_at"]) > clock.now + timedelta(days=29)


# --------------------------------------------------------------------------
# R10: the absence of a failure is not a recovery
# --------------------------------------------------------------------------

_INCONCLUSIVE = [
    ("sync_failing", "unreachable", None),
    ("sync_failing", "git_disabled", {"git_enabled": False}),
    ("sync_failing", "never_cycle", _payload(status="never")),
    ("sync_failing", "no_status", _payload(status=None)),
    ("sync_diverged", "unreachable", None),
    ("sync_diverged", "git_disabled", {"git_enabled": False}),
    ("sync_diverged", "unknown_tuple", _payload(ahead="lots", behind=None)),
    ("git_bloat_size", "unreachable", None),
    ("git_bloat_size", "size_absent", _payload()),
    ("git_bloat_size", "size_garbage", _payload(git_dir_bytes="44 gigabytes")),
    ("git_bloat_size", "size_negative", _payload(git_dir_bytes=-5)),
    ("git_bloat_maintenance", "unreachable", None),
    ("git_bloat_maintenance", "streak_absent", _payload()),
    ("git_bloat_maintenance", "streak_garbage", _payload(maintenance_failures="many")),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind, shape, payload", _INCONCLUSIVE,
                         ids=[f"{k}-{s}" for k, s, _p in _INCONCLUSIVE])
async def test_an_inconclusive_poll_never_ends_the_alert(
        kind, shape, payload, service, seed_agent, db, clock):
    """An unreachable agent, git switched off, a cycle that did not run, or a
    missing / malformed measurement says nothing about recovery; the row stays.
    Control: the conclusive recovery that follows still ends it."""
    sc = SCENARIOS[kind]
    seed_agent()
    await _raise(service, clock, sc)
    (row,) = _pending(db, sc["prefix"])
    for _ in range(3):
        clock.advance(**_MIN)
        await _poll(service, payload)
    assert db.get_operator_queue_item(row["id"])["status"] == "pending"

    clock.advance(**_MIN)
    await _poll(service, sc["recover"])
    assert db.get_operator_queue_item(row["id"])["status"] == "cancelled"
