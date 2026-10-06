"""Caller-declared idempotency key + TTL on the human-facing sends (ent#665).

`effect_guard` (#1084) de-duplicates a send WITHIN one execution. Two runs of a
recurring agent are two executions, so nothing stopped run B repeating what run
A had already told a person. These tests pin the cross-execution key:

* the store decides: two executions with the same (agent, recipient, key)
  inside the TTL deliver exactly once, and a concurrent second claim is a
  retryable 409, never a "suppressed" that would lie if the first send dies;
* the result says what happened (`sent`, `suppressed_by`, `first_sent_at`,
  `first_execution_id`) and the suppression is written to the audit log and to
  the conversation;
* absent a key, nothing changes;
* the key is the caller's string: the message text is never part of it (#1422).

Runs on the real schema through `db_harness` (SQLite always, PostgreSQL when
TEST_POSTGRES_URL is set). Issue: abilityai/trinity-enterprise#665
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import types
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
_TESTS = Path(__file__).resolve().parents[1]
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))
from db_harness import db_backend, run, scalar  # noqa: E402,F401

pytestmark = pytest.mark.unit

AGENT = "corbin"
EMAIL = "eugene@example.com"
KEY = "gcp-card-ceiling-correction"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def ops(db_backend):
    from db.idempotency import IdempotencyOperations
    return IdempotencyOperations()


@pytest.fixture
def executions(db_backend, monkeypatch):
    """Executions the guards resolve, registered per test: {id: agent_name}."""
    from database import db

    known: dict = {}
    monkeypatch.setattr(
        db, "get_execution",
        lambda eid: types.SimpleNamespace(agent_name=known[eid]) if eid in known else None,
    )
    return known


@pytest.fixture
def idem(executions):
    from services import idempotency_service
    return idempotency_service


@pytest.fixture
def audit(monkeypatch):
    """Capture audit events on the instance each consumer module holds: the unit
    conftest pops `services.platform_audit_service` between tests, so a fresh
    import here is not the object an already-imported sink calls."""
    import importlib

    events: list = []

    async def _log(**kw):
        events.append(kw)

    # The audit module itself covers sinks that import it lazily (channel_history).
    for name in ("services.platform_audit_service", "services.proactive_message_service",
                 "routers.voip", "routers.telegram", "routers.slack"):
        mod = importlib.import_module(name)
        if hasattr(mod, "platform_audit_service"):
            monkeypatch.setattr(mod.platform_audit_service, "log", _log)
    return events


@pytest.fixture
def pms(executions, audit, monkeypatch):
    """The real ProactiveMessageService with channel delivery stubbed."""
    from database import db
    from services import proactive_message_service as mod

    consent = {"ok": True}
    monkeypatch.setattr(db, "can_agent_message_email", lambda a, e: consent["ok"])

    svc = mod.ProactiveMessageService()
    svc.delivered = []
    svc.rate_increments = []
    svc.consent = consent
    svc._check_rate_limit = lambda a, e: True
    svc._increment_rate_limit = lambda a, e: svc.rate_increments.append((a, e))

    async def _deliver(agent_name, recipient_email, text, channel, reply_to_thread):
        svc.delivered.append(text)
        return mod.DeliveryResult(
            success=True, channel="telegram", message_id=f"m{len(svc.delivered)}",
            session_identifier="bot-1:dm:42",
        )

    svc._deliver_via_channel = _deliver
    svc.mod = mod
    return svc


def _backdate(column: str, seconds: int) -> None:
    from utils.helpers import iso_cutoff
    run(f"UPDATE idempotency_keys SET {column} = :ts", ts=iso_cutoff(seconds=seconds))


# --------------------------------------------------------------------------- #
# Store: TTL and in-flight lease on the intent path
# --------------------------------------------------------------------------- #
class TestIntentClaim:
    def test_completed_row_inside_ttl_replays(self, ops):
        assert ops.claim("intent:a", "k", ttl_seconds=3600, in_flight_lease_seconds=300)["state"] == "new"
        ops.complete("intent:a", "k", "exec-a", {"sent_at": "t"})
        res = ops.claim("intent:a", "k", ttl_seconds=3600, in_flight_lease_seconds=300)
        assert res["state"] == "completed"
        assert res["execution_id"] == "exec-a"

    def test_completed_row_past_ttl_is_reclaimed(self, ops):
        ops.claim("intent:a", "k", ttl_seconds=3600, in_flight_lease_seconds=300)
        ops.complete("intent:a", "k", "exec-a", {"sent_at": "t"})
        _backdate("created_at", 3601)
        assert ops.claim("intent:a", "k", ttl_seconds=3600, in_flight_lease_seconds=300)["state"] == "new"

    def test_checking_calls_ttl_decides(self, ops):
        """A send 2 minutes ago is inside a 1h window and outside a 60s one."""
        ops.claim("intent:a", "k", ttl_seconds=86400, in_flight_lease_seconds=300)
        ops.complete("intent:a", "k", "exec-a", {"sent_at": "t"})
        _backdate("created_at", 120)
        assert ops.claim("intent:a", "k", ttl_seconds=3600, in_flight_lease_seconds=300)["state"] == "completed"
        assert ops.claim("intent:a", "k", ttl_seconds=60, in_flight_lease_seconds=300)["state"] == "new"

    def test_live_in_flight_row_is_not_expired_by_a_short_ttl(self, ops):
        """A slow send must not be stolen by a caller with a shorter TTL."""
        ops.claim("intent:a", "k", ttl_seconds=60, in_flight_lease_seconds=300)
        _backdate("created_at", 120)  # older than the TTL, still inside the lease
        assert ops.claim("intent:a", "k", ttl_seconds=60, in_flight_lease_seconds=300)["state"] == "in_flight"

    def test_stale_in_flight_row_is_reclaimed_after_the_lease(self, ops):
        """A process that died mid-send must not block the message for 24h."""
        ops.claim("intent:a", "k", ttl_seconds=86400, in_flight_lease_seconds=300)
        _backdate("updated_at", 301)
        assert ops.claim("intent:a", "k", ttl_seconds=86400, in_flight_lease_seconds=300)["state"] == "new"

    def test_default_path_keeps_its_24h_rule(self, ops):
        """Callers that pass no ttl_seconds keep today's behaviour."""
        ops.claim("webhook:t", "k")
        _backdate("updated_at", 3600)
        assert ops.claim("webhook:t", "k")["state"] == "in_flight"


