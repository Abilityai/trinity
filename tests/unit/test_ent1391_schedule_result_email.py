"""Each run of a schedule mailed to the person who asked (abilityai/trinity-enterprise#1391).

Real routes, the real `get_current_user`, real JWTs, the real terminal
chokepoint (`channel_completion_report.report_completion`), the real
idempotency guard, on the unit conftest's SQLite. Only the mail provider's
`send_html` is replaced (it records, or answers as a provider / relay would).

Pinned, one test (or more) per review finding of the first validation:

1. turning email on mails THIS schedule's own latest finished run, never a
   chat, another schedule's run or another person's turn; with no finished run
   it says so and mails a plain confirmation with no dead link;
2. every result mail is redacted like every other delivery path: a credential
   is masked, a failure is only the person-safe sentence (no traceback);
3. the recipient is resolved at send time from who turned it on: it follows an
   account email change, stops (and says why) once that person no longer
   manages the agent, and a take-over clears it unless the taker set it;
4. a mail that did not go is recorded on the schedule, and a later one that
   went clears it;
5. a relay refusal reaches the person with its own reason;
6. a schedule name with ordinary punctuation is sent as it is;
7. the mail names the control that exists for turning it off;
8. the route takes the schedules fence and a person only;
9. a failed attempt with a retry coming is not mailed, and a chain of attempts
   is mailed once;
10. a schedule read never carries the address; a person the agent is shared
    with sees only that results are mailed (to someone else);
11. (second validation) turning it on or off each writes an audit row, and a
    failed attempt held back for a retry that is then dropped by the scheduler
    is still mailed, once per chain.

Module: src/backend/services/schedule_result_email.py
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from database import db
from db.engine import get_engine
from db_models import UserCreate
from dependencies import create_access_token
from routers import schedules as schedules_router
from services import channel_completion_report as ccr
from services import email_service as email_module
from services import schedule_result_email as sre

pytestmark = pytest.mark.unit

BODY = {"name": "Daily digest: 9am — news/feedback?", "cron_expression": "0 9 * * *",
        "message": "Summarise my inbox", "timezone": "UTC"}
SECRET = "sk-ant-api03-" + "A" * 40
TRACEBACK = ('Traceback (most recent call last):\n  File "/app/x.py", line 3, in run\n'
             'RuntimeError: boom ANTHROPIC_API_KEY=' + SECRET)


# ---- harness ----------------------------------------------------------------------------

@pytest.fixture
def mails(monkeypatch):
    """`send_html` as a provider that delivers; `.answer` changes what it does."""
    sent = []

    async def send_html(to, subject, *, intro, paragraphs=None, cta=None, relay=None, raise_refusal=False):
        sent.append({"to": to, "subject": subject, "intro": intro, "paragraphs": list(paragraphs or []),
                     "cta": cta, "relay": relay, "raise_refusal": raise_refusal})
        answer = send_html.answer
        if isinstance(answer, BaseException):
            raise answer
        return answer

    send_html.answer = True
    monkeypatch.setattr(email_module.email_service, "send_html", send_html)
    monkeypatch.setenv("FRONTEND_URL", "https://trinity.example.org")
    return SimpleNamespace(sent=sent, fake=send_html)


@pytest.fixture
def client(monkeypatch):
    async def audit(**kw):
        return None
    monkeypatch.setattr(schedules_router.platform_audit_service, "log",
                        lambda event_type, event_action, source, **kw: audit(**kw))
    app = FastAPI()
    app.include_router(schedules_router.router)
    return TestClient(app)


def _person(role):
    username = f"t1391-{uuid.uuid4().hex[:8]}@example.com"
    db.create_user(UserCreate(username=username, email=username, role=role, password="x"))
    user = db.get_user_by_username(username)
    return SimpleNamespace(username=username, email=username, id=user["id"],
                           jwt=create_access_token({"sub": username}))


@pytest.fixture
def world():
    owner, mate = _person("creator"), _person("operator")
    agent = f"t1391-{uuid.uuid4().hex[:8]}"
    db.register_agent_owner(agent, owner.username)
    db.share_agent(agent, owner.username, mate.username)
    key = db.create_agent_mcp_api_key(agent, owner.username).api_key
    return SimpleNamespace(agent=agent, owner=owner, mate=mate, key=key)


def call(client, world, who, method, path="", **kw):
    token = world.key if who == "key" else getattr(world, who).jwt
    return client.request(method, f"/api/agents/{world.agent}/schedules{path}",
                          headers={"Authorization": f"Bearer {token}"}, **kw)


def created(client, world, who="owner", **extra):
    resp = call(client, world, who, "POST", json={**BODY, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def email_me(client, world, sid, who="owner", enabled=True):
    return call(client, world, who, "POST", f"/{sid}/email-me", json={"enabled": enabled})


def stored(sid):
    with get_engine().connect() as conn:
        return dict(conn.execute(text("SELECT * FROM agent_schedules WHERE id = :sid"),
                                 {"sid": sid}).mappings().one())


_CLOCK = iter(range(10, 59))


def run_row(world, schedule_id, *, status="success", response=None, error=None, attempt=1,
            retry_of=None, source_user_id=None, completed=True):
    """One execution row, as the terminal writer leaves it."""
    eid = f"e-{uuid.uuid4().hex[:10]}"
    minute = next(_CLOCK)
    with get_engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO schedule_executions (id, schedule_id, agent_name, status, started_at, completed_at, "
            "message, response, error, triggered_by, attempt_number, retry_of_execution_id, source_user_id) "
            "VALUES (:id, :sid, :agent, :status, :started, :completed, 'm', :response, :error, 'schedule', "
            ":attempt, :retry_of, :uid)"),
            {"id": eid, "sid": schedule_id, "agent": world.agent, "status": status,
             "started": f"2026-11-25T10:{minute:02d}:00Z",
             "completed": f"2026-11-25T10:{minute:02d}:30Z" if completed else None,
             "response": response, "error": error, "attempt": attempt, "retry_of": retry_of,
             "uid": source_user_id})
    return eid


def set_retries(sid, n):
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE agent_schedules SET max_retries = :n WHERE id = :sid"), {"n": n, "sid": sid})


def finish(world, eid, status="success"):
    """The real chokepoint every CAS-won terminal calls."""
    asyncio.run(ccr.report_completion(execution_id=eid, agent_name=world.agent, status=status,
                                      summary_or_error=None))


def turned_on(client, world, mails, who="owner"):
    sid = created(client, world)
    resp = email_me(client, world, sid, who=who)
    assert resp.status_code == 200, resp.text
    mails.sent.clear()
    return sid


def body_of(mail):
    return "\n".join([mail["intro"], *mail["paragraphs"]])


# ---- 1. turning it on mails this schedule's own latest finished run --------------------

def test_turning_it_on_mails_only_this_schedules_own_latest_run(client, world, mails):
    sid = created(client, world)
    other = created(client, world, name="Other one")
    run_row(world, sid, response="Three new reviews.")
    # Newer than it: another schedule's run, a chat (no schedule) and another
    # person's turn on the agent. None of them may be mailed under this title.
    run_row(world, other, response="NOT THIS: another schedule")
    run_row(world, "__chat__", response="NOT THIS: someone's chat", source_user_id=world.mate.id)
    run_row(world, sid, status="running", completed=False)      # unfinished: skipped

    resp = email_me(client, world, sid)

    assert resp.status_code == 200, resp.text
    assert resp.json()["no_finished_run_yet"] is False
    [mail] = mails.sent
    assert mail["to"] == world.owner.email
    assert "Three new reviews." in body_of(mail) and "NOT THIS" not in body_of(mail)
    assert stored(sid)["email_results_user_id"] == world.owner.id


def test_with_no_finished_run_it_says_so_and_sends_a_plain_confirmation(client, world, mails):
    sid = created(client, world)
    run_row(world, "__chat__", response="NOT THIS: a chat")

    resp = email_me(client, world, sid)

    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["no_finished_run_yet"] is True and out["sent_execution_id"] is None
    assert "has not finished a run yet" in out["message"]
    [mail] = mails.sent
    assert "has not finished a run yet" in body_of(mail) and "NOT THIS" not in body_of(mail)
    assert mail["cta"] is None and mail["relay"][1]["url"] == ""     # no link that ends at /executions/


# ---- 2. redaction ------------------------------------------------------------------------

def test_a_result_is_credential_sanitised_before_it_leaves(client, world, mails):
    sid = turned_on(client, world, mails)
    finish(world, run_row(world, sid, response=f"Done. The key is {SECRET}"))
    [mail] = mails.sent
    assert SECRET not in body_of(mail) and SECRET not in mail["relay"][1]["result"]
    assert "Done." in mail["relay"][1]["result"]


def test_a_failure_is_only_the_person_safe_sentence(client, world, mails):
    sid = turned_on(client, world, mails)
    finish(world, run_row(world, sid, status="failed", error=TRACEBACK), status="failed")
    [mail] = mails.sent
    assert mail["relay"][1]["result"] == ccr.GENERIC_FAILURE
    assert "Traceback" not in body_of(mail) and SECRET not in body_of(mail)
    assert mail["relay"][1]["status"] == "failed"


def test_turning_it_on_redacts_the_run_it_mails_now(client, world, mails):
    sid = created(client, world)
    run_row(world, sid, status="failed", error=TRACEBACK)
    assert email_me(client, world, sid).status_code == 200
    [mail] = mails.sent
    assert "Traceback" not in body_of(mail) and SECRET not in body_of(mail)


# ---- 3. the recipient, at send time ------------------------------------------------------

def test_it_follows_a_changed_account_email(client, world, mails):
    sid = turned_on(client, world, mails)
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE users SET email = 'ada.new@example.com' WHERE id = :id"), {"id": world.owner.id})
    finish(world, run_row(world, sid, response="ok"))
    assert [m["to"] for m in mails.sent] == ["ada.new@example.com"]


def test_mail_stops_when_the_person_no_longer_manages_the_agent(client, world, mails):
    admin = _person("admin")
    sid = turned_on(client, world, mails, who="owner")
    # An admin turned it on; then the admin is demoted to a plain operator
    # with no share: they no longer manage the agent.
    resp = email_me(client, world, sid, who="owner", enabled=False)
    assert resp.status_code == 200
    world.admin = admin
    assert email_me(client, world, sid, who="admin").status_code == 200
    mails.sent.clear()
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE users SET role = 'operator' WHERE id = :id"), {"id": admin.id})

    finish(world, run_row(world, sid, response="ok"))

    assert mails.sent == []
    row = stored(sid)
    assert row["email_results_user_id"] is None
    assert row["email_results_error"] == sre.LOST_ACCESS
    seen = call(client, world, "owner", "GET", f"/{sid}").json()
    assert (seen["email_results"], seen["email_results_error"]) == (False, sre.LOST_ACCESS)


def test_a_take_over_clears_mail_to_anyone_but_the_taker(client, world, mails):
    admin = _person("admin")
    world.admin = admin
    sid = created(client, world, who="mate")                    # a shared person's schedule
    assert email_me(client, world, sid, who="admin").status_code == 200
    row = call(client, world, "owner", "GET", f"/{sid}").json()
    body = {"expected_updated_at": row["updated_at"],
            "reviewed": {k: row[k] for k in ("message", "allowed_tools", "model",
                                             "validation_enabled", "validation_prompt")}}

    resp = call(client, world, "owner", "POST", f"/{sid}/take-over", json=body)

    assert resp.status_code == 200, resp.text
    assert stored(sid)["email_results_user_id"] is None
    assert resp.json()["email_results"] is False


def test_a_take_over_keeps_the_takers_own(client, world, mails):
    sid = created(client, world, who="mate")
    assert email_me(client, world, sid, who="owner").status_code == 200
    row = call(client, world, "owner", "GET", f"/{sid}").json()
    body = {"expected_updated_at": row["updated_at"],
            "reviewed": {k: row[k] for k in ("message", "allowed_tools", "model",
                                             "validation_enabled", "validation_prompt")}}
    assert call(client, world, "owner", "POST", f"/{sid}/take-over", json=body).status_code == 200
    assert stored(sid)["email_results_user_id"] == world.owner.id


# ---- 4. a dropped mail is visible ---------------------------------------------------------

def test_a_mail_that_did_not_go_shows_on_the_schedule_until_one_goes(client, world, mails):
    sid = turned_on(client, world, mails)
    mails.fake.answer = email_module.EmailRefused(
        "result_address_limit", "You reached today's limit of result emails for this address.", 429)
    finish(world, run_row(world, sid, response="first"))
    seen = call(client, world, "owner", "GET", f"/{sid}").json()
    assert seen["email_results"] is True
    assert seen["email_results_error"] == "You reached today's limit of result emails for this address."
    assert seen["email_results_error_at"]

    mails.fake.answer = True
    finish(world, run_row(world, sid, response="second"))
    seen = call(client, world, "owner", "GET", f"/{sid}").json()
    assert (seen["email_results_error"], seen["email_results_error_at"]) == (None, None)


# ---- 5. a refusal reaches the person with its reason -------------------------------------

def test_a_relay_refusal_reaches_the_person_with_its_reason(client, world, mails):
    sid = created(client, world)
    mails.fake.answer = email_module.EmailRefused(
        "not_own_address", "Trinity mails a run's result only to this instance's own registered address.", 403)

    resp = email_me(client, world, sid)

    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "not_own_address"
    assert "only to this instance's own registered address" in detail["message"]
    assert "Try again in a moment" not in detail["message"]
    assert stored(sid)["email_results_user_id"] is None
    assert mails.sent[0]["raise_refusal"] is True


def test_the_email_service_passes_a_refusal_only_to_a_caller_that_asked(monkeypatch):
    """The seam itself: `_relayed` re-raises the relay's EmailRefused only with
    `raise_refusal`; every other mail still reads False."""
    async def refuse(kind, to, fields):
        raise email_module.EmailRefused("result_address_limit", "Limit reached.", 429)
    monkeypatch.setattr(email_module, "_relay", {"available": lambda: True, "send": refuse})
    monkeypatch.setattr(email_module.EmailService, "_provider_can_deliver", lambda self: False)
    service = email_module.EmailService()
    assert asyncio.run(service.send_html("a@example.com", "s", intro="i", relay=("code", {}))) is False
    with pytest.raises(email_module.EmailRefused):
        asyncio.run(service.send_html("a@example.com", "s", intro="i", relay=("run_result", {}),
                                      raise_refusal=True))


# ---- 6. ordinary punctuation in the name -------------------------------------------------

def test_a_name_with_ordinary_punctuation_is_sent_as_it_is(client, world, mails):
    sid = turned_on(client, world, mails)
    finish(world, run_row(world, sid, response="ok"))
    [mail] = mails.sent
    assert mail["relay"][1]["title"] == BODY["name"]
    assert mail["subject"] == f"{world.agent}: {BODY['name']}"


# ---- 7. the mail names the control that turns it off -------------------------------------

def test_the_mail_names_the_stop_control(client, world, mails):
    sid = turned_on(client, world, mails)
    finish(world, run_row(world, sid, response="ok"))
    assert '"Stop emailing"' in body_of(mails.sent[0])
    assert sre.STOP_CONTROL == "Stop emailing"
    # ...and that control works: the person turns it off and nothing more is mailed.
    assert email_me(client, world, sid, enabled=False).status_code == 200
    finish(world, run_row(world, sid, response="after"))
    assert len(mails.sent) == 1 and stored(sid)["email_results_user_id"] is None


# ---- 8. the route's gates -----------------------------------------------------------------

def test_an_agent_key_is_refused_and_a_shared_person_cannot_reach_it(client, world, mails):
    sid = created(client, world)
    assert email_me(client, world, sid, who="key").status_code == 403
    assert email_me(client, world, sid, who="mate").status_code in (403, 404)
    assert mails.sent == [] and stored(sid)["email_results_user_id"] is None


# ---- 9. retries ---------------------------------------------------------------------------

def test_a_retried_run_is_mailed_once_with_its_final_outcome(client, world, mails):
    sid = turned_on(client, world, mails)
    set_retries(sid, 2)
    first = run_row(world, sid, status="failed", error="network blip", attempt=1)
    finish(world, first, status="failed")
    assert mails.sent == []                                    # a retry is coming: not mailed
    second = run_row(world, sid, status="success", response="Done on retry.", attempt=2, retry_of=first)
    finish(world, second)
    finish(world, second)                                      # a redelivered report
    assert len(mails.sent) == 1 and "Done on retry." in mails.sent[0]["relay"][1]["result"]


def test_the_last_attempt_that_fails_is_mailed_once(client, world, mails):
    sid = turned_on(client, world, mails)
    set_retries(sid, 1)
    first = run_row(world, sid, status="failed", error="blip", attempt=1)
    finish(world, first, status="failed")
    second = run_row(world, sid, status="failed", error="blip", attempt=2, retry_of=first)
    finish(world, second, status="failed")
    finish(world, second, status="failed")
    assert len(mails.sent) == 1 and mails.sent[0]["relay"][1]["status"] == "failed"


# ---- 10. who may read what on a schedule read ---------------------------------------------

def test_a_schedule_read_never_carries_the_address(client, world, mails):
    sid = turned_on(client, world, mails)
    mine = call(client, world, "owner", "GET", f"/{sid}").json()
    theirs = call(client, world, "mate", "GET", f"/{sid}").json()
    assert (mine["email_results"], mine["email_results_to_me"]) == (True, True)
    assert (theirs["email_results"], theirs["email_results_to_me"]) == (True, False)
    # The person the agent is shared with reads nothing that carries the owner's address.
    assert world.owner.email not in str(theirs)
    for read in (mine, theirs):
        assert "deliver_to_email" not in read and "email_results_user_id" not in read


def test_an_attempt_of_a_chain_already_mailed_is_not_mailed_again(client, world, mails):
    """The guard is keyed on the run the chain started from, not the attempt:
    should the retry rule here and the scheduler's ever disagree (a retry of an
    attempt that was mailed), the run is still mailed once."""
    sid = turned_on(client, world, mails)
    first = run_row(world, sid, status="failed", error="blip", attempt=1)
    finish(world, first, status="failed")                       # no retries configured: mailed
    second = run_row(world, sid, status="success", response="late", attempt=2, retry_of=first)
    finish(world, second)
    assert len(mails.sent) == 1


# ---- 11. third validation: an audit row per grant, a dropped retry is still mailed -------

@pytest.fixture
def audited(monkeypatch):
    rows = []

    async def log(event_type, event_action, source, **kw):
        rows.append({"event_type": event_type, "event_action": event_action, "source": source, **kw})

    monkeypatch.setattr(schedules_router.platform_audit_service, "log", log)
    return rows


def test_turning_it_on_and_off_each_write_an_audit_row(client, world, mails, audited):
    sid = created(client, world)
    audited.clear()
    assert email_me(client, world, sid).status_code == 200
    assert email_me(client, world, sid, enabled=False).status_code == 200
    actions = [(r["event_action"], r["target_id"], r["details"]["schedule_id"]) for r in audited]
    assert actions == [("schedule_email_results_enabled", world.agent, sid),
                       ("schedule_email_results_disabled", world.agent, sid)]
    for row in audited:
        assert row["actor_user"].username == world.owner.username
        assert world.owner.email not in str(row["details"])      # who, not where


def test_a_refused_turn_on_writes_no_audit_row(client, world, mails, audited):
    sid = created(client, world)
    audited.clear()
    mails.fake.answer = False
    assert email_me(client, world, sid).status_code >= 400
    assert audited == []


def _dropped(execution_id):
    """The scheduler's notice that a pending retry will never run."""
    from routers import internal as internal_router

    app = FastAPI()
    app.include_router(internal_router.router)
    app.dependency_overrides[internal_router.verify_internal_secret] = lambda: None
    return TestClient(app).post("/api/internal/schedule-retry-dropped", json={"execution_id": execution_id})


