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


# ===========================================================================
# 3. The sink — ownership gate, the two refusals, the ending, the receipt (CP3)
# ===========================================================================

OWNER = "owner-3247@example.com"
RECEIPT_KEYS = {"status", "id", "request_id", "raised_by", "channel", "type", "to_role",
                "resolved", "ask_status", "disposition", "disposed_at", "expires_at",
                "wakes_on_ending", "supersedes_expired",
                "replaces", "replaced_by", "disposed_by"}


class _Rejected:
    def __init__(self, svc, status, code):
        self.svc, self.status, self.code = svc, status, code

    def __enter__(self):
        self._cm = pytest.raises(self.svc.AskRejected)
        self._info = self._cm.__enter__()
        return self._info

    def __exit__(self, *exc):
        ok = self._cm.__exit__(*exc)
        assert (self._info.value.status_code, self._info.value.code) == (self.status, self.code)
        return ok


@pytest.fixture
def ask(real_db, monkeypatch):
    """The real sink over the real SQLite; the world around it stubbed (the
    ent#611 fixture): audit + broadcast recorded, owner, thread, opt-in, rate."""
    import json as _json
    from types import SimpleNamespace
    import services.ask_service as svc
    import services.operator_queue_service as oqs
    import services.operator_resume_service as ors
    from services import assignment_provider
    from services.rate_limiter import RateLimitResult

    audit, sent, events, wakes = [], [], [], []
    state = {"owner": OWNER, "rate_ok": True, "opted_in": False}

    class _Audit:
        async def log(self, **kw):
            audit.append(kw)
            return "evt"

    class _WS:
        async def broadcast(self, message):
            sent.append(_json.loads(message))

    monkeypatch.setattr(svc, "platform_audit_service", _Audit())
    monkeypatch.setattr(svc, "_websocket_manager", _WS())
    monkeypatch.setattr(svc, "_owner_email", lambda agent: state["owner"])
    monkeypatch.setattr(oqs, "_workspace_attachment", lambda agent, email, **_: (f"thread-{email}", False))
    monkeypatch.setattr(real_db, "get_operator_resume_enabled", lambda agent: state["opted_in"], raising=False)
    monkeypatch.setattr(oqs.rate_limiter, "check",
                        lambda *a, **k: RateLimitResult(state["rate_ok"], 10, 0, 60))
    monkeypatch.setattr(ors, "spawn_ending_dispatch", lambda rows, **kw: wakes.append(("ending", rows, kw)))
    monkeypatch.setattr(ors, "spawn_resume_dispatch", lambda item, **kw: wakes.append(("resume", item, kw)))
    svc.register_ending_observer(events.append)
    assignment_provider.clear_provider()
    yield SimpleNamespace(svc=svc, audit=audit, sent=sent, events=events, wakes=wakes,
                          state=state, db=real_db)
    svc._observers.remove(events.append)
    assignment_provider.clear_provider()


async def _drain():
    import asyncio
    import services.operator_resume_service as ors
    for _ in range(5):
        await asyncio.sleep(0)
        pending = list(ors._inflight)
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


def _body(request_id, **over):
    b = {"request_id": request_id, "type": "approval", "title": "Pay invoice",
         "question": "Release 500 USDC to the vendor?", "options": ["approve", "reject"],
         "proposal": {"pay": 500, "to": f"vendor-{request_id}"}}
    b.update(over)
    return b


def _raise(ask, agent, body):
    return ask.svc.raise_ask(agent, body, raised_by="agent", channel="mcp")


