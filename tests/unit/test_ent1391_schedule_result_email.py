"""Each run of a schedule mailed to the person who asked (abilityai/trinity-enterprise#1391).

The first session's email offer, made real. Pinned by executing the code:

- `enable` mails the latest finished run to the caller's own address and only
  then keeps it, so a setting that never delivered is never a "working" channel;
  a mail that did not go refuses with a named reason and keeps nothing;
- the mail goes through the instance's provider, or the relay as the structured
  `run_result` kind (the gateway sends that kind only to its own address for
  the instance);
- the terminal chokepoint `report_completion` mails a run whose schedule asks
  for it, also when the run has no channel context, and nothing otherwise;
- the route takes no address: it uses the caller's own, and refuses one with none.

Module: src/backend/services/schedule_result_email.py
"""
import asyncio
import os
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@127.0.0.1:1")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services import schedule_result_email as sre  # noqa: E402
from services import channel_completion_report as ccr  # noqa: E402
from routers import schedules as routes  # noqa: E402  (imported before any module is stubbed)
from models import ScheduleEmailMeRequest  # noqa: E402

SCHEDULE = types.SimpleNamespace(id="s1", name="Weekly reviews", agent_name="acme-scout", deliver_to_email=None)


def _run(coro):
    return asyncio.run(coro)


class FakeDB:
    def __init__(self, schedule=SCHEDULE, executions=(), execution=None):
        self.schedule, self.executions, self.execution = schedule, list(executions), execution
        self.kept = []

    def get_schedule(self, sid):
        return self.schedule if self.schedule and sid == self.schedule.id else None

    def get_agent_executions(self, agent_name, limit=50):
        return self.executions

    def get_execution(self, eid):
        return self.execution

    def set_schedule_result_email(self, sid, email):
        self.kept.append((sid, email))
        return True


@pytest.fixture
def wired(monkeypatch):
    mails, guards = [], set()

    class Mailer:
        ok = True

        async def send_html(self, to, subject, *, intro, paragraphs=None, cta=None, relay=None):
            mails.append({"to": to, "subject": subject, "intro": intro, "paragraphs": paragraphs, "cta": cta, "relay": relay})
            return Mailer.ok

    monkeypatch.setitem(sys.modules, "services.email_service", types.SimpleNamespace(email_service=Mailer()))

    class Guard:
        def __init__(self, replay):
            self.replay, self.snapshot = replay, None

    @asynccontextmanager
    async def effect_guard(scope, identity, *, execution_id, agent_name):
        key = (scope, tuple(sorted(identity.items())), execution_id)
        guard = Guard(key in guards)
        yield guard
        if guard.snapshot is not None:
            guards.add(key)

    monkeypatch.setitem(sys.modules, "services.idempotency_service",
                        types.SimpleNamespace(effect_guard=effect_guard, EffectInProgressError=RuntimeError))
    monkeypatch.setenv("FRONTEND_URL", "https://trinity.example.org")

    def use(fake):
        monkeypatch.setitem(sys.modules, "database", types.SimpleNamespace(db=fake))
        return fake
    return types.SimpleNamespace(mails=mails, mailer=Mailer, use=use)


def _finished(eid="e1", status="success", response="Three new reviews."):
    return types.SimpleNamespace(id=eid, status=status, completed_at="2026-11-25T10:00:00Z", response=response,
                                 error=None, agent_name="acme-scout", schedule_id="s1", triggered_by="schedule")


# -- enable -----------------------------------------------------------------
def test_enable_mails_the_latest_run_to_the_callers_address_then_keeps_it(wired):
    db = wired.use(FakeDB(executions=[_finished()]))
    out = _run(sre.enable(agent_name="acme-scout", schedule=SCHEDULE, email="ada@example.com"))
    assert out == {"deliver_to_email": "ada@example.com", "sent": True, "sent_execution_id": "e1"}
    assert db.kept == [("s1", "ada@example.com")]
    mail = wired.mails[-1]
    assert mail["to"] == "ada@example.com" and "Three new reviews." in mail["paragraphs"]
    kind, fields = mail["relay"]
    assert kind == "run_result" and fields["status"] == "succeeded" and fields["result"] == "Three new reviews."
    assert fields["url"] == "https://trinity.example.org/agents/acme-scout/executions/e1"


