"""Telegram in-progress status indicator (ent#264).

Three layers under test:

1. **Telegram adapter** — 👀 reaction ack at dispatch, elapsed-time placeholder
   (send-then-edit) past the threshold, terminal teardown (clear reaction,
   delete placeholder, neutral edit-to-done fallback), all fail-soft, all
   per-turn state on ``NormalizedMessage.metadata`` (the adapter is a shared
   singleton handling concurrent turns).
2. **Router driver** — channel-agnostic start/progress/resolve seam: driver
   armed only for adapters declaring a threshold; every terminal cancels AND
   awaits the driver dead BEFORE ``indicate_done`` (no tick after resolve);
   the shielded in-flight first-send race is closed by construction.
3. **Toggle** — ``telegram_bindings.progress_indicator_enabled`` (default ON,
   dual-track migration), Python-side ``v is None or v != 0`` read predicate,
   owner-only human-only PUT, GET response field pinned end-to-end.

Modules: src/backend/adapters/{base,telegram_adapter,message_router}.py,
         src/backend/db/telegram_channels.py, src/backend/routers/telegram.py
Issue:   https://github.com/abilityai/trinity-enterprise/issues/264
"""

import asyncio
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import types
from pathlib import Path

# IMPORTANT: set REDIS_URL BEFORE any backend import (Issue #589 hard-fail).
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import db_backend, run  # noqa: E402,F401

from adapters import telegram_adapter as ta  # noqa: E402
from adapters import message_router as mr  # noqa: E402
from adapters.base import ChannelAdapter, NormalizedMessage  # noqa: E402
from adapters.telegram_adapter import TelegramAdapter  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _msg(*, is_group=False, eligible=True, thread_id="42", metadata=None):
    md = {
        "agent_name": "analytics",
        "bot_id": "b1",
        "is_group": is_group,
        "progress_ack_eligible": eligible,
    }
    if metadata:
        md.update(metadata)
    return NormalizedMessage(
        sender_id="u1",
        text="hi",
        channel_id="-100",
        thread_id=thread_id,
        timestamp="0",
        metadata=md,
    )


def _cfg(msg, *, enabled=True, token="123:tok"):
    msg.metadata["_progress_cfg"] = {"enabled": enabled, "bot_token": token}
    return msg


# ---------------------------------------------------------------------------
# Fake httpx transport — captures (method_url, payload) per call, pops queued
# responses (last one repeats).
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {"ok": True, "result": {"message_id": 777}}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def _fake_http(monkeypatch, responses=None):
    """Patch ta.httpx.AsyncClient; return the recorded [(url, payload)] list."""
    calls = []
    queue = list(responses or [])

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, **kw):
            calls.append((url, json))
            return queue.pop(0) if len(queue) > 1 else (queue[0] if queue else _FakeResp())

    monkeypatch.setattr(ta.httpx, "AsyncClient", _Client)
    return calls


# ===========================================================================
# Adapter — API primitives
# ===========================================================================


class TestSetMessageReaction:
    def test_set_payload_shape(self, monkeypatch):
        calls = _fake_http(monkeypatch)
        ok = _run(TelegramAdapter()._set_message_reaction("t", "-100", "42", "👀"))
        assert ok is True
        url, payload = calls[0]
        assert url.endswith("/setMessageReaction")
        assert payload["reaction"] == [{"type": "emoji", "emoji": "👀"}]
        assert payload["message_id"] == 42

    def test_clear_sends_empty_reaction_list(self, monkeypatch):
        calls = _fake_http(monkeypatch)
        assert _run(TelegramAdapter()._set_message_reaction("t", "-100", "42", None)) is True
        assert calls[0][1]["reaction"] == []

    def test_400_is_fail_soft(self, monkeypatch):
        """Reactions disabled per-chat / old chat → 400 → False, no raise."""
        _fake_http(monkeypatch, [_FakeResp(400, {"ok": False, "description": "reactions disabled"})])
        assert _run(TelegramAdapter()._set_message_reaction("t", "-100", "42", "👀")) is False

    def test_transport_error_is_fail_soft(self, monkeypatch):
        class _Boom:
            def __init__(self, *a, **k):
                raise RuntimeError("no network")

        monkeypatch.setattr(ta.httpx, "AsyncClient", _Boom)
        assert _run(TelegramAdapter()._set_message_reaction("t", "-100", "42", "👀")) is False