class TestSinkOwnership:
    AGENT = "agent-3247-sink-own"

    def test_the_model_accepts_replaces(self):
        from models import OperatorAskCreate
        assert OperatorAskCreate(request_id="m-1", title="t", replaces="m-0").replaces == "m-0"

    @pytest.mark.parametrize("bad", ["", "has space", "x" * 300, 7])
    def test_a_malformed_replaces_is_refused_before_anything_is_read(self, ask, bad):
        with _Rejected(ask.svc, 422, "invalid_replaces"):
            _raise(ask, self.AGENT, _body("own-bad", replaces=bad))

    def test_replacing_itself_is_refused(self, ask):
        with _Rejected(ask.svc, 422, "invalid_replaces"):
            _raise(ask, self.AGENT, _body("own-self", replaces="own-self"))

    def test_a_missing_predecessor_is_one_uniform_refusal(self, ask):
        with _Rejected(ask.svc, 422, "invalid_replaces"):
            _raise(ask, self.AGENT, _body("own-missing", replaces="never-raised"))

    def test_another_agents_ask_is_not_found(self, ask):
        other = _raise(ask, "agent-3247-sink-other", _body("theirs-1"))
        with _Rejected(ask.svc, 422, "invalid_replaces"):
            _raise(ask, self.AGENT, _body("own-theirs", replaces="theirs-1"))
        assert ask.db.get_operator_queue_item(other["id"])["status"] == "pending"

    def test_a_gate_row_is_refused(self, ask):
        ask.db.create_operator_queue_item(
            self.AGENT, {"id": "gate-own-1", "type": "approval", "title": "Run gated skill",
                         "question": "?", "created_at": _iso()}, channel="gate", raised_by="gate")
        with _Rejected(ask.svc, 422, "invalid_replaces"):
            _raise(ask, self.AGENT, _body("own-gate", replaces="gate-own-1"))
        assert ask.db.get_operator_queue_item_for_agent_by_request_id(
            self.AGENT, "gate-own-1")["status"] == "pending"

    @pytest.mark.parametrize("rid", [
        pytest.param("queue-flood-own", id="reserved-prefix-platform-alarm"),
        pytest.param("skills-reconcile-own", id="unreserved-prefix-null-raiser-alarm"),
        pytest.param("legacy-file-own", id="pre-611-file-row"),
    ])
    def test_a_null_raiser_row_under_the_agents_name_is_refused(self, ask, rid):
        """`_raiser_of` calls an unreserved NULL-raiser row "agent"; the gate
        must check the column itself, or an agent could end the alarm about
        its own leaked credential and stop it counting."""
        uid = _pending(ask.db, self.AGENT, rid)
        assert ask.db.get_operator_queue_item(uid)["raised_by"] is None
        with _Rejected(ask.svc, 422, "invalid_replaces"):
            _raise(ask, self.AGENT, _body(f"own-{rid}", replaces=rid))
        row = ask.db.get_operator_queue_item(uid)
        assert row["status"] == "pending" and row["replaced_by"] is None


