"""What tier a schedule's runs get, and why: `schedule_run_tier` (#4281).

One function answers it for every surface: the turn-tier answer the hook
reads, every schedule read, and (later slices) the execution stamp and the
platform condition. `turn_actor` reads the same tier half, so the frozen
matrix in `test_4281_turn_actor_frozen_matrix.py` pins that the rule did not
move; this file pins the cause, the creator id, the memo the reads use, and
the turn-tier answer's new fields.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_UNIT = Path(__file__).resolve().parent
_BACKEND = _UNIT.parents[1] / "src" / "backend"
for _p in (str(_BACKEND), str(_UNIT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _turn_tier_world import (  # noqa: E402
    ADMIN_ID, AGENT, DANGLING_ID, FORMER_OWNER_ID, MATE_ID, ORPHAN_AGENT, OWNER_ID, World, schedule,
)

pytestmark = pytest.mark.unit


def _t():
    from services import turn_tier_service
    return turn_tier_service


# (schedule id, agent, RunTier)
CAUSES = [
    ("s-owner", AGENT, ("owner", None, "creator_owner_tier", OWNER_ID)),
    ("s-admin", AGENT, ("owner", None, "creator_owner_tier", ADMIN_ID)),
    ("s-none", AGENT, ("owner", None, "no_recorded_creator", None)),
    ("s-mate", AGENT, ("share", "person", "creator_not_owner_tier", MATE_ID)),
    ("s-dangling", AGENT, ("share", "person", "creator_missing", DANGLING_ID)),
    ("c-admin", ORPHAN_AGENT, ("owner", None, "creator_owner_tier", ADMIN_ID)),
    ("c-former-owner", ORPHAN_AGENT, ("share", "person", "creator_missing", FORMER_OWNER_ID)),
    ("c-mate", ORPHAN_AGENT, ("share", "person", "agent_has_no_owner", MATE_ID)),
]


@pytest.mark.parametrize("sid, agent, expected", CAUSES, ids=[c[0] for c in CAUSES])
def test_each_schedule_rates_with_its_cause(sid, agent, expected):
    rated = _t().schedule_run_tier(World(), agent, schedule(sid))
    assert tuple(rated) == expected
    assert (rated.tier, rated.kind, rated.cause, rated.creator_id) == expected


@pytest.mark.parametrize("how", ["none", "another-agents"])
def test_a_schedule_that_cannot_be_read_rates_a_shared_person_with_no_creator(how):
    row = None if how == "none" else schedule("s-other-agent")
    assert tuple(_t().schedule_run_tier(World(), AGENT, row)) == ("share", "person", "schedule_unreadable", None)


@pytest.mark.parametrize("sid, agent, expected", CAUSES, ids=[c[0] for c in CAUSES])
def test_turn_actor_is_the_projection_of_the_run_tier(sid, agent, expected):
    """The schedule branch of `turn_actor` IS the tier half: the same tier and
    kind, and the creator as the person only on a share row (never on owner)."""
    t = _t()
    run = SimpleNamespace(agent_name=agent, source_user_id=None, schedule_id=sid, triggered_by="schedule")
    tier, kind, _, creator = expected
    assert t.turn_actor(World(), agent, execution=run) == (tier, kind, creator if tier == "share" else None)


def test_a_cause_lookup_that_fails_is_unknown_and_the_tier_stands():
    """The tier half reads the owner once; the cause's own read then fails."""
    db = World(fail_after={"get_agent_owner": 1})
    rated = _t().schedule_run_tier(db, AGENT, schedule("s-mate"))
    assert tuple(rated) == ("share", "person", "unknown", MATE_ID)


def test_a_tier_lookup_that_fails_raises_as_turn_actor_always_has():
    """Not swallowed: a read surface turns it into `runs_as: null`, never into
    a guessed tier."""
    with pytest.raises(RuntimeError):
        _t().schedule_run_tier(World(fail_after={"get_agent_owner": 0}), AGENT, schedule("s-mate"))