class TestEditMessageText:
    def test_429_sleeps_capped_retry_after_then_retries_once(self, monkeypatch):
        sleeps = []

        async def _sleep(s):
            sleeps.append(s)

        monkeypatch.setattr(ta.asyncio, "sleep", _sleep)
        calls = _fake_http(monkeypatch, [
            _FakeResp(429, {"ok": False, "parameters": {"retry_after": 120}}),
            _FakeResp(200, {"ok": True, "result": True}),
        ])
        ok = _run(TelegramAdapter()._edit_message_text("t", "-100", "777", "x"))
        assert ok is True
        assert len(calls) == 2          # exactly one retry
        assert sleeps == [30]           # retry_after honored but capped at 30s

    def test_message_not_modified_counts_as_success(self, monkeypatch):
        _fake_http(monkeypatch, [_FakeResp(
            400, {"ok": False, "description": "Bad Request: message is not modified"})])
        assert _run(TelegramAdapter()._edit_message_text("t", "-100", "777", "x")) is True

    def test_uses_html_parse_mode(self, monkeypatch):
        """Never MarkdownV2 — its reserved —/·/. would 400 on the template."""
        calls = _fake_http(monkeypatch)
        _run(TelegramAdapter()._edit_message_text("t", "-100", "777", "x"))
        assert calls[0][1]["parse_mode"] == "HTML"


class TestPlaceholderSend:
    def test_disable_notification_and_html(self, monkeypatch):
        """gemini-critic HIGH: a 'working…' ping must not push-notify."""
        calls = _fake_http(monkeypatch)
        _run(TelegramAdapter()._send_placeholder_message("t", "-100", "x", "42"))
        payload = calls[0][1]
        assert payload["disable_notification"] is True
        assert payload["parse_mode"] == "HTML"
        assert payload["reply_parameters"]["message_id"] == 42

    def test_dm_send_has_no_reply_parameters(self, monkeypatch):
        calls = _fake_http(monkeypatch)
        _run(TelegramAdapter()._send_placeholder_message("t", "5", "x", None))
        assert "reply_parameters" not in calls[0][1]


# ===========================================================================
# Adapter — indicate_processing
# ===========================================================================


def _fake_db(monkeypatch, *, binding="default", token="123:tok"):
    if binding == "default":
        binding = {
            "id": 1,
            "agent_name": "analytics",
            "bot_token_encrypted": "enc",
            "progress_indicator_enabled": 1,
        }
    fake = types.SimpleNamespace(
        get_telegram_binding=lambda name: binding,
        decrypt_telegram_bot_token=lambda enc: token,
    )
    monkeypatch.setattr(ta, "db", fake)
    return fake


class TestIndicateProcessing:
    def test_typing_always_sent_even_when_toggle_off(self, monkeypatch):
        """Behavior-preservation guard: the pre-ent#264 typing garnish fires on
        every turn regardless of the new toggle."""
        _fake_db(monkeypatch, binding={
            "id": 1, "agent_name": "analytics", "bot_token_encrypted": "enc",
            "progress_indicator_enabled": 0,
        })
        calls = _fake_http(monkeypatch)
        msg = _msg()
        _run(TelegramAdapter().indicate_processing(msg))
        assert [u for u, _ in calls if u.endswith("/sendChatAction")]
        assert not [u for u, _ in calls if u.endswith("/setMessageReaction")]
        assert msg.metadata["_progress_cfg"]["enabled"] is False

    def test_reaction_set_when_enabled_and_eligible(self, monkeypatch):
        _fake_db(monkeypatch)
        calls = _fake_http(monkeypatch)
        msg = _msg()
        _run(TelegramAdapter().indicate_processing(msg))
        reactions = [(u, p) for u, p in calls if u.endswith("/setMessageReaction")]
        assert len(reactions) == 1
        assert reactions[0][1]["reaction"] == [{"type": "emoji", "emoji": "👀"}]
        assert msg.metadata["_indicator_reaction_set"] is True

    def test_no_reaction_when_not_eligible(self, monkeypatch):
        """Observe-mode un-mentioned group turn: typing only — the indicator
        must never reveal a silently-observing bot."""
        _fake_db(monkeypatch)
        calls = _fake_http(monkeypatch)
        _run(TelegramAdapter().indicate_processing(_msg(is_group=True, eligible=False)))
        assert not [u for u, _ in calls if u.endswith("/setMessageReaction")]

    def test_no_reaction_on_empty_thread_id(self, monkeypatch):
        _fake_db(monkeypatch)
        calls = _fake_http(monkeypatch)
        _run(TelegramAdapter().indicate_processing(_msg(thread_id="")))
        assert not [u for u, _ in calls if u.endswith("/setMessageReaction")]

    def test_null_toggle_reads_as_enabled(self, monkeypatch):
        """Default-ON predicate: NULL/legacy value ⇒ enabled (only explicit 0
        disables) — evaluated in Python, never SQL."""
        _fake_db(monkeypatch, binding={
            "id": 1, "agent_name": "analytics", "bot_token_encrypted": "enc",
            "progress_indicator_enabled": None,
        })
        _fake_http(monkeypatch)
        msg = _msg()
        _run(TelegramAdapter().indicate_processing(msg))
        assert msg.metadata["_progress_cfg"]["enabled"] is True

    def test_never_raises_when_db_raises(self, monkeypatch):
        """eng-voice HIGH: the hook body (incl. the DB read) never raises — a
        raise here would abort the turn with no user-visible error."""
        def _boom(name):
            raise RuntimeError("db down")

        monkeypatch.setattr(ta, "db", types.SimpleNamespace(get_telegram_binding=_boom))
        _run(TelegramAdapter().indicate_processing(_msg()))  # must not raise

    def test_never_raises_when_http_raises(self, monkeypatch):
        _fake_db(monkeypatch)

        class _Boom:
            def __init__(self, *a, **k):
                raise RuntimeError("no network")

        monkeypatch.setattr(ta.httpx, "AsyncClient", _Boom)
        _run(TelegramAdapter().indicate_processing(_msg()))  # must not raise