def test_a_failed_run_whose_retry_never_fires_is_mailed_once(client, world, mails):
    sid = turned_on(client, world, mails)
    set_retries(sid, 2)
    first = run_row(world, sid, status="failed", error="network blip", attempt=1)
    finish(world, first, status="failed")
    assert mails.sent == []                                     # a retry was expected
    assert _dropped(first).status_code == 200                   # ...and it was dropped
    assert _dropped(first).status_code == 200                   # a repeated notice
    assert len(mails.sent) == 1 and mails.sent[0]["relay"][1]["status"] == "failed"


def test_a_dropped_retry_of_a_chain_already_mailed_is_not_mailed_again(client, world, mails):
    sid = turned_on(client, world, mails)
    first = run_row(world, sid, status="failed", error="blip", attempt=1)
    finish(world, first, status="failed")                       # no retries: mailed now
    assert _dropped(first).status_code == 200
    assert len(mails.sent) == 1


def test_a_dropped_retry_of_a_schedule_that_does_not_mail_sends_nothing(client, world, mails):
    sid = created(client, world)
    first = run_row(world, sid, status="failed", error="blip", attempt=1)
    assert _dropped(first).status_code == 200
    assert _dropped("no-such-execution").status_code == 200
    assert mails.sent == []


class TestTheSchedulerSaysWhenARetryIsDropped:
    """Each of `_execute_retry`'s drop paths tells the backend, once, which
    failed attempt will now never be followed; a retry that runs does not."""

    @staticmethod
    def _service(*, enabled=True, autonomy=True, freeze=None, schedule=True):
        import sys
        from pathlib import Path
        from unittest.mock import AsyncMock, MagicMock

        repo = Path(__file__).resolve().parents[2]
        if str(repo) not in sys.path:
            sys.path.append(str(repo))
        import src.scheduler.service as ssvc

        svc = object.__new__(ssvc.SchedulerService)
        svc.db = MagicMock()
        svc.db.get_schedule.return_value = SimpleNamespace(
            id="s1", name="Brief", agent_name="a1", enabled=enabled, message="m",
            deliver_to_workspace_email=None) if schedule else None
        svc.db.get_execution.return_value = SimpleNamespace(
            triggered_by="schedule", source_user_id=None, source_user_email=None,
            source_agent_name=None, source_mcp_key_id=None, source_mcp_key_name=None, chain_depth=None)
        svc.db.get_autonomy_enabled.return_value = autonomy
        svc.db.sync_freeze_reason.return_value = freeze
        svc.db.create_execution.return_value = SimpleNamespace(id="retry-1")
        svc.db.create_skipped_execution.return_value = SimpleNamespace(id="skipped-1")
        svc._call_backend_execute_task = AsyncMock(return_value={"status": "dispatched"})
        svc._publish_event = AsyncMock()
        svc._report_retry_dropped = AsyncMock()
        asyncio.run(svc._execute_retry(
            original_execution_id="orig", failed_execution_id="failed", schedule_id="s1",
            agent_name="a1", message="m", timeout_seconds=None, model=None,
            allowed_tools=None, next_attempt_number=2))
        return svc

    @pytest.mark.parametrize("kw", [dict(enabled=False), dict(schedule=False),
                                    dict(autonomy=False), dict(freeze="git sync failing")])
    def test_each_drop_path_reports_the_failed_attempt(self, kw):
        svc = self._service(**kw)
        svc._call_backend_execute_task.assert_not_called()
        svc._report_retry_dropped.assert_awaited_once_with("failed")

    def test_a_retry_that_runs_reports_nothing(self):
        svc = self._service()
        svc._call_backend_execute_task.assert_called_once()
        svc._report_retry_dropped.assert_not_called()

    def test_the_report_posts_to_the_backend_and_never_raises(self, monkeypatch):
        import src.scheduler.service as ssvc

        posted = []

        class _Client:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, json=None, headers=None):
                posted.append((url, json, headers))
                raise RuntimeError("backend down")

        monkeypatch.setattr(ssvc.httpx, "AsyncClient", _Client)
        svc = object.__new__(ssvc.SchedulerService)
        asyncio.run(svc._report_retry_dropped("failed"))        # does not raise
        url, body, headers = posted[0]
        assert url.endswith("/api/internal/schedule-retry-dropped")
        assert body == {"execution_id": "failed"} and "X-Internal-Secret" in headers
