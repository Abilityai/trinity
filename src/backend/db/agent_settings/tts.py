"""Shared per-agent outbound-voice (TTS) config (epic #24 / #25; v2 ent#117).

An agent-level primitive — ``tts_voice_replies_enabled`` + ``tts_voice_id`` on
``agent_ownership`` — that decides whether the agent may speak a reply and with
which ElevenLabs voice. Voice replies v2 (ent#117) makes voice a per-message
capability (agents opt in per turn via the ``send_voice_reply`` MCP tool) and adds
three per-channel "voice allowed here" flags
(``tts_voice_{telegram,slack,whatsapp}_enabled``, DEFAULT 1) so an owner can gate
voice per channel without touching the shared enable/voice config.

Guards ``deleted_at IS NULL`` on write so a soft-deleted agent can't be mutated
(same rule as the MCP-exposure / circuit-breaker toggles).
"""

from typing import Dict

from sqlalchemy import select, update, func, and_

from ..engine import get_engine
from ..tables import agent_ownership

# Channel key -> per-channel flag column on agent_ownership.
_CHANNEL_FLAG_COLUMNS = {
    "telegram": "tts_voice_telegram_enabled",
    "slack": "tts_voice_slack_enabled",
    "whatsapp": "tts_voice_whatsapp_enabled",
}


class TtsMixin:
    """Mixin for the shared per-agent outbound-voice toggle + voice id + channel flags."""

    def get_tts_config(self, agent_name: str) -> Dict:
        """Return the agent's voice config:
        ``{"enabled": bool, "voice_id": str|None, "channels": {telegram, slack, whatsapp}}``.

        Per-channel flags default to True (DEFAULT 1 in schema) so an already-enabled
        agent keeps voice on every channel. Defaults to disabled / no voice / all
        channels allowed when the agent or columns are unset.
        """
        stmt = select(
            func.coalesce(agent_ownership.c.tts_voice_replies_enabled, 0).label("enabled"),
            agent_ownership.c.tts_voice_id.label("voice_id"),
            func.coalesce(agent_ownership.c.tts_voice_telegram_enabled, 1).label("telegram"),
            func.coalesce(agent_ownership.c.tts_voice_slack_enabled, 1).label("slack"),
            func.coalesce(agent_ownership.c.tts_voice_whatsapp_enabled, 1).label("whatsapp"),
        ).where(
            and_(
                agent_ownership.c.agent_name == agent_name,
                agent_ownership.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if not row:
            return {
                "enabled": False,
                "voice_id": None,
                "channels": {"telegram": True, "slack": True, "whatsapp": True},
            }
        return {
            "enabled": bool(row["enabled"]),
            "voice_id": row["voice_id"],
            "channels": {
                "telegram": bool(row["telegram"]),
                "slack": bool(row["slack"]),
                "whatsapp": bool(row["whatsapp"]),
            },
        }

    def set_tts_config(self, agent_name: str, enabled: bool, voice_id: str | None) -> bool:
        """Persist the agent-level outbound-voice toggle + voice id. Empty/whitespace
        voice_id normalizes to NULL. Guards ``deleted_at IS NULL``. Returns True if a
        row updated. Per-channel flags are set separately via ``set_tts_channel_flags``.
        """
        clean_voice = (voice_id or "").strip() or None
        stmt = (
            update(agent_ownership)
            .where(
                and_(
                    agent_ownership.c.agent_name == agent_name,
                    agent_ownership.c.deleted_at.is_(None),
                )
            )
            .values(
                tts_voice_replies_enabled=1 if enabled else 0,
                tts_voice_id=clean_voice,
            )
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0

    def set_tts_channel_flags(self, agent_name: str, channels: Dict[str, bool]) -> bool:
        """Set per-channel voice-allowed flags. ``channels`` is a partial map of
        ``{channel: bool}`` over telegram/slack/whatsapp; unknown keys are ignored.
        Guards ``deleted_at IS NULL``. Returns True if a row updated (or no-op with a
        matching live row)."""
        values = {}
        for channel, allowed in channels.items():
            col = _CHANNEL_FLAG_COLUMNS.get(channel)
            if col is not None:
                values[col] = 1 if allowed else 0
        if not values:
            return False
        stmt = (
            update(agent_ownership)
            .where(
                and_(
                    agent_ownership.c.agent_name == agent_name,
                    agent_ownership.c.deleted_at.is_(None),
                )
            )
            .values(**values)
        )
        with get_engine().begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount > 0