# ===========================================================================
# Adapter — indicate_progress lifecycle
# ===========================================================================


class TestIndicateProgress:
    def test_first_call_sends_placeholder_then_edits(self, monkeypatch):
        calls = _fake_http(monkeypatch)
        adapter = TelegramAdapter()
        msg = _cfg(_msg(is_group=True))

        _run(adapter.indicate_progress(msg, 32.0))
        assert msg.metadata["_indicator_placeholder_id"] == "777"
        sends = [(u, p) for u, p in calls if u.endswith("/sendMessage")]
        assert len(sends) == 1
        assert sends[0][1]["reply_parameters"]["message_id"] == 42  # group → threaded

        _run(adapter.indicate_progress(msg, 95.0))
        edits = [(u, p) for u, p in calls if u.endswith("/editMessageText")]
        assert len(edits) == 1
        assert "1 min" in edits[0][1]["text"]

    def test_dm_placeholder_not_threaded(self, monkeypatch):
        calls = _fake_http(monkeypatch)
        msg = _cfg(_msg(is_group=False))
        _run(TelegramAdapter().indicate_progress(msg, 31.0))
        assert "reply_parameters" not in calls[0][1]

    def test_gate_no_http_when_disabled_or_ineligible(self, monkeypatch):
        calls = _fake_http(monkeypatch)
        adapter = TelegramAdapter()
        _run(adapter.indicate_progress(_cfg(_msg(), enabled=False), 31.0))
        _run(adapter.indicate_progress(_cfg(_msg(is_group=True, eligible=False)), 31.0))
        assert calls == []

    def test_two_consecutive_failures_degrade_and_stop_http(self, monkeypatch):
        """eng-voice 2am scenario: a fleet-wide Telegram outage must quiesce —
        after 2 consecutive failed attempts no further progress HTTP fires."""
        calls = _fake_http(monkeypatch, [_FakeResp(500, {"ok": False})])
        adapter = TelegramAdapter()
        msg = _cfg(_msg())
        _run(adapter.indicate_progress(msg, 31.0))   # send fails (1)
        _run(adapter.indicate_progress(msg, 91.0))   # send fails (2) → degraded
        assert msg.metadata["_indicator_degraded"] is True
        n = len(calls)
        _run(adapter.indicate_progress(msg, 151.0))  # no HTTP
        assert len(calls) == n

    def test_static_template_text_only(self, monkeypatch):
        """ent#224 egress class closed by construction: placeholder text is the
        elapsed-time template only — no agent/error content path exists."""
        calls = _fake_http(monkeypatch)
        msg = _cfg(_msg(metadata={"raw_message": {"text": "SECRET"}}))
        _run(TelegramAdapter().indicate_progress(msg, 45.0))
        text = calls[0][1]["text"]
        assert re.fullmatch(
            r"⏳ Working on it — (\d+s|\d+ min) elapsed · updated \d{2}:\d{2} UTC", text
        )
        assert "SECRET" not in text

    def test_metadata_isolation_across_concurrent_turns(self, monkeypatch):
        """The adapter is a long-lived shared singleton — two interleaved turns
        must never cross indicator state (dossier gotcha 6)."""
        _fake_http(monkeypatch, [
            _FakeResp(200, {"ok": True, "result": {"message_id": 111}}),
            _FakeResp(200, {"ok": True, "result": {"message_id": 222}}),
        ])
        adapter = TelegramAdapter()
        m1, m2 = _cfg(_msg()), _cfg(_msg())

        async def interleave():
            await adapter.indicate_progress(m1, 31.0)
            await adapter.indicate_progress(m2, 31.0)

        _run(interleave())
        assert m1.metadata["_indicator_placeholder_id"] == "111"
        assert m2.metadata["_indicator_placeholder_id"] == "222"
        assert "_indicator_degraded" not in m1.metadata


# ===========================================================================
# Adapter — indicate_done
# ===========================================================================