# --------------------------------------------------------------------------- #
# Guard
# --------------------------------------------------------------------------- #
async def _guarded_send(idem, *, execution_id, key=KEY, target=EMAIL, ttl=None, effect="message",
                        sends=None, record=None, fail=False):
    async with idem.intent_guard(
        effect, agent_name=AGENT, target=target, idempotency_key=key,
        ttl_seconds=ttl, execution_id=execution_id,
    ) as g:
        if g.suppressed:
            return g
        if fail:
            raise RuntimeError("delivery failed")
        if sends is not None:
            sends.append(execution_id)
        g.record = record or {}
        return g


class TestIntentGuard:
    @pytest.mark.asyncio
    async def test_no_key_never_touches_the_store(self, idem, monkeypatch):
        from database import db

        def _boom(*a, **k):
            raise AssertionError("claim must not run without a key")

        monkeypatch.setattr(db, "idempotency_claim", _boom)
        g = await _guarded_send(idem, execution_id="exec-a", key=None)
        assert g.suppressed is False

    @pytest.mark.asyncio
    async def test_second_execution_is_suppressed_and_told_when(self, idem, executions):
        executions.update({"exec-a": AGENT, "exec-b": AGENT})
        sends: list = []
        await _guarded_send(idem, execution_id="exec-a", sends=sends)
        g = await _guarded_send(idem, execution_id="exec-b", sends=sends)
        assert sends == ["exec-a"]
        assert g.suppressed is True
        assert g.first_execution_id == "exec-a"
        assert g.first_sent_at

    @pytest.mark.asyncio
    async def test_foreign_execution_id_is_not_recorded(self, idem, executions):
        executions.update({"exec-other": "someone-else"})
        await _guarded_send(idem, execution_id="exec-other")
        g = await _guarded_send(idem, execution_id="exec-other")
        assert g.suppressed is True
        assert g.first_execution_id is None

    @pytest.mark.asyncio
    async def test_independent_dimensions(self, idem):
        sends: list = []
        await _guarded_send(idem, execution_id="a", sends=sends)
        await _guarded_send(idem, execution_id="b", target="other@example.com", sends=sends)
        await _guarded_send(idem, execution_id="c", key="another-key", sends=sends)
        await _guarded_send(idem, execution_id="d", effect="voip_call", sends=sends)
        assert sends == ["a", "b", "c", "d"]

    @pytest.mark.asyncio
    async def test_failed_send_releases_the_key(self, idem):
        sends: list = []
        with pytest.raises(RuntimeError):
            await _guarded_send(idem, execution_id="a", fail=True)
        await _guarded_send(idem, execution_id="b", sends=sends)
        assert sends == ["b"]

    @pytest.mark.asyncio
    async def test_in_flight_claim_is_a_retryable_conflict(self, idem):
        """Another run mid-send: 409, never `suppressed` (that would lie if it dies)."""
        async with idem.intent_guard(
            "message", agent_name=AGENT, target=EMAIL, idempotency_key=KEY,
            ttl_seconds=None, execution_id="a",
        ):
            with pytest.raises(idem.IntentInProgressError):
                await _guarded_send(idem, execution_id="b")

    def test_key_has_no_text_argument(self, idem):
        """#1422: the key is the caller's string; message text cannot reach it."""
        a = idem.derive_intent_key("message", EMAIL, KEY)
        assert a == idem.derive_intent_key("message", EMAIL.upper(), KEY)
        assert a != idem.derive_intent_key("message", EMAIL, KEY + "-v2")

    def test_ttl_ceiling_is_the_purge_window(self, idem):
        """Rows are hard-deleted after 24h by the cleanup sweep; a longer TTL
        would promise suppression the store cannot keep."""
        import inspect

        from services import cleanup_service

        src = inspect.getsource(cleanup_service)
        assert "idempotency_purge_expired(ttl_hours=24)" in src
        assert idem.INTENT_TTL_MAX_SECONDS == 24 * 3600

    def test_concurrent_claims_resolve_in_the_store(self, idem, monkeypatch):
        """Hold the first claimer inside the critical section (after its INSERT,
        before commit); the second must lose to the PK, not read stale state."""
        from database import db

        real_claim = db.idempotency_claim
        entered = threading.Event()
        results: dict = {}

        def _slow_claim(scope, key, **kw):
            res = real_claim(scope, key, **kw)
            if threading.current_thread().name == "first":
                entered.set()
                time.sleep(0.3)
            return res

        monkeypatch.setattr(db, "idempotency_claim", _slow_claim)

        def _worker(name):
            async def _go():
                try:
                    await _guarded_send(idem, execution_id=name)
                    results[name] = "sent"
                except idem.IntentInProgressError:
                    results[name] = "conflict"
            asyncio.run(_go())

        t1 = threading.Thread(target=_worker, args=("first",), name="first")
        t1.start()
        assert entered.wait(5)
        t2 = threading.Thread(target=_worker, args=("second",), name="second")
        t2.start()
        t1.join()
        t2.join()
        assert sorted(results.values()) == ["conflict", "sent"]


