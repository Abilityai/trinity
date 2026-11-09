"""A delegated turn runs at the callee's own tier, and a person a private module
rates as the owner of an agent created for them runs at owner tier
(abilityai/trinity-enterprise#1148).

`services/turn_tier_service.turn_actor` is the one chokepoint every symptom
reads: the guard hook's shell answer, the Gemini/Codex spawn gate, the
standing-work fence (`set_reminder` refused "as the owner"), the read narrowing
and the dispatch stamping. Two rules are added there:

  rule A  a row the caller AGENT dispatched (`source_agent_name` set) for a
          person P who cannot open the callee at all rates OWNER — bounded by
          the clamp: only when the calling agent's owner is owner/admin of the
          callee (ruling T1, 2026-11-07). P who CAN open the callee keeps P's
          tier (AC2). Every lookup fails closed to SHARE.
  rule B  a registered person-tier provider (an optional private module) may
          rate a person the owner of one agent; the OSS answer without a
          provider is unchanged.

The new OWNER answers carry a `kind` (`member` / `delegated`) and the person,
so a turn they dispatch carries that person forward (C1) and rule A re-applies
at the next hop. Related: ent#926 (`test_ent926_turn_tier.py`), ent#875.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services import turn_tier_service as tts  # noqa: E402

pytestmark = pytest.mark.unit

CALLER = "agent-a"      # the agent P chats with (P is merely shared it)
CALLEE = "agent-b"      # the agent CALLER delegates to
THIRD = "agent-c"       # a second hop
HUB_OWNER, OTHER_OWNER, ADMIN, MATE = 1, 2, 3, 9
USERS = {
    HUB_OWNER: {"id": HUB_OWNER, "username": "hub", "role": "user", "email": "hub@example.com"},
    OTHER_OWNER: {"id": OTHER_OWNER, "username": "other", "role": "user", "email": "other@example.com"},
    ADMIN: {"id": ADMIN, "username": "admin", "role": "admin", "email": "admin@example.com"},
    MATE: {"id": MATE, "username": "mate", "role": "user", "email": "mate@example.com"},
}
BY_EMAIL = {u["email"]: u for u in USERS.values()}
BY_NAME = {u["username"]: u for u in USERS.values()}


class _Db:
    """A fake platform db: who owns what, who is shared what, which turns run."""

    def __init__(self, *, owners=None, shares=frozenset()):
        self.owners = owners if owners is not None else {CALLER: HUB_OWNER, CALLEE: HUB_OWNER, THIRD: HUB_OWNER}
        self.shares = set(shares) or {("mate", CALLER)}
        self.executions = {}
        self.schedules = {}
        self.running = []
        self.access_calls = []

    def get_user_by_id(self, uid):
        return USERS.get(int(uid))

    def get_user_by_email(self, email):
        return BY_EMAIL.get(email)

    def get_execution(self, run):
        return self.executions.get(run)

    def get_schedule(self, sid):
        return self.schedules.get(sid)

    def get_guardrails_config(self, agent):
        return {}

    def get_running_turn_attribution(self, agent):
        return [r for r in self.running if r["agent_name"] == agent]

    def get_agent_owner(self, agent):
        oid = self.owners.get(agent)
        return None if oid is None else {"owner_id": oid, "owner_username": USERS[oid]["username"]}

    def can_user_share_agent(self, username, agent):
        user = BY_NAME.get(username)
        return bool(user) and (user["role"] == "admin" or self.owners.get(agent) == user["id"])

    def can_user_access_agent(self, username, agent):
        self.access_calls.append((username, agent))
        return self.can_user_share_agent(username, agent) or (username, agent) in self.shares


def _row(agent, *, uid=MATE, caller=None, trigger="agent", rid="e1", channel=None, email=None,
         schedule_id="__manual__"):
    return {"id": rid, "agent_name": agent, "source_user_id": uid, "triggered_by": trigger,
            "schedule_id": schedule_id, "source_channel": channel, "source_user_email": email,
            "source_agent_name": caller}


@pytest.fixture(autouse=True)
def _no_provider():
    tts.clear_person_tier_providers()
    yield
    tts.clear_person_tier_providers()


# ---- rule A: a delegated turn runs at the callee's tier --------------------------------

class TestRuleADelegation:
    def test_delegated_turn_runs_at_callee_tier(self):
        """The hub: P is shared CALLER only; CALLER and CALLEE share an owner."""
        db = _Db()
        assert tts.turn_actor(db, CALLEE, execution=_row(CALLEE, caller=CALLER)) == ("owner", "delegated", MATE)

    def test_delegation_to_openable_agent_keeps_person_tier(self):
        """AC2: P can open the callee -> P's own tier there, as today."""
        db = _Db(shares={("mate", CALLER), ("mate", CALLEE)})
        assert tts.turn_actor(db, CALLEE, execution=_row(CALLEE, caller=CALLER)) == ("share", "person", MATE)

    def test_direct_turn_of_non_accessor_is_share(self):
        """No calling agent on the row: rule A has nothing to say (defensive)."""
        db = _Db()
        assert tts.turn_actor(db, CALLEE, execution=_row(CALLEE, caller=None, trigger="user")) == ("share", "person", MATE)
        assert db.access_calls == []

    @pytest.mark.parametrize("caller", ["", "   ", 0, 7, object()])
    def test_a_caller_that_is_not_an_agent_name_does_not_fire(self, caller):
        db = _Db()
        assert tts.turn_actor(db, CALLEE, execution=_row(CALLEE, caller=caller)) == ("share", "person", MATE)

    def test_cross_owner_delegation_stays_person_tier(self):
        """The clamp (ruling T1): CALLER's owner is not owner/admin of CALLEE, so a
        share holder on CALLER gets no more on CALLEE than CALLER's owner has."""
        db = _Db(owners={CALLER: OTHER_OWNER, CALLEE: HUB_OWNER})
        assert tts.turn_actor(db, CALLEE, execution=_row(CALLEE, caller=CALLER)) == ("share", "person", MATE)

    def test_a_caller_owned_by_an_admin_passes_the_clamp(self):
        db = _Db(owners={CALLER: ADMIN, CALLEE: HUB_OWNER})
        assert tts.turn_actor(db, CALLEE, execution=_row(CALLEE, caller=CALLER)) == ("owner", "delegated", MATE)

    def test_unreadable_access_lookup_stays_share(self):
        db = _Db()

        def boom(username, agent):
            raise RuntimeError("db down")
        db.can_user_access_agent = boom
        assert tts.turn_actor(db, CALLEE, execution=_row(CALLEE, caller=CALLER)) == ("share", "person", MATE)

    def test_an_access_answer_that_is_not_false_does_not_fire(self):
        """`is False`, never `not x`: None (unknown) must not elevate."""
        db = _Db()
        db.can_user_access_agent = lambda u, a: None
        assert tts.turn_actor(db, CALLEE, execution=_row(CALLEE, caller=CALLER)) == ("share", "person", MATE)

    def test_an_unreadable_or_missing_caller_owner_stays_share(self):
        db = _Db()

        def boom(agent):
            raise RuntimeError("db down")
        db.get_agent_owner = boom
        assert tts.turn_actor(db, CALLEE, execution=_row(CALLEE, caller=CALLER)) == ("share", "person", MATE)
        db = _Db(owners={CALLEE: HUB_OWNER})          # the caller is gone (deleted, renamed)
        assert tts.turn_actor(db, CALLEE, execution=_row(CALLEE, caller=CALLER)) == ("share", "person", MATE)

    def test_a_self_task_in_a_share_turn_stays_the_persons(self):
        """P can open the agent they chat with; its self-task is still P's turn."""
        db = _Db()
        assert tts.turn_actor(db, CALLER, execution=_row(CALLER, caller=CALLER, trigger="self_task")) == ("share", "person", MATE)

    def test_a_visitors_delegation_stays_a_visitor(self):
        """No person on the row: the callee's `visitor_shell` governs, as before."""
        db = _Db()
        assert tts.turn_actor(db, CALLEE, execution=_row(CALLEE, uid=None, caller=CALLER)) == ("share", "visitor", None)
        assert db.access_calls == []

    def test_the_owner_delegating_is_still_the_owner(self):
        db = _Db()
        assert tts.turn_actor(db, CALLEE, execution=_row(CALLEE, uid=HUB_OWNER, caller=CALLER)) == ("owner", None, None)

    def test_a_person_with_no_account_stays_share(self):
        db = _Db()
        assert tts.turn_actor(db, CALLEE, execution=_row(CALLEE, uid=404, caller=CALLER)) == ("share", "person", 404)