class TestSinkReplace:
    AGENT = "agent-3247-sink"

    @pytest.mark.asyncio
    async def test_a_replace_ends_the_old_shows_the_new_and_records_both(self, ask):
        old = _raise(ask, self.AGENT, _body("sr-old-1"))
        await _drain()                       # the predecessor's own announcement lands first
        ask.audit.clear(); ask.sent.clear()
        r = _raise(ask, self.AGENT, _body("sr-new-1", replaces="sr-old-1"))
        await _drain()
        assert set(r) == RECEIPT_KEYS
        assert r["status"] == "created" and r["replaces"] == "sr-old-1"
        assert r["replaced_by"] is None and r["disposed_by"] is None
        ended = ask.db.get_operator_queue_item(old["id"])
        assert (ended["status"], ended["disposition"], ended["disposed_by"],
                ended["disposition_reason"]) == ("cancelled", "cancelled", "agent", "replaced")
        assert ended["replaced_by"] == r["id"] and ended["disposed_by_email"] is None
        # the ending reached the observers with the row AS THE CAS LEFT IT
        [ev] = [e for e in ask.events if e.disposition == "cancelled"]
        assert ev.reason == "replaced" and ev.actor_email is None
        assert ev.rows[0]["id"] == old["id"] and ev.rows[0]["disposed_by"] == "agent"
        assert ev.rows[0]["replaced_by"] == r["id"]
        # two audit rows, ids and enums only; the `replaced` row is agent-keyed
        actions = {a["event_action"]: a for a in ask.audit}
        assert set(actions) == {"raised", "replaced"}
        assert actions["raised"]["details"]["replaces"] == old["id"]
        replaced = actions["replaced"]
        assert replaced["actor_agent_name"] == self.AGENT and replaced["target_id"] == old["id"]
        assert replaced["details"] == {"agent_name": self.AGENT, "request_id": "sr-old-1",
                                       "replaced_by": r["id"]}
        assert "actor_user" not in replaced
        # two thin triggers
        assert sorted(s["type"] for s in ask.sent) == ["operator_queue_cancelled", "operator_queue_new"]
        assert all(set(s["data"]) == {"id", "agent_name"} for s in ask.sent)

    def test_an_ending_the_agent_authored_wakes_nobody(self, ask):
        ask.state["opted_in"] = True
        _raise(ask, self.AGENT, _body("sw-old"))
        _raise(ask, self.AGENT, _body("sw-new", replaces="sw-old"))
        [ev] = [e for e in ask.events if e.disposition == "cancelled"]
        assert ev.rows[0]["disposed_by"] == "agent"      # the post-CAS row, not a hand-built one
        assert ask.wakes == []
        # the skip is the row's author, not the event: the same observer still
        # wakes for a person's cancel
        ask.svc._wake_filer(ask.svc.EndingEvent("cancelled", (
            {**ev.rows[0], "disposed_by": "person", "disposed_by_email": "op@example.com"},), "op@example.com"))
        assert len(ask.wakes) == 1

    def test_a_person_who_answered_first_wins_with_ids_and_enums_only(self, ask):
        old = _raise(ask, self.AGENT, _body("sa-old"))
        ask.db.respond_to_operator_queue_item(old["id"], "approve", "the secret words", None,
                                              "op-3247@example.com")
        with _Rejected(ask.svc, 409, "replaces_ended") as info:
            _raise(ask, self.AGENT, _body("sa-new", replaces="sa-old"))
        extra = info.value.extra
        assert extra == {"replaces": "sa-old", "ask_status": "responded", "disposition": "answered",
                         "disposed_at": extra["disposed_at"], "replaced_by": None}
        assert "secret" not in str(info.value.extra) and "secret" not in info.value.message
        assert "get_my_ask" in info.value.message
        assert ask.db.get_operator_queue_item_for_agent_by_request_id(self.AGENT, "sa-new") is None

    def test_a_cancelled_predecessor_names_the_operators_act(self, ask):
        old = _raise(ask, self.AGENT, _body("sc-old"))
        ask.db.cancel_operator_queue_item(old["id"], disposed_by_email="op-3247@example.com")
        with _Rejected(ask.svc, 409, "replaces_ended") as info:
            _raise(ask, self.AGENT, _body("sc-new", replaces="sc-old"))
        assert info.value.extra["disposition"] == "cancelled" and "operator" in info.value.message

    def test_an_already_replaced_predecessor_names_its_successor(self, ask):
        _raise(ask, self.AGENT, _body("sx-old"))
        _raise(ask, self.AGENT, _body("sx-mid", replaces="sx-old"))
        with _Rejected(ask.svc, 409, "replaces_ended") as info:
            _raise(ask, self.AGENT, _body("sx-new", replaces="sx-old"))
        assert info.value.extra["replaced_by"] == "sx-mid"
        assert "sx-mid" in info.value.message

    @pytest.mark.asyncio
    async def test_a_past_deadline_predecessor_is_expired_announced_then_refused(self, ask):
        ask.state["opted_in"] = True
        old = _raise(ask, self.AGENT, _body("se-old", expires_at=_iso(20)))
        # the clock moved past the deadline without a sweep
        from sqlalchemy import update
        from db.engine import get_engine
        from db.tables import operator_queue
        with get_engine().begin() as conn:
            conn.execute(update(operator_queue).where(operator_queue.c.id == old["id"])
                         .values(expires_at=_iso(-5)))
        await _drain()                       # the predecessor's `raised` row lands first
        ask.audit.clear()
        with _Rejected(ask.svc, 409, "replaces_ended") as info:
            _raise(ask, self.AGENT, _body("se-new", replaces="se-old"))
        await _drain()
        assert info.value.extra["disposition"] == "expired"
        assert "supersedes_expired" in info.value.message
        row = ask.db.get_operator_queue_item(old["id"])
        assert row["status"] == "expired" and row["disposed_by"] == "timeout"
        # the normal expiry event: observers, audit, and the ent#329 expiry wake
        [ev] = [e for e in ask.events if e.disposition == "expired"]
        assert ev.rows[0]["id"] == old["id"] and ev.actor_email is None
        assert [a["event_action"] for a in ask.audit] == ["expired"]
        assert [w[0] for w in ask.wakes] == ["ending"] and ask.wakes[0][2]["disposition"] == "expired"
        # and now the re-ask link works at once
        r = _raise(ask, self.AGENT, _body("se-again", supersedes_expired="se-old",
                                         proposal={"pay": 500, "to": "vendor-se-old"}))
        assert r["status"] == "created" and r["supersedes_expired"] == "se-old"

    def test_a_retry_replays_and_does_not_replace_again(self, ask):
        _raise(ask, self.AGENT, _body("rr-old"))
        first = _raise(ask, self.AGENT, _body("rr-new", replaces="rr-old"))
        _raise(ask, self.AGENT, _body("rr-later"))
        again = _raise(ask, self.AGENT, _body("rr-new", replaces="rr-later"))
        assert again["status"] == "replayed" and again["id"] == first["id"]
        assert "replaces" in again["differs"]
        assert ask.db.get_operator_queue_item_for_agent_by_request_id(self.AGENT, "rr-later")["status"] == "pending"
        same = _raise(ask, self.AGENT, _body("rr-new", replaces="rr-old"))
        assert "replaces" not in same["differs"]

    def test_a_file_channel_predecessor_is_replaceable(self, ask):
        """T7: ingest stamps `raised_by='agent'`; the terminal write-back selects
        a `cancelled` row, so the entry's status flips within a cycle."""
        from services.operator_queue_service import _FLIPPED_BY_WRITE_BACK
        uid = ask.db.create_operator_queue_item(
            self.AGENT, {"id": "file-old", "type": "approval", "title": "From the file",
                         "question": "?", "created_at": _iso()}, channel="file", raised_by="agent")
        r = _raise(ask, self.AGENT, _body("file-new", replaces="file-old"))
        assert r["replaces"] == "file-old"
        row = ask.db.get_operator_queue_item(uid)
        assert row["status"] in _FLIPPED_BY_WRITE_BACK and row["disposed_by"] == "agent"

    def test_a_replay_of_a_replaced_ask_says_who_ended_it(self, ask):
        _raise(ask, self.AGENT, _body("rp-old"))
        _raise(ask, self.AGENT, _body("rp-new", replaces="rp-old"))
        r = _raise(ask, self.AGENT, _body("rp-old"))
        assert r["status"] == "replayed"
        assert (r["disposition"], r["disposed_by"], r["replaced_by"]) == ("cancelled", "agent", "rp-new")

    @pytest.mark.asyncio
    async def test_the_readback_maps_both_links_to_request_ids(self, ask):
        from routers import operator_queue as r
        _raise(ask, self.AGENT, _body("rb-old"))
        _raise(ask, self.AGENT, _body("rb-new", replaces="rb-old"))
        assert {"replaces", "replaced_by"} <= set(r._READBACK_FIELDS)
        old = await r.get_my_ask("rb-old", name=self.AGENT)
        new = await r.get_my_ask("rb-new", name=self.AGENT)
        assert old["replaced_by"] == "rb-new" and old["replaces"] is None
        assert new["replaces"] == "rb-old" and new["replaced_by"] is None
        assert old["disposed_by"] == "agent"