# --------------------------------------------------------------------------- #
# send_message, end to end through both guards
# --------------------------------------------------------------------------- #
async def _send(pms, *, execution_id, key=KEY, ttl=None, text="Card ceiling is $500, not $50.",
                dedup_label=""):
    return await pms.send_message(
        agent_name=AGENT, recipient_email=EMAIL, text=text, channel="auto",
        execution_id=execution_id, dedup_label=dedup_label,
        idempotency_key=key, idempotency_ttl=ttl,
    )


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_two_executions_deliver_one_message(self, pms, executions):
        executions.update({"exec-a": AGENT, "exec-b": AGENT})
        first = await _send(pms, execution_id="exec-a")
        second = await _send(pms, execution_id="exec-b")
        assert len(pms.delivered) == 1
        assert first.sent is True
        assert second.success is True
        assert second.sent is False
        assert second.suppressed_by == "idempotency_key"
        assert second.first_execution_id == "exec-a"
        assert second.first_sent_at

    @pytest.mark.asyncio
    async def test_key_is_not_derived_from_text(self, pms):
        await _send(pms, execution_id="a", text="numbers unchanged")
        await _send(pms, execution_id="b", text="numbers moved, same key")
        await _send(pms, execution_id="c", key=None, text="numbers unchanged")
        assert pms.delivered == ["numbers unchanged", "numbers unchanged"]

    @pytest.mark.asyncio
    async def test_without_a_key_nothing_changes(self, pms):
        res = await _send(pms, execution_id="a", key=None)
        assert res.sent is None and res.suppressed_by is None
        await _send(pms, execution_id="b", key=None)
        assert len(pms.delivered) == 2

    @pytest.mark.asyncio
    async def test_two_keys_in_one_execution_are_two_messages(self, pms, executions):
        """The per-turn guard must not replay key A's result for key B."""
        executions.update({"exec-a": AGENT, "exec-b": AGENT})
        await _send(pms, execution_id="exec-a", key="alert-1")
        await _send(pms, execution_id="exec-b", key="alert-1")          # suppressed
        res = await _send(pms, execution_id="exec-b", key="alert-2")    # new information
        assert res.sent is True
        assert len(pms.delivered) == 2

    @pytest.mark.asyncio
    async def test_redelivery_of_the_suppressing_run_replays_suppressed(self, pms, executions):
        executions.update({"exec-a": AGENT, "exec-b": AGENT})
        await _send(pms, execution_id="exec-a")
        await _send(pms, execution_id="exec-b")
        again = await _send(pms, execution_id="exec-b")
        assert again.sent is False and again.first_execution_id == "exec-a"
        assert len(pms.delivered) == 1

    @pytest.mark.asyncio
    async def test_revoked_consent_is_refused_not_suppressed(self, pms):
        await _send(pms, execution_id="a")
        pms.consent["ok"] = False
        with pytest.raises(pms.mod.NotAuthorizedError):
            await _send(pms, execution_id="b")

    @pytest.mark.asyncio
    async def test_suppressed_send_costs_no_rate_limit(self, pms):
        await _send(pms, execution_id="a")
        await _send(pms, execution_id="b")
        assert len(pms.rate_increments) == 1

    @pytest.mark.asyncio
    async def test_suppression_is_audited(self, pms, audit):
        await _send(pms, execution_id="a")
        await _send(pms, execution_id="b")
        actions = [e["event_action"] for e in audit]
        assert actions == ["send", "suppressed"]
        assert audit[-1]["details"]["idempotency_key"] == KEY

    @pytest.mark.asyncio
    async def test_suppression_is_written_to_the_conversation(self, pms):
        await _send(pms, execution_id="a")
        await _send(pms, execution_id="b")
        rows = scalar(
            "SELECT COUNT(*) FROM public_chat_messages m JOIN public_chat_sessions s "
            "ON s.id = m.session_id WHERE s.session_identifier = :sid", sid="bot-1:dm:42",
        )
        assert rows == 2
        note = scalar("SELECT content FROM public_chat_messages WHERE role = 'system'")
        assert KEY in note and "Not sent" in note
        assert scalar("SELECT sender_email FROM public_chat_messages WHERE role = 'system'") is None


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #
class TestRequestValidation:
    @pytest.mark.parametrize("ttl", [True, 0, 59, 86401, "3600"])
    def test_bad_ttl_is_refused(self, ttl):
        from models import SendMessageRequest
        with pytest.raises(ValidationError):
            SendMessageRequest(to="primary", text="x", idempotency_key=KEY, idempotency_ttl=ttl)

    def test_ttl_without_key_is_refused(self):
        from models import SendMessageRequest
        with pytest.raises(ValidationError):
            SendMessageRequest(to="primary", text="x", idempotency_ttl=3600)

    @pytest.mark.parametrize("key", ["", "has space", "x" * 201])
    def test_bad_key_is_refused(self, key):
        from models import SendMessageRequest
        with pytest.raises(ValidationError):
            SendMessageRequest(to="primary", text="x", idempotency_key=key)

    @pytest.mark.parametrize("model", [
        "SendMessageRequest", "VoipCallRequest", "TelegramGroupMessageRequest",
        "SlackChannelMessageRequest",
    ])
    def test_every_sink_accepts_the_key(self, model):
        import models
        base = {"SendMessageRequest": {"to": "primary", "text": "x"},
                "VoipCallRequest": {"to_number": "+14155550100"}}.get(model, {"message": "x"})
        req = getattr(models, model)(**base, idempotency_key=KEY, idempotency_ttl=3600)
        assert req.idempotency_key == KEY and req.idempotency_ttl == 3600


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
class TestMessagesRoute:
    @pytest.mark.asyncio
    async def test_keyless_response_is_unchanged(self, monkeypatch):
        from models import SendMessageRequest
        from routers import messages

        async def _send_message(**kw):
            from services.proactive_message_service import DeliveryResult
            return DeliveryResult(success=True, channel="telegram", message_id="m1")

        monkeypatch.setattr(messages.proactive_message_service, "send_message", _send_message)
        req = SendMessageRequest(recipient_email=EMAIL, text="x")
        res = await messages.send_proactive_message(req, AGENT, current_user=None)
        route = next(r for r in messages.router.routes if r.path.endswith("/messages"))
        assert route.response_model_exclude_unset is True
        assert res.model_dump(exclude_unset=True) == {
            "success": True, "channel": "telegram", "message_id": "m1", "error": None,
        }

    @pytest.mark.asyncio
    async def test_suppressed_response_names_why(self, monkeypatch):
        from models import SendMessageRequest
        from routers import messages
        from services.proactive_message_service import DeliveryResult

        async def _send_message(**kw):
            assert kw["idempotency_key"] == KEY and kw["idempotency_ttl"] == 3600
            return DeliveryResult(success=True, channel="telegram", sent=False,
                                  suppressed_by="idempotency_key", first_sent_at="t",
                                  first_execution_id="exec-a")

        monkeypatch.setattr(messages.proactive_message_service, "send_message", _send_message)
        req = SendMessageRequest(recipient_email=EMAIL, text="x", idempotency_key=KEY,
                                 idempotency_ttl=3600)
        res = (await messages.send_proactive_message(req, AGENT, current_user=None)).model_dump(
            exclude_unset=True)
        assert res["sent"] is False and res["suppressed_by"] == "idempotency_key"
        assert res["first_execution_id"] == "exec-a" and res["first_sent_at"] == "t"


