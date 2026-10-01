"""trinity-enterprise#638 — the seat-level decision record (R25).

Why a thing was approved, deferred or killed — lintable, seat-owned, expiring,
corrected by supersession, and the evidence base for the autonomy dial. The
properties, one per acceptance criterion:

1. a companion records for its seat over MCP (seat from `execution_id`, never
   sent; no email comes back) and the person reads / corrects / closes it in
   the Workspace, readers per ownership or the assignment provider's word;
2. the record carries the fields — outcome, decided, alternatives, criterion,
   who (role + person), date + review_by, what reverses it — prose only in notes;
3. prose-only entries are refused with a receipt; no alternatives is a note;
4. it expires at `review_by` (computed, never written); superseded rows stay;
5. the platform answers "reused" and "same criterion, no reversals" per ask class;
6. a direction decision is kept as `routed`, never as a seat decision.

Every behavioural test EXECUTES the path — the service over a real SQLite
file, the routers over stubbed `db` boundaries; the DDL / revision / registry
presence is pinned by text (their live consumer is the migration runner).
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
AGENT = "sales-companion"
SEAT = "gary@example.com"
OWNER = "owner@example.com"
TODAY = date(2026, 9, 22)
FUTURE = (TODAY + timedelta(days=90)).isoformat()


def _payload(**over):
    base = dict(
        outcome="approved", decided="Renew the Acme contract",
        alternatives=["let it lapse", "renegotiate first"],
        criterion="renewal cost below the switching cost",
        reversal="Acme raises the price above the switching cost",
        review_by=FUTURE, ask_class="vendor-renewal",
    )
    base.update(over)
    return base


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    """A real SQLite file behind the mixin; the facade forwards to it."""
    from sqlalchemy import create_engine
    import db.engine as engine_module
    import db.tables as tables
    import db.seat_decisions as sd
    engine = create_engine(f"sqlite:///{tmp_path / 'd.db'}")
    tables.seat_decisions.create(engine)
    monkeypatch.setattr(engine_module, "get_engine", lambda: engine)
    monkeypatch.setattr(sd, "get_engine", lambda: engine)
    return sd.SeatDecisionOperations()


@pytest.fixture
def svc(monkeypatch):
    from services import seat_decision_service as s
    monkeypatch.setattr(s, "_today", lambda: TODAY)
    return s


# ---------------------------------------------------------------------------
# 2 + 3 — the grammar and its receipts
# ---------------------------------------------------------------------------

class TestGrammar:
    def test_a_clean_record_validates_and_normalizes(self, svc):
        f = svc.validate_record(_payload(outcome="Approved ", ask_class="Vendor-Renewal", cites=["a", "a", "b"]))
        assert f["outcome"] == "approved" and f["ask_class"] == "vendor-renewal"
        assert f["alternatives"] == ["let it lapse", "renegotiate first"]
        assert f["cites"] == ["a", "b"] and f["scope"] == "seat"

    def test_no_alternatives_is_a_note_and_nothing_else_is_reported(self, svc):
        with pytest.raises(svc.DecisionRefused) as e:
            svc.validate_record(_payload(alternatives=[]))
        assert e.value.code == "decision_is_a_note" and e.value.status_code == 422
        assert "alternatives" in e.value.receipt["fields"]

    @pytest.mark.parametrize("field,value,fragment", [
        ("decided", "", "required"),
        ("criterion", "x" * 281, "over 280"),
        ("reversal", "first line\nsecond paragraph", "one line"),
        ("review_by", "tomorrow", "YYYY-MM-DD"),
        ("review_by", "2026-09-22", "after today"),
        ("review_by", "2028-01-01", "within 366"),
        ("outcome", "maybe", "one of"),
        ("scope", "global", "one of"),
        ("ask_class", "Vendor Renewal!", "slug"),
        ("alternatives", ["x" * 161], "over 160"),
    ])
    def test_prose_or_garbage_in_a_field_is_refused_naming_that_field(self, svc, field, value, fragment):
        with pytest.raises(svc.DecisionRefused) as e:
            svc.validate_record(_payload(**{field: value}))
        assert e.value.code == "decision_prose_only"
        assert fragment in e.value.receipt["fields"][field], e.value.receipt

    def test_every_failing_field_is_named_at_once(self, svc):
        with pytest.raises(svc.DecisionRefused) as e:
            svc.validate_record(_payload(decided="", criterion="", review_by="x"))
        assert set(e.value.receipt["fields"]) == {"decided", "criterion", "review_by"}

    def test_the_decided_option_is_not_also_an_alternative(self, svc):
        with pytest.raises(svc.DecisionRefused) as e:
            svc.validate_record(_payload(alternatives=["Renew the Acme contract"]))
        assert "alternatives" in e.value.receipt["fields"]

    def test_review_by_equal_to_today_is_expired_only_strictly_before(self, svc):
        row = {"status": "active", "review_by": TODAY.isoformat()}
        assert svc.effective_status(row, today=TODAY) == "active"
        assert svc.effective_status({"status": "active", "review_by": "2026-09-21"}, today=TODAY) == "expired"
        assert svc.effective_status({"status": "closed", "review_by": "2020-01-01"}, today=TODAY) == "closed"
        # a routed record is kept so it does not evaporate, not so it accretes
        assert svc.effective_status({"status": "routed", "review_by": "2020-01-01"}, today=TODAY) == "expired"
        assert svc.effective_status({"status": "routed", "review_by": FUTURE}, today=TODAY) == "routed"


# ---------------------------------------------------------------------------
# 1 + 4 + 6 — lifecycle over a real store
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_record_lowercases_the_seat_and_round_trips_json_lists(self, svc, real_db):
        row = svc.record(real_db, agent_name=AGENT, seat_email="Gary@Example.com",
                         decided_by_person="Gary@Example.com", payload=_payload(), source_execution_id="e1")
        assert row["seat_email"] == SEAT and row["decided_by_person"] == SEAT
        got = real_db.get_seat_decision(AGENT, row["id"])
        assert got["alternatives"] == ["let it lapse", "renegotiate first"] and got["cites"] == []
        assert got["status"] == "active" and got["source_execution_id"] == "e1"

    def test_a_direction_decision_is_kept_as_routed_never_active(self, svc, real_db):
        row = svc.record(real_db, agent_name=AGENT, seat_email=SEAT, decided_by_person=SEAT,
                         payload=_payload(scope="direction", decided="Raise prices 10%"))
        assert row["status"] == "routed" and row["scope"] == "direction"
        assert svc.to_human(row, today=TODAY)["status"] == "routed"
        # It is not a seat decision: the evidence ignores it.
        assert svc.stats([row], today=TODAY)["recorded"] == 0

    def test_correct_supersedes_and_history_stays(self, svc, real_db):
        old = svc.record(real_db, agent_name=AGENT, seat_email=SEAT, decided_by_person=SEAT, payload=_payload())
        new = svc.act(real_db, agent_name=AGENT, seat_email=SEAT, decision_id=old["id"], action="supersede",
                      by=SEAT, fields={"decided": "Renew Acme for one year"})
        assert new["supersedes_id"] == old["id"] and new["decided"] == "Renew Acme for one year"
        assert new["criterion"] == old["criterion"]                 # untouched fields carry over
        assert real_db.get_seat_decision(AGENT, old["id"])["status"] == "superseded"
        with pytest.raises(svc.DecisionRefused) as e:               # a second correction of the old row
            svc.act(real_db, agent_name=AGENT, seat_email=SEAT, decision_id=old["id"], action="supersede",
                    by=SEAT, fields={"decided": "again"})
        assert e.value.code == "decision_not_active" and e.value.status_code == 409

    def test_close_reverse_reconfirm_and_their_refusals(self, svc, real_db):
        a = svc.record(real_db, agent_name=AGENT, seat_email=SEAT, decided_by_person=SEAT, payload=_payload())
        with pytest.raises(svc.DecisionRefused) as e:
            svc.act(real_db, agent_name=AGENT, seat_email=SEAT, decision_id=a["id"], action="reverse", by=SEAT)
        assert e.value.code == "reason_required"
        r = svc.act(real_db, agent_name=AGENT, seat_email=SEAT, decision_id=a["id"], action="reconfirm",
                    by=SEAT, review_by=(TODAY + timedelta(days=200)).isoformat())
        assert r["review_by"] == (TODAY + timedelta(days=200)).isoformat() and r["reconfirmed_at"]
        r = svc.act(real_db, agent_name=AGENT, seat_email=SEAT, decision_id=a["id"], action="reverse",
                    by="Owner@Example.com", reason="Acme raised the price")
        assert r["status"] == "reversed" and r["closed_by"] == OWNER and r["close_reason"] == "Acme raised the price"
        with pytest.raises(svc.DecisionRefused) as e:
            svc.act(real_db, agent_name=AGENT, seat_email=SEAT, decision_id=a["id"], action="close", by=SEAT)
        assert e.value.code == "decision_not_active"
        b = svc.record(real_db, agent_name=AGENT, seat_email=SEAT, decided_by_person=SEAT, payload=_payload())
        assert svc.act(real_db, agent_name=AGENT, seat_email=SEAT, decision_id=b["id"], action="close",
                       by=SEAT)["status"] == "closed"

    def test_another_seats_id_is_the_uniform_not_found(self, svc, real_db):
        a = svc.record(real_db, agent_name=AGENT, seat_email="other@example.com",
                       decided_by_person="other@example.com", payload=_payload())
        with pytest.raises(svc.DecisionRefused) as e:
            svc.act(real_db, agent_name=AGENT, seat_email=SEAT, decision_id=a["id"], action="close", by=SEAT)
        assert e.value.code == "decision_not_found" and e.value.status_code == 404

    def test_request_id_must_name_a_decision_request_of_this_agent(self, svc, real_db, monkeypatch):
        items = {"q1": {"agent_name": AGENT}, "q2": {"agent_name": "someone-else"}}
        real_db.get_operator_queue_item = lambda i: items.get(i)
        ok = svc.record(real_db, agent_name=AGENT, seat_email=SEAT, decided_by_person=SEAT,
                        payload=_payload(request_id="q1"))
        assert ok["request_id"] == "q1"
        for bad in ("q2", "nope"):
            with pytest.raises(svc.DecisionRefused) as e:
                svc.record(real_db, agent_name=AGENT, seat_email=SEAT, decided_by_person=SEAT,
                           payload=_payload(request_id=bad))
            assert e.value.code == "unknown_request", bad

    def test_cites_must_be_this_seats_own_records_any_status(self, svc, real_db):
        a = svc.record(real_db, agent_name=AGENT, seat_email=SEAT, decided_by_person=SEAT, payload=_payload())
        svc.act(real_db, agent_name=AGENT, seat_email=SEAT, decision_id=a["id"], action="close", by=SEAT)
        other = svc.record(real_db, agent_name=AGENT, seat_email="other@example.com",
                           decided_by_person="other@example.com", payload=_payload())
        ok = svc.record(real_db, agent_name=AGENT, seat_email=SEAT, decided_by_person=SEAT,
                        payload=_payload(cites=[a["id"]]))
        assert ok["cites"] == [a["id"]]
        with pytest.raises(svc.DecisionRefused) as e:
            svc.record(real_db, agent_name=AGENT, seat_email=SEAT, decided_by_person=SEAT,
                       payload=_payload(cites=[other["id"]]))
        assert e.value.code == "unknown_citation"


# ---------------------------------------------------------------------------
# 5 — the evidence
# ---------------------------------------------------------------------------

class TestEvidence:
    def _row(self, i, **over):
        base = dict(id=f"d{i}", status="active", decided_at=f"2026-09-{10 + i:02d}T00:00:00Z",
                    review_by=FUTURE, criterion="renewal cost below the switching cost",
                    ask_class="vendor-renewal", cites=[])
        base.update(over)
        return base

    def test_reused_counts_only_citations_by_a_later_record(self, svc):
        rows = [self._row(1), self._row(2, cites=["d1"]), self._row(3, cites=["d9", "d3"])]
        s = svc.stats(rows, today=TODAY)
        assert s["recorded"] == 3 and s["reused"] == 1 and s["reuse_rate"] == round(1 / 3, 3)

    def test_superseding_does_not_implicitly_cite(self, svc):
        rows = [self._row(1, status="superseded"), self._row(2, supersedes_id="d1")]
        assert svc.stats(rows, today=TODAY)["reused"] == 0

    def test_stable_means_three_records_one_criterion_no_reversal_expired_excluded(self, svc):
        three = [self._row(i) for i in (1, 2, 3)]
        assert svc.stats(three, today=TODAY)["ask_classes"][0]["stable"] is True
        two_plus_expired = [self._row(1), self._row(2), self._row(3, review_by="2026-01-01")]
        cls = svc.stats(two_plus_expired, today=TODAY)["ask_classes"][0]
        assert cls["stable"] is False and cls["expired"] == 1
        paraphrased = three[:2] + [self._row(3, criterion="Renewal cost below the switching cost.")]
        assert svc.stats(paraphrased, today=TODAY)["ask_classes"][0]["stable"] is True   # case/punctuation fold
        different = three[:2] + [self._row(3, criterion="cheapest vendor wins")]
        cls = svc.stats(different, today=TODAY)["ask_classes"][0]
        assert cls["stable"] is False and len(cls["criteria"]) == 2
        reversed_one = three[:2] + [self._row(3, status="reversed")]
        cls = svc.stats(reversed_one, today=TODAY)["ask_classes"][0]
        assert cls["stable"] is False and cls["reversals"] == 1

    def test_the_prompt_block_lists_active_records_criterion_first_bounded_and_email_free(self, svc, monkeypatch):
        rows = [self._row(i, outcome="approved", decided=f"thing {i}", reversal="r",
                          decided_by_person=SEAT) for i in range(1, 20)]
        rows.append(self._row(99, status="closed", decided="closed thing"))
        fake = SimpleNamespace(list_seat_decisions=lambda a, e, limit=500: rows)
        block = svc.prompt_block(fake, AGENT, SEAT, today=TODAY)
        assert block.startswith("## This seat's standing decisions")
        assert "because renewal cost below the switching cost" in block
        assert "closed thing" not in block and SEAT not in block
        assert block.count("\n- [") <= svc.PROMPT_BLOCK_MAX + 1
        assert svc.prompt_block(SimpleNamespace(list_seat_decisions=lambda *a, **k: []), AGENT, SEAT) is None
        assert svc.prompt_block(SimpleNamespace(list_seat_decisions=lambda *a, **k: 1 / 0), AGENT, SEAT) is None

    def test_the_memory_block_carries_the_decisions_on_every_caller(self, monkeypatch):
        from services import platform_prompt_service as pps
        monkeypatch.setattr(pps, "_seat_decisions_block", lambda rec: "## This seat's standing decisions\n- [d1] x")
        block = pps.format_user_memory_block({"agent_name": AGENT, "user_email": SEAT, "agent_notes": "", "conversation_summary": ""})
        assert "standing decisions" in block
        monkeypatch.setattr(pps, "_seat_decisions_block", lambda rec: None)
        assert pps.format_user_memory_block({"agent_name": AGENT, "user_email": SEAT}) is None


# ---------------------------------------------------------------------------
# 1 — readers and the two shapes
# ---------------------------------------------------------------------------

class TestReaders:
    def test_the_agent_shape_never_carries_an_email(self, svc):
        row = dict(id="d1", seat_email=SEAT, decided_by_person=SEAT, closed_by=OWNER, outcome="approved",
                   decided="x", alternatives=["y"], criterion="c", reversal="r", decided_at="2026-09-20T00:00:00Z",
                   review_by=FUTURE, status="active", cites=[])
        shaped = svc.to_agent(row, seat_email=SEAT, today=TODAY)
        assert shaped["decided_by"] == {"role": None, "person": "seat"}
        assert "closed_by" not in shaped and "seat_email" not in shaped
        assert SEAT not in str(shaped) and OWNER not in str(shaped)
        row["decided_by_person"] = OWNER
        assert svc.to_agent(row, seat_email=SEAT)["decided_by"]["person"] == "owner"

    def test_without_a_provider_only_the_own_seat_is_readable(self, svc, monkeypatch):
        from services import assignment_provider as ap
        monkeypatch.setattr(ap, "_provider", None)
        fake = SimpleNamespace(list_seat_decision_seats=lambda a, limit=50: [SEAT, "ops@example.com"])
        assert svc.readable_seats(fake, AGENT, SEAT, is_owner=False) == {SEAT: True}

    def test_the_owner_reads_every_seat_writable_and_a_provider_kind_reads_them_read_only(self, svc, monkeypatch):
        from services import assignment_provider as ap
        fake = SimpleNamespace(list_seat_decision_seats=lambda a, limit=50: [SEAT, "ops@example.com"])
        assert svc.readable_seats(fake, AGENT, SEAT, is_owner=True) == {SEAT: True, "ops@example.com": True}

        class Provider:
            def assignment_for(self, a, t): return None
            def kinds_for(self, a, reader): return {"kind": "approver", "role_id": "sales-lead"}
        monkeypatch.setattr(ap, "_provider", Provider())
        assert svc.readable_seats(fake, AGENT, SEAT, is_owner=False) == {SEAT: True, "ops@example.com": False}

    def test_a_provider_without_kinds_for_a_raise_or_a_bad_kind_reads_as_none(self, svc, monkeypatch):
        from services import assignment_provider as ap

        class Old:
            def assignment_for(self, a, t): return None
        monkeypatch.setattr(ap, "_provider", Old())
        assert svc.reader_kind(AGENT, SEAT) is None

        class Raises:
            def kinds_for(self, a, r): raise RuntimeError("boom")
        monkeypatch.setattr(ap, "_provider", Raises())
        assert svc.reader_kind(AGENT, SEAT) is None

        class Bad:
            def kinds_for(self, a, r): return {"kind": "god"}
        monkeypatch.setattr(ap, "_provider", Bad())
        assert svc.reader_kind(AGENT, SEAT) is None


# ---------------------------------------------------------------------------
# 1 — the two routers
# ---------------------------------------------------------------------------

def _execution(**over):
    base = dict(id="e1", agent_name=AGENT, triggered_by="schedule", source_user_email=None,
                source_channel="portal", source_channel_chat_id="s", source_channel_client=SEAT,
                schedule_id="sch-1")
    base.update(over)
    return SimpleNamespace(**base)


class TestAgentRouter:
    @pytest.fixture
    def wired(self, monkeypatch, svc):
        from routers import seat_decisions as r
        from services import idempotency_service as idem
        store = {}
        monkeypatch.setattr(r.db, "get_execution", lambda eid: store.get("execution"))
        monkeypatch.setattr(r, "assert_agent_access", lambda *a, **k: None)
        monkeypatch.setattr(r.rate_limiter, "enforce", lambda *a, **k: None)
        monkeypatch.setattr(r, "resolve_assignment", lambda a, t: {"role_id": "sales-lead"})
        recorded = []
        monkeypatch.setattr(svc, "record", lambda db, **kw: recorded.append(kw) or {
            "id": "d1", "seat_email": kw["seat_email"], "decided_by_person": kw["decided_by_person"],
            "outcome": "approved", "decided": "x", "alternatives": ["y"], "criterion": "c", "reversal": "r",
            "decided_at": "2026-09-22T00:00:00Z", "review_by": FUTURE, "status": "active", "cites": [],
            "decided_by_role": kw["payload"].get("decided_by_role"),
        })
        monkeypatch.setattr(idem, "begin", lambda scope, key: SimpleNamespace(enabled=False, replay=False, in_flight=False))
        monkeypatch.setattr(idem, "complete", lambda *a, **k: None)
        monkeypatch.setattr(idem, "fail", lambda *a, **k: None)
        return r, store, recorded

    def _body(self, **over):
        from models import RecordDecisionRequest
        return RecordDecisionRequest(execution_id="e1", **_payload(**over))

    def test_a_seat_run_records_for_its_seat_with_the_providers_role_and_no_email_back(self, wired):
        r, store, recorded = wired
        store["execution"] = _execution()
        out = asyncio.run(r.record_seat_decision(AGENT, self._body(), current_user=SimpleNamespace(id=1)))
        assert recorded[0]["seat_email"] == SEAT and recorded[0]["decided_by_person"] == SEAT
        assert recorded[0]["payload"]["decided_by_role"] == "sales-lead"
        assert recorded[0]["source_execution_id"] == "e1"
        assert out["success"] and out["decision"]["decided_by"] == {"role": "sales-lead", "person": "seat"}
        assert SEAT not in str(out)

    def test_a_user_facing_turn_uses_the_verified_email_and_other_triggers_are_refused(self, wired):
        r, store, recorded = wired
        store["execution"] = _execution(triggered_by="public", schedule_id=None, source_channel_client=None,
                                        source_user_email="Client@Example.com")
        asyncio.run(r.record_seat_decision(AGENT, self._body(), current_user=None))
        assert recorded[-1]["seat_email"] == "client@example.com"
        store["execution"] = _execution(triggered_by="mcp", schedule_id=None, source_channel_client=None)
        with pytest.raises(HTTPException) as e:
            asyncio.run(r.record_seat_decision(AGENT, self._body(), current_user=None))
        assert e.value.status_code == 422 and e.value.detail["code"] == "no_seat"

    def test_a_foreign_execution_is_403_and_a_missing_one_404(self, wired):
        r, store, _ = wired
        store["execution"] = _execution(agent_name="someone-else")
        with pytest.raises(HTTPException) as e:
            asyncio.run(r.record_seat_decision(AGENT, self._body(), current_user=None))
        assert e.value.status_code == 403
        store["execution"] = None
        with pytest.raises(HTTPException) as e:
            asyncio.run(r.record_seat_decision(AGENT, self._body(), current_user=None))
        assert e.value.status_code == 404

    def test_a_refusal_is_the_named_receipt_and_the_idempotency_claim_is_released(self, wired, monkeypatch, svc):
        r, store, _ = wired
        from services import idempotency_service as idem
        released = []
        monkeypatch.setattr(idem, "begin", lambda scope, key: SimpleNamespace(enabled=True, replay=False, in_flight=False))
        monkeypatch.setattr(idem, "fail", lambda d: released.append(d))
        monkeypatch.setattr(svc, "record", lambda db, **kw: (_ for _ in ()).throw(
            svc.DecisionRefused("decision_prose_only", "prose", receipt={"fields": {"criterion": "one line"}})))
        store["execution"] = _execution()
        with pytest.raises(HTTPException) as e:
            asyncio.run(r.record_seat_decision(AGENT, self._body(), current_user=None, idempotency_key="k"))
        assert e.value.status_code == 422 and e.value.detail["code"] == "decision_prose_only"
        assert e.value.detail["receipt"]["fields"] == {"criterion": "one line"}
        assert released

    def test_a_replayed_key_returns_the_snapshot_and_records_nothing(self, wired, monkeypatch):
        r, store, recorded = wired
        from services import idempotency_service as idem
        monkeypatch.setattr(idem, "begin", lambda scope, key: SimpleNamespace(
            enabled=True, replay=True, in_flight=False, snapshot={"success": True, "decision": {"id": "d1"}}))
        store["execution"] = _execution()
        out = asyncio.run(r.record_seat_decision(AGENT, self._body(), current_user=None, idempotency_key="k"))
        assert out["replayed"] is True and out["decision"]["id"] == "d1" and recorded == []

    def test_list_returns_active_only_by_default_email_free_with_stats(self, wired, monkeypatch):
        r, store, _ = wired
        store["execution"] = _execution()
        rows = [dict(id="d1", seat_email=SEAT, decided_by_person=SEAT, outcome="approved", decided="x",
                     alternatives=["y"], criterion="c", reversal="r", decided_at="2026-09-20T00:00:00Z",
                     review_by=FUTURE, status="active", cites=[], ask_class="k"),
                dict(id="d0", seat_email=SEAT, decided_by_person=OWNER, outcome="killed", decided="z",
                     alternatives=["y"], criterion="c", reversal="r", decided_at="2026-09-19T00:00:00Z",
                     review_by=FUTURE, status="closed", cites=[])]
        monkeypatch.setattr(r.db, "list_seat_decisions", lambda a, e, limit=500: rows)
        out = asyncio.run(r.list_seat_decisions(AGENT, execution_id="e1", current_user=None))
        assert [d["id"] for d in out["decisions"]] == ["d1"] and out["stats"]["recorded"] == 2
        assert OWNER not in str(out) and SEAT not in str(out)
        out = asyncio.run(r.list_seat_decisions(AGENT, execution_id="e1", include_history=True, current_user=None))
        assert [d["id"] for d in out["decisions"]] == ["d1", "d0"]


class TestPortalRouter:
    @pytest.fixture
    def wired(self, monkeypatch, svc):
        from client_portal import seat_decisions as ps
        from client_portal import role_card as rc
        owners = {"owner@example.com"}
        monkeypatch.setattr(rc, "_is_owner", lambda a, e, p: p and e in owners)
        from services import assignment_provider as ap
        monkeypatch.setattr(ap, "_provider", None)
        rows = {}
        monkeypatch.setattr(ps.db, "list_seat_decision_seats", lambda a, limit=50: sorted({r["seat_email"] for r in rows.values()}))
        monkeypatch.setattr(ps.db, "list_seat_decisions", lambda a, e=None, limit=500: [r for r in rows.values() if e is None or r["seat_email"] == e])
        monkeypatch.setattr(ps.db, "get_seat_decision", lambda a, i: rows.get(i))
        def _insert(values):
            row = {"id": f"d{len(rows) + 1}", "status": "active", "cites": [], **values}
            rows[row["id"]] = row
            return row
        monkeypatch.setattr(ps.db, "insert_seat_decision", _insert)
        def _status(a, i, status, *, reason, by):
            rows[i].update(status=status, close_reason=reason, closed_by=by, closed_at="now"); return True
        monkeypatch.setattr(ps.db, "set_seat_decision_status", _status)
        return ps, rows

    def test_own_seat_records_reads_and_acts_but_not_on_another_seat(self, wired):
        ps, rows = wired
        mine = ps.record(AGENT, SEAT, is_platform=False, payload=_payload())
        assert mine["decision"]["writable"] and mine["decision"]["decided_by"]["person"] == SEAT
        ps.record(AGENT, "ops@example.com", is_platform=False, payload=_payload(decided="Ops thing"))
        page = ps.page(AGENT, SEAT, is_platform=False)
        assert page["my_seat"] == SEAT and page["seats"] == [SEAT]
        assert [d["decided"] for d in page["decisions"]] == ["Renew the Acme contract"]
        other_id = next(i for i, r in rows.items() if r["seat_email"] == "ops@example.com")
        with pytest.raises(ps.svc.DecisionRefused) as e:
            ps.act(AGENT, SEAT, is_platform=False, decision_id=other_id, action="close", reason=None, review_by=None, fields=None)
        assert e.value.status_code == 404                              # uniform: learns nothing
        with pytest.raises(ps.svc.DecisionRefused) as e:
            ps.record(AGENT, SEAT, is_platform=False, payload=_payload(seat="ops@example.com"))
        assert e.value.code == "seat_not_yours" and e.value.status_code == 403

    def test_the_owner_sees_every_seat_writable_and_may_record_for_a_named_seat(self, wired):
        ps, rows = wired
        ps.record(AGENT, SEAT, is_platform=False, payload=_payload())
        out = ps.record(AGENT, "owner@example.com", is_platform=True, payload=_payload(seat="ops@example.com", decided="Ops thing"))
        assert out["decision"]["seat"] == "ops@example.com" and out["decision"]["decided_by"]["person"] == "owner@example.com"
        page = ps.page(AGENT, "owner@example.com", is_platform=True)
        assert set(page["seats"]) == {"owner@example.com", SEAT, "ops@example.com"}
        assert all(d["writable"] for d in page["decisions"])
        closed = ps.act(AGENT, "owner@example.com", is_platform=True, decision_id="d1", action="close",
                        reason="no longer live", review_by=None, fields=None)
        assert closed["decision"]["status"] == "closed" and closed["decision"]["closed_by"] == "owner@example.com"

    def test_a_platform_user_who_is_not_the_owner_is_not_an_owner(self, wired):
        ps, rows = wired
        ps.record(AGENT, "ops@example.com", is_platform=False, payload=_payload())
        page = ps.page(AGENT, "shared@example.com", is_platform=True)
        assert page["seats"] == ["shared@example.com"] and page["decisions"] == []

    def test_a_direction_decision_returns_the_canon_hint(self, wired):
        ps, _ = wired
        out = ps.record(AGENT, SEAT, is_platform=False, payload=_payload(scope="direction"))
        assert out["decision"]["status"] == "routed" and "canon" in out["hint"]


class TestPortalRoutes:
    """The route layer itself: roster gate first, receipts mapped to HTTP."""

    @pytest.fixture
    def wired(self, monkeypatch):
        from client_portal import router as pr
        calls = []
        monkeypatch.setattr(pr, "_require_roster", lambda a, e, p=False: calls.append((a, e)))
        from services import rate_limiter
        monkeypatch.setattr(rate_limiter, "enforce", lambda *a, **k: None)
        return pr, calls

    def _principal(self):
        return SimpleNamespace(email=SEAT, is_platform=False)

    def test_record_is_roster_gated_and_a_refusal_is_the_receipt_as_http(self, wired, monkeypatch):
        pr, calls = wired
        from client_portal.models import PortalSeatDecisionRecord
        monkeypatch.setattr(pr.seat_decisions, "record", lambda *a, **k: (_ for _ in ()).throw(
            pr.seat_decisions.svc.DecisionRefused("decision_is_a_note", "a note", receipt={"fields": {"alternatives": "x"}})))
        body = PortalSeatDecisionRecord(**_payload(alternatives=[]))
        with pytest.raises(HTTPException) as e:
            pr.portal_seat_decision_record(AGENT, body, principal=self._principal())
        assert calls == [(AGENT, SEAT)]
        assert e.value.status_code == 422 and e.value.detail["code"] == "decision_is_a_note"
        assert e.value.detail["receipt"]["fields"] == {"alternatives": "x"}

    def test_actions_are_roster_gated_and_forward_the_body(self, wired, monkeypatch):
        pr, calls = wired
        from client_portal.models import PortalSeatDecisionAction
        seen = {}
        monkeypatch.setattr(pr.seat_decisions, "act", lambda a, e, **kw: seen.update(kw) or {"decision": None, "hint": None})
        pr.portal_seat_decision_act(AGENT, "d1", PortalSeatDecisionAction(action="reverse", reason="changed"),
                                    principal=self._principal())
        assert calls == [(AGENT, SEAT)]
        assert seen["decision_id"] == "d1" and seen["action"] == "reverse" and seen["reason"] == "changed"

    def test_the_page_is_roster_gated(self, wired, monkeypatch):
        pr, calls = wired
        monkeypatch.setattr(pr.seat_decisions, "page", lambda a, e, **kw: {"agent_name": a, "my_seat": e, "seats": [e],
                                                                            "decisions": [], "stats": {}, "can_record": True})
        out = pr.portal_seat_decisions(AGENT, principal=self._principal())
        assert calls == [(AGENT, SEAT)] and out["my_seat"] == SEAT


# ---------------------------------------------------------------------------
# both migration tracks, the registry, the facade
# ---------------------------------------------------------------------------

def test_the_table_is_on_both_tracks_and_in_the_cleanup_registry():
    schema = (REPO / "src/backend/db/schema.py").read_text()
    tables = (REPO / "src/backend/db/tables.py").read_text()
    mig = (REPO / "src/backend/db/migrations.py").read_text()
    cleanup = (REPO / "src/backend/db/agent_cleanup.py").read_text()
    rev = (REPO / "src/backend/migrations/versions/0071_seat_decisions.py").read_text()
    assert "CREATE TABLE IF NOT EXISTS seat_decisions" in schema
    assert "idx_seat_decisions_seat" in schema and "idx_seat_decisions_review" in schema
    assert "seat_decisions = Table(" in tables
    assert '("seat_decisions_table", _migrate_seat_decisions_table)' in mig
    assert 'AgentRef("seat_decisions"' in cleanup
    assert 'down_revision = "0070_metric_points"' in rev     # re-parented onto dev's head by the merge-train
    assert 'has_table("seat_decisions")' in rev and "IF NOT EXISTS idx_seat_decisions_seat" in rev


def test_the_migration_graph_still_has_exactly_one_head():
    import subprocess
    out = subprocess.run(["python3", str(REPO / "scripts/ci/check_alembic_heads.py"),
                          str(REPO / "src/backend/migrations/versions")], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr


def test_the_facade_forwards_every_parameter_of_every_mixin_method():
    """learnings 2026-09-01: a kwarg the mixin gains must land on the facade."""
    from database import DatabaseManager
    from db.seat_decisions import SeatDecisionOperations
    for name, fn in inspect.getmembers(SeatDecisionOperations, inspect.isfunction):
        if name.startswith("_"):
            continue
        assert list(inspect.signature(getattr(DatabaseManager, name)).parameters) == \
            list(inspect.signature(fn).parameters), name


def test_the_router_declares_its_mcp_surface_and_registers_before_the_catch_all():
    src = (REPO / "src/backend/routers/seat_decisions.py").read_text()
    assert src.startswith("# mcp: decisions.ts")
    main = (REPO / "src/backend/main.py").read_text()
    assert "app.include_router(seat_decisions_router)" in main
