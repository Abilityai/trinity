"""trinity-enterprise#641 — the autonomy dial (P12).

A companion starts on-request for every kind of ask and graduates, class by
class, only when the evidence says so. Two scopes meet here: the instance level
(canon §2.3, L0–L3, a CEILING) and the per-(seat, ask class) state earned from
the decision record (§2.2 + R25 — the same criterion applied repeatedly with no
reversals, AND a clean rating history; never frequency).

The shape under test is **stored earned-state × three live conjuncts**. Written
instead of read, each conjunct is a silent failure, and the tests below are
organised around exactly that:

1. the rule table — each conjunct alone flips the verdict, and every refusal is
   NAMED (a state nobody can explain is a state nobody can trust);
2. the live conjuncts — a level drop, the hard off and an expiry demote WITHOUT
   any event firing, and restore what was earned when they go back;
3. the ratings window — the three shapes that were each a defect first: the
   `operator:` prefix, `COALESCE(updated_at, created_at)`, and a FIXED span;
4. reversal counting over the WINDOW, not all history (the one-way-ratchet
   that #638's `stats` would otherwise impose);
5. the gates — hold is a refusal anyone may make, release is the owner's grant,
   and a seat never reads another seat's classes.

Behavioural tests run the real service over a real SQLite file; the DDL /
revision / registry presence is pinned by text (their consumer is the runner).
"""
from __future__ import annotations

import inspect
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
AGENT = "sales-companion"
SEAT = "gary@example.com"
OTHER = "ops@example.com"
OWNER = "owner@example.com"
TODAY = date(2026, 9, 23)
FUTURE = (TODAY + timedelta(days=90)).isoformat()
CRITERION = "renewal cost below the switching cost"


# --------------------------------------------------------------------------- #
# Fixtures — the real service over a real store
# --------------------------------------------------------------------------- #

@pytest.fixture
def store(tmp_path, monkeypatch):
    """A real SQLite file behind both mixins, plus the settings + autonomy +
    ratings reads the dial needs, so the rule runs end to end."""
    from sqlalchemy import create_engine
    import db.engine as engine_module
    import db.tables as tables
    import db.seat_decisions as sd
    import db.seat_ask_class_state as sacs

    engine = create_engine(f"sqlite:///{tmp_path / 'dial.db'}")
    tables.seat_decisions.create(engine)
    tables.seat_ask_class_state.create(engine)
    monkeypatch.setattr(engine_module, "get_engine", lambda: engine)
    monkeypatch.setattr(sd, "get_engine", lambda: engine)
    monkeypatch.setattr(sacs, "get_engine", lambda: engine)

    decisions = sd.SeatDecisionOperations()
    states = sacs.SeatAskClassStateOperations()
    settings = {}
    negatives = {}

    class Fake:
        # decisions
        insert_seat_decision = staticmethod(decisions.insert_seat_decision)
        get_seat_decision = staticmethod(decisions.get_seat_decision)
        list_seat_decisions = staticmethod(decisions.list_seat_decisions)
        list_seat_decision_seats = staticmethod(decisions.list_seat_decision_seats)
        supersede_seat_decision = staticmethod(decisions.supersede_seat_decision)
        set_seat_decision_status = staticmethod(decisions.set_seat_decision_status)
        reconfirm_seat_decision = staticmethod(decisions.reconfirm_seat_decision)
        # dial state
        list_seat_ask_class_states = staticmethod(states.list_seat_ask_class_states)
        get_seat_ask_class_state = staticmethod(states.get_seat_ask_class_state)
        upsert_seat_ask_class_state = staticmethod(states.upsert_seat_ask_class_state)
        set_seat_ask_class_hold = staticmethod(states.set_seat_ask_class_hold)
        set_seat_ask_class_guard = staticmethod(states.set_seat_ask_class_guard)
        # the live conjuncts + ratings
        autonomy = True
        @staticmethod
        def get_setting_value(key, default=None):
            return settings.get(key, default)
        @staticmethod
        def set_setting(key, value):
            settings[key] = value
        @staticmethod
        def get_autonomy_enabled(agent_name):
            return Fake.autonomy
        @staticmethod
        def latest_negative_seat_rating(agent_name, evaluators, since):
            hit = negatives.get(agent_name)
            return hit if hit and hit >= since else None
        @staticmethod
        def get_operator_queue_item(_):
            return None

    Fake.settings = settings
    Fake.negatives = negatives
    return Fake