class TestVoip:
    @pytest.mark.asyncio
    async def test_second_execution_does_not_dial(self, executions, monkeypatch):
        from database import db
        from services.voip_service import voip_service

        executions.update({"exec-a": AGENT, "exec-b": AGENT})
        dials: list = []
        monkeypatch.setattr(voip_service, "is_available", lambda: True)
        monkeypatch.setattr(db, "get_voip_binding", lambda a: {"enabled": True, "account_sid": "AC1"})

        async def _inner(**kw):
            dials.append(kw["dest"])
            return {"call_id": "voip_x", "status": "ringing", "to_number": kw["dest"]}

        monkeypatch.setattr(voip_service, "_place_call_inner", _inner)
        kw = dict(agent_name=AGENT, to_number="+14155550100", initiator_user_id=1,
                  initiator_email="o@example.com", public_url="https://example.com",
                  idempotency_key=KEY, idempotency_ttl=None)
        first = await voip_service.place_outbound_call(execution_id="exec-a", **kw)
        second = await voip_service.place_outbound_call(execution_id="exec-b", **kw)
        assert len(dials) == 1
        assert first["sent"] is True
        assert second["sent"] is False and second["suppressed_by"] == "idempotency_key"
        assert second["call_id"] is None and second["first_execution_id"] == "exec-a"

    @pytest.mark.asyncio
    async def test_route_audits_a_suppressed_call_as_suppressed(self, audit, monkeypatch):
        from models import VoipCallRequest
        from routers import voip

        monkeypatch.setattr(voip, "_require_enabled", lambda: None)

        async def _place(**kw):
            return {"call_id": None, "status": "suppressed", "to_number": "+14155550100",
                    "sent": False, "suppressed_by": "idempotency_key"}

        monkeypatch.setattr(voip.voip_service, "place_outbound_call", _place)
        user = types.SimpleNamespace(id=1, email="o@example.com", username="o")
        await voip.place_voip_call(VoipCallRequest(to_number="+14155550100", idempotency_key=KEY),
                                   AGENT, current_user=user, idempotency_key=None)
        assert [e["event_action"] for e in audit] == ["voip_call_suppressed"]


