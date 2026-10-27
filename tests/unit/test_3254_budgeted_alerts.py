"""#3254 — the #1677 budget family reports through the platform-alert seam.

The budgeted emitters (the flood alarm, the effect guard's refusal, the missing
skill, the gitignore sweep, the Workspace problem report) and the budget's own
episode alert report through ``platform_alerts.observe``, which composes the
#1677 per-(agent, type) budget inside its one locked create. The old helper,
``create_bounded_alert(_outcome)``, is a thin delegate of it.

Driven against a fresh database per test (``db_backend``: SQLite, and
PostgreSQL when ``TEST_POSTGRES_URL`` is set) with both clocks frozen — the
wall clock the seam stamps rows and lifetimes with, and the monotonic clock the
cooldowns read. ``observe`` never raises and a failure only logs, so every
assertion here is about the rows.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from db_harness import db_backend  # noqa: F401 — fixture

pytestmark = pytest.mark.unit

AGENT = "agent-3254"
T0 = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
TS = "2026-01-05T09:00:00.000000Z"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _Clock:
    """The wall clock (`utc_now_iso`, `datetime.now`) and the monotonic clock,
    frozen together and moved together."""

    def __init__(self, start: datetime):
        self.now = start
        self.mono = 10_000.0

    def advance(self, **delta):
        step = timedelta(**delta)
        self.now += step
        self.mono += step.total_seconds()

    def iso(self) -> str:
        return self.now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def clock(monkeypatch):
    import utils.helpers as helpers
    import db.operator_queue as dbq
    from client_portal import service as portal
    from services import operator_queue_service as oqs
    from services import platform_alerts as pa

    c = _Clock(T0)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return c.now if tz is not None else c.now.replace(tzinfo=None)

    monkeypatch.setattr(pa, "datetime", _FrozenDatetime)
    monkeypatch.setattr(dbq, "datetime", _FrozenDatetime)   # the ent#815 commit-lag check
    monkeypatch.setattr(helpers, "utc_now_iso", c.iso)
    monkeypatch.setattr(dbq, "utc_now_iso", c.iso)
    monkeypatch.setattr(portal, "utc_now_iso", c.iso)
    monkeypatch.setattr(oqs.time, "monotonic", lambda: c.mono)
    # R6: freeze the clocks the RUNNING methods read too. A test file that pops
    # `database` and `db.*` from sys.modules and restores only some of them
    # (test_1595_sync_health_signals does) leaves `database.db` and
    # `ask_service.db` on an older `db.operator_queue` module than the one
    # imported above, so the module patches would miss the create, the expiry
    # sweep and the answer's compare-and-set, which then read the real clock.
    import database
    from services import ask_service
    for ops in (database.db._operator_queue_ops, ask_service.db._operator_queue_ops):
        live = type(ops).create_platform_item.__globals__
        if live is not vars(dbq):
            monkeypatch.setitem(live, "utc_now_iso", c.iso)
            monkeypatch.setitem(live, "datetime", _FrozenDatetime)
    return c


@pytest.fixture
def seam(db_backend, clock, monkeypatch):  # noqa: F811 — the db_harness fixture
    """A fresh database, the frozen clocks, and what `observe` spawns collected
    (`drain` runs it the way the event loop would; a task's failure stays in it)."""
    import database
    from services import operator_queue_service as oqs
    from services import operator_resume_service

    jobs: list = []
    monkeypatch.setattr(operator_resume_service, "spawn_on_loop", jobs.append)
    monkeypatch.setattr(oqs, "_websocket_manager", None)
    oqs.reset_alert_budget_state()
    yield SimpleNamespace(db=database.db, clock=clock, jobs=jobs, errors=[])
    oqs.reset_alert_budget_state()


def drain(seam):
    while seam.jobs:
        factory = seam.jobs.pop(0)
        try:
            asyncio.run(factory())
        except Exception as e:  # noqa: BLE001
            seam.errors.append(e)


def rows(seam, agent=AGENT, status=None):
    kwargs = {"agent_name": agent, "limit": 500}
    if status:
        kwargs["status"] = status
    return seam.db.list_operator_queue_items(**kwargs)


def one(seam, agent=AGENT, **match):
    found = [
        r
        for r in rows(seam, agent)
        if all(
            (r.get(k) if k != "kind" else (r.get("subject") or "").split(":")[0]) == v
            for k, v in match.items()
        )
    ]
    assert (
        len(found) == 1
    ), f"expected one row matching {match}, found {len(found)}: {found}"
    return found[0]


def person_ends(seam, row):
    from services import ask_service

    return ask_service.answer(
        row,
        response="acknowledged",
        response_text=None,
        actor=ask_service.Actor(email="op@example.com"),
    ).rows[0]


def _parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# The registry and the names other modules reach back for
# ---------------------------------------------------------------------------


def test_budgeted_types_equal_the_registrys_budgeted_kinds():
    """`_BUDGETED_ALERT_TYPES` is the delegate's closed set; the registry is the
    seam's. They must name the same five types or a type admitted by one is
    unroutable through the other."""
    from services import operator_queue_service as oqs
    from services import platform_alerts as pa

    registry = {k.budgeted_type for k in pa.KINDS.values() if k.budgeted_type}
    assert oqs._BUDGETED_ALERT_TYPES == registry
    assert len(registry) == 5
    for kind in pa.KINDS.values():
        if kind.budgeted_type:
            assert [
                k.name
                for k in pa.KINDS.values()
                if k.budgeted_type == kind.budgeted_type
            ] == [kind.name]


def test_reach_back_names_are_importable():
    """`observe` reaches these by a function-local import and never raises: a
    split of operator_queue_service that dropped one would turn every budgeted
    alert into `failed` and an ERROR line, not a red build. This is the build."""
    from services.operator_queue_service import (  # noqa: F401
        ALERT_CREATED,
        ALERT_FAILED_COUNT,
        ALERT_FAILED_CREATE,
        ALERT_REFUSED_AT_BUDGET,
        ALERT_REFUSED_UNREGISTERED,
        OPERATOR_ALERT_MAX_PENDING_PER_TYPE,
        _BUDGETED_ALERT_TYPES,
        _maybe_emit_alert_budget_episode,
        _truncate_with_marker,
        _websocket_manager,
        create_bounded_alert,
        create_bounded_alert_outcome,
        reset_alert_budget_state,
    )

    assert inspect.iscoroutinefunction(_maybe_emit_alert_budget_episode)
    assert list(inspect.signature(_maybe_emit_alert_budget_episode).parameters) == [
        "agent_name",
        "item_type",
        "last_triggered_by",
    ]
    assert inspect.iscoroutinefunction(create_bounded_alert_outcome)
    assert isinstance(OPERATOR_ALERT_MAX_PENDING_PER_TYPE, int)


# ---------------------------------------------------------------------------
# The delegate (D3 / D12)
# ---------------------------------------------------------------------------


def _legacy(item_type, agent=AGENT, *, command="/gen"):
    """A pre-#3254 item of each budgeted type, as its emitter built it."""
    base = {
        "agent_name": agent,
        "status": "pending",
        "priority": "high",
        "title": f"{item_type} title",
        "question": f"{item_type} question",
        "created_at": TS,
        "type": item_type,
    }
    return {
        "skill_not_found": {
            **base,
            "id": f"skill-not-found-{agent}-{TS}",
            "context": {
                "command": command,
                "triggered_by": "schedule",
                "execution_id": "",
            },
        },
        "queue_flood": {
            **base,
            "id": f"queue-flood-{agent}-{TS}",
            "context": {"held": 3, "reason": "ingestion_cap"},
        },
        "effect_unguarded": {
            **base,
            "id": f"effect-unguarded-{agent}-{TS}",
            "context": {"effect_type": "message", "reason": "absent"},
        },
        "gitignore_untracked": {
            **base,
            "id": f"gitignore-untracked-{agent}-{TS}",
            "context": {
                "removed_paths": ["a.env"],
                "removed_count": 1,
                "unignored_paths": [],
                "unignored_count": 0,
            },
        },
        "workspace_problem_report": {
            **base,
            "id": "workspace-problem-" + "ab" * 16,
            "priority": "medium",
            "context": {
                "target_kind": "message",
                "target_id": "m1",
                "has_comment": False,
            },
        },
    }[item_type]


# (type, its kind, the key the delegate must derive from the legacy item)
_ADMITTED = [
    ("skill_not_found", "skill_not_found", "/gen"),
    ("queue_flood", "queue_flood", "ingestion_cap"),
    ("effect_unguarded", "effect_unguarded", "message:absent"),
    # The two kinds whose legacy ids carry no subject: the request id minus the
    # kind's prefix (D12). For the problem report that is its day digest.
    ("gitignore_untracked", "gitignore_untracked", f"{AGENT}-{TS}"),
    ("workspace_problem_report", "workspace_problem_report", "ab" * 16),
]


@pytest.mark.parametrize("item_type,kind,key", _ADMITTED, ids=[a[0] for a in _ADMITTED])
def test_the_delegate_admits_every_budgeted_type_onto_its_kind(
    seam, item_type, kind, key
):
    from services import operator_queue_service as oqs
    from services import platform_alerts as pa

    out = asyncio.run(oqs.create_bounded_alert_outcome(AGENT, _legacy(item_type)))
    assert out == oqs.ALERT_CREATED
    (row,) = rows(seam)
    assert row["type"] == item_type
    assert row["request_id"].startswith(pa.KINDS[kind].prefix)
    assert row["subject"] == pa.subject_for(kind, key)
    assert row["subject"] is not None
    assert row["status"] == "pending" and row["context"]["seen_count"] == 1


def test_a_second_reading_through_the_delegate_is_an_update_and_not_created(seam):
    """D3: `updated` is not `created` — the bool view stays False, so a paired
    side effect never fires twice for one pending row."""
    from services import operator_queue_service as oqs

    assert (
        asyncio.run(oqs.create_bounded_alert(AGENT, _legacy("skill_not_found"))) is True
    )
    seam.clock.advance(hours=1)
    assert (
        asyncio.run(oqs.create_bounded_alert_outcome(AGENT, _legacy("skill_not_found")))
        == "updated"
    )
    assert (
        asyncio.run(oqs.create_bounded_alert(AGENT, _legacy("skill_not_found")))
        is False
    )
    (row,) = rows(seam)
    assert row["context"]["seen_count"] == 3


@pytest.mark.parametrize(
    "item",
    [
        # an id of another kind under a registered type
        {
            "id": f"queue-flood-{AGENT}-{TS}",
            "type": "skill_not_found",
            "title": "t",
            "question": "q",
            "context": {"reason": "ingestion_cap"},
        },
        # the type's own kind, but no key to read back (the command is missing)
        {
            "id": f"skill-not-found-{AGENT}-{TS}",
            "type": "skill_not_found",
            "title": "t",
            "question": "q",
            "context": {},
        },
        # no id at all
        {
            "type": "effect_unguarded",
            "title": "t",
            "question": "q",
            "context": {"effect_type": "message", "reason": "absent"},
        },
    ],
    ids=["other-kind", "no-key", "no-id"],
)
def test_an_item_that_does_not_read_back_as_its_kind_is_refused_loudly(
    seam, caplog, item
):
    from services import operator_queue_service as oqs

    with caplog.at_level(logging.ERROR, logger="services.operator_queue_service"):
        out = asyncio.run(oqs.create_bounded_alert_outcome(AGENT, item))
    assert out == oqs.ALERT_FAILED_CREATE
    assert rows(seam) == []
    assert any("[#1677 budget]" in r.message for r in caplog.records)


def test_an_unregistered_type_is_refused_and_writes_nothing(seam, caplog):
    from services import operator_queue_service as oqs

    with caplog.at_level(logging.ERROR, logger="services.operator_queue_service"):
        out = asyncio.run(
            oqs.create_bounded_alert_outcome(
                AGENT, {**_legacy("skill_not_found"), "type": "alert"}
            )
        )
    assert out == oqs.ALERT_REFUSED_UNREGISTERED
    assert rows(seam) == []
    assert any("[#1677 budget]" in r.message for r in caplog.records)


def test_a_failed_seam_write_is_failed_create_with_the_marker(
    seam, caplog, monkeypatch
):
    from services import operator_queue_service as oqs

    def _down(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(seam.db, "create_platform_operator_queue_item", _down)
    with caplog.at_level(logging.ERROR):
        out = asyncio.run(
            oqs.create_bounded_alert_outcome(AGENT, _legacy("queue_flood"))
        )
    assert out == oqs.ALERT_FAILED_CREATE
    assert any(
        "[#1677 budget]" in r.message
        for r in caplog.records
        if r.name == "services.operator_queue_service"
    )
    assert seam.jobs == []  # a failure spawns no episode


def test_a_person_ended_subject_is_snoozed_through_the_delegate(seam):
    from services import operator_queue_service as oqs

    asyncio.run(oqs.create_bounded_alert(AGENT, _legacy("effect_unguarded")))
    person_ends(seam, rows(seam)[0])
    seam.clock.advance(days=1)
    assert (
        asyncio.run(
            oqs.create_bounded_alert_outcome(AGENT, _legacy("effect_unguarded"))
        )
        == "snoozed"
    )
    assert [r["status"] for r in rows(seam)] == ["acknowledged"]


# ---------------------------------------------------------------------------
# The budget and its episode alert (subject `alert_budget:<type>`)
# ---------------------------------------------------------------------------


def _fill_budget(seam, item_type="skill_not_found"):
    from services import operator_queue_service as oqs

    for i in range(oqs.OPERATOR_ALERT_MAX_PENDING_PER_TYPE):
        assert (
            asyncio.run(
                oqs.create_bounded_alert_outcome(
                    AGENT, _legacy(item_type, command=f"/cmd-{i}")
                )
            )
            == oqs.ALERT_CREATED
        )


def test_at_the_budget_a_new_subject_is_refused_and_the_episode_files_once(
    seam, caplog
):
    from services import operator_queue_service as oqs

    _fill_budget(seam)
    with caplog.at_level(logging.WARNING, logger="services.operator_queue_service"):
        out = asyncio.run(
            oqs.create_bounded_alert_outcome(
                AGENT, _legacy("skill_not_found", command="/one-too-many")
            )
        )
    assert out == oqs.ALERT_REFUSED_AT_BUDGET
    assert any("[#1677 budget]" in r.message for r in caplog.records)
    drain(seam)
    episode = one(seam, kind="alert_budget")
    assert episode["subject"] == "alert_budget:skill_not_found"
    assert episode["request_id"].startswith("alert-budget-")
    assert episode["type"] == "alert" and episode["priority"] == "high"
    assert episode["context"]["alert_type"] == "skill_not_found"
    assert episode["context"]["cap"] == oqs.OPERATOR_ALERT_MAX_PENDING_PER_TYPE
    assert "/one-too-many" not in str(episode)  # no agent text (G-04)
    assert (
        len([r for r in rows(seam) if r["type"] == "skill_not_found"])
        == oqs.OPERATOR_ALERT_MAX_PENDING_PER_TYPE
    )


def test_at_the_budget_a_reading_of_a_pending_subject_is_never_refused(seam):
    from services import operator_queue_service as oqs

    _fill_budget(seam)
    out = asyncio.run(
        oqs.create_bounded_alert_outcome(
            AGENT, _legacy("skill_not_found", command="/cmd-0")
        )
    )
    assert out == "updated"
    drain(seam)
    assert [r for r in rows(seam) if r["request_id"].startswith("alert-budget-")] == []


def test_the_episode_is_one_row_reread_by_a_refusal_after_the_cooldown(seam):
    """The seam properties of `alert_budget`: one subject per (agent, type); a
    second reading bumps `seen_count` and moves `last_seen_at` and the 14-day
    deadline; inside the cooldown a refusal does not re-read it."""
    from services import operator_queue_service as oqs

    _fill_budget(seam)
    refuse = lambda c: asyncio.run(
        oqs.create_bounded_alert_outcome(  # noqa: E731
            AGENT, _legacy("skill_not_found", command=c)
        )
    )
    assert refuse("/x1") == oqs.ALERT_REFUSED_AT_BUDGET
    drain(seam)
    first = one(seam, kind="alert_budget")
    assert first["context"]["seen_count"] == 1
    assert _parse(first["expires_at"]) == T0 + timedelta(days=14)

    seam.clock.advance(seconds=oqs.OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS - 1)
    refuse("/x2")
    drain(seam)
    assert (
        one(seam, kind="alert_budget")["context"]["seen_count"] == 1
    )  # inside the cooldown

    seam.clock.advance(hours=2)
    refuse("/x3")
    drain(seam)
    again = one(seam, kind="alert_budget")
    assert again["id"] == first["id"]
    assert again["context"]["seen_count"] == 2
    assert again["last_seen_at"] > first["last_seen_at"]
    assert again["expires_at"] > first["expires_at"]


def test_a_person_ending_of_the_episode_stands_through_a_later_refusal(seam):
    """AC 8 for `alert_budget`: a person acknowledges the budget episode through
    `ask_service.answer`; the next refusal past the cooldown re-reads the
    subject, and the person's ending stands (snoozed, no new episode row)."""
    from services import operator_queue_service as oqs

    for i in range(oqs.OPERATOR_ALERT_MAX_PENDING_PER_TYPE):
        asyncio.run(oqs.create_bounded_alert_outcome(AGENT, _legacy("skill_not_found", command=f"/c{i}")))
    asyncio.run(oqs.create_bounded_alert_outcome(AGENT, _legacy("skill_not_found", command="/x1")))
    drain(seam)
    episode = one(seam, kind="alert_budget")
    person_ends(seam, episode)
    seam.clock.advance(seconds=oqs.OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS + 60)
    asyncio.run(oqs.create_bounded_alert_outcome(AGENT, _legacy("skill_not_found", command="/x2")))
    drain(seam)
    episodes = [r for r in rows(seam) if (r.get("subject") or "").startswith("alert_budget:")]
    assert [(r["id"], r["status"], r["disposed_by"]) for r in episodes] == [
        (episode["id"], "acknowledged", "person")]


def test_the_episode_is_unbudgeted_so_it_cannot_recurse(seam):
    from services import platform_alerts as pa

    assert pa.KINDS["alert_budget"].budgeted_type is None


def test_legacy_episode_rows_derive_the_subject_observe_uses():
    """R1: the second upgrade sweep stamps a pre-#3254 episode row with the
    subject the seam now writes, so the two land on one row."""
    from services import platform_alerts as pa

    got = pa.derive_legacy_subject(
        f"alert-budget-{AGENT}-skill_not_found-b5891234",
        {"reason": "platform_alert_budget", "alert_type": "skill_not_found", "cap": 5},
    )
    assert got.kind == "alert_budget"
    assert got.subject == pa.subject_for("alert_budget", "skill_not_found")


# ---------------------------------------------------------------------------
# The flood alarm (`queue_flood`): one row per reason, re-read while the
# episode lasts (R9), ended where the episode ends (D8)
# ---------------------------------------------------------------------------

def _sync_loop(monkeypatch, *, over_cap=True):
    """The real `_sync_agent` over a stand-in agent file: the file side of the
    db (`oqs.db`) is stubbed, and the alert lands in the real seam."""
    import json
    from unittest.mock import AsyncMock, MagicMock

    from services import operator_queue_service as oqs
    from services.rate_limiter import RateLimitResult

    state = {"over_cap": over_cap}
    db = MagicMock()
    db.count_operator_queue_pending_for_agent.side_effect = (
        lambda agent, item_type=None, exclude_request_id_prefixes=None:
        oqs.OPERATOR_QUEUE_MAX_PENDING_PER_AGENT if state["over_cap"] else 0)
    db.get_operator_queue_responded_for_agent.return_value = []
    db.get_operator_queue_terminal_for_agent.return_value = []
    db.get_operator_queue_sync_index_for_agent.return_value = {
        "open": [], "terminal": {}, "foreign": []}
    db.set_operator_queue_sync_state.return_value = False
    db.set_operator_queue_delivery_state.return_value = False
    db.create_operator_queue_item_with_outcome.side_effect = lambda agent, item, **kw: (None, True)
    db.mark_operator_queue_unconfirmed.return_value = 0
    db.refresh_operator_queue_last_confirmed.return_value = 0
    db.get_setting_value.return_value = "24"
    monkeypatch.setattr(oqs.deps, "db", db)

    requests = [{"id": f"req-{i}", "status": "pending", "title": "t", "question": "q"}
                for i in range(3)]
    file = {"content": json.dumps({"requests": requests})}
    client = MagicMock()

    async def _read(path, timeout=5.0):
        return {"success": True, "content": file["content"]}

    async def _write(path, content, **kw):
        file["content"] = content
        return {"success": True}

    client.read_file = AsyncMock(side_effect=_read)
    client.write_file = AsyncMock(side_effect=_write)
    monkeypatch.setattr(oqs.deps, "AgentClient", lambda name: client)
    monkeypatch.setattr(oqs.rate_limiter, "check",
                        lambda *a, **k: RateLimitResult(True, 10, 0, 60))
    svc = oqs.OperatorQueueSyncService()
    return SimpleNamespace(svc=svc, state=state,
                           cycle=lambda: asyncio.run(svc._sync_agent(AGENT)))


def _flood_rows(seam, status=None):
    return [r for r in rows(seam, status=status)
            if r["request_id"].startswith("queue-flood-")]


def test_a_persisting_flood_is_one_row_reread_for_thirty_days_then_cleared(seam, monkeypatch):
    """R9 through the real poll: an over-cap episode that lasts 30 simulated
    days, swept by the real expiry every 3 hours, keeps exactly one pending row
    (subject `queue_flood:ingestion_cap`) whose deadline the 6-hourly re-read
    moves; the cycle that holds nothing ends it as the platform (D8)."""
    loop = _sync_loop(monkeypatch)
    loop.cycle()
    (first,) = _flood_rows(seam)
    assert first["subject"] == "queue_flood:ingestion_cap"
    assert first["type"] == "queue_flood" and first["priority"] == "high"
    assert first["context"]["reason"] == "ingestion_cap" and first["context"]["held"] >= 1
    assert first["context"]["seen_count"] == 1
    assert _parse(first["expires_at"]) == T0 + timedelta(days=14)

    for _ in range(30 * 8):
        seam.clock.advance(hours=3)
        loop.cycle()
        seam.db.mark_operator_queue_expired()
    (row,) = _flood_rows(seam)
    assert row["id"] == first["id"] and row["status"] == "pending"
    assert row["context"]["seen_count"] == 1 + 30 * 4         # one re-read per 6 hours
    assert _parse(row["last_seen_at"]) == T0 + timedelta(days=30)
    assert _parse(row["expires_at"]) == T0 + timedelta(days=44)

    loop.state["over_cap"] = False
    loop.cycle()
    ended = seam.db.get_operator_queue_item(first["id"])
    assert (ended["status"], ended["disposed_by"], ended["disposition_reason"]) == (
        "cancelled", "platform", "condition_cleared")
    assert _flood_rows(seam, status="pending") == []


def test_without_an_episode_the_clear_site_reads_nothing(seam, monkeypatch):
    """D8: `_end_flood_episode` runs every cycle the condition is absent; with no
    episode it must cost no database read."""
    from services import operator_queue_service as oqs

    reads = []
    real = seam.db.find_pending_operator_queue_by_subject
    monkeypatch.setattr(seam.db, "find_pending_operator_queue_by_subject",
                        lambda *a: reads.append(a) or real(*a))
    svc = oqs.OperatorQueueSyncService()
    svc._end_flood_episode(AGENT, "ingestion_cap")
    svc._end_flood_episode(AGENT, "oversize_file")
    assert reads == []


def test_the_two_reasons_are_two_subjects_and_end_separately(seam):
    from services import operator_queue_service as oqs

    svc = oqs.OperatorQueueSyncService()
    asyncio.run(svc._maybe_emit_flood_alert(AGENT, held=4))
    seam.clock.advance(seconds=oqs.OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS + 1)
    asyncio.run(svc._maybe_emit_flood_alert(AGENT, reason="oversize_file"))
    assert sorted(r["subject"] for r in _flood_rows(seam)) == [
        "queue_flood:ingestion_cap", "queue_flood:oversize_file"]
    svc._end_flood_episode(AGENT, "oversize_file")
    assert [r["subject"] for r in _flood_rows(seam, status="pending")] == [
        "queue_flood:ingestion_cap"]


def test_a_failed_flood_reading_is_retried_after_the_cooldown(seam, monkeypatch):
    """#3130 I1 on the real seam: a write that fails releases the episode, and
    the next window files the alert once."""
    from services import operator_queue_service as oqs

    real = seam.db.create_platform_operator_queue_item
    calls = {"n": 0}

    def _flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return real(*a, **k)

    monkeypatch.setattr(seam.db, "create_platform_operator_queue_item", _flaky)
    loop = _sync_loop(monkeypatch)
    loop.cycle()
    assert _flood_rows(seam) == []
    loop.cycle()                                   # inside the cooldown: no retry yet
    assert calls["n"] == 1
    seam.clock.advance(seconds=oqs.OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS + 1)
    loop.cycle()
    assert len(_flood_rows(seam)) == 1 and calls["n"] == 2


def test_a_failed_reread_is_retried_no_sooner_than_the_cooldown(seam, monkeypatch):
    """#3130 I1 on R9's re-read: a re-read whose write fails keeps the episode
    (its alert was filed by an earlier reading), and the retry waits out the
    cooldown like any other attempt."""
    from services import operator_queue_service as oqs

    real = seam.db.create_platform_operator_queue_item
    attempts = []

    def _flaky(*a, **k):
        attempts.append(seam.clock.now)
        if len(attempts) == 2:                     # the 6-hourly re-read fails
            raise RuntimeError("database is locked")
        return real(*a, **k)

    monkeypatch.setattr(seam.db, "create_platform_operator_queue_item", _flaky)
    loop = _sync_loop(monkeypatch)
    loop.cycle()                                   # the alert files
    seam.clock.advance(hours=6, seconds=1)
    loop.cycle()                                   # the re-read fails
    assert len(attempts) == 2
    seam.clock.advance(seconds=5)
    loop.cycle()                                   # inside the cooldown: no retry yet
    assert len(attempts) == 2
    seam.clock.advance(seconds=oqs.OPERATOR_QUEUE_FLOOD_ALERT_COOLDOWN_SECONDS)
    loop.cycle()                                   # after it: the re-read lands
    assert len(attempts) == 3
    (row,) = _flood_rows(seam)
    assert row["context"]["seen_count"] == 2


def test_a_failed_reread_keeps_the_episode_so_its_end_still_clears_the_alert(seam, monkeypatch):
    """AC 2 through a failed re-read: the 6-hourly re-read's write fails, the
    condition then clears before the retry; the episode was kept, so the cycle
    that holds nothing still ends the alert as the platform."""
    real = seam.db.create_platform_operator_queue_item
    attempts = []

    def _flaky(*a, **k):
        attempts.append(1)
        if len(attempts) == 2:                     # the 6-hourly re-read fails
            raise RuntimeError("database is locked")
        return real(*a, **k)

    monkeypatch.setattr(seam.db, "create_platform_operator_queue_item", _flaky)
    loop = _sync_loop(monkeypatch)
    loop.cycle()                                   # the alert files
    seam.clock.advance(hours=6, seconds=1)
    loop.cycle()                                   # the re-read fails
    loop.state["over_cap"] = False
    seam.clock.advance(seconds=5)
    loop.cycle()                                   # nothing held: the condition cleared
    (row,) = _flood_rows(seam)
    assert (row["status"], row["disposed_by"], row["disposition_reason"]) == (
        "cancelled", "platform", "condition_cleared")
    assert len(attempts) == 2


def test_a_person_ending_of_the_flood_stands_through_the_reread_and_the_clear(seam, monkeypatch):
    loop = _sync_loop(monkeypatch)
    loop.cycle()
    (row,) = _flood_rows(seam)
    person_ends(seam, row)
    seam.clock.advance(hours=7)                    # past the 6-hour re-read
    loop.cycle()
    assert [r["status"] for r in _flood_rows(seam)] == ["acknowledged"]
    loop.state["over_cap"] = False
    loop.cycle()                                   # the episode ends; the clear finds nothing
    still = seam.db.get_operator_queue_item(row["id"])
    assert (still["status"], still["disposed_by"]) == ("acknowledged", "person")
    assert len(_flood_rows(seam)) == 1


def test_the_flood_sends_the_thin_sync_trigger_not_its_title(seam, monkeypatch):
    """#918 / D16: the old `operator_queue_new` carried the title; the seam's
    trigger names the host agent only."""
    import json
    from unittest.mock import AsyncMock, MagicMock

    from services import operator_queue_service as oqs

    ws = MagicMock()
    ws.broadcast = AsyncMock()
    monkeypatch.setattr(oqs, "_websocket_manager", ws)
    asyncio.run(oqs.OperatorQueueSyncService()._maybe_emit_flood_alert(AGENT, held=2))
    drain(seam)
    payloads = [json.loads(c.args[0]) for c in ws.broadcast.await_args_list]
    assert payloads == [{"type": "operator_queue_sync", "data": {"agent_name": AGENT}}]


def test_legacy_flood_rows_derive_the_subject_observe_uses():
    from services import platform_alerts as pa
    got = pa.derive_legacy_subject(f"queue-flood-{AGENT}-{TS}",
                                   {"held": 3, "reason": "ingestion_cap"})
    assert got.kind == "queue_flood"
    assert got.subject == pa.subject_for("queue_flood", "ingestion_cap")


# ---------------------------------------------------------------------------
# The effect guard's refusal (`effect_unguarded`, lifetime-only)
# ---------------------------------------------------------------------------

def _refuse_effect(monkeypatch, effect_type="message", reason="absent"):
    from services import idempotency_service as idem
    monkeypatch.setattr(idem, "effect_dedup_required", lambda agent: True)
    with pytest.raises(idem.EffectUnguardedError):
        asyncio.run(idem._on_unguarded_effect(effect_type, AGENT, reason))


def test_an_unguarded_effect_is_one_row_per_effect_and_reason(seam, monkeypatch):
    """The seam properties of a lifetime-only kind: the subject, a second
    reading bumping `seen_count` and moving `last_seen_at` and the deadline,
    another reason another subject — and the send refused every time."""
    _refuse_effect(monkeypatch)
    (first,) = rows(seam)
    assert first["subject"] == "effect_unguarded:message:absent"
    assert first["type"] == "effect_unguarded" and first["request_id"].startswith("effect-unguarded-")
    assert first["context"] == {"effect_type": "message", "reason": "absent", "seen_count": 1}
    assert _parse(first["expires_at"]) == T0 + timedelta(days=14)

    seam.clock.advance(hours=1)
    _refuse_effect(monkeypatch)
    (again,) = rows(seam)
    assert again["id"] == first["id"] and again["context"]["seen_count"] == 2
    assert again["last_seen_at"] > first["last_seen_at"] and again["expires_at"] > first["expires_at"]

    _refuse_effect(monkeypatch, reason="foreign")
    assert sorted(r["subject"] for r in rows(seam)) == [
        "effect_unguarded:message:absent", "effect_unguarded:message:foreign"]


def test_an_unguarded_effect_row_ends_with_its_lifetime(seam, monkeypatch):
    _refuse_effect(monkeypatch)
    seam.clock.advance(days=14, seconds=1)
    seam.db.mark_operator_queue_expired()
    assert [r["status"] for r in rows(seam)] == ["expired"]


def test_legacy_effect_rows_derive_the_subject_observe_uses():
    from services import platform_alerts as pa
    got = pa.derive_legacy_subject(f"effect-unguarded-{AGENT}-{TS}",
                                   {"effect_type": "message", "reason": "absent"})
    assert got.kind == "effect_unguarded"
    assert got.subject == pa.subject_for("effect_unguarded", "message:absent")


# ---------------------------------------------------------------------------
# A scheduled command whose skill is missing (`skill_not_found`)
# ---------------------------------------------------------------------------

def _missing_skill(seam, command="/gen"):
    import services.task_execution_service as tes
    asyncio.run(tes._alert_skill_not_found(AGENT, command, "exec-1", "schedule"))


@pytest.fixture
def notifications(seam, monkeypatch):
    sent = []
    real = seam.db.create_notification

    def _record(agent, notification):
        sent.append(notification)
        return real(agent, notification)

    monkeypatch.setattr(seam.db, "create_notification", _record)
    return sent


def test_a_repeat_of_a_pending_command_updates_its_row_and_sends_no_notification(seam, notifications):
    """D3: the notification goes with a NEW row only. A repeat of a pending
    command is an update (each run re-reads it, D9), and its row already had
    its notification."""
    from services import platform_alerts as pa

    _missing_skill(seam)
    (first,) = rows(seam)
    assert first["subject"] == pa.subject_for("skill_not_found", "/gen")
    assert first["context"]["command"] == "/gen" and first["context"]["seen_count"] == 1
    assert len(notifications) == 1

    seam.clock.advance(days=1)
    _missing_skill(seam)
    (again,) = rows(seam)
    assert again["id"] == first["id"] and again["context"]["seen_count"] == 2
    assert again["expires_at"] > first["expires_at"]
    assert len(notifications) == 1


def test_a_pending_pre_seam_row_for_the_command_still_stands_for_it(seam, notifications):
    """A subject-less row an older build filed is matched by the dedup read
    until the upgrade sweep stamps it: no second row beside it, no notification."""
    from db_harness import run as _run

    _run("INSERT INTO operator_queue (id, agent_name, request_id, type, status, priority, "
         "title, question, context, created_at) VALUES ('legacy-1', :a, :r, 'skill_not_found', "
         "'pending', 'high', 't', 'q', :c, :ts)",
         a=AGENT, r=f"skill-not-found-{AGENT}-{TS}", c='{"command": "/gen"}', ts=TS)
    _missing_skill(seam)
    assert [r["id"] for r in rows(seam)] == ["legacy-1"]
    assert notifications == []


def test_at_the_budget_a_new_command_is_refused_without_a_notification(seam, notifications):
    from services import operator_queue_service as oqs

    for i in range(oqs.OPERATOR_ALERT_MAX_PENDING_PER_TYPE):
        _missing_skill(seam, f"/cmd-{i}")
    sent = len(notifications)
    _missing_skill(seam, "/one-more")
    drain(seam)
    assert len(notifications) == sent
    assert "/one-more" not in str([r["context"] for r in rows(seam)])
    assert one(seam, kind="alert_budget")["subject"] == "alert_budget:skill_not_found"


def test_a_person_ending_of_a_missing_skill_stands_and_sends_no_notification(
        seam, notifications, monkeypatch):
    """AC 8 for `skill_not_found`: a person acknowledges the alert through
    `ask_service.answer`; the next scheduled run of the same command is
    `snoozed`, files no row, and sends no paired notification (D3: only a
    `created` row is notified)."""
    from services import platform_alerts as pa

    _missing_skill(seam)
    (row,) = rows(seam)
    assert len(notifications) == 1
    person_ends(seam, row)
    seam.clock.advance(days=1)
    outcomes = []
    real = pa.observe
    monkeypatch.setattr(pa, "observe", lambda *a, **k: outcomes.append(real(*a, **k)) or outcomes[-1])
    _missing_skill(seam)
    assert outcomes == ["snoozed"]
    assert [(r["id"], r["status"], r["disposed_by"]) for r in rows(seam)] == [
        (row["id"], "acknowledged", "person")]
    assert len(notifications) == 1


def test_legacy_skill_rows_derive_the_subject_observe_uses():
    from services import platform_alerts as pa
    got = pa.derive_legacy_subject(f"skill-not-found-{AGENT}-{TS}",
                                   {"command": "/gen", "triggered_by": "schedule"})
    assert got.kind == "skill_not_found"
    assert got.subject == pa.subject_for("skill_not_found", "/gen")


# ---------------------------------------------------------------------------
# The gitignore sweep's alert (`gitignore_untracked`, must-act, D1)
# ---------------------------------------------------------------------------

def _push(removed=(), unignored=(), removed_total=0, agent=AGENT):
    from services.git_service import gitignore_sweep as sw
    sweep = sw.GitignoreSweep(removed=tuple(removed), unignored=tuple(unignored),
                              removed_total=removed_total)
    asyncio.run(sw._emit_gitignore_untracked_alert(agent, sweep))


def _gitignore_rows(seam, status=None):
    return [r for r in rows(seam, status=status) if r["type"] == "gitignore_untracked"]


def test_a_repeated_change_set_is_one_row_that_never_expires(seam):
    """The seam properties of a must-act kind: the subject is the change-set
    digest, a repeat bumps `seen_count` and `last_seen_at`, and the deadline
    stays NULL."""
    _push(removed=("b.key", "a.env"))
    (first,) = _gitignore_rows(seam)
    assert first["subject"].startswith("gitignore_untracked:")
    assert len(first["subject"].split(":", 1)[1]) == 32
    assert first["expires_at"] is None and first["priority"] == "high"
    assert first["context"]["removed_paths"] == ["b.key", "a.env"]

    seam.clock.advance(hours=1)
    _push(removed=("a.env", "b.key"))                  # the same set, in another order
    (again,) = _gitignore_rows(seam)
    assert again["id"] == first["id"] and again["context"]["seen_count"] == 2
    assert again["last_seen_at"] > first["last_seen_at"]
    assert again["expires_at"] is None


def test_a_different_change_set_is_a_different_row(seam):
    """A later Push never overwrites an earlier Push's path list on a pending
    must-act alert (#2529: THE surface that outlives the session)."""
    _push(removed=("a.env",))
    _push(removed=("a.env", "c.pem"))
    _push(removed=("a.env",), removed_total=2500)       # a capped list, another total
    paths = sorted(tuple(r["context"]["removed_paths"]) for r in _gitignore_rows(seam))
    assert paths == [("a.env",), ("a.env",), ("a.env", "c.pem")]
    assert len({r["subject"] for r in _gitignore_rows(seam)}) == 3


def test_the_direction_tag_keeps_an_untracked_path_apart_from_an_unignored_one(seam):
    _push(removed=("secret.env",))
    _push(unignored=("secret.env",))
    by_direction = {(r["context"]["removed_count"], r["context"]["unignored_count"]): r
                    for r in _gitignore_rows(seam)}
    assert set(by_direction) == {(1, 0), (0, 1)}
    assert by_direction[(1, 0)]["subject"] != by_direction[(0, 1)]["subject"]
    assert by_direction[(0, 1)]["priority"] == "medium"


def test_a_person_ending_of_a_change_set_stands(seam):
    _push(removed=("a.env",))
    person_ends(seam, _gitignore_rows(seam)[0])
    seam.clock.advance(days=2)
    _push(removed=("a.env",))
    assert [r["status"] for r in _gitignore_rows(seam)] == ["acknowledged"]


# ---------------------------------------------------------------------------
# The Workspace problem report (`workspace_problem_report`, must-act, D1/D10)
# ---------------------------------------------------------------------------

CLIENT = "client@example.com"


def _rate(comment=None, *, target=("message", "msg-1"), email=CLIENT):
    from client_portal import service
    return asyncio.run(service.raise_problem_report(
        AGENT, email, target_kind=target[0], target_id=target[1], comment=comment))


def _reports(seam, status=None):
    return [r for r in rows(seam, status=status) if r["type"] == "workspace_problem_report"]


def test_keep_text_counts_a_reading_and_leaves_the_text(seam):
    """D10 at the sink: with `keep_text` the pending row's title, question,
    priority and context stand; `seen_count`, `last_seen_at` and the deadline
    move. With no pending row the reading files as usual."""
    def reading(question, keep_text, **ctx):
        return seam.db.create_platform_operator_queue_item(AGENT, {
            "id": f"cb-dormant-{question}", "type": "alert", "status": "pending",
            "priority": "high", "title": f"title {question}", "question": question,
            "context": ctx, "expires_at": seam.clock.iso()}, subject="circuit_dormant:x",
            keep_text=keep_text)

    first = reading("bare", True, words=False)
    assert first["outcome"] == "created" and first["row"]["question"] == "bare"
    seam.clock.advance(hours=1)
    second = reading("with words", False, words=True)
    assert second["outcome"] == "updated" and second["row"]["question"] == "with words"
    seam.clock.advance(hours=1)
    third = reading("bare again", True, words=False)
    assert third["outcome"] == "updated" and third["changed"] is False
    row = third["row"]
    assert (row["title"], row["question"], row["priority"]) == ("title with words", "with words", "high")
    assert row["context"] == {"words": True, "seen_count": 3}
    assert row["last_seen_at"] > second["row"]["last_seen_at"]
    assert row["expires_at"] > second["row"]["expires_at"]


def test_the_comment_first_order_keeps_the_words(seam):
    """The two requests in the order the old pre-check could not handle: the
    comment's reading files the row, then the bare click lands on it."""
    assert _rate("I wanted Q3, not Q2.") is True
    seam.clock.advance(seconds=2)
    assert _rate(None) is True                      # updated: the reading is counted
    (row,) = _reports(seam)
    assert "I wanted Q3, not Q2." in row["question"]
    assert "They left no comment." not in row["question"]
    assert row["context"]["has_comment"] is True and row["context"]["seen_count"] == 2


def test_the_click_first_order_lands_the_words(seam):
    assert _rate(None) is True
    seam.clock.advance(seconds=2)
    assert _rate("Wrong customer.") is True
    (row,) = _reports(seam)
    assert "Wrong customer." in row["question"] and row["context"]["has_comment"] is True


def test_the_report_is_one_must_act_row_per_person_target_and_day(seam):
    _rate(None)
    _rate(None, target=("message", "msg-2"))
    _rate(None, email="someone-else@example.com")
    assert len(_reports(seam)) == 3
    assert {r["expires_at"] for r in _reports(seam)} == {None}
    seam.clock.advance(days=1)
    _rate(None)                                     # tomorrow's bucket is a new subject
    assert len(_reports(seam)) == 4


def test_the_report_stays_owner_only_and_about_a_person(seam):
    """trinity-enterprise#866 and the machine-key exclusion both key on the
    `workspace-problem-` request-id prefix, which the seam's ids keep."""
    from services import operator_queue_service as oqs
    _rate("words")
    (row,) = _reports(seam)
    assert row["request_id"].startswith("workspace-problem-")
    assert oqs.is_owner_only(row) and oqs.is_platform_minted(row)
    assert CLIENT not in row["subject"] and "words" not in str(row["context"])


def test_the_delegate_and_the_emitter_land_on_one_subject(seam):
    """D12: the delegate's request-id key for the problem report is the same day
    digest the emitter keys by."""
    from client_portal.service import _problem_report_id, workspace_evaluator
    from services import operator_queue_service as oqs

    rid = _problem_report_id(workspace_evaluator(CLIENT), "message", "msg-1")
    item = {**_legacy("workspace_problem_report"), "id": rid}
    assert asyncio.run(oqs.create_bounded_alert_outcome(AGENT, item)) == oqs.ALERT_CREATED
    assert _rate("the words") is True
    (row,) = _reports(seam)
    assert "the words" in row["question"] and row["context"]["seen_count"] == 2


def test_a_person_ending_of_the_report_is_not_rewritten_or_reopened(seam):
    _rate(None)
    person_ends(seam, _reports(seam)[0])
    assert _rate("arrived after the operator looked") is False      # snoozed
    (row,) = _reports(seam)
    assert row["status"] == "acknowledged" and "arrived after" not in row["question"]


# ---------------------------------------------------------------------------
# The other shape: the clock 30 days ahead
# ---------------------------------------------------------------------------

def test_the_clock_30_days_ahead(seam, notifications):
    """The must-act gitignore and problem-report rows are still pending after
    the real expiry sweep; a full `skill_not_found` budget frees itself: its
    rows pass their 14-day lifetime and the sweep ends them, so a new distinct
    command files (with its notification)."""
    from services import operator_queue_service as oqs

    _push(removed=("a.env",))
    _rate("still waiting for a person")
    for i in range(oqs.OPERATOR_ALERT_MAX_PENDING_PER_TYPE):
        _missing_skill(seam, f"/cmd-{i}")
    _missing_skill(seam, "/refused-today")
    assert "/refused-today" not in str([r["context"] for r in rows(seam)])

    seam.clock.advance(days=30)
    seam.db.mark_operator_queue_expired()
    skill_rows = [r for r in rows(seam) if r["type"] == "skill_not_found"]
    assert {r["status"] for r in skill_rows} == {"expired"}

    (must_act,) = _gitignore_rows(seam)
    assert must_act["status"] == "pending" and must_act["expires_at"] is None
    (report,) = _reports(seam)
    assert report["status"] == "pending" and report["expires_at"] is None

    sent = len(notifications)
    _missing_skill(seam, "/a-new-command")
    (fresh,) = [r for r in rows(seam, status="pending") if r["type"] == "skill_not_found"]
    assert fresh["context"]["command"] == "/a-new-command"
    assert len(notifications) == sent + 1


# ---------------------------------------------------------------------------
# A person's ending that lands while a reading is in flight (D17)
#
# `observe` checks the snooze BEFORE the create's lock. Two moments let a
# person's acknowledgement (through the real `ask_service.answer`) slip past
# that check: (a) after `observe` read the pending row and before the locked
# create runs — the locked find then misses; (b) after the locked find and
# before `_touch`'s compare-and-set — the CAS then loses. Either way the create
# falls through to an insert, and the seam must decide the snooze there, under
# the lock, or the alert a person just ended is back at once.
#
# Moment (b) cannot be produced with a second connection in one process on
# SQLite (BEGIN IMMEDIATE holds every other writer until COMMIT), so the
# ending is landed first through `ask_service.answer` and both finds are handed
# the row as it stood just before it — the snapshot a find taken at that
# instant holds, which is exactly what the CAS then meets.
# ---------------------------------------------------------------------------

def _problem_report_case(seam, monkeypatch):
    _rate(None)
    return lambda: _rate("words typed after the operator looked")


def _inbox_collision_case(seam, monkeypatch):
    """`portal_inbox_collision` as registered on dev (a must-act kind, no
    material keys), read through `observe` directly."""
    from services import platform_alerts as pa

    def read():
        return pa.observe(AGENT, "portal_inbox_collision", "legacy_inbox",
                          title="Two clients share one inbox folder",
                          question="q", priority="high")
    read()
    return read


def _flood_refresh_case(seam, monkeypatch):
    """A hook kind read through `observe` in the R9 refresh shape: the flood
    episode persists and is re-read 6 hours on."""
    from services import operator_queue_service as oqs
    svc = oqs.OperatorQueueSyncService()
    asyncio.run(svc._maybe_emit_flood_alert(AGENT, held=2))

    def read():
        seam.clock.advance(hours=7)
        asyncio.run(svc._maybe_emit_flood_alert(AGENT, held=2))
    return read


_CASES = {"problem_report": _problem_report_case,
          "inbox_collision": _inbox_collision_case,
          "flood_refresh": _flood_refresh_case}


def _end_before_the_locked_create(seam, monkeypatch, subject):
    """(a): the ending lands between `observe`'s read and the locked create."""
    real = seam.db.create_platform_operator_queue_item
    done = []

    def create(agent, item, **kw):
        if kw.get("subject") == subject and not done:
            done.append(person_ends(seam, seam.db.find_pending_operator_queue_by_subject(agent, subject)))
        return real(agent, item, **kw)

    monkeypatch.setattr(seam.db, "create_platform_operator_queue_item", create)
    return done


def _end_between_the_find_and_the_cas(seam, monkeypatch, subject):
    """(b): the ending lands after the locked find and before `_touch`'s CAS."""
    from db.engine import get_engine

    ops = seam.db._operator_queue_ops
    real_find = ops._find_pending_by_subject
    with get_engine().connect() as conn:
        before = real_find(conn, AGENT, subject)
    done = [person_ends(seam, seam.db.find_pending_operator_queue_by_subject(AGENT, subject))]
    monkeypatch.setattr(ops, "_find_pending_by_subject",
                        lambda conn, agent, subj: before if subj == subject
                        else real_find(conn, agent, subj))
    return done


@pytest.mark.parametrize("case", sorted(_CASES))
@pytest.mark.parametrize("moment", ["before_the_locked_create", "between_the_find_and_the_cas"])
def test_a_person_ending_that_lands_mid_reading_snoozes_it(seam, monkeypatch, case, moment):
    from services import platform_alerts as pa

    outcomes = []
    real_observe = pa.observe

    def recording(*a, **k):
        out = real_observe(*a, **k)
        outcomes.append(out)
        return out

    monkeypatch.setattr(pa, "observe", recording)
    read = _CASES[case](seam, monkeypatch)
    (row,) = rows(seam)
    subject = row["subject"]
    inject = {"before_the_locked_create": _end_before_the_locked_create,
              "between_the_find_and_the_cas": _end_between_the_find_and_the_cas}[moment]
    ended = inject(seam, monkeypatch, subject)

    read()

    assert len(ended) == 1                                  # the ending did land mid-reading
    assert outcomes[-1] == pa.OBSERVED_SNOOZED
    (after,) = rows(seam)                                   # no new row
    assert after["id"] == row["id"]
    assert (after["status"], after["disposed_by"]) == ("acknowledged", "person")
    assert after["question"] == row["question"]             # and nothing rewrote it