@pytest.fixture
def dial(monkeypatch):
    from services import autonomy_dial_service as d
    monkeypatch.setattr(d, "_today", lambda: TODAY)
    return d


def _decide(db, *, ask_class="vendor-renewal", criterion=CRITERION, seat=SEAT,
            review_by=FUTURE, decided="Renew Acme", i=0, status="active"):
    from services import seat_decision_service as sd
    row = sd.record(db, agent_name=AGENT, seat_email=seat, decided_by_person=seat,
                    payload=dict(outcome="approved", decided=f"{decided} {i}",
                                 alternatives=["let it lapse"], criterion=criterion,
                                 reversal="the price moves", review_by=review_by,
                                 ask_class=ask_class))
    if status != "active":
        db.set_seat_decision_status(AGENT, row["id"], status, reason="x", by=seat)
    return row


def _three(db, **kw):
    return [_decide(db, i=i, **kw) for i in range(3)]


def _classes(dial, db, seat=SEAT):
    return {c["ask_class"]: c for c in dial.evaluate_seat(db, AGENT, seat, today=TODAY)}


# --------------------------------------------------------------------------- #
# 1. The rule table — each conjunct alone flips the verdict, and names itself
# --------------------------------------------------------------------------- #

class TestTheRule:
    def test_three_records_one_criterion_no_reversal_no_rating_graduates(self, dial, store):
        store.set_setting(dial.LEVEL_KEY, "L2")
        _three(store)
        c = _classes(dial, store)["vendor-renewal"]
        assert c["unprompted"] is True and c["state"] == dial.STATE_GRADUATED
        assert c["blocked_by"] == []
        assert c["evidence"]["criteria"] == [CRITERION] and c["evidence"]["count"] == 3
        assert c["evidence"]["rule_version"] == dial.RULE_VERSION

    def test_two_records_is_not_enough_and_says_so(self, dial, store):
        store.set_setting(dial.LEVEL_KEY, "L2")
        [_decide(store, i=i) for i in range(2)]
        c = _classes(dial, store)["vendor-renewal"]
        assert c["unprompted"] is False and dial.BLOCK_TOO_FEW in c["blocked_by"]

    def test_a_second_criterion_is_not_a_stable_judgment(self, dial, store):
        store.set_setting(dial.LEVEL_KEY, "L2")
        _three(store)
        _decide(store, criterion="cheapest vendor wins", i=9)
        c = _classes(dial, store)["vendor-renewal"]
        assert dial.BLOCK_CRITERIA in c["blocked_by"] and c["unprompted"] is False

    def test_a_reversal_in_the_window_blocks_and_is_named(self, dial, store):
        store.set_setting(dial.LEVEL_KEY, "L2")
        _three(store)
        _decide(store, i=9, status="reversed")
        c = _classes(dial, store)["vendor-renewal"]
        assert dial.BLOCK_REVERSAL in c["blocked_by"] and c["evidence"]["reversals"] == 1

    def test_a_negative_rating_blocks_and_is_named(self, dial, store):
        store.set_setting(dial.LEVEL_KEY, "L2")
        _three(store)
        store.negatives[AGENT] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        c = _classes(dial, store)["vendor-renewal"]
        assert dial.BLOCK_RATING in c["blocked_by"] and c["unprompted"] is False

    def test_a_guard_capped_class_never_graduates_on_this_evidence(self, dial, store):
        store.set_setting(dial.LEVEL_KEY, "L2")
        _three(store)
        store.set_seat_ask_class_guard(agent_name=AGENT, seat_email=SEAT,
                                       ask_class="vendor-renewal", guard_metric=dial.GUARD_CAPPED)
        c = _classes(dial, store)["vendor-renewal"]
        assert dial.BLOCK_GUARD in c["blocked_by"] and c["unprompted"] is False
        assert c["guard_metric"] == dial.GUARD_CAPPED

    def test_guard_defaults_to_not_assessed_so_unreviewed_never_reads_as_cleared(self, dial, store):
        store.set_setting(dial.LEVEL_KEY, "L2")
        _three(store)
        assert _classes(dial, store)["vendor-renewal"]["guard_metric"] == dial.GUARD_NOT_ASSESSED

    def test_every_failing_conjunct_is_listed_not_just_the_first(self, dial, store):
        store.set_setting(dial.LEVEL_KEY, "L2")
        [_decide(store, i=i) for i in range(2)]
        _decide(store, i=9, status="reversed")
        store.negatives[AGENT] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        blocked = _classes(dial, store)["vendor-renewal"]["blocked_by"]
        assert {dial.BLOCK_TOO_FEW, dial.BLOCK_REVERSAL, dial.BLOCK_RATING} <= set(blocked)

    def test_a_class_with_no_evidence_is_on_request_not_unknown(self, dial, store):
        store.set_setting(dial.LEVEL_KEY, "L3")
        assert _classes(dial, store) == {}
        assert dial.live_verdict(None, level="L3", autonomy_enabled=True, today=TODAY)["unprompted"] is False