class TestGroupMessages:
    @pytest.fixture
    def slack(self, executions, audit, monkeypatch):
        from database import db
        from routers import slack

        posts: list = []
        monkeypatch.setattr(slack, "assert_agent_owner", lambda *a, **k: None)
        monkeypatch.setattr(slack, "get_proactive_rate_limit", lambda k: 0)
        monkeypatch.setattr(db, "get_slack_channels_for_agent", lambda a: [{
            "slack_channel_id": "C1", "team_id": "T1", "allow_proactive": True,
            "slack_channel_name": "general"}])
        monkeypatch.setattr(db, "get_slack_workspace_bot_token", lambda t: "xoxb")

        async def _post(**kw):
            posts.append(kw["text"])
            return True, None, f"17000000.{len(posts)}"

        monkeypatch.setattr(slack.slack_service, "send_message_detailed", _post)
        slack.posts = posts
        return slack

    @pytest.mark.asyncio
    async def test_slack_second_execution_is_suppressed(self, slack, executions):
        from models import SlackChannelMessageRequest

        executions.update({"exec-a": AGENT, "exec-b": AGENT})
        user = types.SimpleNamespace(id=1)
        req = lambda eid: SlackChannelMessageRequest(  # noqa: E731
            message="deploy at 4pm", idempotency_key=KEY, execution_id=eid)
        first = await slack.send_agent_slack_channel_message(AGENT, "C1", req("exec-a"), user)
        second = await slack.send_agent_slack_channel_message(AGENT, "C1", req("exec-b"), user)
        assert len(slack.posts) == 1
        assert first["sent"] is True
        assert second["sent"] is False and second["suppressed_by"] == "idempotency_key"
        assert second["first_execution_id"] == "exec-a"
        note = scalar("SELECT content FROM public_chat_messages WHERE role = 'system'")
        assert note and KEY in note

    @pytest.mark.asyncio
    async def test_slack_in_flight_key_is_a_409(self, slack, idem):
        from fastapi import HTTPException
        from models import SlackChannelMessageRequest

        async with idem.intent_guard(
            "group_message", agent_name=AGENT, target="slack:T1:C1", idempotency_key=KEY,
            ttl_seconds=None, execution_id=None,
        ):
            with pytest.raises(HTTPException) as e:
                await slack.send_agent_slack_channel_message(
                    AGENT, "C1", SlackChannelMessageRequest(message="x", idempotency_key=KEY),
                    types.SimpleNamespace(id=1))
        assert e.value.status_code == 409
        assert slack.posts == []

    @pytest.mark.asyncio
    async def test_slack_keyless_response_is_unchanged(self, slack):
        from models import SlackChannelMessageRequest

        res = await slack.send_agent_slack_channel_message(
            AGENT, "C1", SlackChannelMessageRequest(message="hi"), types.SimpleNamespace(id=1))
        assert set(res) == {"sent", "channel_type", "channel_id", "channel_name", "thread_ts"}

    @pytest.mark.asyncio
    async def test_telegram_second_execution_is_suppressed(self, executions, audit, monkeypatch):
        from database import db
        from models import TelegramGroupMessageRequest
        from routers import telegram

        executions.update({"exec-a": AGENT, "exec-b": AGENT})
        posts: list = []
        monkeypatch.setattr(telegram, "get_proactive_rate_limit", lambda k: 0)
        monkeypatch.setattr(db, "get_telegram_binding", lambda a: {"bot_id": "bot-1"})
        monkeypatch.setattr(db, "get_telegram_groups_for_agent", lambda a: [{
            "chat_id": "-100", "is_active": True, "chat_title": "ops"}])
        monkeypatch.setattr(db, "get_telegram_bot_token", lambda a: "tok")

        class _Resp:
            status_code = 200

            def json(self):
                return {"result": {"message_id": len(posts)}}

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json):
                posts.append(json["text"])
                return _Resp()

        monkeypatch.setattr(telegram.httpx, "AsyncClient", _Client)
        req = lambda eid: TelegramGroupMessageRequest(  # noqa: E731
            message="deploy at 4pm", idempotency_key=KEY, execution_id=eid)
        first = await telegram.send_telegram_group_message(AGENT, "-100", req("exec-a"))
        second = await telegram.send_telegram_group_message(AGENT, "-100", req("exec-b"))
        assert len(posts) == 1
        assert first["sent"] is True
        assert second["ok"] is True and second["sent"] is False
        assert second["first_execution_id"] == "exec-a"
        assert [e["event_action"] for e in audit] == ["group_message_suppressed"]

        from fastapi import HTTPException
        async with __import__("services.idempotency_service", fromlist=["x"]).intent_guard(
            "group_message", agent_name=AGENT, target="telegram:-100", idempotency_key="busy",
            ttl_seconds=None, execution_id=None,
        ):
            with pytest.raises(HTTPException) as e:
                await telegram.send_telegram_group_message(
                    AGENT, "-100", TelegramGroupMessageRequest(message="x", idempotency_key="busy"))
        assert e.value.status_code == 409