def test_a_mail_that_did_not_go_keeps_nothing_and_says_why(wired):
    db = wired.use(FakeDB(executions=[_finished()]))
    wired.mailer.ok = False
    with pytest.raises(sre.EmailResultsRefused) as refused:
        _run(sre.enable(agent_name="acme-scout", schedule=SCHEDULE, email="ada@example.com"))
    assert refused.value.reason == "email_not_sent" and refused.value.status_code == 422
    assert db.kept == []


def test_no_address_on_the_account_is_a_named_refusal(wired):
    db = wired.use(FakeDB())
    with pytest.raises(sre.EmailResultsRefused) as refused:
        _run(sre.enable(agent_name="acme-scout", schedule=SCHEDULE, email=None))
    assert refused.value.reason == "no_email_address" and db.kept == [] and wired.mails == []


def test_with_no_run_yet_it_still_proves_the_address_works(wired):
    wired.use(FakeDB())
    out = _run(sre.enable(agent_name="acme-scout", schedule=SCHEDULE, email="ada@example.com"))
    assert out["sent"] is True and out["sent_execution_id"] is None and len(wired.mails) == 1


# -- the terminal leg -----------------------------------------------------------
def test_a_finished_run_of_a_schedule_that_asks_is_mailed_once_through_report_completion(wired):
    asked = types.SimpleNamespace(**{**vars(SCHEDULE), "deliver_to_email": "ada@example.com"})
    row = _finished(eid="e2", response="Done this week.")
    row.source_channel = row.source_channel_chat_id = None     # no channel context at all
    wired.use(FakeDB(schedule=asked, execution=row))
    _run(ccr.report_completion(execution_id="e2", agent_name="acme-scout", status="success", summary_or_error=None))
    _run(ccr.report_completion(execution_id="e2", agent_name="acme-scout", status="success", summary_or_error=None))
    assert [m["to"] for m in wired.mails] == ["ada@example.com"]           # at most once
    assert "Done this week." in wired.mails[0]["paragraphs"]


def test_a_failed_run_is_mailed_as_failed(wired):
    asked = types.SimpleNamespace(**{**vars(SCHEDULE), "deliver_to_email": "ada@example.com"})
    row = _finished(eid="e3", status="failed", response=None)
    row.error = "The agent stopped."
    wired.use(FakeDB(schedule=asked, execution=row))
    assert _run(sre.deliver(execution_id="e3", status="failed")) is True
    assert wired.mails[0]["relay"][1]["status"] == "failed" and "did not finish" in wired.mails[0]["intro"]


def test_a_schedule_that_did_not_ask_mails_nothing(wired):
    row = _finished(eid="e4")
    row.source_channel = row.source_channel_chat_id = None
    wired.use(FakeDB(execution=row))
    _run(ccr.report_completion(execution_id="e4", agent_name="acme-scout", status="success", summary_or_error=None))
    assert wired.mails == []


# -- the route ---------------------------------------------------------------
def test_the_route_uses_the_callers_own_address_and_names_a_refusal(wired, monkeypatch):
    from fastapi import HTTPException

    fake = wired.use(FakeDB(executions=[_finished()]))
    monkeypatch.setattr(routes, "db", fake)
    me = types.SimpleNamespace(email="ada@example.com", id=1)
    out = _run(routes.email_me_schedule_results("acme-scout", "s1", ScheduleEmailMeRequest(enabled=True), me))
    assert out["deliver_to_email"] == "ada@example.com" and wired.mails[-1]["to"] == "ada@example.com"
    assert "email" not in ScheduleEmailMeRequest.model_fields          # never an address the caller supplies

    with pytest.raises(HTTPException) as refused:
        _run(routes.email_me_schedule_results("acme-scout", "s1", ScheduleEmailMeRequest(enabled=True),
                                              types.SimpleNamespace(email=None, id=2)))
    assert refused.value.status_code == 422 and refused.value.detail["code"] == "no_email_address"
    with pytest.raises(HTTPException) as missing:
        _run(routes.email_me_schedule_results("other-agent", "s1", ScheduleEmailMeRequest(enabled=True), me))
    assert missing.value.status_code == 404

    off = _run(routes.email_me_schedule_results("acme-scout", "s1", ScheduleEmailMeRequest(enabled=False), me))
    assert off["deliver_to_email"] is None and fake.kept[-1] == ("s1", None)
