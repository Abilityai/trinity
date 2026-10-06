"""trinity-enterprise#814 — a shared companion serves the seat of the person each run is for.

R26 lets one companion serve several people; the one-primary rule stays. So the
seat is resolved per run, from the served person's own assignment row:

* ``assignment_provider.resolve_served_seat(agent, email)`` — the person's own
  seat; the agent's own seat when it holds one (holds wins, ruling 2026-10-06);
  the primary's as the fallback, which is also the whole answer for a provider
  that predates ``served_seat_for``. Never raises.
* A seat decision (ent#638) is stamped with that seat, not the primary's.
* The prompt keeps its `Primary human` line exactly as it was and adds a
  `Seat this run serves` line only when the run serves a different seat — for a
  chat user or for the Workspace a scheduled brief is delivered to.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from services import assignment_provider as ap
from services import platform_prompt_service as pps
from services.platform_prompt_service import ExecutionContext, compose_system_prompt

pytestmark = pytest.mark.unit

AGENT = "shared-companion"
ANN, BOB, CAT = "ann@example.com", "bob@example.com", "cat@example.com"


@pytest.fixture(autouse=True)
def _clean_provider():
    ap.clear_provider()
    yield
    ap.clear_provider()


class _Roster:
    """Ann is the primary (cfo); Bob holds head-of-sales; Cat has a row and no seat."""

    def __init__(self, *, held=None, served=True, raises=False, answer=None):
        self.held, self.served, self.raises, self.answer = held, served, raises, answer
        self.asked = []
        if not served:
            self.served_seat_for = None

    def assignment_for(self, agent_name, triggered_by):
        if triggered_by in ("public", "paid"):
            return None
        return {"primary_user_display": "Ann", "role_id": "cfo", "proactive_consent": False}

    def seat_for(self, agent_name):
        if self.held:
            return {"case": "holds", "role_id": self.held, "seats": ["cfo", "head-of-sales"]}
        return {"case": "serves", "role_id": "cfo", "seats": ["cfo", "head-of-sales"]}

    def served_seat_for(self, agent_name, person_email):  # noqa: F811 — nulled when served=False
        self.asked.append(person_email)
        if self.raises:
            raise RuntimeError("boom")
        if self.answer is not None:
            return self.answer
        if self.held:
            return {"role_id": self.held, "source": "holds"}
        own = {BOB: "head-of-sales"}.get(person_email)
        return {"role_id": own, "source": "person"} if own else {"role_id": "cfo", "source": "primary"}


def _register(**kw):
    roster = _Roster(**kw)
    ap.register_provider(roster)
    return roster


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------

def test_the_served_persons_own_seat_wins_over_the_primarys():
    roster = _register()
    assert ap.resolve_served_seat(AGENT, " Bob@Example.com ") == {"role_id": "head-of-sales", "source": "person"}
    assert roster.asked == [BOB], "the email reaches the provider case-folded"


def test_a_person_with_no_seat_of_their_own_gets_the_primarys():
    _register()
    assert ap.resolve_served_seat(AGENT, CAT) == {"role_id": "cfo", "source": "primary"}


def test_an_agent_that_holds_a_seat_serves_it_whoever_the_run_is_for():
    _register(held="orchestrator")
    assert ap.resolve_served_seat(AGENT, BOB) == {"role_id": "orchestrator", "source": "holds"}


@pytest.mark.parametrize("kw", [
    {"served": False}, {"raises": True}, {"answer": {"role_id": 7, "source": "person"}},
    {"answer": {"role_id": None, "source": "person"}}, {"answer": "cfo"},
    {"answer": {"role_id": "x", "source": "someone"}},
], ids=["predates-method", "raises", "non-str-role", "person-without-role", "not-a-dict", "unknown-source"])
def test_every_degraded_answer_falls_back_to_the_primarys_seat(kw):
    _register(**kw)
    assert ap.resolve_served_seat(AGENT, BOB) == {"role_id": "cfo", "source": "primary"}


def test_no_person_or_no_provider_is_the_trigger_free_answer():
    assert ap.resolve_served_seat(AGENT, BOB) == {"role_id": None, "source": "none"}
    roster = _register()
    assert ap.resolve_served_seat(AGENT, None) == {"role_id": "cfo", "source": "primary"}
    assert ap.resolve_served_seat(AGENT, "") == {"role_id": "cfo", "source": "primary"}
    assert roster.asked == [], "no person, no per-person read"


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------

@pytest.fixture
def quiet_db():
    fake = MagicMock()
    fake.get_setting_value = MagicMock(return_value=None)
    fake.get_permitted_agents = MagicMock(return_value=[])
    fake.list_recent_operator_queue_endings = MagicMock(return_value=[])
    with patch.object(pps, "db", fake):
        yield fake


def _block(**ctx):
    return compose_system_prompt(ExecutionContext(agent_name=AGENT, **ctx))


def test_a_chat_with_bob_names_bobs_seat_and_leaves_the_primary_line_alone(quiet_db):
    _register()
    out = _block(triggered_by="chat", source_user_email=BOB)
    assert "- **Primary human**: Ann (role: cfo)" in out
    assert "- **Seat this run serves**: head-of-sales" in out


def test_a_brief_delivered_to_bobs_workspace_names_bobs_seat_without_his_email(quiet_db):
    _register()
    out = _block(triggered_by="schedule", served_person_email=BOB)
    assert "- **Seat this run serves**: head-of-sales" in out
    assert BOB not in out, "the delivery address picks the seat; it is never rendered"


@pytest.mark.parametrize("ctx", [
    {"triggered_by": "chat", "source_user_email": ANN},
    {"triggered_by": "chat", "source_user_email": CAT},
    {"triggered_by": "schedule"},
], ids=["the-primary", "no-seat-of-their-own", "no-person"])
def test_no_second_line_when_the_run_serves_the_primarys_seat(quiet_db, ctx):
    _register()
    out = _block(**ctx)
    assert "- **Primary human**: Ann (role: cfo)" in out
    assert "Seat this run serves" not in out


def test_a_suppressed_audience_still_renders_nothing(quiet_db):
    roster = _register()
    out = _block(triggered_by="public", source_user_email=BOB)
    assert "Primary human" not in out and "Seat this run serves" not in out
    assert roster.asked == [], "no assignment answer, no per-person read"


def test_an_agent_that_holds_a_seat_says_so(quiet_db):
    _register(held="orchestrator")
    out = _block(triggered_by="chat", source_user_email=BOB)
    assert "- **Seat this run serves**: orchestrator" in out


def test_a_raising_provider_still_yields_the_block(quiet_db):
    _register(raises=True)
    out = _block(triggered_by="chat", source_user_email=BOB)
    assert "- **Primary human**: Ann (role: cfo)" in out
    assert "Seat this run serves" not in out


def test_a_scheduled_seat_run_reads_its_delivery_address_off_the_row(monkeypatch):
    from services import task_execution_service as tes

    row = SimpleNamespace(triggered_by="schedule", schedule_id="sch-1", source_channel="portal",
                          source_channel_client="Bob@Example.com")
    reads = []
    monkeypatch.setattr(tes.db, "get_execution", lambda eid: reads.append(eid) or row)
    assert tes._served_person_for("e1", "schedule") == BOB
    assert tes._served_person_for("e1", "chat") is None, "a chat turn names its user itself"
    assert tes._served_person_for(None, "schedule") is None
    assert reads == ["e1"], "one row read, and only for a schedule fire"


# ---------------------------------------------------------------------------
# Seat decisions (ent#638)
# ---------------------------------------------------------------------------

@pytest.fixture
def wired(monkeypatch):
    from routers import seat_decisions as r
    from services import idempotency_service as idem
    from services import seat_decision_service as sds

    store = {}
    monkeypatch.setattr(r.db, "get_execution", lambda eid: store.get("execution"))
    monkeypatch.setattr(r, "assert_agent_access", lambda *a, **k: None)
    monkeypatch.setattr(r.rate_limiter, "enforce", lambda *a, **k: None)
    recorded = []
    monkeypatch.setattr(sds, "record", lambda db, **kw: recorded.append(kw) or {
        "id": "d1", "seat_email": kw["seat_email"], "decided_by_person": kw["decided_by_person"],
        "outcome": "approved", "decided": "x", "alternatives": ["y"], "criterion": "c", "reversal": "r",
        "decided_at": "2026-10-06T00:00:00Z", "review_by": "2027-01-01", "status": "active", "cites": [],
        "decided_by_role": kw["payload"].get("decided_by_role"),
    })
    monkeypatch.setattr(idem, "begin", lambda scope, key: SimpleNamespace(enabled=False, replay=False, in_flight=False))
    monkeypatch.setattr(idem, "complete", lambda *a, **k: None)
    monkeypatch.setattr(idem, "fail", lambda *a, **k: None)
    return r, store, recorded


def _record(r, store, email, **body):
    from models import RecordDecisionRequest

    store["execution"] = SimpleNamespace(
        id="e1", agent_name=AGENT, triggered_by="schedule", source_user_email=None,
        source_channel="portal", source_channel_chat_id="s", source_channel_client=email,
        schedule_id="sch-1")
    req = RecordDecisionRequest(
        execution_id="e1", outcome="approved", decided="Renew the Acme contract",
        alternatives=["let it lapse"], criterion="margin above 20%", reversal="margin drops",
        review_by="2027-01-01", **body)
    return asyncio.run(r.record_seat_decision(AGENT, req, current_user=SimpleNamespace(id=1)))


def test_a_decision_for_bob_is_stamped_with_bobs_seat_not_the_primarys(wired):
    r, store, recorded = wired
    _register()
    out = _record(r, store, BOB)
    assert recorded[0]["seat_email"] == BOB
    assert recorded[0]["payload"]["decided_by_role"] == "head-of-sales"
    assert out["decision"]["decided_by"]["role"] == "head-of-sales"


def test_the_agents_word_does_not_override_the_seat_on_record(wired):
    r, store, recorded = wired
    _register()
    _record(r, store, BOB, decided_by_role="cfo")
    assert recorded[0]["payload"]["decided_by_role"] == "head-of-sales"


def test_a_person_without_a_seat_gets_the_primarys_and_no_record_at_all_keeps_the_agents(wired):
    r, store, recorded = wired
    _register()
    _record(r, store, CAT)
    assert recorded[-1]["payload"]["decided_by_role"] == "cfo"

    ap.clear_provider()
    _record(r, store, CAT, decided_by_role="named-by-agent")
    assert recorded[-1]["payload"]["decided_by_role"] == "named-by-agent"
