"""trinity-enterprise#465 — Workspace suggestions.

Requirement: docs/memory/requirements/core-agent.md §5.39.
Flow: docs/memory/feature-flows/workspace-suggestions.md.

What is pinned here, and why:

* **The rules** (`service.build`) are pure over an injected `now`, so every
  threshold is asserted at its boundary: one tick before is silent, the
  threshold fires.
* **Honesty**: a skipped/cancelled run neither counts toward nor breaks a
  failure streak; autonomy off collapses to ONE agent-level item and suppresses
  "never fired"; a playbook a schedule already runs is never offered; an agent
  that did not answer reports capabilities `unavailable`, never "nothing unused".
* **Dismissal** keys on the state's identity, never a count: another failure
  in the same streak keeps it dismissed, a new streak brings it back.
* **The doors**: a portal token gets 404 on read and write; `configure` items
  reach only owner or admin; the write is bounded to currently-emitted keys.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
AGENT = "atlas"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _svc():
    from client_portal.suggestions import service
    return service


def _sched(sid="s1", *, enabled=True, last_run=None, created=None, updated=None,
           cron="0 9 * * *", message="do the thing", name="Morning check"):
    return {
        "id": sid, "name": name, "enabled": 1 if enabled else 0, "cron_expression": cron,
        "timezone": "UTC", "message": message,
        "created_at": _iso(created or NOW - timedelta(days=30)),
        "updated_at": _iso(updated or NOW - timedelta(days=30)),
        "last_run_at": _iso(last_run) if last_run else None,
    }


def _runs(*statuses):
    """Runs newest-first, ids r0 (newest) .. rN."""
    return [{"id": f"r{i}", "status": st, "started_at": _iso(NOW - timedelta(hours=i + 1))}
            for i, st in enumerate(statuses)]


def _build(signals, *, can_configure=True, now=NOW):
    return _svc().build(signals, agent_name=AGENT, now=now, can_configure=can_configure)


def _keys(pairs):
    return [s.key for s, _ in pairs]


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

class TestRules:
    def test_nothing_to_say_is_an_empty_list(self):
        assert _build(_svc().Signals()) == []

    def test_waiting_items_lead_and_cite_their_count(self):
        S = _svc().Signals
        pairs = _build(S(ask_ids=["a1", "a2"], decisions_due_ids=["d1"],
                         schedules=[_sched(last_run=NOW)], runs={"s1": _runs("failed", "failed", "failed")}))
        assert _keys(pairs)[:3] == ["asks", "decisions_due", "schedule_failing:s1"]
        assert pairs[0][0].signal == "2 questions waiting on you"
        assert pairs[0][0].action.type == "open_section" and pairs[0][0].action.value == "asks"
        assert pairs[1][0].signal == "1 decision past the review date"

    @pytest.mark.parametrize("statuses,expected", [
        (("failed", "failed"), None),                          # below the threshold
        (("failed", "failed", "failed"), 3),                   # at it
        (("failed", "skipped", "failed", "cancelled", "failed"), 3),  # neutral rows stepped over
        (("failed", "failed", "success", "failed"), None),     # a success ends the streak
        (("running", "failed", "error", "failed"), 3),         # in-flight has no verdict
    ])
    def test_failure_streak(self, statuses, expected):
        pairs = _build(_svc().Signals(schedules=[_sched(last_run=NOW)], runs={"s1": _runs(*statuses)}))
        failing = [s for s, _ in pairs if s.key == "schedule_failing:s1"]
        if expected is None:
            assert failing == []
        else:
            assert failing[0].signal.startswith(f"Failed {expected} runs in a row")
            assert failing[0].kind == "configure" and failing[0].door == "owner_or_admin"
            assert failing[0].action.type == "link"
            assert failing[0].action.value == "/agents/atlas?tab=schedules"

    def test_streak_fingerprint_is_the_first_failure_not_the_count(self):
        """Another failure in the SAME streak keeps the fingerprint (no re-nag);
        a new streak after a success changes it."""
        S = _svc().Signals
        def fps(runs):
            return [fp for _, fp in _build(S(schedules=[_sched(last_run=NOW)], runs={"s1": runs}))]

        three = _runs("failed", "failed", "failed", "success")
        # A fourth failure lands on top: the oldest failure of the streak is the same row.
        four = [{"id": "new", "status": "failed", "started_at": _iso(NOW)}] + three
        assert fps(three) == fps(four) == ["r2"]
        new_streak = [{"id": f"x{i}", "status": "failed", "started_at": _iso(NOW)} for i in (1, 2, 3)]
        new_streak.append({"id": "ok", "status": "success", "started_at": _iso(NOW)})
        assert fps(new_streak) == ["x3"]

    def test_autonomy_off_is_one_item_and_suppresses_never_fired(self):
        S = _svc().Signals
        pairs = _build(S(autonomy_enabled=False, overdue_reminders=2,
                         schedules=[_sched("s1"), _sched("s2", last_run=NOW - timedelta(days=3)),
                                    _sched("s3", enabled=False)]))
        assert _keys(pairs) == ["autonomy_held"]
        s = pairs[0][0]
        assert s.signal == "2 schedules won't run and 2 reminders held — autonomy is off · last run Sep 21"

    def test_autonomy_held_fingerprint_is_the_schedule_set_not_counts(self):
        S = _svc().Signals
        a = _build(S(autonomy_enabled=False, overdue_reminders=1, schedules=[_sched("s1")]))[0][1]
        b = _build(S(autonomy_enabled=False, overdue_reminders=5, schedules=[_sched("s1")]))[0][1]
        c = _build(S(autonomy_enabled=False, overdue_reminders=5, schedules=[_sched("s1"), _sched("s2")]))[0][1]
        assert a == b != c

    def test_never_fired_waits_for_the_crons_first_expected_fire(self):
        """Measured against the cron, not a fixed delay: a weekly schedule created
        two days ago has not missed anything yet."""
        S = _svc().Signals
        created = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)   # first 09:00 fire: Sep 23 09:00
        daily = _sched(created=created, cron="0 9 * * *")
        # 1h grace after the first expected fire — silent one tick before, fires at it.
        assert _build(S(schedules=[daily]), now=datetime(2026, 9, 23, 9, 59, 59, tzinfo=timezone.utc)) == []
        fired = _build(S(schedules=[daily]), now=datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc))
        assert _keys(fired) == ["schedule_never_fired:s1"]
        assert fired[0][0].signal == "Enabled since Sep 23 · has never run"
        weekly = _sched(created=datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc), cron="0 9 * * 1")  # Mondays
        assert _build(S(schedules=[weekly])) == []                  # next Monday is Sep 28

    def test_unparseable_cron_is_not_a_suggestion(self):
        assert _build(_svc().Signals(schedules=[_sched(cron="not a cron")])) == []

    def test_disabled_schedule_that_used_to_run(self):
        S = _svc().Signals
        seven = _sched(enabled=False, last_run=NOW - timedelta(days=20), updated=NOW - timedelta(days=7))
        six = _sched(enabled=False, last_run=NOW - timedelta(days=20),
                     updated=NOW - timedelta(days=7) + timedelta(seconds=1))
        never_ran = _sched(enabled=False, updated=NOW - timedelta(days=30))
        assert _keys(_build(S(schedules=[seven]))) == ["schedule_disabled:s1"]
        assert _build(S(schedules=[six])) == []
        assert _build(S(schedules=[never_ran])) == []

    def test_configure_items_need_owner_or_admin(self):
        S = _svc().Signals
        sig = S(ask_ids=["a"], autonomy_enabled=False, schedules=[_sched()])
        assert _keys(_build(sig, can_configure=False)) == ["asks"]
        assert _keys(_build(sig, can_configure=True)) == ["asks", "autonomy_held"]

    def test_dormant_boundary(self):
        S = _svc().Signals
        at = _build(S(last_user_message_at=_iso(NOW - timedelta(days=14))))
        before = _build(S(last_user_message_at=_iso(NOW - timedelta(days=14) + timedelta(seconds=1))))
        assert _keys(at) == ["dormant"] and at[0][0].action.type == "open_chat"
        assert before == []

    def test_unused_playbooks_honest_and_bounded(self):
        svc = _svc()
        P = svc.Playbook
        playbooks = [P("weekly-report", "Weekly report", "Summarise the week"), P("triage", "Triage"),
                     P("scheduled-one", "Scheduled"), P("a", "A"), P("b", "B"), P("c", "C")]
        pairs = _build(svc.Signals(
            playbooks=playbooks, used_playbooks=frozenset({"triage"}),
            schedules=[_sched(message="/scheduled-one please", last_run=NOW)],
        ))
        unused = [s for s, _ in pairs if s.key.startswith("unused_playbook:")]
        assert [s.key for s in unused] == ["unused_playbook:weekly-report", "unused_playbook:a", "unused_playbook:b"]
        first = unused[0]
        assert first.signal == "You haven't run /weekly-report yet"
        assert first.action.type == "prefill" and first.action.value == "/weekly-report "
        assert first.description == "Summarise the week"

    def test_schedule_names_are_capped(self):
        long = "x" * 300
        pairs = _build(_svc().Signals(schedules=[_sched(name=long, last_run=NOW)],
                                      runs={"s1": _runs("failed", "failed", "failed")}))
        assert len(pairs[0][0].title) < 100

    def test_slash_name_parsing(self):
        f = _svc().slash_name
        assert f("/weekly-report now") == "weekly-report"
        assert f("  /Triage") == "triage"
        assert f("please /weekly-report") is None
        assert f(None) is None

    def test_briefing_items_keep_playbooks_and_drop_use_cases(self):
        from client_portal.models import PortalPlaybook
        items = [PortalPlaybook(title="Weekly report", description="d", starter_prompt="/weekly-report "),
                 PortalPlaybook(title="Ask me about sales", description=None, starter_prompt="Ask me about sales")]
        pbs = _svc().playbooks_from_briefing(items)
        assert [(p.name, p.title) for p in pbs] == [("weekly-report", "Weekly report")]


class TestShape:
    def _pairs(self, n):
        from client_portal.suggestions.models import Suggestion, SuggestionAction
        return [(Suggestion(key=f"unused_playbook:p{i}", kind="invoke", source="capability", door="platform",
                            title=f"P{i}", signal="s", action=SuggestionAction(type="prefill", value="/p ")), f"p{i}")
                for i in range(n)]

    def test_cap_and_total(self):
        out = _svc().shape(self._pairs(7), {}, agent_name=AGENT, playbooks=[1], has_history=True)
        assert len(out.suggestions) == 5 and out.total == 7

    def test_dismissed_hidden_only_while_fingerprint_matches(self):
        pairs = self._pairs(2)
        out = _svc().shape(pairs, {"unused_playbook:p0": "p0", "unused_playbook:p1": "stale"},
                           agent_name=AGENT, playbooks=[1], has_history=True)
        assert [s.key for s in out.suggestions] == ["unused_playbook:p1"]

    @pytest.mark.parametrize("playbooks,expected", [(None, "unavailable"), ([], "none"), ([1], "available")])
    def test_capabilities_state_is_stated(self, playbooks, expected):
        assert _svc().shape([], {}, agent_name=AGENT, playbooks=playbooks, has_history=True).capabilities == expected

    def test_no_history_says_capabilities_only(self):
        assert _svc().shape([], {}, agent_name=AGENT, playbooks=[], has_history=False).basis == "capabilities_only"


# ---------------------------------------------------------------------------
# The SQL
# ---------------------------------------------------------------------------

@pytest.fixture()
def sdb(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-465.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from sqlalchemy import delete
    from db.engine import get_engine
    from db.tables import (
        metadata as m, agent_schedules, schedule_executions, agent_reminders,
        enterprise_portal_messages, workspace_suggestion_feedback,
    )
    tables = [agent_schedules, schedule_executions, agent_reminders,
              enterprise_portal_messages, workspace_suggestion_feedback]
    m.create_all(get_engine(), tables=tables)
    with get_engine().begin() as conn:  # idempotent on a shared PG tier
        for t in tables:
            col = t.c.agent_name
            conn.execute(delete(t).where(col.in_([AGENT, "borealis"])))
    from client_portal.suggestions import db as sdb_mod
    return sdb_mod


def _insert(table, **values):
    from sqlalchemy import insert
    from db.engine import get_engine
    with get_engine().begin() as conn:
        conn.execute(insert(table).values(**values))


class TestSql:
    def test_runs_windowed_per_schedule_newest_first(self, sdb):
        from db.tables import schedule_executions as t
        for sid in ("s1", "s2"):
            for i in range(4):
                _insert(t, id=f"{sid}-{i}", schedule_id=sid, agent_name=AGENT, status="failed",
                        started_at=_iso(NOW - timedelta(hours=i)))
        _insert(t, id="other", schedule_id="s1", agent_name="borealis", status="failed", started_at=_iso(NOW))
        _insert(t, id="old", schedule_id="s1", agent_name=AGENT, status="success",
                started_at=_iso(NOW - timedelta(days=200)))
        _insert(t, id="chat", schedule_id="__manual__", agent_name=AGENT, status="failed", started_at=_iso(NOW))
        since = _iso(NOW - timedelta(days=90))
        runs = sdb.recent_runs_by_schedule(AGENT, since, 3, ["s1", "s2"])
        assert [r["id"] for r in runs["s1"]] == ["s1-0", "s1-1", "s1-2"]
        assert [r["id"] for r in runs["s2"]] == ["s2-0", "s2-1", "s2-2"]
        # Only the live schedules asked for are ranked — never chat/API runs.
        assert "__manual__" not in runs
        assert set(sdb.recent_runs_by_schedule(AGENT, since, 3, ["s2"])) == {"s2"}
        assert sdb.recent_runs_by_schedule(AGENT, since, 3, []) == {}

    def test_soft_deleted_schedules_excluded(self, sdb):
        from db.tables import agent_schedules as t
        base = dict(agent_name=AGENT, cron_expression="0 9 * * *", message="m", enabled=1,
                    created_at=_iso(NOW), updated_at=_iso(NOW))
        _insert(t, id="live", name="Live", **base)
        _insert(t, id="gone", name="Gone", deleted_at=_iso(NOW), **base)
        assert [s["id"] for s in sdb.list_schedules(AGENT)] == ["live"]

    def test_usage_is_the_viewers_own(self, sdb):
        from db.tables import enterprise_portal_messages as pm, schedule_executions as ex
        _insert(pm, id="m1", agent_name=AGENT, client_email="alice@example.com", session_id="x",
                role="user", content="/weekly-report now", created_at=_iso(NOW - timedelta(days=20)))
        _insert(pm, id="m2", agent_name=AGENT, client_email="bob@example.com", session_id="y",
                role="user", content="/triage", created_at=_iso(NOW))
        _insert(pm, id="m3", agent_name=AGENT, client_email="alice@example.com", session_id="x",
                role="assistant", content="/not-mine", created_at=_iso(NOW))
        _insert(ex, id="e1", schedule_id="__manual__", agent_name=AGENT, status="success",
                started_at=_iso(NOW), message="/digest", source_user_email="Alice@Example.com")
        texts = sdb.slash_texts_by_viewer(AGENT, "ALICE@example.com", _iso(NOW - timedelta(days=90)))
        assert sorted(texts) == ["/digest", "/weekly-report now"]
        assert sdb.last_user_message_at(AGENT, "alice@example.com") == _iso(NOW - timedelta(days=20))
        assert sdb.last_user_message_at(AGENT, "carol@example.com") is None

    def test_runs_attributed_to_the_viewer_count_as_history(self, sdb):
        from db.tables import schedule_executions as ex
        since = _iso(NOW - timedelta(days=90))
        assert sdb.viewer_has_runs(AGENT, "owner@example.com", since) is False
        _insert(ex, id="op1", schedule_id="__manual__", agent_name=AGENT, status="success",
                started_at=_iso(NOW), message="hello", source_user_email="Owner@Example.com")
        assert sdb.viewer_has_runs(AGENT, "owner@example.com", since) is True
        assert sdb.viewer_has_runs(AGENT, "someone@example.com", since) is False

    def test_overdue_reminders(self, sdb):
        from db.tables import agent_reminders as t
        _insert(t, id="due", agent_name=AGENT, message="m", fire_at=_iso(NOW - timedelta(hours=1)),
                status="pending", created_at=_iso(NOW))
        _insert(t, id="later", agent_name=AGENT, message="m", fire_at=_iso(NOW + timedelta(hours=1)),
                status="pending", created_at=_iso(NOW))
        _insert(t, id="done", agent_name=AGENT, message="m", fire_at=_iso(NOW - timedelta(hours=1)),
                status="fired", created_at=_iso(NOW))
        assert sdb.count_overdue_reminders(AGENT, _iso(NOW)) == 1

    def test_accept_and_dismiss_live_side_by_side(self, sdb):
        kw = dict(email="Alice@example.com", agent_name=AGENT, key="asks", source="asks", now=_iso(NOW))
        sdb.record_feedback(action="dismiss", fingerprint="fp1", **kw)
        sdb.record_feedback(action="accept", fingerprint="fp1", **kw)
        sdb.record_feedback(action="accept", fingerprint="fp1", **kw)
        assert sdb.dismissed_fingerprints(AGENT, "alice@example.com") == {"asks": "fp1"}
        from sqlalchemy import select
        from db.engine import get_engine
        from db.tables import workspace_suggestion_feedback as f
        with get_engine().connect() as conn:
            row = conn.execute(select(f).where(f.c.client_email == "alice@example.com")).mappings().one()
        assert row["accept_count"] == 2 and row["surface"] == "agent"

    def test_cap_evicts_oldest_and_existing_key_stays_writable(self, sdb, monkeypatch):
        monkeypatch.setattr(sdb, "MAX_FEEDBACK_ROWS", 2)
        for i, key in enumerate(["k1", "k2", "k3"]):
            sdb.record_feedback(email="a@example.com", agent_name=AGENT, key=key, source="usage",
                                action="dismiss", fingerprint="x", now=_iso(NOW + timedelta(seconds=i)))
        assert set(sdb.dismissed_fingerprints(AGENT, "a@example.com")) == {"k2", "k3"}
        sdb.record_feedback(email="a@example.com", agent_name=AGENT, key="k2", source="usage",
                            action="dismiss", fingerprint="y", now=_iso(NOW + timedelta(seconds=9)))
        assert sdb.dismissed_fingerprints(AGENT, "a@example.com") == {"k2": "y", "k3": "x"}


# ---------------------------------------------------------------------------
# The service: bounded writes and the stopped agent
# ---------------------------------------------------------------------------

class TestService:
    @pytest.fixture()
    def stubbed(self, monkeypatch):
        svc = _svc()
        state = {"gathered": {}, "playbooks": None, "writes": []}
        monkeypatch.setattr(svc, "_gather", lambda *a, **k: dict(state["gathered"]))

        async def playbooks(agent):
            return state["playbooks"]
        monkeypatch.setattr(svc, "_playbooks", playbooks)
        monkeypatch.setattr(svc, "can_configure", lambda email, agent, is_admin: is_admin)
        monkeypatch.setattr(svc.sdb, "record_feedback", lambda **kw: state["writes"].append(kw))
        return svc, state

    @pytest.mark.asyncio
    async def test_feedback_for_a_key_not_emitted_is_404_and_writes_nothing(self, stubbed):
        svc, state = stubbed
        with pytest.raises(svc.SuggestionError) as e:
            await svc.record_feedback(AGENT, "a@example.com", is_admin=False, key="asks", action="dismiss", now=NOW)
        assert e.value.status_code == 404 and state["writes"] == []

    @pytest.mark.asyncio
    async def test_feedback_unknown_class_is_422(self, stubbed):
        svc, state = stubbed
        with pytest.raises(svc.SuggestionError) as e:
            await svc.record_feedback(AGENT, "a@example.com", is_admin=False, key="bogus:x", action="accept", now=NOW)
        assert e.value.status_code == 422 and state["writes"] == []

    @pytest.mark.asyncio
    async def test_feedback_stores_the_servers_fingerprint(self, stubbed):
        svc, state = stubbed
        state["gathered"] = {"ask_ids": ["a1"]}
        await svc.record_feedback(AGENT, "a@example.com", is_admin=False, key="asks", action="dismiss", now=NOW)
        (w,) = state["writes"]
        assert w["fingerprint"] == svc._hash(["a1"]) and w["source"] == "asks"

    @pytest.mark.asyncio
    async def test_configure_item_not_writable_by_a_viewer(self, stubbed):
        svc, state = stubbed
        state["gathered"] = {"autonomy_enabled": False, "schedules": [_sched()]}
        with pytest.raises(svc.SuggestionError):
            await svc.record_feedback(AGENT, "a@example.com", is_admin=False, key="autonomy_held",
                                      action="dismiss", now=NOW)
        await svc.record_feedback(AGENT, "a@example.com", is_admin=True, key="autonomy_held",
                                  action="dismiss", now=NOW)
        assert len(state["writes"]) == 1

    @pytest.mark.asyncio
    async def test_stopped_agent_never_calls_the_briefing(self, monkeypatch):
        svc = _svc()
        from client_portal import service as portal
        svc._briefing_cache.clear()
        called = []

        async def availability(names):
            return {n: "stopped" for n in names}

        async def briefing(*a, **k):
            called.append(a)
            raise AssertionError("must not be called for a stopped agent")
        monkeypatch.setattr(portal, "_availability_map", availability)
        monkeypatch.setattr(portal, "_agent_briefing", briefing)
        assert await svc._playbooks("stopped-agent") is None
        assert called == []
        svc._briefing_cache.clear()


# ---------------------------------------------------------------------------
# The doors
# ---------------------------------------------------------------------------

class TestDoors:
    @pytest.fixture()
    def client(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from client_portal.suggestions import router as r
        from client_portal import service as portal
        from services import rate_limiter
        from client_portal.suggestions.models import PortalSuggestions

        principal = {"value": None}
        calls = []
        app = FastAPI()
        app.include_router(r.router)
        app.dependency_overrides[r.get_portal_principal] = lambda: principal["value"]
        monkeypatch.setattr(portal, "agent_on_roster", lambda a, e, inc: a == AGENT)
        monkeypatch.setattr(rate_limiter, "enforce", lambda *a, **k: None)

        async def get(agent, email, *, is_admin):
            calls.append(("get", agent, email, is_admin))
            return PortalSuggestions(agent_name=agent)

        async def feedback(agent, email, *, is_admin, key, action):
            calls.append(("post", agent, email, is_admin, key, action))
        monkeypatch.setattr(r.service, "get_suggestions", get)
        monkeypatch.setattr(r.service, "record_feedback", feedback)
        return TestClient(app), principal, calls

    def test_portal_token_is_404_on_read_and_write(self, client):
        from client_portal.portal_auth import PortalPrincipal
        c, principal, calls = client
        principal["value"] = PortalPrincipal("a@example.com", False)
        assert c.get(f"/api/enterprise/client-portal/agents/{AGENT}/suggestions").status_code == 404
        assert c.post(f"/api/enterprise/client-portal/agents/{AGENT}/suggestions/feedback",
                      json={"key": "asks", "action": "dismiss"}).status_code == 404
        assert calls == []

    def test_off_roster_is_404(self, client):
        from client_portal.portal_auth import PortalPrincipal
        c, principal, calls = client
        principal["value"] = PortalPrincipal("a@example.com", True)
        assert c.get("/api/enterprise/client-portal/agents/other/suggestions").status_code == 404
        assert calls == []

    def test_platform_viewer_reads_and_admin_flag_travels(self, client):
        from client_portal.portal_auth import PortalPrincipal
        c, principal, calls = client
        principal["value"] = PortalPrincipal("a@example.com", True, True)
        assert c.get(f"/api/enterprise/client-portal/agents/{AGENT}/suggestions").status_code == 200
        assert calls == [("get", AGENT, "a@example.com", True)]

    def test_feedback_body_is_validated(self, client):
        from client_portal.portal_auth import PortalPrincipal
        c, principal, calls = client
        principal["value"] = PortalPrincipal("a@example.com", True)
        url = f"/api/enterprise/client-portal/agents/{AGENT}/suggestions/feedback"
        assert c.post(url, json={"key": "asks", "action": "explode"}).status_code == 422
        assert c.post(url, json={"key": "x" * 201, "action": "accept"}).status_code == 422
        assert c.post(url, json={"key": "unused_playbook:a/b", "action": "accept"}).status_code == 200
        assert calls[-1] == ("post", AGENT, "a@example.com", False, "unused_playbook:a/b", "accept")


class TestPrincipal:
    def test_positional_construction_keeps_its_meaning(self):
        from client_portal.portal_auth import PortalPrincipal
        p = PortalPrincipal("a@example.com", True)
        assert p.is_admin is False and p.is_platform is True

    @pytest.mark.parametrize("role,scope,expected", [
        ("admin", None, True), ("admin", "user", True), ("admin", "system", True),
        ("admin", "ops", False), ("admin", "portal_delegate", False), ("creator", None, False),
    ])
    def test_admin_needs_role_and_an_allowlisted_scope(self, role, scope, expected):
        from types import SimpleNamespace
        from client_portal.portal_auth import _is_admin_principal
        assert _is_admin_principal(SimpleNamespace(role=role, mcp_scope=scope)) is expected

    def test_admin_fails_closed_without_a_scope_attribute(self):
        from types import SimpleNamespace
        from client_portal.portal_auth import _is_admin_principal
        assert _is_admin_principal(SimpleNamespace(role="admin")) is False


def test_table_is_a_cascade_agent_ref():
    from db.agent_cleanup import AGENT_REFS
    refs = {(r.table, r.column): r.policy.name for r in AGENT_REFS}
    assert refs[("workspace_suggestion_feedback", "agent_name")] == "CASCADE"


@pytest.mark.asyncio
@pytest.mark.parametrize("role,scope,expected", [("admin", None, True), ("admin", "ops", False), ("user", None, False)])
async def test_the_platform_door_stamps_is_admin_on_the_principal(monkeypatch, role, scope, expected):
    """Executes the wiring line itself (`PortalPrincipal(email, True, _is_admin_principal(user))`),
    not only the helper: a platform principal carries `is_admin`, and a portal token never does."""
    from types import SimpleNamespace
    from client_portal import portal_auth as pa
    from client_portal import db as portal_db
    import database

    user = SimpleNamespace(username="u1", role=role, mcp_scope=scope, agent_name=None)

    async def current_user(request, token):
        return user
    monkeypatch.setattr(pa, "decode_portal_session", lambda t: None)
    monkeypatch.setattr(pa, "get_current_user", current_user)
    monkeypatch.setattr(database.db, "get_user_by_username", lambda name: {"email": "U1@Example.com"})
    monkeypatch.setattr(portal_db, "is_client_blocked", lambda e: False)

    principal = await pa.get_portal_principal(SimpleNamespace(), SimpleNamespace(headers={}), token="jwt")
    assert (principal.email, principal.is_platform, principal.is_admin) == ("u1@example.com", True, expected)


class TestGatherEndToEnd:
    """`_gather` + `_compute` over a real SQLite file — the composition the unit
    rules cannot see: live schedule ids reach the streak query, a chat run never
    does, and a console-only owner has history."""

    @pytest.fixture()
    def wired(self, sdb, monkeypatch):
        import database
        from client_portal.asks import service as asks_service
        svc = _svc()
        monkeypatch.setattr(asks_service, "list_asks", lambda email, is_platform, agent: [])
        monkeypatch.setattr(database.db, "list_seat_decisions", lambda agent, seat, limit=500: [])
        monkeypatch.setattr(database.db, "get_autonomy_enabled", lambda agent: True)
        from client_portal import service as portal
        monkeypatch.setattr(portal, "portal_owns_agent", lambda email, agent, include_owned: False)

        async def no_playbooks(agent):
            return []
        monkeypatch.setattr(svc, "_playbooks", no_playbooks)
        return svc

    @pytest.mark.asyncio
    async def test_a_failing_schedule_surfaces_and_chat_runs_do_not_count(self, wired):
        from db.tables import agent_schedules as sch, schedule_executions as ex
        _insert(sch, id="s1", agent_name=AGENT, name="Nightly sync", cron_expression="0 2 * * *",
                message="sync", enabled=1, created_at=_iso(NOW - timedelta(days=30)),
                updated_at=_iso(NOW - timedelta(days=30)), last_run_at=_iso(NOW - timedelta(days=1)))
        for i in range(3):
            _insert(ex, id=f"f{i}", schedule_id="s1", agent_name=AGENT, status="failed",
                    started_at=_iso(NOW - timedelta(days=i + 1)))
        # Newer chat runs on the same agent: never part of any schedule's streak.
        for i in range(5):
            _insert(ex, id=f"c{i}", schedule_id="__manual__", agent_name=AGENT, status="success",
                    started_at=_iso(NOW - timedelta(hours=i + 1)), source_user_email="owner@example.com")
        out = await wired.get_suggestions(AGENT, "owner@example.com", is_admin=True, now=NOW)
        assert [s.key for s in out.suggestions] == ["schedule_failing:s1"]
        assert out.suggestions[0].signal.startswith("Failed 3 runs in a row")
        # The owner never used the Workspace, but ran things from the console: history.
        assert out.basis == "history"

    @pytest.mark.asyncio
    async def test_no_workspace_and_no_runs_is_capabilities_only(self, wired):
        out = await wired.get_suggestions(AGENT, "new@example.com", is_admin=False, now=NOW)
        assert out.suggestions == [] and out.basis == "capabilities_only"
