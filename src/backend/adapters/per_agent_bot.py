"""Per-agent bot resolution seam (ent#222) — the OSS half of per-agent Slack bots.

Edition-agnostic and **inert by default**: with no resolver registered (OSS-only
builds, or before the enterprise module loads) every function here is a no-op and
channel routing is byte-for-byte unchanged. The private ``slack_per_agent_bots``
enterprise module registers a resolver + token provider at startup; from then on
an inbound Slack event received by an agent's *dedicated* bot resolves to that
agent (over the channel binding) and replies through that bot's own token.

Mirrors the connector-seam pattern (#118): the OSS code owns the seam, the
enterprise module owns the policy. Both hooks fail **open** — any error falls
back to normal channel routing, so a bug here can never take Slack offline.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# (team_id, bot_user_id, app_id) -> agent_name | None
_resolver: Optional[Callable[[Optional[str], Optional[str], Optional[str]], Optional[str]]] = None
# agent_name -> per-agent bot token | None
_token_provider: Optional[Callable[[str], Optional[str]]] = None


def set_resolver(fn: Optional[Callable]) -> None:
    """Install the enterprise resolver. ``None`` disables (returns to inert)."""
    global _resolver
    _resolver = fn


def set_token_provider(fn: Optional[Callable]) -> None:
    global _token_provider
    _token_provider = fn


def is_active() -> bool:
    return _resolver is not None


def resolve_from_message(message) -> Optional[str]:
    """The agent whose dedicated bot RECEIVED this event, or None.

    Reads only the receiving-bot identity the Slack adapter stamps into
    ``message.metadata`` (``slack_team_id`` + ``slack_recipient_bot_user_id`` /
    ``slack_recipient_app_id``). Non-Slack messages carry none of these, so this
    is inert for every other channel. Fail-open: any error → None → normal
    channel routing.
    """
    if _resolver is None:
        return None
    md = getattr(message, "metadata", None) or {}
    team_id = md.get("slack_team_id")
    bot_user_id = md.get("slack_recipient_bot_user_id")
    app_id = md.get("slack_recipient_app_id")
    if not team_id or not (bot_user_id or app_id):
        return None
    try:
        return _resolver(team_id, bot_user_id, app_id)
    except Exception as e:  # noqa: BLE001 — never break routing
        logger.warning("per-agent bot resolver raised; falling back to channel routing: %s", e)
        return None


def get_token(agent_name: str) -> Optional[str]:
    """The dedicated bot token for a per-agent-routed reply, or None to fall back
    to the workspace token. Fail-open."""
    if _token_provider is None or not agent_name:
        return None
    try:
        return _token_provider(agent_name)
    except Exception as e:  # noqa: BLE001
        logger.warning("per-agent bot token provider raised; falling back: %s", e)
        return None