class TestAlreadyPending:
    AGENT = "agent-3247-t8"

    def test_repeating_a_pending_proposal_is_refused_naming_the_ask(self, ask):
        _raise(ask, self.AGENT, _body("t8-first", proposal={"pay": 1, "to": "v"}))
        with _Rejected(ask.svc, 409, "already_pending") as info:
            _raise(ask, self.AGENT, _body("t8-second", proposal={"to": "v", "pay": 1}))
        assert info.value.extra == {"request_id": "t8-first"}
        assert ask.db.get_operator_queue_item_for_agent_by_request_id(self.AGENT, "t8-second") is None

    def test_replacing_that_ask_passes(self, ask):
        _raise(ask, self.AGENT, _body("t8-a", proposal={"pay": 2, "to": "v"}))
        r = _raise(ask, self.AGENT, _body("t8-b", proposal={"pay": 2, "to": "v"}, replaces="t8-a"))
        assert r["status"] == "created" and r["replaces"] == "t8-a"

    def test_replacing_a_different_ask_does_not_lift_the_guard(self, ask):
        _raise(ask, self.AGENT, _body("t8-c", proposal={"pay": 3, "to": "v"}))
        _raise(ask, self.AGENT, _body("t8-d", proposal={"pay": 4, "to": "v"}))
        with _Rejected(ask.svc, 409, "already_pending") as info:
            _raise(ask, self.AGENT, _body("t8-e", proposal={"pay": 3, "to": "v"}, replaces="t8-d"))
        assert info.value.extra["request_id"] == "t8-c"
        assert ask.db.get_operator_queue_item_for_agent_by_request_id(self.AGENT, "t8-d")["status"] == "pending"

    def test_an_ended_or_proposal_less_ask_is_not_compared(self, ask):
        old = _raise(ask, self.AGENT, _body("t8-f", proposal={"pay": 5, "to": "v"}))
        ask.db.cancel_operator_queue_item(old["id"], disposed_by_email="op@example.com")
        assert _raise(ask, self.AGENT, _body("t8-g", proposal={"pay": 5, "to": "v"}))["status"] == "created"
        _raise(ask, self.AGENT, _body("t8-h", proposal=None))
        assert _raise(ask, self.AGENT, _body("t8-i", proposal=None))["status"] == "created"

    def test_another_raisers_pending_proposal_is_not_the_agents(self, ask):
        ask.db.create_native_operator_queue_item(
            self.AGENT, {"id": "gate-t8-1", "type": "approval", "title": "g", "question": "?"},
            max_pending=None, channel="gate", raised_by="gate", to_role="primary",
            resolved_to=None, proposal={"pay": 6, "to": "v"}, supersedes_expired=None)
        assert _raise(ask, self.AGENT, _body("t8-j", proposal={"pay": 6, "to": "v"}))["status"] == "created"