class TestIndicateDone:
    def test_clears_reaction_and_deletes_placeholder(self, monkeypatch):
        calls = _fake_http(monkeypatch)
        msg = _cfg(_msg())
        msg.metadata["_indicator_reaction_set"] = True
        msg.metadata["_indicator_placeholder_id"] = "777"
        _run(TelegramAdapter().indicate_done(msg))
        clears = [p for u, p in calls if u.endswith("/setMessageReaction")]
        deletes = [p for u, p in calls if u.endswith("/deleteMessage")]
        assert clears and clears[0]["reaction"] == []  # cleared, no 👍/✅ swap
        assert deletes and deletes[0]["message_id"] == 777

    def test_delete_failure_falls_back_to_neutral_edit(self, monkeypatch):
        calls = _fake_http(monkeypatch, [
            _FakeResp(400, {"ok": False, "description": "message can't be deleted"}),
            _FakeResp(200, {"ok": True, "result": True}),
        ])
        msg = _cfg(_msg())
        msg.metadata["_indicator_placeholder_id"] = "777"
        msg.metadata["_indicator_terminal_ok"] = True
        _run(TelegramAdapter().indicate_done(msg))
        edits = [p for u, p in calls if u.endswith("/editMessageText")]
        assert edits and edits[0]["text"] == "✔ Done."   # NOT "see reply below"

    def test_failed_terminal_fallback_text(self, monkeypatch):
        calls = _fake_http(monkeypatch, [
            _FakeResp(400, {"ok": False, "description": "message can't be deleted"}),
            _FakeResp(200, {"ok": True, "result": True}),
        ])
        msg = _cfg(_msg())
        msg.metadata["_indicator_placeholder_id"] = "777"
        msg.metadata["_indicator_terminal_ok"] = False
        _run(TelegramAdapter().indicate_done(msg))
        edits = [p for u, p in calls if u.endswith("/editMessageText")]
        assert edits and edits[0]["text"] == "⚠️ Finished with an error."

    def test_noop_when_nothing_armed(self, monkeypatch):
        calls = _fake_http(monkeypatch)
        _run(TelegramAdapter().indicate_done(_cfg(_msg())))
        assert calls == []

    def test_noop_without_cfg(self, monkeypatch):
        """Non-Telegram state absent (e.g. hook fired before processing)."""
        calls = _fake_http(monkeypatch)
        _run(TelegramAdapter().indicate_done(_msg()))
        assert calls == []

    def test_never_raises(self, monkeypatch):
        class _Boom:
            def __init__(self, *a, **k):
                raise RuntimeError("no network")

        monkeypatch.setattr(ta.httpx, "AsyncClient", _Boom)
        msg = _cfg(_msg())
        msg.metadata["_indicator_reaction_set"] = True
        _run(TelegramAdapter().indicate_done(msg))  # must not raise


# ===========================================================================
# parse_message — ack-eligibility gate
# ===========================================================================


def _tg_update(*, group=False, mention=False, reply=False, text="hello"):
    message = {
        "message_id": 42,
        "date": 1,
        "from": {"id": 9, "is_bot": False, "username": "alice"},
        "chat": {"id": -100 if group else 9,
                 "type": "supergroup" if group else "private"},
        "text": (f"@bot {text}" if mention else text),
    }
    if mention:
        message["entities"] = [{"type": "mention", "offset": 0, "length": 4}]
    if reply:
        message["reply_to_message"] = {"from": {"id": 77, "is_bot": True}}
    return {"message": message, "_bot_id": "77", "_bot_username": "bot",
            "_agent_name": "analytics"}


class TestParseMessageAckGate:
    def _parse(self, monkeypatch, update, trigger_mode=None):
        fake = types.SimpleNamespace(
            get_telegram_binding=lambda name: {"id": 1, "agent_name": name},
            get_telegram_group_config=lambda b, c: (
                {"trigger_mode": trigger_mode} if trigger_mode else None),
        )
        monkeypatch.setattr(ta, "db", fake)
        return TelegramAdapter().parse_message(update)

    def test_dm_is_eligible(self, monkeypatch):
        msg = self._parse(monkeypatch, _tg_update())
        assert msg.metadata["progress_ack_eligible"] is True

    def test_group_mention_is_eligible(self, monkeypatch):
        msg = self._parse(monkeypatch, _tg_update(group=True, mention=True))
        assert msg.metadata["progress_ack_eligible"] is True

    def test_group_reply_to_bot_is_eligible(self, monkeypatch):
        msg = self._parse(monkeypatch, _tg_update(group=True, reply=True))
        assert msg.metadata["progress_ack_eligible"] is True

    def test_all_mode_unmentioned_is_eligible(self, monkeypatch):
        """Orchestrator amendment: `all` trigger mode always replies, so those
        turns get the ack — excluding them would recreate the zero-feedback
        problem exactly where the bot is used most."""
        msg = self._parse(monkeypatch, _tg_update(group=True), trigger_mode="all")
        assert msg.metadata["progress_ack_eligible"] is True

    def test_observe_mode_unmentioned_is_not_eligible(self, monkeypatch):
        msg = self._parse(monkeypatch, _tg_update(group=True), trigger_mode="observe")
        assert msg is not None
        assert msg.metadata["progress_ack_eligible"] is False


# ===========================================================================
# Router driver — arm / tick / resolve / backstop
# ===========================================================================


