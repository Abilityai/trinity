"""The frozen rating matrix of `turn_actor` (#4281, plan §7.1).

#4281 moves the schedule half of `turn_actor` into `schedule_run_tier`, which
the schedule reads, the turn-tier answer and the later slices share. The rule
itself must not move (the ent#926 ruling, 2026-10-19, and #4281 A0 for a
schedule that cannot be read). This file states the full input matrix once and
pins, for every row and for both settings of the owner's `visitor_shell` switch:

* the whole `turn_actor` triple, with NO person id on an owner row;
* `shell_allowed`, `restricts_standing_work` and `turn_person`.

It is a characterization test: it passed against the unmodified
`turn_tier_service.py` at the base of the slice, and stays green after it. What
makes it a guard is that every lookup comes from one table of users and one
ownership map (`_turn_tier_world.World`), so a refactor that reads the tier
through a different lookup, leaks the creator on an owner row, or swaps a share
turn's kind changes a row here.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_UNIT = Path(__file__).resolve().parent
_BACKEND = _UNIT.parents[1] / "src" / "backend"
for _p in (str(_BACKEND), str(_UNIT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _turn_tier_world import (  # noqa: E402
    ADMIN_ID, AGENT, BOOM, DANGLING_ID, FORMER_OWNER_ID, MATE_ID, ORPHAN_AGENT, OTHER_AGENT,
    OWNER_ID, World,
)

pytestmark = pytest.mark.unit

OWNER_ROW = ("owner", None, None)
VS = "follows visitor_shell"


def _run(agent=AGENT, *, uid=None, sid=None, trigger="schedule", channel=None, email=None):
    return SimpleNamespace(id="e1", agent_name=agent, source_user_id=uid, schedule_id=sid,
                           triggered_by=trigger, source_channel=channel, source_user_email=email)


def _portal(email, trigger="public"):
    return _run(trigger=trigger, channel="portal", email=email)


# (row id, the execution, the turn_actor triple, shell, turn_person)
MATRIX = [
    # -- the turn carries a platform user: that user decides ----------------------------
    ("user/owner", _run(uid=OWNER_ID, trigger="user"), OWNER_ROW, True, OWNER_ID),
    ("user/admin", _run(uid=ADMIN_ID, trigger="user"), OWNER_ROW, True, ADMIN_ID),
    ("user/share", _run(uid=MATE_ID, trigger="user"), ("share", "person", MATE_ID), False, MATE_ID),
    ("user/dangling", _run(uid=DANGLING_ID, trigger="user"), ("share", "person", DANGLING_ID), False,
     DANGLING_ID),
    ("user/non-int", _run(uid="x", trigger="user"), ("share", "person", None), False, None),
    ("user/zero", _run(uid=0, trigger="user"), ("share", "person", None), False, None),
    ("user/share-on-owner-schedule", _run(uid=MATE_ID, sid="s-owner"), ("share", "person", MATE_ID),
     False, MATE_ID),
    ("user/owner-on-share-schedule", _run(uid=OWNER_ID, sid="s-mate", trigger="manual"), OWNER_ROW,
     True, OWNER_ID),
    # -- a Workspace turn: the signed-in person's account -------------------------------
    ("portal/owner", _portal("olga@example.com"), OWNER_ROW, True, OWNER_ID),
    ("portal/admin", _portal("ada@example.com"), OWNER_ROW, True, ADMIN_ID),
    ("portal/share", _portal("mate@example.com"), ("share", "person", MATE_ID), False, MATE_ID),
    ("portal/no-account", _portal("nobody@example.com"), ("share", "person", None), False, None),
    ("portal/no-email", _portal(None), ("share", "person", None), False, None),
    ("portal/not-an-email", _portal("olga"), ("share", "person", None), False, None),
    ("public-link/owner-email-typed", _run(trigger="public", email="olga@example.com"),
     ("share", "visitor", None), VS, None),
    # -- no user, no schedule: the trigger label ----------------------------------------
    *[(f"trigger/{t}", _run(trigger=t), OWNER_ROW, True, None)
      for t in ("schedule", "system", "reminder", "loop", "validation", "webhook", "manual", "event")],
    ("trigger/schedule-id-__manual__", _run(sid="__manual__", trigger="reminder"), OWNER_ROW, True, None),
    ("trigger/schedule-id-empty", _run(sid="", trigger="schedule"), OWNER_ROW, True, None),
    ("trigger/__manual__-visitor", _run(sid="__manual__", trigger="slack"), ("share", "visitor", None),
     VS, None),
    *[(f"visitor/{t}", _run(trigger=t), ("share", "visitor", None), VS, None)
      for t in ("slack", "public", "agent", "self_task", "mcp", "")],
    ("visitor/none-trigger", _run(trigger=None), ("share", "visitor", None), VS, None),
    ("visitor/upper-case-owner-label", _run(trigger="SCHEDULE"), OWNER_ROW, True, None),
    # -- a schedule-backed turn: the schedule's creator ---------------------------------
    *[(f"schedule/owner-creator/{t}", _run(sid="s-owner", trigger=t), OWNER_ROW, True, OWNER_ID)
      for t in ("schedule", "webhook", "retry", "validation", "manual")],
    ("schedule/admin-creator", _run(sid="s-admin"), OWNER_ROW, True, ADMIN_ID),
    *[(f"schedule/share-creator/{t}", _run(sid="s-mate", trigger=t), ("share", "person", MATE_ID),
       False, MATE_ID)
      for t in ("schedule", "webhook", "retry", "validation", "manual", "agent")],
    ("schedule/dangling-creator", _run(sid="s-dangling"), ("share", "person", DANGLING_ID), False,
     DANGLING_ID),
    ("schedule/no-recorded-creator", _run(sid="s-none"), OWNER_ROW, True, None),
    # #4281 A0: a schedule that cannot be read is a shared person with no account.
    ("schedule/soft-deleted", _run(sid="s-deleted"), ("share", "person", None), False, None),
    ("schedule/gone", _run(sid="s-never-existed"), ("share", "person", None), False, None),
    ("schedule/read-raises", _run(sid=BOOM, trigger="webhook"), ("share", "person", None), False, None),
    ("schedule/another-agents", _run(sid="s-other-agent"), ("share", "person", None), False, None),
    # -- the agent's owner has no user row (its ownership row names a missing user) -----
    ("orphan/admin-creator", _run(ORPHAN_AGENT, sid="c-admin"), OWNER_ROW, True, ADMIN_ID),
    ("orphan/former-owner-creator", _run(ORPHAN_AGENT, sid="c-former-owner"),
     ("share", "person", FORMER_OWNER_ID), False, FORMER_OWNER_ID),
    ("orphan/other-creator", _run(ORPHAN_AGENT, sid="c-mate"), ("share", "person", MATE_ID), False,
     MATE_ID),
    ("orphan/user-former-owner", _run(ORPHAN_AGENT, uid=FORMER_OWNER_ID, trigger="user"),
     ("share", "person", FORMER_OWNER_ID), False, FORMER_OWNER_ID),
    ("orphan/user-admin", _run(ORPHAN_AGENT, uid=ADMIN_ID, trigger="user"), OWNER_ROW, True, ADMIN_ID),
    # -- not this agent's turn -----------------------------------------------------------
    ("unknown/another-agents-run", _run(OTHER_AGENT, uid=OWNER_ID, trigger="user"),
     ("unknown", None, None), False, OWNER_ID),
]


@pytest.mark.parametrize("visitor_shell", [True, False], ids=["visitor_shell-on", "visitor_shell-off"])
@pytest.mark.parametrize("execution, triple, shell, person",
                         [row[1:] for row in MATRIX], ids=[row[0] for row in MATRIX])
def test_the_rating_matrix_is_frozen(execution, triple, shell, person, visitor_shell):
    from services import turn_tier_service as t
    db = World(executions={"e1": execution}, visitor_shell=visitor_shell)
    expected_shell = visitor_shell if shell == VS else shell
    # The agent asking: the run's own, except on the row that pins another agent's run.
    agent = AGENT if triple[0] == "unknown" else execution.agent_name

    tier, kind, who = t.turn_actor(db, agent, "e1")
    assert (tier, kind, who) == triple
    assert t.turn_tier(db, agent, "e1") == triple[0]
    assert t.turn_actor(db, agent, execution=execution) == triple
    assert t.shell_allowed(db, agent, tier, kind) is expected_shell
    assert t.restricts_standing_work(db, agent, tier, kind) is (tier != "owner" and not expected_shell)
    assert t.turn_person(db, agent, execution) == person


def test_a_missing_execution_is_unknown_and_restricted():
    from services import turn_tier_service as t
    db = World()
    assert t.turn_actor(db, AGENT, "e-missing") == ("unknown", None, None)
    assert t.turn_actor(db, AGENT, None) == ("unknown", None, None)
    assert t.shell_allowed(db, AGENT, "unknown", None) is False
    assert t.restricts_standing_work(db, AGENT, "unknown", None) is True


def test_the_world_is_the_one_answering():
    """The fake is spec'd: a method the rating does not read raises, so a
    refactor that adds a lookup fails here rather than reading a truthy mock."""
    with pytest.raises(AttributeError):
        World().can_user_access_agent("olga", AGENT)
    assert World().can_user_share_agent("olga", AGENT) is True
    assert World().can_user_share_agent("olga", ORPHAN_AGENT) is False
    assert World().get_agent_owner(ORPHAN_AGENT) is None


def test_the_matrix_covers_every_owner_configured_trigger():
    from services import turn_tier_service as t
    pinned = {row[0].split("/", 1)[1] for row in MATRIX if row[0].startswith("trigger/")}
    assert set(t.OWNER_CONFIGURED_TRIGGERS) <= pinned
