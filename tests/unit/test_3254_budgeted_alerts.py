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
    monkeypatch.setattr(helpers, "utc_now_iso", c.iso)
    monkeypatch.setattr(dbq, "utc_now_iso", c.iso)
    monkeypatch.setattr(portal, "utc_now_iso", c.iso)
    monkeypatch.setattr(oqs.time, "monotonic", lambda: c.mono)
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
    assert row["status"] == "pending"
    # an event kind (no subject) counts no readings
    assert row["context"].get("seen_count") == (1 if row["subject"] else None)


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