class _StubAdapter(ChannelAdapter):
    """Minimal concrete adapter with a recordable indicator surface."""

    progress_threshold_seconds = 0.02
    progress_interval_seconds = 0.02

    def __init__(self):
        self.events = []

    @property
    def channel_type(self):
        return "stub"

    def get_rate_key(self, message):
        return "stub"

    def get_session_identifier(self, message):
        return "stub"

    def get_source_identifier(self, message):
        return "stub"

    def get_bot_token(self, message):
        return None

    async def get_agent_name(self, message):
        return "analytics"

    def parse_message(self, raw_event):
        return None

    async def send_response(self, channel_id, response, thread_id=None):
        self.events.append("send")

    async def indicate_progress(self, message, elapsed_seconds):
        self.events.append(("tick", elapsed_seconds))

    async def indicate_done(self, message):
        self.events.append("done")


class _NoProgressAdapter(_StubAdapter):
    progress_threshold_seconds = None  # Slack/WhatsApp/VoIP shape today


def _router():
    return mr.ChannelMessageRouter()


class TestRouterDriver:
    def test_not_armed_without_threshold(self):
        """Behavior-preservation guard: a None-threshold adapter (Slack shape)
        never gets a driver task."""
        router, adapter, msg = _router(), _NoProgressAdapter(), _msg()
        router._arm_progress_driver(adapter, msg)
        assert "_indicator_driver" not in msg.metadata

    def test_armed_with_threshold_and_resolved(self):
        async def scenario():
            router, adapter, msg = _router(), _StubAdapter(), _msg()
            router._arm_progress_driver(adapter, msg)
            task = msg.metadata["_indicator_driver"]
            await asyncio.sleep(0.08)  # past threshold → ≥1 tick
            await router._resolve_indicator(adapter, msg, success=True)
            assert task.done()
            assert "_indicator_driver" not in msg.metadata
            return adapter.events

        events = _run(scenario())
        ticks = [e for e in events if isinstance(e, tuple) and e[0] == "tick"]
        assert ticks, "driver should have ticked past the threshold"
        assert events[-1] == "done", "indicate_done runs AFTER the driver stops"

    def test_no_tick_before_threshold(self):
        async def scenario():
            router = _router()
            adapter = _StubAdapter()
            adapter.progress_threshold_seconds = 5.0
            msg = _msg()
            router._arm_progress_driver(adapter, msg)
            await asyncio.sleep(0.05)
            await router._resolve_indicator(adapter, msg)
            return adapter.events

        events = _run(scenario())
        assert not [e for e in events if isinstance(e, tuple)]

    def test_first_tick_reports_at_least_threshold_elapsed(self):
        """Elapsed origin is captured BEFORE the threshold sleep, so the first
        tick reports ~threshold seconds, not ~0."""
        async def scenario():
            router, adapter, msg = _router(), _StubAdapter(), _msg()
            router._arm_progress_driver(adapter, msg)
            await asyncio.sleep(0.06)
            await router._resolve_indicator(adapter, msg)
            return adapter.events

        events = _run(scenario())
        ticks = [e[1] for e in events if isinstance(e, tuple)]
        assert ticks and ticks[0] >= _StubAdapter.progress_threshold_seconds
        assert ticks == sorted(ticks), "elapsed must be increasing"

    def test_raising_tick_does_not_kill_the_loop(self):
        class _Raising(_StubAdapter):
            async def indicate_progress(self, message, elapsed_seconds):
                self.events.append(("tick", elapsed_seconds))
                raise RuntimeError("tick boom")

        async def scenario():
            router, adapter, msg = _router(), _Raising(), _msg()
            router._arm_progress_driver(adapter, msg)
            await asyncio.sleep(0.09)
            await router._resolve_indicator(adapter, msg)
            return adapter.events

        events = _run(scenario())
        assert len([e for e in events if isinstance(e, tuple)]) >= 2

    def test_resolve_orders_cancel_before_done_on_all_terminals(self):
        """All three router terminals call _resolve_indicator, which awaits the
        driver dead BEFORE indicate_done — asserted via event ordering with a
        tick mid-flight."""
        class _SlowTick(_StubAdapter):
            progress_threshold_seconds = 0.0
            progress_interval_seconds = 0.0

            async def indicate_progress(self, message, elapsed_seconds):
                self.events.append("tick-start")
                await asyncio.sleep(10)  # parked mid-tick until cancelled
                self.events.append("tick-end")  # must never run

        async def scenario(success):
            router, adapter, msg = _router(), _SlowTick(), _msg()
            router._arm_progress_driver(adapter, msg)
            await asyncio.sleep(0.02)  # let the tick park
            await router._resolve_indicator(adapter, msg, success=success)
            assert msg.metadata["_indicator_terminal_ok"] is success
            return adapter.events

        for success in (True, False):
            events = _run(scenario(success))
            assert "tick-end" not in events
            assert events[-1] == "done"

    def test_backstop_is_idempotent_after_resolve(self):
        async def scenario():
            router, adapter, msg = _router(), _StubAdapter(), _msg()
            router._arm_progress_driver(adapter, msg)
            await router._resolve_indicator(adapter, msg)
            done_count = adapter.events.count("done")
            await router._cancel_progress_driver(msg)  # safe double-pop
            return done_count, adapter.events.count("done")

        before, after = _run(scenario())
        assert before == after == 1  # backstop never re-runs indicate_done

    def test_backstop_kills_a_still_armed_driver(self):
        async def scenario():
            router, adapter, msg = _router(), _StubAdapter(), _msg()
            router._arm_progress_driver(adapter, msg)
            task = msg.metadata["_indicator_driver"]
            await router._cancel_progress_driver(msg)
            return task.done(), "_indicator_driver" in msg.metadata

        done, still_there = _run(scenario())
        assert done and not still_there

    def test_resolve_swallows_a_raising_indicate_done(self):
        class _RaisingDone(_StubAdapter):
            async def indicate_done(self, message):
                raise RuntimeError("done boom")

        async def scenario():
            router, adapter, msg = _router(), _RaisingDone(), _msg()
            await router._resolve_indicator(adapter, msg)  # must not raise

        _run(scenario())

    def test_step8_call_and_arm_are_wrapped(self):
        """Regression guard (eng-voice): the step-8 indicate_processing call +
        arm live inside a try/except in the caller — a raising adapter hook
        must never abort the turn."""
        import inspect

        src = inspect.getsource(mr.ChannelMessageRouter._handle_message_inner)
        m = re.search(
            r"try:\s*\n\s*await adapter\.indicate_processing\(message\)\s*\n"
            r"\s*self\._arm_progress_driver\(adapter, message\)\s*\n\s*except Exception",
            src,
        )
        assert m, "step-8 indicate_processing/arm must be try/except-wrapped"
        assert "finally:" in src and "_cancel_progress_driver" in src


