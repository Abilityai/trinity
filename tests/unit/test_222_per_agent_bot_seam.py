"""The OSS per-agent bot resolution seam (ent#222).

The seam is edition-agnostic and inert by default: with no resolver registered,
every path is a no-op and channel routing is unchanged. Once the enterprise
module registers a resolver + token provider, an event received by an agent's
dedicated bot resolves to that agent. Both hooks fail OPEN.
"""
from __future__ import annotations

import types

import pytest

from adapters import per_agent_bot


def _msg(metadata=None):
    return types.SimpleNamespace(metadata=metadata or {})


@pytest.fixture(autouse=True)
def _reset_hooks():
    per_agent_bot.set_resolver(None)
    per_agent_bot.set_token_provider(None)
    yield
    per_agent_bot.set_resolver(None)
    per_agent_bot.set_token_provider(None)


def test_inert_by_default():
    assert per_agent_bot.is_active() is False
    # A real Slack-shaped message still resolves to nothing with no resolver.
    m = _msg({"slack_team_id": "T1", "slack_recipient_bot_user_id": "UBOT"})
    assert per_agent_bot.resolve_from_message(m) is None
    assert per_agent_bot.get_token("analytics") is None


def test_resolver_reads_recipient_bot_and_returns_agent():
    seen = {}

    def resolver(team_id, bot_user_id, app_id):
        seen.update(team_id=team_id, bot_user_id=bot_user_id, app_id=app_id)
        return "analytics" if bot_user_id == "U_ANALYTICS" else None

    per_agent_bot.set_resolver(resolver)
    m = _msg({"slack_team_id": "T1", "slack_recipient_bot_user_id": "U_ANALYTICS",
              "slack_recipient_app_id": "A1"})
    assert per_agent_bot.resolve_from_message(m) == "analytics"
    assert seen == {"team_id": "T1", "bot_user_id": "U_ANALYTICS", "app_id": "A1"}


def test_non_slack_message_is_ignored():
    per_agent_bot.set_resolver(lambda *a: "should-not-be-used")
    # A Telegram/other message carries none of the slack_* metadata → inert.
    assert per_agent_bot.resolve_from_message(_msg({"telegram_chat_id": "123"})) is None
    assert per_agent_bot.resolve_from_message(_msg(None)) is None


def test_resolver_and_token_provider_fail_open():
    def boom(*a):
        raise RuntimeError("db down")

    per_agent_bot.set_resolver(boom)
    per_agent_bot.set_token_provider(boom)
    m = _msg({"slack_team_id": "T1", "slack_recipient_bot_user_id": "UBOT"})
    # Both swallow the error and fall back (None) rather than break routing.
    assert per_agent_bot.resolve_from_message(m) is None
    assert per_agent_bot.get_token("analytics") is None


def test_token_provider_returns_per_agent_token():
    per_agent_bot.set_token_provider(lambda name: "xoxb-analytics" if name == "analytics" else None)
    assert per_agent_bot.get_token("analytics") == "xoxb-analytics"
    assert per_agent_bot.get_token("other") is None