# --------------------------------------------------------------------------- #
# 2. The live conjuncts — they demote with NO event, and restore what was earned
# --------------------------------------------------------------------------- #

class TestLiveConjuncts:
    def _graduate(self, dial, store):
        store.set_setting(dial.LEVEL_KEY, "L2")
        _three(store)
        from services import autonomy_dial_service
        autonomy_dial_service.evaluate_seat(store, AGENT, SEAT, persist=True, today=TODAY)
        stored = store.get_seat_ask_class_state(AGENT, SEAT, "vendor-renewal")
        assert stored["state"] == dial.STATE_GRADUATED
        return stored

    def test_the_level_is_a_ceiling_and_lowering_it_writes_nothing(self, dial, store):
        stored = self._graduate(dial, store)
        store.set_setting(dial.LEVEL_KEY, "L1")
        c = _classes(dial, store)["vendor-renewal"]
        assert c["unprompted"] is False and dial.BLOCK_LEVEL in c["blocked_by"]
        assert c["earned_state"] == dial.STATE_GRADUATED     # the row is untouched
        assert store.get_seat_ask_class_state(AGENT, SEAT, "vendor-renewal")["state"] == dial.STATE_GRADUATED
        store.set_setting(dial.LEVEL_KEY, "L3")
        assert _classes(dial, store)["vendor-renewal"]["unprompted"] is True

    def test_the_agent_switch_is_the_hard_off_and_flipping_it_back_restores(self, dial, store):
        self._graduate(dial, store)
        store.autonomy = False
        c = _classes(dial, store)["vendor-renewal"]
        assert c["unprompted"] is False and dial.BLOCK_AUTONOMY_OFF in c["blocked_by"]
        assert c["earned_state"] == dial.STATE_GRADUATED
        store.autonomy = True
        assert _classes(dial, store)["vendor-renewal"]["unprompted"] is True

    def test_expiry_demotes_with_no_event_at_all(self, dial, store):
        """The 2am-Friday case: every record's review_by lapses, nothing fires,
        and a materialised verdict would outlive its own evidence."""
        stored = self._graduate(dial, store)
        assert stored["evidence_expires_at"] == FUTURE
        later = date.fromisoformat(FUTURE) + timedelta(days=1)
        live = dial.live_verdict(stored, level="L2", autonomy_enabled=True, today=later)
        assert live["unprompted"] is False and dial.BLOCK_EVIDENCE_EXPIRED in live["blocked_by"]

    def test_the_expiry_is_the_earliest_review_date_not_the_latest(self, dial, store):
        store.set_setting(dial.LEVEL_KEY, "L2")
        soon = (TODAY + timedelta(days=10)).isoformat()
        _decide(store, i=0, review_by=soon)
        _decide(store, i=1); _decide(store, i=2)
        c = _classes(dial, store)["vendor-renewal"]
        assert c["evidence"]["expires_at"] == soon


# --------------------------------------------------------------------------- #
# 3. The ratings window — three shapes, each a defect first
# --------------------------------------------------------------------------- #