class TestResolveVsFirstSendRace:
    def test_resolve_during_first_send_records_id_and_deletes(self, monkeypatch):
        """Cross-phase HIGH: resolve fired while the first placeholder send is
        mid-await → the shielded future completes, the id is recorded, and
        indicate_done deletes it — no stranded 'working…'."""
        deleted = []

        async def scenario():
            router = _router()
            adapter = TelegramAdapter()
            adapter.progress_threshold_seconds = 0.0
            adapter.progress_interval_seconds = 60.0
            msg = _cfg(_msg())

            started = asyncio.Event()
            release = asyncio.Event()

            async def slow_send(bot_token, chat_id, text, reply_to=None):
                started.set()
                await release.wait()
                return {"message_id": 777}

            monkeypatch.setattr(adapter, "_send_placeholder_message", slow_send)
            monkeypatch.setattr(adapter, "_delete_message",
                                lambda *a: _record_delete(deleted, a))
            monkeypatch.setattr(adapter, "_set_message_reaction",
                                _async_true)

            router._arm_progress_driver(adapter, msg)
            await started.wait()          # first send is mid-await
            release_later = asyncio.get_event_loop().call_later(0.05, release.set)
            await router._resolve_indicator(adapter, msg, success=True)
            release_later.cancel()
            return msg, deleted

        msg, deleted_calls = _run(scenario())
        assert msg.metadata.get("_indicator_placeholder_id") is None  # popped by done
        assert deleted_calls and deleted_calls[0][2] == "777"

    def test_cancel_during_429_backoff_is_prompt(self, monkeypatch):
        """A resolve landing during the edit's 429 retry_after sleep (up to
        30s) must cancel promptly — the resolve path is never blocked for the
        full backoff."""
        _fake_http(monkeypatch, [_FakeResp(429, {"ok": False,
                                                 "parameters": {"retry_after": 30}})])

        async def scenario():
            adapter = TelegramAdapter()
            msg = _cfg(_msg())
            msg.metadata["_indicator_placeholder_id"] = "777"
            tick = asyncio.ensure_future(adapter.indicate_progress(msg, 90.0))
            await asyncio.sleep(0.05)     # let it enter the backoff sleep
            t0 = time.monotonic()
            tick.cancel()
            await asyncio.gather(tick, return_exceptions=True)
            return time.monotonic() - t0

        assert _run(scenario()) < 1.0


async def _async_true(*a, **k):
    return True


async def _record_delete(sink, args):
    sink.append(args)
    return True


# ===========================================================================
# [NO_REPLY] path still resolves the indicator
# ===========================================================================