# ---- rule B: a registered provider rates a person the owner of one agent ---------------

class TestRuleBProvider:
    def test_no_provider_is_oss_behaviour(self):
        db = _Db()
        assert tts.turn_actor(db, CALLER, execution=_row(CALLER, caller=None, trigger="user")) == ("share", "person", MATE)

    def test_member_turn_is_owner_tier_direct(self):
        tts.register_person_tier_provider(lambda db, uid, agent: uid == MATE and agent == CALLER)
        db = _Db()
        assert tts.turn_actor(db, CALLER, execution=_row(CALLER, caller=None, trigger="user")) == ("owner", "member", MATE)

    def test_member_turn_is_owner_tier_workspace(self):
        tts.register_person_tier_provider(lambda db, uid, agent: uid == MATE and agent == CALLER)
        db = _Db()
        row = _row(CALLER, uid=None, trigger="public", channel=tts.PORTAL_SOURCE_CHANNEL, email="mate@example.com")
        assert tts.turn_actor(db, CALLER, execution=row) == ("owner", "member", MATE)

    def test_member_schedule_runs_owner_tier(self):
        tts.register_person_tier_provider(lambda db, uid, agent: uid == MATE and agent == CALLER)
        db = _Db()
        db.schedules["s1"] = SimpleNamespace(agent_name=CALLER, owner_id=MATE)
        row = _row(CALLER, uid=None, trigger="schedule", schedule_id="s1")
        assert tts.turn_actor(db, CALLER, execution=row) == ("owner", "member", MATE)

    def test_member_elsewhere_stays_share(self):
        """The member's reach on any other agent is unchanged."""
        tts.register_person_tier_provider(lambda db, uid, agent: uid == MATE and agent == CALLER)
        db = _Db(shares={("mate", CALLER), ("mate", CALLEE)})
        assert tts.turn_actor(db, CALLEE, execution=_row(CALLEE, caller=None, trigger="user")) == ("share", "person", MATE)

    def test_a_true_owner_is_rated_before_the_provider_is_asked(self):
        asked = []

        def provider(db, uid, agent):
            asked.append(uid)
            return True
        tts.register_person_tier_provider(provider)
        db = _Db()
        assert tts.turn_actor(db, CALLER, execution=_row(CALLER, uid=HUB_OWNER, caller=None, trigger="user")) == ("owner", None, None)
        assert asked == []

    def test_provider_error_is_not_a_member(self):
        def boom(db, uid, agent):
            raise RuntimeError("table unreadable")
        tts.register_person_tier_provider(boom)
        db = _Db()
        assert tts.turn_actor(db, CALLER, execution=_row(CALLER, caller=None, trigger="user")) == ("share", "person", MATE)

    @pytest.mark.parametrize("answer", [1, "yes", MagicMock(), None, object()])
    def test_an_answer_that_is_not_true_is_not_a_member(self, answer):
        tts.register_person_tier_provider(lambda db, uid, agent: answer)
        db = _Db()
        assert tts.turn_actor(db, CALLER, execution=_row(CALLER, caller=None, trigger="user")) == ("share", "person", MATE)

    def test_the_provider_is_asked_with_the_db_the_person_and_the_agent(self):
        asked = []

        def provider(db, uid, agent):
            asked.append((db, uid, agent))
            return False
        tts.register_person_tier_provider(provider)
        db = _Db()
        tts.turn_actor(db, CALLER, execution=_row(CALLER, caller=None, trigger="user"))
        assert asked == [(db, MATE, CALLER)]

    def test_the_last_registered_provider_wins_and_clear_removes_it(self):
        tts.register_person_tier_provider(lambda db, uid, agent: True)
        tts.register_person_tier_provider(lambda db, uid, agent: False)
        db = _Db()
        assert tts.turn_actor(db, CALLER, execution=_row(CALLER, caller=None, trigger="user")) == ("share", "person", MATE)
        tts.register_person_tier_provider(lambda db, uid, agent: True)
        assert tts.turn_actor(db, CALLER, execution=_row(CALLER, caller=None, trigger="user")) == ("owner", "member", MATE)
        tts.clear_person_tier_providers()
        assert tts.turn_actor(db, CALLER, execution=_row(CALLER, caller=None, trigger="user")) == ("share", "person", MATE)

    def test_a_member_who_is_also_delegated_to_is_rated_member_first(self):
        tts.register_person_tier_provider(lambda db, uid, agent: uid == MATE and agent == CALLEE)
        db = _Db()
        assert tts.turn_actor(db, CALLEE, execution=_row(CALLEE, caller=CALLER)) == ("owner", "member", MATE)
        assert db.access_calls == []


