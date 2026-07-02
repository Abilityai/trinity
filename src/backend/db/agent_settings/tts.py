"""Shared per-agent outbound-voice (TTS) config (epic #24 / #25).

A single agent-level primitive — ``tts_voice_replies_enabled`` + ``tts_voice_id``
on ``agent_ownership`` — that every channel adapter reuses to decide whether to
speak a reply and with which ElevenLabs voice. Deliberately agent-level (not
per-binding) so Telegram (#25), Slack (#26), and WhatsApp (trinity-enterprise#56)
share one config with no per-channel column sprawl.

Guards ``deleted_at IS NULL`` on write so a soft-deleted agent can't be mutated
(same rule as the MCP-exposure / circuit-breaker toggles).
"""

from typing import Dict

from sqlalchemy import select, update, func, and_

from ..engine import get_engine
from ..tables import agent_ownership


class TtsMixin:
    """Mixin for the shared per-agent outbound-voice toggle + voice id."""

    def get_tts_config(self, agent_name: str) -> Dict:
        """Return ``{"enabled": bool, "voice_id": str|None}`` for the agent.
        Defaults to disabled / no voice when the agent or columns are unset."""
        stmt = select(
            func.coalesce(agent_ownership.c.tts_voice_replies_enabled, 0).label("enabled"),
            agent_ownership.c.tts_voice_id.label("voice_id"),
        ).where(
            and_(
                agent_ownership.c.agent_name == agent_name,
                agent_ownership.c.deleted_at.is_(None),
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        if not row:
            return {"enabled": False, "voice_id": None}
        return {"enabled": bool(row["enabled"]), "voice_id": row["voice_id"]}

    def set_tts_config(self, agent_name: str, enabled: bool, voice_id: str | None) -> bool:
        """Persist the outbound-voice toggle + voice id. Empty/whitespace voice_id
        normalizes to NULL. Guards ``deleted_at IS NULL``. Returns True if a row
        updated."""
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
