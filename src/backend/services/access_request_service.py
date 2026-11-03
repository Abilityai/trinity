"""Access requests as the owner sees them (trinity-enterprise#737).

A channel access request records who asked, as the channel saw them
(``db.access_requests.REQUESTER_FIELDS``). This module turns the stored rows
into the owner's view: whether an identity was captured at all, and where the
same channel identity has been seen before — an earlier request on another of
the viewer's agents, or an email it verified on one — so a known person is not
presented as a stranger.

Prior context is limited to agents the viewer may manage
(``can_user_share_agent``: the owner, or an admin). The same person's
history on someone else's agent is not this owner's to read.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from database import db

logger = logging.getLogger(__name__)

# Channels whose chat links key on the same stable user id the request records.
_VERIFIED_LINK_SOURCES = {
    "telegram": "list_telegram_verified_links_for_user",
    "whatsapp": "list_whatsapp_verified_links_for_user",
}
_MAX_KNOWN_AS = 10


def list_access_requests(agent_name: str, status: Optional[str], viewer_username: str) -> List[dict]:
    rows = db.list_access_requests(agent_name, status)
    may_manage: Dict[str, bool] = {}

    def visible(name: str) -> bool:
        if name not in may_manage:
            try:
                may_manage[name] = bool(db.can_user_share_agent(viewer_username, name))
            except Exception:  # noqa: BLE001 — unknown agent, deleted owner
                may_manage[name] = False
        return may_manage[name]

    for row in rows:
        row["identity_captured"] = bool(row.get("requester_channel_id"))
        row["known_as"] = _known_as(row, visible)
    return rows


def _known_as(row: dict, visible) -> List[dict]:
    """Where this channel identity was seen before, newest request first."""
    channel, user_id = row.get("channel"), row.get("requester_channel_id")
    if not channel or not user_id:
        return []
    seen: List[dict] = []
    keys = set()

    def add(agent_name: str, status: str, email: Optional[str]) -> None:
        email = (email or "").lower() or None
        # The request itself, and its own verification on this agent, are not "before".
        if agent_name == row["agent_name"] and email == (row.get("email") or "").lower():
            return
        key = (agent_name, status, email)
        if key in keys or not visible(agent_name):
            return
        keys.add(key)
        seen.append({"agent_name": agent_name, "status": status, "email": email})

    try:
        for other in db.list_access_requests_by_requester(channel, user_id):
            if other["id"] != row["id"]:
                add(other["agent_name"], other["status"], other["email"])
        source = _VERIFIED_LINK_SOURCES.get(channel)
        if source:
            for link in getattr(db, source)(user_id):
                add(link["agent_name"], "verified", link["verified_email"])
    except Exception as e:  # noqa: BLE001 — context is best-effort; the request still lists
        logger.warning("[ent#737] prior context lookup failed: %s", type(e).__name__)
    return seen[:_MAX_KNOWN_AS]