# ---- the new kinds are full owner tier: shell and standing work (ruling T2) ------------

@pytest.mark.parametrize("kind", [tts.MEMBER, tts.DELEGATED])
def test_member_and_delegated_turns_get_shell_and_standing_work(kind):
    db = _Db()
    assert tts.shell_allowed(db, CALLEE, tts.OWNER, kind) is True
    assert tts.restricts_standing_work(db, CALLEE, tts.OWNER, kind) is False


def test_the_kind_constants_are_what_the_consumers_whitelist_against():
    assert (tts.MEMBER, tts.DELEGATED) == ("member", "delegated")
    assert len({tts.PERSON, tts.VISITOR, tts.MEMBER, tts.DELEGATED}) == 4


# ---- C1: a turn dispatched from an OWNER-by-rule turn carries the person ----------------

def _key(db, agent, run, owner=HUB_OWNER):
    return SimpleNamespace(id=owner, username=USERS[owner]["username"], role="user", mcp_scope="agent",
                           agent_name=agent, _acting_execution_id=run)


class TestDispatchCarriesThePerson:
    def test_dispatch_from_member_turn_carries_member(self):
        tts.register_person_tier_provider(lambda db, uid, agent: uid == MATE and agent == CALLER)
        db = _Db()
        db.executions["e1"] = _row(CALLER, caller=None, trigger="user")
        key = _key(db, CALLER, "e1")
        assert tts.turn_origin_user_id(db, key) == MATE
        assert tts.turn_origin_email(db, key, "hub@example.com") == "mate@example.com"

    def test_dispatch_from_delegated_turn_carries_person(self):
        db = _Db()
        db.executions["e1"] = _row(CALLEE, caller=CALLER)
        key = _key(db, CALLEE, "e1")
        assert tts.turn_actor(db, CALLEE, "e1") == ("owner", "delegated", MATE)
        assert tts.turn_origin_user_id(db, key) == MATE
        assert tts.turn_origin_email(db, key, "hub@example.com") == "mate@example.com"

    def test_dispatch_from_owner_turn_still_owner(self):
        db = _Db()
        db.executions["e1"] = _row(CALLER, uid=HUB_OWNER, caller=None, trigger="user")
        key = _key(db, CALLER, "e1")
        assert tts.turn_origin_user_id(db, key) == HUB_OWNER
        assert tts.turn_origin_email(db, key, "hub@example.com") == "hub@example.com"

    def test_dispatch_from_visitor_turn_still_none(self):
        db = _Db()
        db.executions["e1"] = _row(CALLER, uid=None, caller=None, trigger="slack")
        key = _key(db, CALLER, "e1")
        assert tts.turn_origin_user_id(db, key) is None
        assert tts.turn_origin_email(db, key, "hub@example.com") is None

    def test_dispatch_from_share_turn_carries_person_as_before(self):
        db = _Db()
        db.executions["e1"] = _row(CALLER, caller=None, trigger="user")
        key = _key(db, CALLER, "e1")
        assert tts.turn_origin_user_id(db, key) == MATE

    def test_the_chain_re_rates_at_every_hop(self):
        """CALLEE runs a delegated OWNER turn for P and calls THIRD: the row on THIRD
        carries P and `source_agent_name=CALLEE`; rule A (with the clamp) decides again."""
        db = _Db()
        db.executions["e-b"] = _row(CALLEE, caller=CALLER, rid="e-b")
        person = tts.turn_origin_user_id(db, _key(db, CALLEE, "e-b"))
        assert person == MATE
        hop = _row(THIRD, uid=person, caller=CALLEE, rid="e-c")
        assert tts.turn_actor(db, THIRD, execution=hop) == ("owner", "delegated", MATE)
        # P can open THIRD: P's own tier there (AC2), even though the caller ran as the owner.
        db_open = _Db(shares={("mate", CALLER), ("mate", THIRD)})
        assert tts.turn_actor(db_open, THIRD, execution=hop) == ("share", "person", MATE)
        # THIRD is someone else's: the clamp holds P's turn at share.
        db_other = _Db(owners={CALLER: HUB_OWNER, CALLEE: HUB_OWNER, THIRD: OTHER_OWNER})
        assert tts.turn_actor(db_other, THIRD, execution=hop) == ("share", "person", MATE)