class TestRatingsWindow:
    def test_the_seats_own_thumbs_down_counts_under_both_prefixes(self, dial, store, monkeypatch):
        """On a single-operator install the seat person IS the platform
        principal, so their rating lands under `operator:` — matching only
        `workspace:` means a seat that can never demote its own agent."""
        seen = {}
        monkeypatch.setattr(store, "latest_negative_seat_rating",
                            lambda a, evaluators, since: seen.update(ev=evaluators, since=since) or None)
        dial.negative_rating_since(store, AGENT, "Gary@Example.com")
        assert seen["ev"] == [f"workspace:{SEAT}", f"operator:{SEAT}"]

    def test_the_window_is_a_fixed_span_anchored_now(self, dial, store, monkeypatch):
        seen = {}
        monkeypatch.setattr(store, "latest_negative_seat_rating",
                            lambda a, ev, since: seen.update(since=since) or None)
        now = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
        dial.negative_rating_since(store, AGENT, SEAT, now=now)
        assert seen["since"] == (now - timedelta(days=dial.RATING_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_an_unreadable_ratings_table_blocks_rather_than_promotes(self, dial, store, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("no table")
        monkeypatch.setattr(store, "latest_negative_seat_rating", boom)
        store.set_setting(dial.LEVEL_KEY, "L2")
        _three(store)
        assert dial.BLOCK_RATING in _classes(dial, store)["vendor-renewal"]["blocked_by"]

    def test_the_query_reads_the_flip_not_only_the_first_rating(self):
        """`upsert_workspace_rating` updates `updated_at` and leaves
        `created_at`, so an up→down flip inside the window is invisible to a
        `created_at` predicate — and that flip is exactly a demotion."""
        from db.evaluations import EvaluationOperations
        src = inspect.getsource(EvaluationOperations.latest_negative_seat_rating)
        assert "coalesce" in src.lower() and "updated_at" in src and "created_at" in src
        assert "quality < 0.5" in src


# --------------------------------------------------------------------------- #
# 4. Reversals count over the WINDOW, not over all history
# --------------------------------------------------------------------------- #

class TestNotAOneWayRatchet:
    def test_a_reversal_whose_record_has_expired_no_longer_blocks(self, dial, store):
        """#638's `stats` counts reversals over every row ever, which makes one
        reversal permanent — with hold-never-promote, `on_request` would be the
        only reachable steady state. The verdict was deferred to this issue."""
        store.set_setting(dial.LEVEL_KEY, "L2")
        old = (TODAY + timedelta(days=5)).isoformat()
        _decide(store, i=9, review_by=old, status="reversed")
        _three(store)
        assert dial.BLOCK_REVERSAL in _classes(dial, store)["vendor-renewal"]["blocked_by"]
        after = date.fromisoformat(old) + timedelta(days=1)
        later = {c["ask_class"]: c for c in dial.evaluate_seat(store, AGENT, SEAT, today=after)}
        assert dial.BLOCK_REVERSAL not in later["vendor-renewal"]["blocked_by"]

    def test_stats_still_counts_all_history_the_dial_just_does_not_use_it(self, dial, store):
        from services import seat_decision_service as sd
        store.set_setting(dial.LEVEL_KEY, "L2")
        _decide(store, i=9, review_by=(TODAY + timedelta(days=5)).isoformat(), status="reversed")
        _three(store)
        after = date.fromisoformat((TODAY + timedelta(days=6)).isoformat())
        rows = store.list_seat_decisions(AGENT, SEAT, limit=500)
        assert sd.stats(rows, today=after)["ask_classes"][0]["reversals"] == 1   # unchanged contract
        assert dial.class_evidence(rows, "vendor-renewal", today=after)["reversals"] == 0


# --------------------------------------------------------------------------- #
# 5. Writes, events and gates
# --------------------------------------------------------------------------- #

class TestWritesAndGates:
    def test_a_read_persists_nothing(self, dial, store):
        """The panel read must not be a writer: an owner opening the page would
        otherwise write rows for every seat they can see."""
        store.set_setting(dial.LEVEL_KEY, "L2")
        _three(store)   # the decision hook wrote the row; the READ must not touch it
        before = store.list_seat_ask_class_states(AGENT, SEAT)
        store.set_setting(dial.LEVEL_KEY, "L1")     # a live conjunct changed …
        dial.evaluate_seat(store, AGENT, SEAT, persist=False, today=TODAY)
        assert store.list_seat_ask_class_states(AGENT, SEAT) == before   # … and nothing was written

    def test_an_unchanged_re_evaluation_writes_nothing(self, dial, store):
        store.set_setting(dial.LEVEL_KEY, "L2")
        _three(store)
        dial.evaluate_seat(store, AGENT, SEAT, persist=True, today=TODAY)
        first = store.get_seat_ask_class_state(AGENT, SEAT, "vendor-renewal")
        dial.evaluate_seat(store, AGENT, SEAT, persist=True, today=TODAY)
        assert store.get_seat_ask_class_state(AGENT, SEAT, "vendor-renewal")["updated_at"] == first["updated_at"]

    def test_recording_a_decision_re_evaluates_without_the_caller_asking(self, dial, store):
        store.set_setting(dial.LEVEL_KEY, "L2")
        _three(store)   # `record` hooks the re-evaluation itself
        assert store.get_seat_ask_class_state(AGENT, SEAT, "vendor-renewal")["state"] == dial.STATE_GRADUATED

    def test_reversing_a_decision_demotes_through_the_same_hook(self, dial, store):
        from services import seat_decision_service as sd
        store.set_setting(dial.LEVEL_KEY, "L2")
        rows = _three(store)
        assert store.get_seat_ask_class_state(AGENT, SEAT, "vendor-renewal")["state"] == dial.STATE_GRADUATED
        sd.act(store, agent_name=AGENT, seat_email=SEAT, decision_id=rows[0]["id"],
               action="reverse", by=SEAT, reason="the price moved")
        assert store.get_seat_ask_class_state(AGENT, SEAT, "vendor-renewal")["state"] == dial.STATE_ON_REQUEST

    def test_a_hold_blocks_and_survives_re_evaluation(self, dial, store):
        store.set_setting(dial.LEVEL_KEY, "L2")
        _three(store)
        store.set_seat_ask_class_hold(agent_name=AGENT, seat_email=SEAT,
                                      ask_class="vendor-renewal", held=True, by=OWNER)
        dial.evaluate_seat(store, AGENT, SEAT, persist=True, today=TODAY)
        c = _classes(dial, store)["vendor-renewal"]
        assert c["held"] is True and dial.BLOCK_HELD in c["blocked_by"] and c["unprompted"] is False

    def test_the_cas_refuses_a_write_computed_against_a_stale_read(self, dial, store):
        store.set_setting(dial.LEVEL_KEY, "L2")
        _three(store)
        dial.evaluate_seat(store, AGENT, SEAT, persist=True, today=TODAY)
        assert store.upsert_seat_ask_class_state(
            agent_name=AGENT, seat_email=SEAT, ask_class="vendor-renewal", state="graduated",
            blocked_by=[], evidence={"x": 1}, evidence_hash="new", evidence_expires_at=FUTURE,
            previous_hash="a-hash-that-is-not-the-stored-one") is False


class TestPortalGates:
    @pytest.fixture
    def portal(self, dial, store, monkeypatch):
        from client_portal import autonomy as pa
        from client_portal import role_card as rc
        monkeypatch.setattr(pa, "db", store)
        monkeypatch.setattr(pa.svc, "readable_seats",
                            lambda db, agent, reader, *, is_owner: ({SEAT: True, OTHER: is_owner}
                                                                    if is_owner else {reader: True}))
        monkeypatch.setattr(rc, "_is_owner", lambda a, e, p: p and e == OWNER)
        monkeypatch.setattr(pa.dial, "_today", lambda: TODAY)
        store.set_setting(dial.LEVEL_KEY, "L2")
        return pa

    def test_a_seat_never_reads_another_seats_classes(self, portal, store):
        _three(store)
        _three(store, seat=OTHER, ask_class="ops-approval")
        page = portal.page(AGENT, SEAT, is_platform=False)
        assert [c["ask_class"] for c in page["classes"]] == ["vendor-renewal"]
        assert page["other_seats"] == [] and page["can_release"] is False

    def test_the_owner_reads_every_seat_and_may_release(self, portal, store):
        _three(store)
        _three(store, seat=OTHER, ask_class="ops-approval")
        page = portal.page(AGENT, OWNER, is_platform=True)
        assert page["can_release"] is True
        assert {c["ask_class"] for c in page["other_seats"]} == {"vendor-renewal", "ops-approval"}

    def test_hold_is_a_refusal_anyone_may_make_release_is_the_owners_grant(self, portal, store):
        _three(store)
        out = portal.act(AGENT, SEAT, is_platform=False, ask_class="vendor-renewal", action="hold")
        assert out["class"]["held"] is True and out["class"]["unprompted"] is False
        with pytest.raises(portal.AutonomyRefused) as e:
            portal.act(AGENT, SEAT, is_platform=False, ask_class="vendor-renewal", action="release")
        assert e.value.code == "release_owner_only" and e.value.status_code == 403
        back = portal.act(AGENT, OWNER, is_platform=True, ask_class="vendor-renewal",
                          action="release", seat=SEAT)
        assert back["class"]["held"] is False and back["class"]["unprompted"] is True

    def test_there_is_no_promote_action_at_all(self, portal):
        assert portal.HOLD_ACTIONS == ("hold", "release")
        with pytest.raises(portal.AutonomyRefused) as e:
            portal.act(AGENT, OWNER, is_platform=True, ask_class="k", action="promote")
        assert e.value.code == "unknown_action"

    def test_another_seats_class_is_the_uniform_not_found_for_a_non_owner(self, portal, store):
        _three(store, seat=OTHER, ask_class="ops-approval")
        with pytest.raises(portal.AutonomyRefused) as e:
            portal.act(AGENT, SEAT, is_platform=False, ask_class="ops-approval",
                       action="hold", seat=OTHER)
        assert e.value.status_code == 404 and e.value.code == "class_not_found"

    def test_only_the_owner_records_the_guard_cap(self, portal, store):
        _three(store)
        with pytest.raises(portal.AutonomyRefused) as e:
            portal.set_guard(AGENT, SEAT, is_platform=False, ask_class="vendor-renewal",
                             guard_metric="capped")
        assert e.value.code == "guard_owner_only"
        out = portal.set_guard(AGENT, OWNER, is_platform=True, ask_class="vendor-renewal",
                               guard_metric="capped", seat=SEAT)
        assert out["class"]["guard_metric"] == "capped" and out["class"]["unprompted"] is False


# --------------------------------------------------------------------------- #
# 6. The consumer — the companion is TOLD, or the verdict is a surface nobody reads
# --------------------------------------------------------------------------- #

class TestTheCompanionIsTold:
    def test_the_prompt_names_each_class_and_why_it_is_on_request(self, dial, store):
        store.set_setting(dial.LEVEL_KEY, "L2")
        _three(store)
        _decide(store, ask_class="ops-approval", i=0)
        lines = dial.prompt_lines(dial.evaluate_seat(store, AGENT, SEAT, today=TODAY))
        assert "`vendor-renewal`: graduated" in lines
        assert "`ops-approval`: on-request" in lines
        assert dial.BLOCKER_TEXT[dial.BLOCK_TOO_FEW] in lines
        assert "Anything not listed here is on-request" in lines
        assert SEAT not in lines          # the block never names a person

    def test_no_classes_means_no_block_at_all(self, dial, store):
        assert dial.prompt_lines([]) is None

    def test_the_seat_memory_block_carries_it(self, monkeypatch):
        from services import seat_decision_service as sd
        monkeypatch.setattr(sd, "prompt_block", sd.prompt_block)
        src = inspect.getsource(sd.prompt_block)
        assert "autonomy_dial_service" in src and "prompt_lines" in src


# --------------------------------------------------------------------------- #
# 7. The level, and the routes that own it
# --------------------------------------------------------------------------- #

class TestTheLevel:
    def test_an_unset_level_is_l1_which_permits_nothing_unprompted(self, dial, store):
        assert dial.get_level(store) == "L1"
        assert dial.level_allows_unprompted("L1") is False
        assert dial.level_allows_unprompted("L0") is False
        assert dial.level_allows_unprompted("L2") is True
        assert dial.level_allows_unprompted("L3") is True

    def test_an_unknown_stored_value_reads_as_the_default_never_as_permission(self, dial, store):
        store.set_setting(dial.LEVEL_KEY, "L9")
        assert dial.get_level(store) == dial.DEFAULT_LEVEL
        store.set_setting(dial.LEVEL_KEY, "")
        assert dial.get_level(store) == dial.DEFAULT_LEVEL

    def test_set_level_validates_the_closed_vocabulary(self, dial, store):
        assert dial.set_level(store, "l3", changed_by="admin") == "L3"
        with pytest.raises(ValueError):
            dial.set_level(store, "L9", changed_by="admin")

    def test_the_generic_settings_catch_all_refuses_the_level_key(self):
        src = (REPO / "src/backend/routers/settings/generic.py").read_text()
        assert "autonomy_dial_service.LEVEL_KEY" in src
        assert "PUT /api/settings/autonomy-dial" in src

    def test_the_level_route_is_registered_before_the_catch_all(self):
        src = (REPO / "src/backend/routers/settings/__init__.py").read_text()
        assert src.index("autonomy_dial.router") < src.index("generic.router")

    def test_the_level_write_is_admin_and_interactive(self):
        from routers.settings import autonomy_dial as r
        src = inspect.getsource(r.set_autonomy_dial)
        assert "reject_non_interactive_principal" in src
        assert "require_admin" in inspect.getsource(r)
        assert "autonomy_dial_change" in src        # audited


# --------------------------------------------------------------------------- #
# 8. Both tracks, the registry, the facade, the MCP surface
# --------------------------------------------------------------------------- #

def test_the_table_is_on_both_tracks_and_in_the_cleanup_registry():
    schema = (REPO / "src/backend/db/schema.py").read_text()
    tables = (REPO / "src/backend/db/tables.py").read_text()
    mig = (REPO / "src/backend/db/migrations.py").read_text()
    cleanup = (REPO / "src/backend/db/agent_cleanup.py").read_text()
    rev = (REPO / "src/backend/migrations/versions/0073_seat_ask_class_state.py").read_text()
    assert "CREATE TABLE IF NOT EXISTS seat_ask_class_state" in schema
    assert "idx_seat_ask_class_state_seat" in schema
    assert "seat_ask_class_state = Table(" in tables
    assert '("seat_ask_class_state_table", _migrate_seat_ask_class_state_table)' in mig
    assert 'AgentRef("seat_ask_class_state"' in cleanup
    assert 'down_revision = "0072_agent_capability_grants"' in rev
    assert 'has_table("seat_ask_class_state")' in rev


def test_the_migration_graph_still_has_exactly_one_head():
    import subprocess
    out = subprocess.run(["python3", str(REPO / "scripts/ci/check_alembic_heads.py"),
                          str(REPO / "src/backend/migrations/versions")], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr


def test_the_facade_forwards_every_parameter_of_every_mixin_method():
    from database import DatabaseManager
    from db.seat_ask_class_state import SeatAskClassStateOperations
    for name, fn in inspect.getmembers(SeatAskClassStateOperations, inspect.isfunction):
        if name.startswith("_"):
            continue
        assert list(inspect.signature(getattr(DatabaseManager, name)).parameters) == \
            list(inspect.signature(fn).parameters), name


def test_the_agent_read_declares_its_mcp_surface_and_resolves_the_seat_from_the_execution():
    src = (REPO / "src/backend/routers/seat_decisions.py").read_text()
    assert src.startswith("# mcp: decisions.ts")
    assert "get_autonomy" in src.split("\n", 1)[0]
    body = src[src.index("async def get_seat_autonomy"):src.index("@router.get(\"/{agent_name}/decisions\")")]
    assert "_seat_for(agent_name, execution_id)" in body      # never a parameter
    assert "assert_agent_access" in body


def test_the_seat_read_is_not_shadowed_by_the_agent_level_autonomy_toggle():
    """`/api/agents/{name}/autonomy` was already taken.

    `agent_config` owns it (the agent-level `autonomy_enabled` toggle) and is
    included FIRST in `main.py`, so declaring the same path again raises
    nothing, warns nothing and logs nothing — FastAPI matches the first route
    and the second is simply never reached. The seat read shipped that way and
    returned the TOGGLE's payload; only calling it live found it.

    Assert the property, not the spelling: among the agent routes, no two
    declarations may share a (path, method), and the seat read must resolve to
    its own endpoint.
    """
    from routers import agent_config, seat_decisions

    seen: dict[tuple[str, str], str] = {}
    collisions: list[str] = []
    for mod in (agent_config, seat_decisions):
        for route in mod.router.routes:
            for method in getattr(route, "methods", ()) or ():
                key = (route.path, method)
                name = getattr(route.endpoint, "__name__", "?")
                if key in seen:
                    collisions.append(f"{method} {route.path}: {seen[key]} vs {name}")
                else:
                    seen[key] = name
    assert collisions == [], f"two endpoints share a path — the later one is dead: {collisions}"
    assert seen[("/api/agents/{agent_name}/seat-autonomy", "GET")] == "get_seat_autonomy"
    # and the toggle is still where every existing caller expects it
    assert seen[("/api/agents/{agent_name}/autonomy", "GET")] != "get_seat_autonomy"