def test_the_cause_is_never_on_the_fence_path(monkeypatch):
    """The fences run `turn_actor` on every agent-key request; the cause's extra
    lookups must not be a new way for that path to raise (review X2)."""
    t = _t()

    def boom(*a, **k):
        raise AssertionError("schedule_run_cause called on the fence path")

    monkeypatch.setattr(t, "schedule_run_cause", boom)
    runs = [SimpleNamespace(agent_name=AGENT, source_user_id=None, schedule_id=sid, triggered_by="schedule")
            for sid in ("s-owner", "s-mate", "s-dangling", "s-deleted")]
    db = World()
    db.get_running_turn_attribution = lambda agent: runs
    assert [t.turn_actor(db, AGENT, execution=r) for r in runs] == [
        ("owner", None, None), ("share", "person", MATE_ID), ("share", "person", DANGLING_ID),
        ("share", "person", None)]
    assert t.running_turn_restricts(db, AGENT) is True
    assert t.running_share_people(db, AGENT) == {MATE_ID, DANGLING_ID, None}


@pytest.mark.parametrize("cause, remedy", [
    ("creator_not_owner_tier", "recreate"), ("creator_missing", "recreate"),
    ("agent_has_no_owner", "repair_agent_owner"), ("schedule_unreadable", None),
    ("creator_owner_tier", None), ("no_recorded_creator", None), ("unknown", None), (None, None),
])
def test_the_remedy_per_cause(cause, remedy):
    assert _t().schedule_remedy(cause) == remedy


# ---- the memo the reads use --------------------------------------------------------------

def test_the_memo_rates_exactly_as_the_database_does():
    t = _t()
    memo = t.RunTierLookups(World())
    for sid, agent, expected in CAUSES:
        assert tuple(t.schedule_run_tier(memo, agent, schedule(sid))) == expected


def test_the_memo_reads_each_creator_and_each_owner_once():
    t = _t()
    db = World()
    memo = t.RunTierLookups(db)
    for _ in range(10):
        for sid in ("s-owner", "s-mate", "s-dangling"):
            t.schedule_run_tier(memo, AGENT, schedule(sid))
    assert db.calls["get_user_by_id"] == 3
    assert db.calls["get_agent_owner"] == 1
    assert db.calls["get_user_by_username"] == 0   # learnt from the id reads


class _BulkWorld(World):
    """The two bulk reads, answered from the same tables (uncounted lookups)."""

    def get_agent_owners(self, agent_names):
        self._count("get_agent_owners")
        answers = {a: World().get_agent_owner(a) for a in agent_names}
        return {a: o for a, o in answers.items() if o is not None}

    def get_users_by_ids(self, ids):
        self._count("get_users_by_ids")
        answers = {i: World().get_user_by_id(i) for i in ids}
        return {i: u for i, u in answers.items() if u is not None}


def test_a_preloaded_memo_answers_without_another_query():
    t = _t()
    db = _BulkWorld()
    memo = t.RunTierLookups(db)
    memo.preload([AGENT, ORPHAN_AGENT], [s for s in (OWNER_ID, ADMIN_ID, MATE_ID, DANGLING_ID,
                                                     FORMER_OWNER_ID, None)])
    before = dict(db.calls)
    for sid, agent, expected in CAUSES:
        assert tuple(t.schedule_run_tier(memo, agent, schedule(sid))) == expected
    assert dict(db.calls) == before == {"get_agent_owners": 1, "get_users_by_ids": 1}


# ---- the turn-tier answer -----------------------------------------------------------------

def _answer(db, agent=AGENT):
    from routers import agents
    db.validate_mcp_api_key = lambda key, track_usage=False: {"scope": "agent", "agent_name": agent}
    request = SimpleNamespace(headers={"Authorization": "Bearer trinity_mcp_k"})
    with patch.object(agents, "db", db):
        return asyncio.run(agents.agent_execution_turn_tier(agent, "e1", request))


def _schedule_run(sid, agent=AGENT):
    return SimpleNamespace(id="e1", agent_name=agent, source_user_id=None, schedule_id=sid,
                           triggered_by="schedule")


@pytest.mark.parametrize("sid, agent, cause, creator, remedy", [
    ("s-mate", AGENT, "creator_not_owner_tier", "Mate Shared", "recreate"),
    ("s-dangling", AGENT, "creator_missing", None, "recreate"),
    ("c-mate", ORPHAN_AGENT, "agent_has_no_owner", "Mate Shared", "repair_agent_owner"),
    ("s-deleted", AGENT, "schedule_unreadable", None, None),
])
def test_a_schedule_backed_shared_turn_names_its_cause(sid, agent, cause, creator, remedy):
    answer = _answer(World(executions={"e1": _schedule_run(sid, agent)}), agent)
    assert answer == {"tier": "share", "kind": "person", "shell": False, "cause": cause,
                      "schedule": None if cause == "schedule_unreadable" else f"brief {sid}",
                      "creator": creator, "remedy": remedy}