class TestNoReplyStillResolves:
    def test_finalize_resolves_before_no_reply_return(self, monkeypatch):
        fake_db = types.SimpleNamespace(
            add_public_chat_message=lambda *a, **k: None,
            increment_public_user_memory_count=lambda *a, **k: 0,
        )
        monkeypatch.setattr(mr, "db", fake_db)
        monkeypatch.setattr(mr, "_agent_avatar_url", lambda name: None)

        async def scenario():
            router, adapter, msg = _router(), _StubAdapter(), _msg()
            router._arm_progress_driver(adapter, msg)
            result = types.SimpleNamespace(cost=0.0, execution_id="e1",
                                           response="[NO_REPLY]")
            await router._finalize_response(
                adapter, msg, "analytics", "tok", "stub", False,
                None, None, "s1", None, result, "[NO_REPLY]",
            )
            return adapter.events, msg.metadata

        events, metadata = _run(scenario())
        assert "done" in events            # indicator resolved…
        assert "send" not in events        # …even though nothing was sent
        assert "_indicator_driver" not in metadata


# ===========================================================================
# Toggle — real-DB harness (learnings: #1533 no-MagicMock-facade, 4-file rule)
# ===========================================================================


@pytest.fixture
def encryption_key(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))


def _seed_binding(agent="analytics", enabled=None):
    cols = "agent_name, bot_token_encrypted, webhook_secret, created_at"
    vals = ":a, 'enc', :ws, 't'"
    if enabled is not None:
        cols += ", progress_indicator_enabled"
        vals += ", :en"
        run(f"INSERT INTO telegram_bindings ({cols}) VALUES ({vals})",
            a=agent, ws=secrets.token_hex(8), en=enabled)
    else:
        run(f"INSERT INTO telegram_bindings ({cols}) VALUES ({vals})",
            a=agent, ws=secrets.token_hex(8))


class TestToggleRealDb:
    def test_live_select_of_the_new_column(self, db_backend):
        """4-file schema rule: schema-parity CI never imports db/tables.py — a
        live select through the Core Table fails loudly if it was missed."""
        from sqlalchemy import select
        from db.engine import get_engine
        from db.tables import telegram_bindings

        _seed_binding()
        with get_engine().connect() as conn:
            row = conn.execute(
                select(telegram_bindings.c.progress_indicator_enabled)
            ).first()
        assert row is not None and row[0] == 1  # DDL default ON

    def test_binding_dict_surfaces_the_flag(self, db_backend):
        from db.telegram_channels import TelegramChannelOperations

        _seed_binding()
        binding = TelegramChannelOperations().get_binding_by_agent("analytics")
        assert binding is not None
        assert binding["progress_indicator_enabled"] == 1

    def test_facade_set_toggle_round_trip(self, db_backend):
        """Real facade on a real DB (#1533: a MagicMock'd `database` module
        auto-passes even when the facade method doesn't exist)."""
        import database

        _seed_binding()
        assert database.db.set_telegram_progress_indicator("analytics", False) is True
        binding = database.db.get_telegram_binding("analytics")
        assert binding["progress_indicator_enabled"] == 0
        assert database.db.set_telegram_progress_indicator("analytics", True) is True
        assert database.db.get_telegram_binding("analytics")["progress_indicator_enabled"] == 1

    def test_facade_set_toggle_unknown_agent_reports_miss(self, db_backend):
        import database

        assert database.db.set_telegram_progress_indicator("nope", True) is False

    def test_create_binding_defaults_on(self, db_backend, encryption_key):
        from db.telegram_channels import TelegramChannelOperations

        ops = TelegramChannelOperations()
        binding = ops.create_binding("analytics", "123:tok", "bot", "77")
        assert binding["progress_indicator_enabled"] == 1

    def test_decrypt_bot_token_from_row(self, db_backend, encryption_key):
        """ent#264 single-binding-read support: decrypt from the row in hand."""
        from db.telegram_channels import TelegramChannelOperations

        ops = TelegramChannelOperations()
        binding = ops.create_binding("analytics", "123:tok", "bot", "77")
        assert ops.decrypt_bot_token(binding["bot_token_encrypted"]) == "123:tok"
        assert ops.decrypt_bot_token("") is None


class TestSqliteMigration:
    def test_legacy_table_gains_column_with_default_on(self, tmp_path):
        """Existing rows read 1 after ALTER TABLE ... DEFAULT 1 — no backfill
        UPDATE needed (unlike ent#223's deny-for-new posture)."""
        from db.migrations import _migrate_telegram_progress_indicator

        conn = sqlite3.connect(str(tmp_path / "legacy.db"))
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE telegram_bindings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL UNIQUE,
                bot_token_encrypted TEXT NOT NULL,
                webhook_secret TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            "INSERT INTO telegram_bindings "
            "(agent_name, bot_token_encrypted, webhook_secret, created_at) "
            "VALUES ('a', 'enc', 'ws', 't')"
        )
        conn.commit()

        _migrate_telegram_progress_indicator(cur, conn)
        conn.commit()

        cur.execute("SELECT progress_indicator_enabled FROM telegram_bindings")
        assert cur.fetchone()[0] == 1

        # Idempotent — safe re-run (the _safe_add_column contract).
        _migrate_telegram_progress_indicator(cur, conn)
        conn.close()

    def test_alembic_twin_exists(self):
        """MEMORY.md caveat: schema-parity CI only guards the SQLite track —
        the Postgres Alembic revision must exist in the tree. Glob (not a
        pinned filename) so the renumber-at-rebase rule vs ent#265 doesn't
        break the test."""
        versions = _BACKEND / "migrations" / "versions"
        hits = [
            p for p in versions.glob("*.py")
            if "progress_indicator_enabled" in p.read_text()
        ]
        assert hits, "missing Alembic revision for telegram progress_indicator_enabled"