# ---- D: the ledger side rates the delegated row the same way ---------------------------

class TestLedgerRatesDelegatedRows:
    def test_ledger_rates_delegated_row_owner(self):
        """The callee's own concurrent key calls meet the ledger: a delegated OWNER
        turn fences nothing and narrows nothing."""
        db = _Db()
        db.running.append(_row(CALLEE, caller=CALLER, rid="e-del"))
        assert tts.running_turn_restricts(db, CALLEE) is False
        assert tts.running_share_people(db, CALLEE) == set()

    def test_a_ledger_row_without_the_caller_column_rates_as_before(self):
        """An older reader (or mock) that omits `source_agent_name`: rule A is silent."""
        db = _Db()
        row = _row(CALLEE, caller=None, trigger="agent", rid="e-old")
        del row["source_agent_name"]
        db.running.append(row)
        assert tts.running_turn_restricts(db, CALLEE) is True
        assert tts.running_share_people(db, CALLEE) == {MATE}

    def test_a_cross_owner_delegated_row_still_fences(self):
        db = _Db(owners={CALLER: OTHER_OWNER, CALLEE: HUB_OWNER})
        db.running.append(_row(CALLEE, caller=CALLER, rid="e-del"))
        assert tts.running_turn_restricts(db, CALLEE) is True
        assert tts.running_share_people(db, CALLEE) == {MATE}

    def test_the_ledger_readers_rate_each_distinct_person_once(self):
        """Decision 17: a busy callee pays the new lookups once per distinct
        attribution per read, not once per running row."""
        db = _Db(shares={("mate", CALLER), ("other", CALLEE)})
        for i in range(5):
            db.running.append(_row(CALLEE, caller=CALLER, rid=f"e-{i}"))
        db.running.append(_row(CALLEE, uid=OTHER_OWNER, caller=CALLER, rid="e-x"))
        assert tts.running_turn_restricts(db, CALLEE) is True        # `other` can open CALLEE: a share turn
        assert len(db.access_calls) == 2                              # mate once, other once
        db.access_calls.clear()
        assert tts.running_share_people(db, CALLEE) == {OTHER_OWNER}
        assert len(db.access_calls) == 2

    def test_a_member_row_in_the_ledger_is_owner_tier(self):
        tts.register_person_tier_provider(lambda db, uid, agent: uid == MATE and agent == CALLER)
        db = _Db()
        db.running.append(_row(CALLER, caller=None, trigger="user", rid="e-m"))
        assert tts.running_turn_restricts(db, CALLER) is False
        assert tts.running_share_people(db, CALLER) == set()