@pytest.mark.parametrize("run", [
    _schedule_run("s-owner"),
    SimpleNamespace(id="e1", agent_name=AGENT, source_user_id=MATE_ID, schedule_id="s-mate",
                    triggered_by="schedule"),
    SimpleNamespace(id="e1", agent_name=AGENT, source_user_id=None, schedule_id=None, triggered_by="slack"),
], ids=["owner-schedule", "shared-person-with-a-user-id", "channel-visitor"])
def test_every_other_turn_answers_null_schedule_fields(run):
    answer = _answer(World(executions={"e1": run}))
    assert {k: answer[k] for k in ("cause", "schedule", "creator", "remedy")} == dict.fromkeys(
        ("cause", "schedule", "creator", "remedy"))


def test_the_answer_never_carries_a_full_email():
    """D5/R14: the answer is readable by anything holding the agent's key."""
    class _EmailOnly(World):
        def get_user_by_id(self, user_id):
            user = World.get_user_by_id(self, user_id)
            return user and {**user, "name": "mate.person@example.com"}
    answer = _answer(_EmailOnly(executions={"e1": _schedule_run("s-mate")}))
    assert answer["creator"] == "mate.person"
    assert "@" not in json.dumps(answer)


def test_a_failing_cause_lookup_still_answers():
    answer = _answer(World(executions={"e1": _schedule_run("s-mate")}, fail_after={"get_agent_owner": 1}))
    assert (answer["tier"], answer["shell"], answer["cause"], answer["remedy"]) == ("share", False, "unknown",
                                                                                   None)


# Characters JSON escapes as \\uXXXX under ensure_ascii, the widest form: an
# astral character becomes a 12-byte surrogate pair.
_WIDE = "\U0001F600"


def _worst_case_world():
    class _Wide(World):
        def get_schedule(self, schedule_id):
            row = World.get_schedule(self, schedule_id)
            row.name = "\u202e\x00\n" + _WIDE * 500
            return row

        def get_user_by_id(self, user_id):
            user = World.get_user_by_id(self, user_id)
            return user and {**user, "name": ("\\\"" + _WIDE) * 500}
    return _Wide(executions={"e1": _schedule_run("c-mate", ORPHAN_AGENT)})


def test_the_worst_case_answer_fits_the_hooks_budget():
    """The hook reads at most 4096 bytes and denies on an unparseable answer;
    the answer stays within 2048 bytes in any encoding (review X10)."""
    from fastapi.responses import JSONResponse
    answer = _answer(_worst_case_world(), ORPHAN_AGENT)
    assert len(answer["schedule"]) == 80 and len(answer["creator"]) == 64
    assert answer["remedy"] == "repair_agent_owner"   # the longest remedy code
    wire = JSONResponse(answer).body
    escaped = json.dumps(answer, ensure_ascii=True).encode()
    assert len(wire) <= 2048 and len(escaped) <= 2048, (len(wire), len(escaped))
    assert json.loads(wire) == answer


def test_labels_drop_control_and_format_characters():
    t = _t()
    assert t.bounded_label("Daily\n\tbrief‮​\x00 now", 80) == "Daily brief now"
    assert t.bounded_label("\x00\n", 80) is None
    assert t.bounded_label(None, 80) is None
    assert t.bounded_label("x" * 500, 80) == "x" * 80


@pytest.mark.parametrize("user, for_turn, label", [
    ({"name": "Mate Shared", "username": "mate@example.com"}, True, "Mate Shared"),
    ({"name": "", "username": "mate@example.com"}, True, "mate"),
    ({"name": "", "username": "mate@example.com"}, False, "mate@example.com"),
    ({"name": None, "username": "olga"}, True, "olga"),
    ({"name": "\x00", "username": ""}, True, None),
    (None, True, None),
])
def test_how_a_creator_is_named(user, for_turn, label):
    assert _t().creator_label(user, for_turn=for_turn) == label