# ===========================================================================
# Router endpoints — GET field pinning + PUT guards
# ===========================================================================


class TestTelegramEndpoints:
    def _fake_router_db(self, monkeypatch, binding="default"):
        from routers import telegram as tg

        if binding == "default":
            binding = {
                "id": 1, "agent_name": "analytics", "bot_username": "bot",
                "bot_id": "77", "webhook_url": "https://x/api/telegram/webhook/s",
                "bot_token_encrypted": "enc",
                "progress_indicator_enabled": None,  # legacy NULL row
            }
        sets = []
        fake = types.SimpleNamespace(
            get_telegram_binding=lambda name: binding,
            get_telegram_groups_for_agent=lambda name: [],
            set_telegram_progress_indicator=lambda name, en: sets.append((name, en)) or True,
        )
        monkeypatch.setattr(tg, "db", fake)
        return tg, fake, sets

    def test_get_pins_the_flag_in_the_response_json(self, monkeypatch):
        """learnings #1809: the router hand-builds TelegramBindingResponse — an
        additive model field silently drops unless the constructor passes it.
        Pin the serialized JSON, not just the model."""
        tg, _, _ = self._fake_router_db(monkeypatch)
        resp = _run(tg.get_telegram_binding("analytics"))
        data = resp.model_dump()
        assert data["progress_indicator_enabled"] is True  # NULL row → enabled
        assert data["configured"] is True

    def test_get_unconfigured_reports_none(self, monkeypatch):
        tg, _, _ = self._fake_router_db(monkeypatch, binding=None)
        data = _run(tg.get_telegram_binding("analytics")).model_dump()
        assert data["configured"] is False
        assert data["progress_indicator_enabled"] is None

    def test_get_route_is_access_hardened(self):
        """Recorded scope add (ent#264): GET /telegram moved from bare
        get_current_user to the uniform-404 AuthorizedAgentByName accessor —
        the response embeds webhook_url (⊃ webhook_secret)."""
        from routers import telegram as tg

        route = next(
            r for r in tg.auth_router.routes
            if getattr(r, "path", "") == "/api/agents/{agent_name}/telegram"
            and "GET" in getattr(r, "methods", set())
        )
        dep_names = [d.call.__name__ for d in route.dependant.dependencies]
        flat = dep_names + [
            d2.call.__name__
            for d in route.dependant.dependencies
            for d2 in d.dependencies
        ]
        assert "get_authorized_agent_by_name" in dep_names + flat

    def test_put_round_trip(self, monkeypatch):
        from models import TelegramProgressIndicatorRequest, User

        tg, _, sets = self._fake_router_db(monkeypatch)
        user = User(id=1, username="owner", role="admin")
        resp = _run(tg.set_telegram_progress_indicator(
            "analytics", TelegramProgressIndicatorRequest(enabled=False), user))
        assert sets == [("analytics", False)]
        assert resp.model_dump()["progress_indicator_enabled"] is False

    def test_put_404_when_unconfigured(self, monkeypatch):
        from fastapi import HTTPException
        from models import TelegramProgressIndicatorRequest, User

        tg, _, _ = self._fake_router_db(monkeypatch, binding=None)
        user = User(id=1, username="owner", role="admin")
        with pytest.raises(HTTPException) as exc:
            _run(tg.set_telegram_progress_indicator(
                "analytics", TelegramProgressIndicatorRequest(enabled=True), user))
        assert exc.value.status_code == 404

    def test_put_rejects_agent_principal(self, monkeypatch):
        """learnings ent#223 / trinity#1763: an agent-scoped key resolves to
        the OWNER on REST — behavior toggles are human-only (403)."""
        from fastapi import HTTPException
        from models import TelegramProgressIndicatorRequest, User

        tg, _, sets = self._fake_router_db(monkeypatch)
        agent_principal = User(id=1, username="owner", role="admin",
                               agent_name="analytics")
        with pytest.raises(HTTPException) as exc:
            _run(tg.set_telegram_progress_indicator(
                "analytics", TelegramProgressIndicatorRequest(enabled=True),
                agent_principal))
        assert exc.value.status_code == 403
        assert sets == []  # rejected BEFORE any write

    def test_read_predicate_matrix(self):
        from routers.telegram import _progress_indicator_enabled

        assert _progress_indicator_enabled({"progress_indicator_enabled": None}) is True
        assert _progress_indicator_enabled({}) is True
        assert _progress_indicator_enabled({"progress_indicator_enabled": 1}) is True
        assert _progress_indicator_enabled({"progress_indicator_enabled": 0}) is False
