"""Replace a pending ask — schema, the atomic compare-and-set, the sink (#3247).

A scheduled run that needs a decision must not file a second ask about what it
already asked. An agent may REPLACE one of its own pending asks: the predecessor
ends `cancelled` / `disposed_by='agent'` / `disposition_reason='replaced'` and
the successor is shown instead — never an edit in place, never over a person's
answer, never a row the agent did not raise itself.

Related flow: docs/memory/feature-flows/operating-room.md
Requirement: docs/memory/requirements/security.md §26.10

Harness: the unit island's real per-process SQLite (`init_database()` builds the
full schema). Agent names are unique to this file.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("sqlalchemy")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

# The two nullable link columns (T3): `replaces` on the successor (the
# predecessor row's uuid), `replaced_by` on the predecessor (the successor's).
LINK_COLUMNS = ("replaces", "replaced_by")
ALEMBIC_REVISION = "0090_operator_queue_replace"
ALEMBIC_PARENT = "0089_supersede_queue_flood_backlog"


@pytest.fixture
def real_db():
    from database import db as real
    return real


def _pending(real_db, agent, rid, **over):
    item = {"id": rid, "type": "approval", "status": "pending", "priority": "high",
            "title": "Approve payout", "question": "Release 500 USDC?",
            "options": ["approve", "reject"], "context": {},
            "created_at": "2026-10-01T10:00:00Z"}
    item.update(over)
    return real_db.create_operator_queue_item(agent, item)


# ===========================================================================
# 1. Schema — one migration pair, both tracks (CP1)
# ===========================================================================

class TestSchema:
    @pytest.mark.parametrize("column", LINK_COLUMNS)
    def test_every_link_column_is_selectable_on_the_migrated_db(self, real_db, column):
        from sqlalchemy import select
        from db.engine import get_engine
        from db.tables import operator_queue
        with get_engine().connect() as conn:
            conn.execute(select(getattr(operator_queue.c, column)).limit(1)).all()

    def test_every_link_column_rides_the_item_projection(self, real_db):
        uid = _pending(real_db, "agent-3247-schema", "s-1")
        item = real_db.get_operator_queue_item(uid)
        missing = [c for c in LINK_COLUMNS if c not in item]
        assert missing == [], missing
        # nullable, no default, no backfill — a plain row links to nothing
        assert {c: item[c] for c in LINK_COLUMNS} == dict.fromkeys(LINK_COLUMNS)

    def test_the_sqlite_migration_is_registered_by_name_and_runs(self):
        import sqlite3
        from db import migrations
        names = [name for name, _fn in migrations.MIGRATIONS]
        assert names.count("operator_queue_replace") == 1
        # it follows the flood-backlog data migration it chains after on the PG track
        assert names.index("operator_queue_replace") > names.index("supersede_queue_flood_backlog")
        fn = dict(migrations.MIGRATIONS)["operator_queue_replace"]
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.execute("CREATE TABLE operator_queue (id TEXT PRIMARY KEY)")
        fn(cur, conn)
        fn(cur, conn)  # idempotent: the second run adds nothing and does not raise
        cols = [r[1] for r in cur.execute("PRAGMA table_info(operator_queue)")]
        assert cols == ["id", *LINK_COLUMNS]

    def test_the_alembic_revision_adds_the_same_two_columns(self):
        import importlib.util
        path = os.path.join(_BACKEND, "migrations", "versions", f"{ALEMBIC_REVISION}.py")
        spec = importlib.util.spec_from_file_location("rev_3247_replace", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.revision == ALEMBIC_REVISION
        assert mod.down_revision == ALEMBIC_PARENT
        assert tuple(mod._COLUMNS) == LINK_COLUMNS

    def test_the_schema_ddl_carries_both_columns(self):
        from db.schema import TABLES as SCHEMA
        ddl = SCHEMA["operator_queue"]
        for column in LINK_COLUMNS:
            assert f"{column} TEXT" in ddl


# ===========================================================================
# 2. The db compare-and-set — end the predecessor + count + insert, one lock (CP2)
# ===========================================================================

def _iso(delta_minutes=0):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _native(real_db, agent, rid, *, replaces=None, max_pending=10, proposal=None, **over):
    item = {"id": rid, "type": "approval", "priority": "high", "title": f"Ask {rid}",
            "question": "Release 500 USDC?", "options": ["approve", "reject"]}
    item.update(over)
    return real_db.create_native_operator_queue_item(
        agent, item, max_pending=max_pending, channel="mcp", raised_by="agent",
        to_role=None, resolved_to=None, proposal=proposal, supersedes_expired=None,
        replaces=replaces,
    )


class TestReplaceCas:
    AGENT = "agent-3247-cas"

    def _own_pending(self, real_db, rid, **over):
        """One of the agent's OWN pending asks, raised natively."""
        out = _native(real_db, self.AGENT, rid, **over)
        assert out["outcome"] == "created", out
        return out["row"]

    def test_a_replace_ends_the_old_and_creates_the_new_in_one_call(self, real_db):
        old = self._own_pending(real_db, "c-old-1")
        out = _native(real_db, self.AGENT, "c-new-1", replaces=old["id"])
        assert out["outcome"] == "created"
        new = out["row"]
        assert new["replaces"] == old["id"] and new["status"] == "pending"
        ended = real_db.get_operator_queue_item(old["id"])
        assert ended["status"] == "cancelled"
        assert ended["disposition"] == "cancelled"
        assert ended["disposed_by"] == "agent"
        assert ended["disposed_by_email"] is None
        assert ended["disposition_reason"] == "replaced"
        assert ended["replaced_by"] == new["id"]
        assert ended["disposed_at"]
        # the call hands back the predecessor as it stands AFTER the compare-and-set
        assert out["predecessor"]["id"] == old["id"]
        assert out["predecessor"]["disposed_by"] == "agent"
        assert out["predecessor"]["replaced_by"] == new["id"]

    def test_a_person_who_answered_first_wins_and_nothing_is_created(self, real_db):
        old = self._own_pending(real_db, "c-old-2")
        answered = real_db.respond_to_operator_queue_item(
            old["id"], "approve", "go", None, "op-3247@example.com")
        assert not answered.get("_status_conflict")
        out = _native(real_db, self.AGENT, "c-new-2", replaces=old["id"])
        assert out["outcome"] == "replaces_ended"
        assert out["row"] is None
        assert out["expired_now"] is False
        assert out["predecessor"]["disposition"] == "answered"
        assert out["predecessor"]["response"] == "approve"
        # the answer is untouched, the link never stamped, no successor row
        after = real_db.get_operator_queue_item(old["id"])
        assert after["response"] == "approve" and after["replaced_by"] is None
        assert real_db.get_operator_queue_item_for_agent_by_request_id(self.AGENT, "c-new-2") is None

    def test_a_cancelled_predecessor_is_refused_as_it_stands(self, real_db):
        old = self._own_pending(real_db, "c-old-3")
        real_db.cancel_operator_queue_item(old["id"], disposed_by_email="op-3247@example.com")
        out = _native(real_db, self.AGENT, "c-new-3", replaces=old["id"])
        assert out["outcome"] == "replaces_ended"
        assert out["predecessor"]["disposition"] == "cancelled"
        assert out["predecessor"]["disposed_by"] == "person"
        assert real_db.get_operator_queue_item_for_agent_by_request_id(self.AGENT, "c-new-3") is None

    def test_a_past_deadline_predecessor_is_expired_in_the_same_transaction_then_refused(self, real_db):
        old = self._own_pending(real_db, "c-old-4", expires_at=_iso(-5))
        assert real_db.get_operator_queue_item(old["id"])["status"] == "pending"
        out = _native(real_db, self.AGENT, "c-new-4", replaces=old["id"])
        assert out["outcome"] == "replaces_ended"
        assert out["expired_now"] is True
        pred = out["predecessor"]
        assert pred["status"] == "expired" and pred["disposition"] == "expired"
        assert pred["disposed_by"] == "timeout" and pred["replaced_by"] is None
        # committed: the clock's vocabulary, visible after the call
        after = real_db.get_operator_queue_item(old["id"])
        assert after["status"] == "expired" and after["disposed_by"] == "timeout"
        assert real_db.get_operator_queue_item_for_agent_by_request_id(self.AGENT, "c-new-4") is None
        # and the sweep finds nothing left to end for it (edge-triggered)
        assert old["id"] not in {r["id"] for r in real_db.mark_operator_queue_expired()}

    def test_a_row_the_agent_did_not_raise_is_never_ended_by_the_cas(self, real_db):
        # a NULL-raiser row filed under the agent's name (a platform alarm, a
        # pre-#611 file row): the sink refuses it first; the CAS belts it again
        uid = _pending(real_db, self.AGENT, "c-alarm-5")
        assert real_db.get_operator_queue_item(uid)["raised_by"] is None
        out = _native(real_db, self.AGENT, "c-new-5", replaces=uid)
        assert out["outcome"] == "replaces_not_own"
        assert out["row"] is None
        untouched = real_db.get_operator_queue_item(uid)
        assert untouched["status"] == "pending" and untouched["replaced_by"] is None
        assert real_db.get_operator_queue_item_for_agent_by_request_id(self.AGENT, "c-new-5") is None

    def test_another_agents_row_is_not_found_by_the_cas(self, real_db):
        other = _native(real_db, "agent-3247-cas-other", "o-1")["row"]
        out = _native(real_db, self.AGENT, "c-new-6", replaces=other["id"])
        assert out["outcome"] == "replaces_not_own"
        assert out["predecessor"] is None
        assert real_db.get_operator_queue_item(other["id"])["status"] == "pending"

    def test_at_the_cap_a_replace_is_admitted(self, real_db):
        agent = "agent-3247-cap"
        old = _native(real_db, agent, "cap-old", max_pending=1)["row"]
        assert _native(real_db, agent, "cap-plain", max_pending=1)["outcome"] == "queue_full"
        out = _native(real_db, agent, "cap-new", max_pending=1, replaces=old["id"])
        assert out["outcome"] == "created"
        assert real_db.get_operator_queue_item(old["id"])["status"] == "cancelled"

    def test_over_the_cap_the_whole_replace_rolls_back(self, real_db):
        agent = "agent-3247-overcap"
        old = _native(real_db, agent, "oc-old", max_pending=5)["row"]
        _native(real_db, agent, "oc-other", max_pending=5)
        # the env was lowered: 1 other pending row already meets a cap of 1
        out = _native(real_db, agent, "oc-new", max_pending=1, replaces=old["id"])
        assert out["outcome"] == "queue_full" and out["row"] is None
        still = real_db.get_operator_queue_item(old["id"])
        assert still["status"] == "pending" and still["replaced_by"] is None
        assert still["disposed_by"] is None
        assert real_db.get_operator_queue_item_for_agent_by_request_id(agent, "oc-new") is None

    def test_a_colliding_insert_after_the_cas_rolls_the_replace_back(self, real_db, monkeypatch):
        """A file entry re-using the successor's id lands between the replay
        check and the insert (the poller never takes the lock; PG can). A
        half-replace — predecessor ended, successor never inserted — must not
        commit: the result is `replayed` with the winner, predecessor untouched."""
        from sqlalchemy import insert
        from db.operator_queue import OperatorQueueOperations
        from db.tables import operator_queue
        agent = "agent-3247-collide"
        old = _native(real_db, agent, "col-old")["row"]
        real_cas = OperatorQueueOperations._replace_predecessor
        winner_id = "f" * 32

        def cas_then_collide(self, conn, *args, **kwargs):
            out = real_cas(self, conn, *args, **kwargs)
            conn.execute(insert(operator_queue).values(
                id=winner_id, agent_name=agent, request_id="col-new", type="question",
                status="pending", priority="low", title="file winner", question="?",
                created_at=_iso(), channel="file", raised_by="agent"))
            return out

        monkeypatch.setattr(OperatorQueueOperations, "_replace_predecessor", cas_then_collide)
        out = _native(real_db, agent, "col-new", replaces=old["id"])
        assert out["outcome"] == "replayed"
        # On SQLite the simulated winner rides the create's own connection, so
        # the rollback takes it too (on PG the poller's row is already
        # committed and is re-read as `row`). What the test proves is the half:
        # the predecessor stands and no successor carrying the link committed.
        still = real_db.get_operator_queue_item(old["id"])
        assert still["status"] == "pending" and still["replaced_by"] is None
        assert still["disposed_by"] is None
        successor = real_db.get_operator_queue_item_for_agent_by_request_id(agent, "col-new")
        assert successor is None or successor["replaces"] is None
        assert out["row"] is None or out["row"]["id"] == winner_id

    def test_a_retried_request_id_replays_and_does_not_replace_again(self, real_db):
        old = self._own_pending(real_db, "c-old-7")
        first = _native(real_db, self.AGENT, "c-new-7", replaces=old["id"])
        assert first["outcome"] == "created"
        later = self._own_pending(real_db, "c-old-7b")
        again = _native(real_db, self.AGENT, "c-new-7", replaces=later["id"])
        assert again["outcome"] == "replayed" and again["row"]["id"] == first["row"]["id"]
        assert real_db.get_operator_queue_item(later["id"])["status"] == "pending"

    def test_respond_and_replace_racing_on_one_row_have_exactly_one_winner(self, real_db):
        import threading
        agent = "agent-3247-race"
        results = {}
        for n in range(6):
            old = _native(real_db, agent, f"race-old-{n}")["row"]
            go = threading.Barrier(2)

            def respond():
                go.wait()
                results["r"] = real_db.respond_to_operator_queue_item(
                    old["id"], "approve", None, None, "op-3247@example.com")

            def replace():
                go.wait()
                results["x"] = _native(real_db, agent, f"race-new-{n}", replaces=old["id"])

            ts = [threading.Thread(target=respond), threading.Thread(target=replace)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            answered = not results["r"].get("_status_conflict")
            replaced = results["x"]["outcome"] == "created"
            assert answered != replaced, (results["r"]["status"], results["x"]["outcome"])
            final = real_db.get_operator_queue_item(old["id"])
            if answered:
                assert final["disposition"] == "answered" and final["replaced_by"] is None
                assert results["x"]["outcome"] == "replaces_ended"
            else:
                assert final["disposed_by"] == "agent" and final["replaced_by"] == results["x"]["row"]["id"]

    def test_the_depth_count_is_unchanged_by_a_replace(self, real_db):
        agent = "agent-3247-depth"
        old = _native(real_db, agent, "d-old", max_pending=3)["row"]
        _native(real_db, agent, "d-new", max_pending=3, replaces=old["id"])
        pending = [i for i in real_db.list_operator_queue_items(agent_name=agent)
                   if i["status"] == "pending"]
        assert [i["request_id"] for i in pending] == ["d-new"]
