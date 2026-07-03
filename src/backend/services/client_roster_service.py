"""External client roster aggregation (#20 — Access/Sharing redesign, epic #16).

Surfaces the external channel users (no Trinity account) who have messaged an
agent, aggregated across channels into one roster. This is the read surface for
the reframed Sharing tab; per-client controls (block/revoke/approve) are a
separate follow-up (#21).

Roster v1 covers the channels that record the full
``(verified_email, message_count, last_active)`` triple per user — Telegram and
WhatsApp. Slack (verifications carry email but no activity counters) and VoIP
(call logs) are additive follow-ups: register another source below and the
endpoint contract is unchanged.

Three-layer (Invariant #1): this service holds the cross-channel aggregation /
sort business logic; the SQL lives in the ``db/*_channels.py`` join methods; the
router stays thin.
"""

from __future__ import annotations

import logging
from typing import List

from database import db

logger = logging.getLogger(__name__)


def get_client_roster(agent_name: str) -> List[dict]:
    """Return external channel clients for ``agent_name``, newest activity first.

    Each entry: ``channel``, ``identity``, ``display_name``, ``verified_email``,
    ``message_count``, ``last_active``. Sorted by ``last_active`` descending with
    never-active rows (``last_active is None``) last. A single channel failing to
    read degrades to skipping that channel rather than failing the whole roster.
    """
    sources = (
        ("telegram", db.list_telegram_clients_for_agent),
        ("whatsapp", db.list_whatsapp_clients_for_agent),
    )

    roster: List[dict] = []
    for channel, fetch in sources:
        try:
            roster.extend(fetch(agent_name))
        except Exception as e:  # pragma: no cover - defensive per-channel guard
            logger.warning(
                "[#20] client roster: %s source failed for agent %s: %s",
                channel, agent_name, e,
            )

    # ISO-Z timestamps sort lexicographically. With reverse=True the newest
    # timestamp comes first and the "" fallback (never-active, last_active=None)
    # sorts last — exactly the ordering we want.
    roster.sort(key=lambda c: c.get("last_active") or "", reverse=True)
    return roster
