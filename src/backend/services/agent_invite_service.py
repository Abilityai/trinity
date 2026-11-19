"""Tell a person an agent was shared with them (trinity-enterprise#459).

A share to an address with no account leaves a **pending** row, and nothing told
that person. This module sends the invitation, on the share and on Resend invite.

The mail names who invited them and which agent, and points at the Workspace
sign-in, where they request their own emailed code. It carries no code and no
secret, so it grants nothing: the share row is the access (ent#837).

Sharing can now send mail to an address the owner typed, so every send counts
against two windows, shared by both paths: per (agent, address), which stops a
share/unshare loop, and per inviter, which stops one owner mailing many addresses.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
from typing import Optional

from config import EMAIL_AUTH_ENABLED
from database import db
from services import login_policy_gate, rate_limiter
from services.email_service import email_service

logger = logging.getLogger(__name__)

# The share field is free text, and this module makes it a recipient: one plain
# address only, or a list or a header could ride on one quota slot.
_NOT_IN_AN_ADDRESS = re.compile(r"""[@\s,;:<>()\[\]"']""")

SENT = "sent"
INVALID_ADDRESS = "invalid_address"
NOT_NEEDED = "not_needed"
UNAVAILABLE = "unavailable"
RATE_LIMITED = "rate_limited"
FAILED = "failed"

ADDRESS_LIMIT = 3
INVITER_LIMIT = 30
WINDOW_SECONDS = 3600
SEND_TIMEOUT_SECONDS = 15
MAX_INVITER_LABEL_CHARS = 120

SUBJECT = "You've been given access to an agent on Trinity"


class InviteRefused(Exception):
    def __init__(self, status_code: int, code: str, message: str, retry_after: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after = retry_after


def is_single_address(email: str) -> bool:
    local, _, domain = (email or "").partition("@")
    host, _, tld = domain.rpartition(".")
    if not (local and host and tld):
        return False
    return not (_NOT_IN_AN_ADDRESS.search(local) or _NOT_IN_AN_ADDRESS.search(domain))


def _email_sign_in_enabled() -> bool:
    """The rule `/api/auth/mode` applies: the setting, else the env default."""
    return db.get_setting_value("email_auth_enabled", str(EMAIL_AUTH_ENABLED).lower()).lower() == "true"


def _delivery_possible() -> bool:
    return _email_sign_in_enabled() and email_service.can_deliver()


def invite_possible(email: str) -> bool:
    """Can a mail reach this person and can they sign in by the code it describes?

    `console` counts as no: it prints to the log while reporting success.
    """
    return _delivery_possible() and login_policy_gate.email_code_allowed_for(email)


def mark_invite_available(rows: list[dict]) -> list[dict]:
    """Add `invite_available` to the Access roster: the server's verdict per row."""
    delivery = _delivery_possible()
    for row in rows:
        row["invite_available"] = (
            row["status"] == "pending"
            and delivery
            and is_single_address(row["email"])
            and login_policy_gate.email_code_allowed_for(row["email"])
        )
    return rows


def _pending_row(agent_name: str, email: str) -> Optional[dict]:
    for row in db.get_agent_operator_access(agent_name):
        if row["email"].lower() == email:
            return row
    return None


def _inviter_label(username: str) -> str:
    user = db.get_user_by_username(username) or {}
    name = (user.get("name") or "").strip()
    address = (user.get("email") or "").strip()
    if name and address:
        label = f"{name} ({address})"
    else:
        label = name or address or username
    return label[:MAX_INVITER_LABEL_CHARS]


def _workspace_url() -> str:
    # ent#1255: FRONTEND_URL only. The public chat host serves only chat, so a
    # link there lands the invitee on a 403.
    base = (os.getenv("FRONTEND_URL") or "").rstrip("/")
    return f"{base}/workspace" if base else ""


async def _deliver(agent_name: str, email: str, username: str) -> bool:
    url = _workspace_url()
    where = f" at {url}" if url else " on your Trinity sign-in page, in the Workspace"
    send = email_service.send_html(
        email,
        SUBJECT,
        intro=f"{_inviter_label(username)} gave you access to the agent {agent_name} on Trinity.",
        paragraphs=[
            f"Sign in with this email address ({email}){where}. "
            "You'll request a one-time code there to complete sign-in.",
        ],
        cta=("Open the Workspace", url) if url else None,
    )
    try:
        return bool(await asyncio.wait_for(send, timeout=SEND_TIMEOUT_SECONDS))
    except Exception as e:  # noqa: BLE001 — the caller reports the failure
        logger.warning("[ent#459] invitation to %s for %s not sent: %s", email, agent_name, type(e).__name__)
        return False


def _windows(agent_name: str, email: str, username: str) -> list[str]:
    return [f"agent_invite:{agent_name}:{email}", f"agent_invite_by:{username}"]


def _limit_for(key: str) -> int:
    return INVITER_LIMIT if key.startswith("agent_invite_by:") else ADDRESS_LIMIT


async def invite_on_share(agent_name: str, email: str, username: str) -> str:
    """Invite after a share. Returns an outcome and never raises: the share stands."""
    email = email.strip().lower()
    try:
        row = _pending_row(agent_name, email)
        if not row or row["status"] != "pending":
            return NOT_NEEDED
        if not is_single_address(email):
            return INVALID_ADDRESS
        if not invite_possible(email):
            return UNAVAILABLE
        for key in _windows(agent_name, email, username):
            if not rate_limiter.check(key, _limit_for(key), WINDOW_SECONDS).allowed:
                return RATE_LIMITED
        return SENT if await _deliver(agent_name, email, username) else FAILED
    except Exception as e:  # noqa: BLE001 — a mail fault must not fail the share
        logger.warning("[ent#459] invitation on share failed for %s: %s", agent_name, type(e).__name__)
        return FAILED


async def resend_invite(agent_name: str, email: str, username: str) -> None:
    """Send the invitation again, or raise a named refusal.

    The gates run first, so a refused call spends no quota. An email that fails
    to send does spend it: the provider was asked.
    """
    email = email.strip().lower()
    row = _pending_row(agent_name, email)
    if not row:
        raise InviteRefused(404, "not_shared", "This agent isn't shared with that address.")
    if row["status"] != "pending":
        raise InviteRefused(409, "not_pending", "This person already has an account, so there's nothing to invite them to.")
    if not is_single_address(email):
        raise InviteRefused(422, "invalid_address", "This isn't a single email address, so no invitation can be sent. Remove it and add the person again.")
    if not invite_possible(email):
        raise InviteRefused(409, "invite_unavailable", "Email isn't set up for sign-in on this instance, so no invitation can be sent. Tell them how to sign in yourself.")
    for key in _windows(agent_name, email, username):
        result = rate_limiter.check(key, _limit_for(key), WINDOW_SECONDS)
        if not result.allowed:
            minutes = max(1, math.ceil(result.retry_after / 60))
            raise InviteRefused(
                429, "invite_rate_limited",
                f"Too many invitations sent recently. Try again in {minutes} minute{'' if minutes == 1 else 's'}.",
                retry_after=result.retry_after,
            )
    if not await _deliver(agent_name, email, username):
        raise InviteRefused(502, "invite_not_sent", "The invitation email couldn't be sent. Try again.")
