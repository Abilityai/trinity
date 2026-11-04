"""External client roster aggregation (#20 — Access/Sharing redesign, epic #16).

Surfaces the external channel users (no Trinity account) who have messaged an
agent, aggregated across channels into one roster. This is the read surface for
the reframed Sharing tab. Each row also carries its access state, and this
module owns the per-client controls that act on it (trinity-enterprise#21):
block / unblock (``channel_client_blocks``, enforced by the channel access gate)
and revoke (the client's share on this agent). Approve is the existing
access-request decision; the row carries the request id it needs.

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
from typing import List, Optional

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

    _add_unlinked_clients(agent_name, roster)
    _annotate_access(agent_name, roster)

    # ISO-Z timestamps sort lexicographically. With reverse=True the newest
    # timestamp comes first and the "" fallback (never-active, last_active=None)
    # sorts last — exactly the ordering we want.
    roster.sort(key=lambda c: c.get("last_active") or "", reverse=True)
    return roster


_ROSTER_CHANNELS = ("telegram", "whatsapp")


def _add_unlinked_clients(agent_name: str, roster: List[dict]) -> None:
    """Rows for clients the controls must reach but who have no chat link
    (trinity-enterprise#21 review). A chat link is written only after the access
    gate and only for a DM (#1533), so two kinds of client were missing:

    * a BLOCKED client whose links are gone (a bot unbind deletes them) — their
      messages now drop before any re-recording, so without this row they never
      returned to the roster and unblock answered 404, making the block permanent;
    * a client held at the gate who filed an access request (an unverified
      stranger, a group-only sender) — the request carries their channel id
      (`requester_channel_id`, ent#737), the same key a block uses.
    """
    seen = {(r["channel"], r.get("client_id") or "") for r in roster}

    def add(channel, client_id, **fields):
        if channel not in _ROSTER_CHANNELS or not client_id or (channel, client_id) in seen:
            return
        seen.add((channel, client_id))
        roster.append({"channel": channel, "identity": client_id, "client_id": client_id,
                       "display_name": None, "verified_email": None, "message_count": 0,
                       "last_active": None, **fields})

    for b in db.list_channel_client_blocks(agent_name):
        add(b["channel"], b["client_id"])
    for r in db.list_access_requests(agent_name, "pending"):
        add(r.get("channel"), r.get("requester_channel_id"),
            identity=r.get("requester_handle") or r.get("requester_channel_id"),
            display_name=r.get("requester_name") or None,
            verified_email=(r.get("email") or None),
            last_active=r.get("requested_at"))


def _annotate_access(agent_name: str, roster: List[dict]) -> None:
    """Stamp each row's access state (trinity-enterprise#21). Block wins over
    everything; a share on this agent is `granted`; a pending access request
    is `pending`; otherwise the agent's policy decides (`none`)."""
    blocks = {(b["channel"], b["client_id"]): b for b in db.list_channel_client_blocks(agent_name)}
    shared = {(s.shared_with_email or "").lower() for s in db.get_agent_shares(agent_name)}
    requests = db.list_access_requests(agent_name, "pending")
    pending = {(r["email"] or "").lower(): r["id"] for r in requests if r.get("email")}
    # ent#737: a request names the channel identity that asked, so a client
    # with no verified email still shows as pending (and approvable).
    pending_by_id = {(r.get("channel"), r.get("requester_channel_id")): r["id"]
                     for r in requests if r.get("requester_channel_id")}
    for row in roster:
        email = (row.get("verified_email") or "").lower()
        key = (row["channel"], row.get("client_id") or "")
        block = blocks.get(key)
        row.update(access="none", access_request_id=None, blocked_at=None,
                   blocked_by_email=None, block_reason=None)
        if block:
            row.update(access="blocked", blocked_at=block["blocked_at"],
                       blocked_by_email=block["blocked_by_email"], block_reason=block["reason"])
        elif email and email in shared:
            row["access"] = "granted"
        elif email and email in pending:
            row.update(access="pending", access_request_id=pending[email])
        elif key in pending_by_id:
            row.update(access="pending", access_request_id=pending_by_id[key])


class ClientControlError(Exception):
    """A refused per-client control, with the HTTP status the router answers."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


def _find_client(agent_name: str, channel: str, client_id: str) -> dict:
    """The roster row for this client on this agent, or 404. The controls act
    only on a client that has reached this agent, never on an arbitrary id."""
    for row in get_client_roster(agent_name):
        if row["channel"] == channel and row.get("client_id") == client_id:
            return row
    raise ClientControlError(404, "Client not found on this agent")


def block_client(agent_name: str, channel: str, client_id: str, *,
                 actor_id: Optional[int], actor_email: Optional[str],
                 reason: Optional[str] = None) -> dict:
    """Stop this client reaching this agent on this channel. Idempotent. Their
    share (if any) is kept: unblocking restores exactly what they had."""
    _find_client(agent_name, channel, client_id)
    reason = (reason or "").strip() or None
    return db.block_channel_client(agent_name, channel, client_id, actor_id, actor_email, reason)


def unblock_client(agent_name: str, channel: str, client_id: str) -> dict:
    _find_client(agent_name, channel, client_id)
    if not db.unblock_channel_client(agent_name, channel, client_id):
        raise ClientControlError(409, "This client is not blocked")
    return {"agent_name": agent_name, "channel": channel, "client_id": client_id}


def revoke_client(agent_name: str, channel: str, client_id: str, *, actor_username: str) -> dict:
    """Remove a granted client's share on this agent. Under a restricted policy
    their next message files a fresh access request; under open access the
    policy still admits them — blocking is the control that always holds."""
    row = _find_client(agent_name, channel, client_id)
    email = (row.get("verified_email") or "").lower()
    # The share itself, not the displayed state: a blocked client can still
    # hold one, and revoking it is how an owner makes the block permanent.
    if not email or not db.unshare_agent(agent_name, actor_username, email):
        raise ClientControlError(409, "This client has no access on this agent to revoke")
    return {"agent_name": agent_name, "channel": channel, "client_id": client_id, "email": email}
