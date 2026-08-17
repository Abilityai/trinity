"""Report a finished task back to its originating Slack channel/thread (ent#224).

The property that matters most here is the **no-double-post rule**: a direct
channel turn is synchronous and the adapter already replies inline, so reporting
on it would duplicate every normal Slack reply in a customer workspace. Only an
execution that INHERITED its channel context (a delegated/background terminal)
may report.

Module: src/backend/services/channel_completion_report.py
Issue:  https://github.com/Abilityai/trinity-enterprise/issues/224
"""

import asyncio
import os
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services import channel_completion_report as ccr  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _execution(*, triggered_by="agent", channel="slack", chat_id="C123", thread="1.5"):
    return types.SimpleNamespace(
        source_channel=channel,
        source_channel_chat_id=chat_id,
        source_channel_thread=thread,
        triggered_by=triggered_by,
    )


@pytest.fixture
def wired(monkeypatch):
    """Wire every collaborator; return the list of sends that actually happened."""
    sent = []

    async def _send(**kwargs):
        sent.append(kwargs)
        return (True, None, "9999.0001")

    fake_slack = types.SimpleNamespace(send_message_detailed=_send)
    monkeypatch.setitem(sys.modules, "services.slack_service",
                        types.SimpleNamespace(slack_service=fake_slack))

    # ent#265: consent is keyed on the BINDING agent (row.source_channel_agent
    # or the executing agent). The fixture rows carry no source_channel_agent,
    # so the lookup key is the executing "analytics" — pinning that the legacy
    # NULL path still resolves consent under the old identity.
    fake_db = types.SimpleNamespace(
        get_execution=lambda eid: _execution(),
        get_slack_channels_for_agent=lambda a: (
            [{"slack_channel_id": "C123", "team_id": "T1", "allow_proactive": True}]
            if a == "analytics" else []
        ),
        get_slack_workspace_bot_token=lambda t: "xoxb-test",
    )
    monkeypatch.setitem(sys.modules, "database", types.SimpleNamespace(db=fake_db))

    class _G:
        replay = False
        snapshot = None

    @asynccontextmanager
    async def _guard(*a, **k):
        yield _G()

    monkeypatch.setitem(
        sys.modules, "services.idempotency_service",
        types.SimpleNamespace(effect_guard=_guard,
                              EffectInProgressError=type("E", (Exception,), {})))
    return sent, fake_db


def _report(**over):
    kw = dict(execution_id="e1", agent_name="analytics",
              status="success", summary_or_error="done")
    kw.update(over)
    return _run(ccr.report_completion(**kw))


class TestNoDoublePost:
    @pytest.mark.parametrize("trigger", ["slack", "telegram", "whatsapp"])
    def test_inline_channel_turn_is_never_reported(self, wired, trigger):
        """The adapter already answered this turn — reporting would duplicate
        every normal Slack reply."""
        sent, db = wired
        db.get_execution = lambda eid: _execution(triggered_by=trigger)
        assert _report() is False
        assert sent == []

    def test_delegated_terminal_is_reported(self, wired):
        """A→B delegation: B inherited A's thread, so B's completion reports."""
        sent, _ = wired
        assert _report() is True
        assert len(sent) == 1
        assert sent[0]["channel"] == "C123"
        assert sent[0]["thread_ts"] == "1.5"      # answers IN the originating thread


class TestConsentGate:
    def test_no_consent_means_no_post(self, wired):
        """ent#223 consent is required for an unprompted post."""
        sent, db = wired
        db.get_slack_channels_for_agent = lambda a: [
            {"slack_channel_id": "C123", "team_id": "T1", "allow_proactive": False}]
        assert _report() is False
        assert sent == []

    def test_unbound_channel_means_no_post(self, wired):
        sent, db = wired
        db.get_slack_channels_for_agent = lambda a: []
        assert _report() is False
        assert sent == []


class TestScope:
    def test_no_channel_context_is_a_noop(self, wired):
        sent, db = wired
        db.get_execution = lambda eid: _execution(channel=None, chat_id=None)
        assert _report() is False
        assert sent == []

    def test_unsupported_channel_is_out_of_scope(self, wired):
        """ent#265 conscious edit: telegram grew its own delivery leg, so the
        remaining channel WITHOUT a resolver is whatsapp (see
        test_265_telegram_completion_report.py for the telegram leg)."""
        sent, db = wired
        db.get_execution = lambda eid: _execution(channel="whatsapp", triggered_by="agent")
        assert _report() is False
        assert sent == []

    def test_failure_terminals_report_too(self, wired):
        """Honest status — a silent failure is the bug this closes."""
        sent, _ = wired
        assert _report(status="failed", summary_or_error="boom") is True
        assert "failed" in sent[0]["text"]

    def test_missing_execution_row_is_a_noop(self, wired):
        sent, db = wired
        db.get_execution = lambda eid: None
        assert _report() is False
        assert sent == []


class TestIdempotency:
    def test_replay_does_not_repost(self, wired, monkeypatch):
        """A re-delivered/retried terminal must not post the completion twice."""
        sent, _ = wired

        class _G:
            replay = True
            snapshot = {"reported": True}

        @asynccontextmanager
        async def _guard(*a, **k):
            yield _G()

        monkeypatch.setitem(
            sys.modules, "services.idempotency_service",
            types.SimpleNamespace(effect_guard=_guard,
                                  EffectInProgressError=type("E", (Exception,), {})))
        assert _report() is False
        assert sent == []


def test_reporter_never_raises(wired):
    """A reporting failure must never disturb an execution that already finished."""
    sent, db = wired

    def _boom(_):
        raise RuntimeError("db down")

    db.get_execution = _boom
    assert _report() is False        # swallowed, not raised


# Assembled at runtime, never written as a literal: this is a PUBLIC repo and a
# realistic token literal trips GitHub push protection (and is the "hardcoded
# credential in a test" the review checklist forbids). The sanitiser still sees a
# complete, well-formed token at run time.
_FAKE_SLACK_TOKEN = "-".join(["xoxb", "1" * 10, "2" * 10, "a" * 24])


class TestCredentialSafety:
    def test_failure_text_is_sanitised_before_it_reaches_slack(self, wired):
        """A failure terminal's error text can carry secrets — that is why the
        #1578 emit chokepoint sanitises. Slack is a persistent, externally hosted,
        human-visible surface, so it must not be the one egress that skips it."""
        sent, _ = wired
        assert _report(status="failed",
                       summary_or_error=f"boom: {_FAKE_SLACK_TOKEN}") is True
        assert _FAKE_SLACK_TOKEN not in sent[0]["text"], (
            "a raw Slack bot token was posted into a channel"
        )

    def test_short_text_gets_no_phantom_ellipsis(self, wired):
        """Redaction changes length, so truncation must be decided on what was
        actually cut — comparing against the raw input would mark a short,
        redacted message as truncated."""
        sent, _ = wired
        assert _report(status="failed",
                       summary_or_error=f"failed: {_FAKE_SLACK_TOKEN}") is True
        assert not sent[0]["text"].endswith("…"), "nothing was truncated, yet an ellipsis was added"