# ===========================================================================
# 4. The pending line in the Execution Context (CP4, T4)
# ===========================================================================

def _aged(real_db, row_id, *, hours):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import update
    from db.engine import get_engine
    from db.tables import operator_queue
    at = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_engine().begin() as conn:
        conn.execute(update(operator_queue).where(operator_queue.c.id == row_id).values(created_at=at))


class TestPendingLine:
    AGENT = "agent-3247-line"

    def _line(self, prompt):
        return next((l for l in prompt.splitlines() if "Pending asks" in l), None)

    def _seed(self, real_db):
        old = _pending(real_db, self.AGENT, "pl-old", type="question",
                       title="Which vendor should receive the quarterly payout batch this time")
        _aged(real_db, old, hours=50)
        mid = _pending(real_db, self.AGENT, "pl-mid", title="Approve payout")
        _aged(real_db, mid, hours=3)
        _pending(real_db, self.AGENT, "pl-new", title="Approve\nnew `thing`", created_at=_iso(-5))
        # Not the agent's budget: a platform alarm, a gate row, an ended ask, another agent.
        _pending(real_db, self.AGENT, "queue-flood-agent-3247-line-1", title="flood")
        real_db.create_native_operator_queue_item(
            self.AGENT, {"id": "gate-pl-1", "type": "approval", "title": "gate", "question": "?"},
            max_pending=None, channel="gate", raised_by="gate", to_role="primary",
            resolved_to=None, proposal=None, supersedes_expired=None)
        gone = _pending(real_db, self.AGENT, "pl-gone")
        real_db.cancel_operator_queue_item(gone, disposed_by_email="op@example.com")
        _pending(real_db, "agent-3247-line-other", "pl-other")

    def test_the_line_lists_own_pending_asks_oldest_first_with_type_age_and_title(self, real_db):
        from services.platform_prompt_service import ExecutionContext, compose_system_prompt
        self._seed(real_db)
        line = self._line(compose_system_prompt(
            ExecutionContext(agent_name=self.AGENT, triggered_by="schedule")))
        assert line is not None
        assert line.index("pl-old") < line.index("pl-mid") < line.index("pl-new")
        assert 'pl-old (question, 2d) "Which vendor should receive the quarterly payou' in line
        assert 'pl-mid (approval, 3h) "Approve payout"' in line
        assert 'pl-new (approval, <1h) "Approve new' in line and "\n" not in line
        for absent in ("queue-flood", "gate-pl-1", "pl-gone", "pl-other"):
            assert absent not in line, absent
        assert "replaces" in line

    def test_the_line_is_drawn_from_the_budget_predicate(self, real_db):
        from services import platform_prompt_service as pps
        from services.operator_queue_service import _RESERVED_ID_PREFIXES
        self._seed(real_db)
        listed, total = pps._resolve_pending_asks(self.AGENT)
        assert len(listed) == total == real_db.count_operator_queue_pending_for_agent(
            self.AGENT, exclude_request_id_prefixes=_RESERVED_ID_PREFIXES) == 3

    def test_the_line_is_bounded_with_a_count_of_the_rest(self, real_db):
        from services.platform_prompt_service import (
            ExecutionContext, compose_system_prompt, MAX_PENDING_ASKS)
        agent = "agent-3247-line-many"
        for i in range(MAX_PENDING_ASKS + 3):
            _aged(real_db, _pending(real_db, agent, f"pm-{i:02d}"), hours=20 - i)
        line = self._line(compose_system_prompt(ExecutionContext(agent_name=agent, triggered_by="schedule")))
        assert line.count("(approval,") == MAX_PENDING_ASKS
        assert "pm-00" in line and f"pm-{MAX_PENDING_ASKS:02d}" not in line
        assert "and 3 more — list them with list_operator_queue" in line

    def test_no_pending_asks_no_line(self, real_db):
        from services.platform_prompt_service import ExecutionContext, compose_system_prompt
        prompt = compose_system_prompt(ExecutionContext(agent_name="agent-3247-line-none", triggered_by="schedule"))
        assert "## Execution Context" in prompt and "Pending asks" not in prompt

    def test_a_failed_read_omits_the_line_never_the_turn(self, real_db, monkeypatch):
        from services import platform_prompt_service as pps
        from services.platform_prompt_service import ExecutionContext, compose_system_prompt
        self._seed(real_db)

        def _boom(*a, **k):
            raise RuntimeError("db down")

        monkeypatch.setattr(pps.db, "list_pending_operator_queue_asks", _boom)
        prompt = compose_system_prompt(ExecutionContext(agent_name=self.AGENT, triggered_by="schedule"))
        assert "## Execution Context" in prompt and "Pending asks" not in prompt

    def test_the_pull_composer_carries_the_line(self, real_db):
        from services.pull_coordination_service import _compose_pull_system_prompt
        self._seed(real_db)
        prompt = _compose_pull_system_prompt(self.AGENT, "schedule", None, execution_id="e-3247")
        assert 'pl-mid (approval, 3h) "Approve payout"' in self._line(prompt)

    def test_the_push_composer_carries_the_line(self, real_db):
        from services.task_execution_service import TaskExecutionService
        self._seed(real_db)
        svc = TaskExecutionService.__new__(TaskExecutionService)
        prompt = svc._compose_effective_system_prompt(
            agent_name=self.AGENT, triggered_by="schedule", source_user_email=None,
            source_agent_name=None, source_mcp_key_name=None, model=None, timeout_seconds=None,
            attempt=None, schedule_context=None, execution_id="e-3247", system_prompt=None)
        assert 'pl-mid (approval, 3h) "Approve payout"' in self._line(prompt)

    def test_an_ending_the_agent_authored_reads_replaced(self, real_db):
        from services.platform_prompt_service import ExecutionContext, compose_system_prompt
        agent = "agent-3247-line-replaced"
        _native(real_db, agent, "lr-old")
        _native(real_db, agent, "lr-new", replaces=real_db.get_operator_queue_item_for_agent_by_request_id(
            agent, "lr-old")["id"])
        prompt = compose_system_prompt(ExecutionContext(agent_name=agent, triggered_by="schedule"))
        ended = next(l for l in prompt.splitlines() if "Ended asks" in l)
        assert "lr-old replaced" in ended and "lr-old cancelled" not in ended
        assert "lr-new (approval," in self._line(prompt)
