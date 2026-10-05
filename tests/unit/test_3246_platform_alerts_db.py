"""
#3246 C3 — the locked platform-alert accessors and the platform ending.

Against the unit island's real SQLite (`init_database()` schema, the #3130
`real_db` shape). What is proved here, each at the layer it lives in:

* `create_platform_item` is find → touch → count → insert in ONE locked
  transaction: two readings of a subject are one pending row, the second
  updates in place (`changed` says whether a rendered field moved), a
  budgeted kind is refused only when there is nothing to update, and the
  partial unique index can never surface as an error.
* The touch is a compare-and-set on `status = 'pending'`: a person's ending
  is never overwritten; the next reading opens a fresh row.
* `mark_expired` no longer expires a row refreshed between its candidate
  select and its per-id compare-and-set (`expires_at < now` joins the CAS).
* `end_items_by_platform` / `ask_service.clear_platform` end only the rows
  they win, in the ent#611 vocabulary authored by the platform, and the
  observers receive exactly those rows.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _item(rid, *, title="t", question="q", priority="high", context=None, expires_in=14, type_="alert"):
    exp = _iso(datetime.now(timezone.utc) + timedelta(days=expires_in)) if expires_in else None
    return {"id": rid, "type": type_, "status": "pending", "priority": priority,
            "title": title, "question": question, "context": context or {}, "expires_at": exp}


@pytest.fixture
def real_db():
    from database import db
    return db


@pytest.fixture
def agent():
    return f"agent-3246-{uuid.uuid4().hex[:6]}"


@pytest.fixture
def subject():
    return f"circuit_dormant:{uuid.uuid4().hex[:6]}"


class TestCreatePlatformItem:
    def test_two_readings_are_one_pending_row_updated_in_place(self, real_db, agent, subject):
        first = real_db.create_platform_operator_queue_item(
            agent, _item("cb-1", context={"k": 1}), subject=subject)
        assert first["outcome"] == "created" and first["changed"] is True
        row = first["row"]
        assert row["subject"] == subject and row["last_seen_at"] and row["context"]["seen_count"] == 1

        second = real_db.create_platform_operator_queue_item(
            agent, _item("cb-2", title="t2", priority="critical", context={"k": 2}), subject=subject)
        assert second["outcome"] == "updated" and second["changed"] is True
        assert second["row"]["id"] == row["id"]
        assert second["row"]["request_id"] == "cb-1"          # the row keeps its first id
        assert second["row"]["title"] == "t2" and second["row"]["priority"] == "critical"
        assert second["row"]["context"] == {"k": 2, "seen_count": 2}
        assert second["row"]["last_seen_at"] >= row["last_seen_at"]
        pending = real_db.list_operator_queue_items(agent_name=agent, status="pending")
        assert len(pending) == 1

    def test_a_bare_repeat_reading_is_not_a_change(self, real_db, agent, subject):
        real_db.create_platform_operator_queue_item(agent, _item("cb-1", context={"k": 1}), subject=subject)
        again = real_db.create_platform_operator_queue_item(agent, _item("cb-2", context={"k": 1}), subject=subject)
        assert again["outcome"] == "updated" and again["changed"] is False
        assert again["row"]["context"]["seen_count"] == 2

    def test_find_pending_by_subject(self, real_db, agent, subject):
        assert real_db.find_pending_operator_queue_by_subject(agent, subject) is None
        real_db.create_platform_operator_queue_item(agent, _item("cb-1"), subject=subject)
        found = real_db.find_pending_operator_queue_by_subject(agent, subject)
        assert found and found["request_id"] == "cb-1"
        assert real_db.find_pending_operator_queue_by_subject("someone-else", subject) is None

    def test_subject_none_always_inserts(self, real_db, agent):
        a = real_db.create_platform_operator_queue_item(agent, _item("ev-1"), subject=None)
        b = real_db.create_platform_operator_queue_item(agent, _item("ev-2"), subject=None)
        assert a["outcome"] == b["outcome"] == "created" and a["row"]["id"] != b["row"]["id"]
        assert a["row"]["subject"] is None

    def test_update_never_overwrites_a_row_a_person_ended(self, real_db, agent, subject):
        first = real_db.create_platform_operator_queue_item(agent, _item("cb-1"), subject=subject)
        real_db.respond_to_operator_queue_item(first["row"]["id"], "ack", "got it", None, "p@example.com")
        ended = real_db.get_operator_queue_item(first["row"]["id"])
        assert ended["status"] == "responded" and ended["disposed_by"] == "person"

        second = real_db.create_platform_operator_queue_item(agent, _item("cb-2", title="t2"), subject=subject)
        assert second["outcome"] == "created" and second["row"]["id"] != first["row"]["id"]
        still = real_db.get_operator_queue_item(first["row"]["id"])
        assert still["status"] == "responded" and still["title"] == "t"   # the person's ending stands

    def test_find_person_ended_by_subject(self, real_db, agent, subject):
        first = real_db.create_platform_operator_queue_item(agent, _item("cb-1"), subject=subject)
        since = _iso(datetime.now(timezone.utc) - timedelta(days=7))
        assert real_db.find_person_ended_operator_queue_by_subject(agent, subject, since) is None
        real_db.respond_to_operator_queue_item(first["row"]["id"], "ack", None, None, "p@example.com")
        found = real_db.find_person_ended_operator_queue_by_subject(agent, subject, since)
        assert found and found["id"] == first["row"]["id"]
        future = _iso(datetime.now(timezone.utc) + timedelta(minutes=1))
        assert real_db.find_person_ended_operator_queue_by_subject(agent, subject, future) is None

    def test_budget_refuses_only_when_there_is_nothing_to_update(self, real_db, agent):
        for i in range(2):
            out = real_db.create_platform_operator_queue_item(
                agent, _item(f"b-{i}", type_="skill_not_found"), subject=f"skill_not_found:{i}",
                max_pending_for_type=2)
            assert out["outcome"] == "created"
        refused = real_db.create_platform_operator_queue_item(
            agent, _item("b-9", type_="skill_not_found"), subject="skill_not_found:9", max_pending_for_type=2)
        assert refused == {"outcome": "refused_at_budget", "row": None, "changed": False}
        # a reading of an EXISTING subject is an update, charged no budget
        upd = real_db.create_platform_operator_queue_item(
            agent, _item("b-x", type_="skill_not_found", title="t2"), subject="skill_not_found:0",
            max_pending_for_type=2)
        assert upd["outcome"] == "updated"

    def test_a_second_pending_row_for_a_subject_is_impossible(self, real_db, agent, subject):
        import sqlalchemy.exc
        from db.engine import get_engine, make_insert
        from db.tables import operator_queue
        real_db.create_platform_operator_queue_item(agent, _item("cb-1"), subject=subject)
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            with get_engine().begin() as conn:
                conn.execute(make_insert(operator_queue).values(
                    id=uuid.uuid4().hex, agent_name=agent, request_id="raw", type="alert",
                    status="pending", priority="high", title="t", question="q",
                    created_at=_iso(datetime.now(timezone.utc)), subject=subject))


class TestMarkExpired:
    def test_a_refresh_between_select_and_cas_keeps_the_row(self, real_db, agent, subject, monkeypatch):
        from db import operator_queue as oq
        past = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))
        item = _item("cb-1"); item["expires_at"] = past
        created = real_db.create_platform_operator_queue_item(agent, item, subject=subject)
        row_id = created["row"]["id"]

        real_select = oq.select
        state = {"refreshed": False}

        def _refreshing_select(*cols, **kw):
            # after the candidate select has been built, refresh the row before the CAS runs
            stmt = real_select(*cols, **kw)
            if not state["refreshed"] and cols and getattr(cols[0], "name", None) == "id":
                state["refreshed"] = True
                real_db.create_platform_operator_queue_item(agent, _item("cb-2"), subject=subject)
            return stmt

        monkeypatch.setattr(oq, "select", _refreshing_select)
        ended_ids = {r["id"] for r in real_db.mark_operator_queue_expired()}
        assert state["refreshed"]
        assert row_id not in ended_ids
        assert real_db.get_operator_queue_item(row_id)["status"] == "pending"

    def test_a_row_past_its_deadline_still_expires(self, real_db, agent, subject):
        item = _item("cb-1"); item["expires_at"] = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))
        row_id = real_db.create_platform_operator_queue_item(agent, item, subject=subject)["row"]["id"]
        assert row_id in {r["id"] for r in real_db.mark_operator_queue_expired()}
        assert real_db.get_operator_queue_item(row_id)["disposed_by"] == "timeout"


class TestPlatformEnding:
    def test_end_items_by_platform_wins_only_pending_rows(self, real_db, agent, subject):
        a = real_db.create_platform_operator_queue_item(agent, _item("cb-1"), subject=subject)["row"]
        b = real_db.create_platform_operator_queue_item(agent, _item("cb-2"), subject=subject + "b")["row"]
        real_db.respond_to_operator_queue_item(b["id"], "ack", None, None, "p@example.com")
        out = real_db.end_operator_queue_items_by_platform([a["id"], b["id"], "nope"], reason="condition_cleared")
        assert [r["id"] for r in out["rows"]] == [a["id"]] and out["batch_id"]
        ended = real_db.get_operator_queue_item(a["id"])
        assert (ended["status"], ended["disposition"], ended["disposed_by"], ended["disposed_by_email"],
                ended["disposition_reason"], ended["batch_id"]) == \
            ("cancelled", "cancelled", "platform", None, "condition_cleared", out["batch_id"])
        assert real_db.get_operator_queue_item(b["id"])["disposed_by"] == "person"
        assert real_db.end_operator_queue_items_by_platform([], reason="superseded") == {"batch_id": None, "rows": []}

    def test_clear_platform_mirrors_expire(self, real_db, agent, subject, monkeypatch):
        from services import ask_service
        a = real_db.create_platform_operator_queue_item(agent, _item("cb-1"), subject=subject)["row"]
        b = real_db.create_platform_operator_queue_item(agent, _item("cb-2"), subject=subject + "b")["row"]
        real_db.respond_to_operator_queue_item(b["id"], "ack", None, None, "p@example.com")

        seen, scheduled = [], []
        ask_service.register_ending_observer(seen.append)
        monkeypatch.setattr(ask_service.operator_resume_service, "spawn_on_loop", scheduled.append)
        try:
            ending = ask_service.clear_platform([a["id"], b["id"]], reason="condition_cleared")
        finally:
            ask_service._observers.remove(seen.append)
        assert [r["id"] for r in ending.rows] == [a["id"]] and ending.batch_id and ending.observers_ok
        assert len(seen) == 1
        event = seen[0]
        assert event.disposition == "cancelled" and event.actor_email is None
        assert event.reason == "condition_cleared" and event.batch_id == ending.batch_id
        assert [r["id"] for r in event.rows] == [a["id"]]
        assert len(scheduled) == 1   # one announcement: audit + thin per-agent trigger

    def test_clear_platform_refuses_a_reason_outside_the_vocabulary(self, real_db):
        from services import ask_service
        assert set(ask_service.PLATFORM_ENDING_REASONS) == {"condition_cleared", "superseded"}
        with pytest.raises(ValueError):
            ask_service.clear_platform(["x"], reason="because")

    def test_clear_platform_with_nothing_won_announces_nothing(self, real_db, monkeypatch):
        from services import ask_service
        scheduled = []
        monkeypatch.setattr(ask_service.operator_resume_service, "spawn_on_loop", scheduled.append)
        ending = ask_service.clear_platform(["nope"], reason="superseded")
        assert ending.rows == [] and ending.batch_id is None and scheduled == []
