"""Who was this turn for? (trinity-enterprise#549)

A side effect that a person can later find in their Workspace — today, a shared
file — belongs to the person the turn was for. The platform decides that; the
agent does not choose. This module is the one place that decision is made, so
that files, and whatever is addressed next, read the execution row the same way.

TWO QUESTIONS, KEPT APART

1. **`audience_of(execution)` — given a turn, who is it for?** Pure and
   table-driven, over columns every entry path already stamps:

   | the turn came from                          | the file is for                          |
   |---------------------------------------------|------------------------------------------|
   | a Workspace chat, or a room turn, with Ada  | Ada (`source_channel_client`)            |
   | a WhatsApp / Telegram / Slack conversation  | that channel identity; ALSO Ada's email  |
   |                                             | when she is a VERIFIED speaker in a      |
   |                                             | one-to-one chat (never in a group, where |
   |                                             | the verified email is the unlocker's)    |
   | a schedule, operator chat, MCP call, loop,  | nobody — the owner only                  |
   | voice post-session turn, agent-to-agent     |                                          |
   | child                                       |                                          |

   The channel set is an ALLOW-LIST. A `source_channel` this table has never
   heard of makes no claim, rather than defaulting to "a person" — the rule
   `canvas_service.canvas_visibility` states for trigger labels, for the same
   reason: a value invented tomorrow must not start addressing files.

2. **`resolve_turn_audience(...)` — which turn did this call come from?** The
   hard one. An MCP tool call carries the agent's key and nothing about the
   turn, so the only link is an `execution_id` the AGENT types — and a resumed
   session cites ids from its own history (observed live: the id of a turn 22
   minutes old). So the rule asks for POSITIVE EVIDENCE and says "could not
   tell" without it:

   - not the agent's own key → no turn at all. An owner or a user-scoped key
     passes the share route's owner gate; whatever id they cite, they are not an
     agent in a turn.
   - the cited id is not an execution of THIS agent (absent, unknown, another
     agent's) → could not tell. This is what makes a web-terminal `claude`
     session safe: it holds the agent's key, has no execution row and no
     Execution Context, and so cites nothing. "The agent has exactly one running
     turn — take it" would hand an operator's file to whichever client happened
     to be mid-conversation.
   - the cited execution is an agent-to-agent child → nobody (question 1).
   - otherwise the cited id proves which CONVERSATION the call came from — and
     never which person, live or finished. The person is whoever that
     conversation's RUNNING direct turns belong to, and they must AGREE: one
     person is an answer; none, or two different people, is "could not tell".
     A room shares one Claude session across its humans, so a model citing
     another participant's id is the ordinary case there — a finished one must
     not send the file to them, and neither must a zombie row a crash left
     `running` beside somebody else's live turn. Delegated children in the same
     conversation are not turns of it and do not vote.
   - a cited execution with no conversation at all (a schedule, an operator
     chat) is its own answer while it runs — nobody — and no evidence once it
     has finished.

   Every "could not tell" is the owner only. It can under-share; it cannot
   over-share.

   RESIDUAL, stated rather than implied away: a session that carries ANOTHER
   conversation's history can cite a real id of it. An operator who `--resume`s a
   client's chat in a terminal does; so can an operator chat, whose `--continue`
   resumes the most recent session in the agent's home. If that client has a
   turn running at that moment and the model prefers the stale id to its own
   Execution Context, the file is addressed to the client. A platform-injected
   id (below) closes it.

`trusted_execution_id` is the slot for a platform-injected id (#2392): every
headless turn's process already carries `TRINITY_EXECUTION_ID`, and once that
reaches the backend it answers question 2 with no cooperation from the model.
Nothing passes it today.

A leaf: no HTTP, no SQL. Its reads go through the `database` facade (and
`idempotency_service` for the execution lookup it already owns).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from config import PORTAL_SOURCE_CHANNEL, ROOM_SOURCE_CHANNEL
from models import TaskExecutionStatus

logger = logging.getLogger(__name__)

# `audience_source` values. Stored on the row so the owner's panel can tell
# "nobody" from "could not tell"; NULL there means a row older than the column.
SOURCE_TURN = "turn"            # decided from the turn the call came from
SOURCE_OVERRIDE = "override"    # the agent named a rostered person
SOURCE_CHANNEL = "channel"      # a platform-side channel caller that holds the recipient
SOURCE_NONE = "none"            # a turn, or a caller, with no person
SOURCE_AMBIGUOUS = "ambiguous"  # could not tell which turn the call came from

# Surfaces where the person IS the recorded client of the turn.
_CLIENT_CHANNELS = frozenset({PORTAL_SOURCE_CHANNEL, ROOM_SOURCE_CHANNEL})
# Messaging channels: the person is a channel identity, and an email only when
# the binding verified one.
_MESSAGING_CHANNELS = frozenset({"telegram", "whatsapp", "slack"})


@dataclass(frozen=True)
class TurnAudience:
    """Who a file is for. ``email`` decides whose Files tab lists it;
    ``channel`` is display-only; ``source`` says how this was decided."""
    email: Optional[str]
    channel: Optional[str]
    source: str

    @property
    def is_owner_only(self) -> bool:
        return self.email is None and self.channel is None


NOBODY = TurnAudience(None, None, SOURCE_NONE)
COULD_NOT_TELL = TurnAudience(None, None, SOURCE_AMBIGUOUS)


def normalize_addressee_email(value: Any) -> Optional[str]:
    """The ONE spelling of an addressee, for every writer and the reader.

    The sources that feed this column do not agree: portal identities are
    lower-cased at login, rooms stamp a raw ``current_user.email`` and
    ``users.email`` is never normalised. Anything that is not email-shaped is
    None — "unaddressed" has exactly one spelling, and a channel-native id
    (``telegram:<bot>:<id>``) can never land in a column a Files tab is matched
    against, whatever column it arrived in.
    """
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if not v or "@" not in v or any(c.isspace() for c in v):
        return None
    local, _, domain = v.rpartition("@")
    if not local or not domain:
        return None
    return v


def channel_address(channel: Optional[str], chat_id: Any) -> Optional[str]:
    """``<channel>:<chat id>``, prefixed exactly once — Twilio's ``From`` already
    arrives as ``whatsapp:+…``."""
    if not channel or chat_id is None:
        return None
    chat = str(chat_id).strip()
    if not chat:
        return None
    prefix = f"{channel}:"
    return chat if chat.startswith(prefix) else prefix + chat


def whatsapp_recipient(binding_id: Any, number: Any) -> tuple:
    """``(verified email | None, "whatsapp:+…" | None)`` for the number a reply
    is going to — what both platform-side WhatsApp callers hand to
    ``create_share_from_bytes``. Outbound media and voice notes are created by
    code that already HOLDS the recipient, so there is no turn to resolve.

    Never raises: a provenance lookup must not cost the person their media. A
    failed lookup is "no email", and the file is then in nobody's Files tab.
    """
    address = channel_address("whatsapp", number)
    if binding_id is None or address is None:
        return None, address
    from database import db
    try:
        email = normalize_addressee_email(db.get_whatsapp_verified_email(binding_id, str(number)))
    except Exception as e:  # noqa: BLE001
        logger.warning("[turn-audience] whatsapp verified-email lookup failed (%s)", type(e).__name__)
        email = None
    return email, address


def audience_of(execution: Any) -> TurnAudience:
    """Given a turn, who is it for? Pure — see the table in the module docstring."""
    if execution is None:
        return NOBODY

    if _is_delegated(execution):
        return NOBODY

    channel = getattr(execution, "source_channel", None)

    if channel in _CLIENT_CHANNELS:
        # Fails CLOSED on a row from before `source_channel_client` existed: the
        # column is the recipient check (ent#457), and `source_user_email` alone
        # is not one.
        email = normalize_addressee_email(getattr(execution, "source_channel_client", None))
        return TurnAudience(email, None, SOURCE_TURN) if email else NOBODY

    if channel in _MESSAGING_CHANNELS:
        # NEVER `source_user_email` here. On a channel turn that column is
        # `verified_email OR the channel-native id`, and in a GROUP the verified
        # email is the UNLOCKER's — set once per group, not per speaker (the
        # reason MEM-001 refuses group mode). The router stamps
        # `source_channel_client` only for a verified speaker in a one-to-one
        # conversation, so a group turn, an unverified user and a row from
        # before that stamp all resolve to the channel address alone.
        email = normalize_addressee_email(getattr(execution, "source_channel_client", None))
        address = channel_address(channel, getattr(execution, "source_channel_chat_id", None))
        if email is None and address is None:
            return NOBODY
        return TurnAudience(email, address, SOURCE_TURN)

    return NOBODY


def _is_delegated(execution: Any) -> bool:
    """An agent-to-agent child. It inherits its parent's channel context so that
    completion reports find their way back (ent#265), and `source_channel_agent`
    is set by that inheritance and by nothing else — so non-NULL MEANS inherited,
    whoever it names. Comparing it to the executing agent would miss the two
    cases where they are equal: an agent tasking ITSELF, and A→B→A. In both, the
    inherited client was chosen through an agent-typed `parent_execution_id`;
    the one way an agent names a person is `audience_email`, which is checked."""
    return bool(getattr(execution, "source_channel_agent", None))


def _validated(execution_id: Optional[str], agent_name: str) -> Any:
    """The execution iff it exists and belongs to ``agent_name`` — never raises."""
    if not execution_id:
        return None
    # Function-local: `idempotency_service` imports `database`, and this module
    # is imported by services that `database` reaches at app start.
    from services.idempotency_service import resolve_and_validate_execution
    try:
        return resolve_and_validate_execution(execution_id, agent_name)
    except Exception as e:  # noqa: BLE001 — provenance must never fail a share
        logger.warning("[turn-audience] execution lookup failed (%s) — owner-only", type(e).__name__)
        return None


def _is_running(execution: Any) -> bool:
    status = getattr(execution, "status", None)
    return getattr(status, "value", status) == TaskExecutionStatus.RUNNING.value


def resolve_turn_audience(
    agent_name: str,
    *,
    actor_is_agent: bool,
    claimed_execution_id: Optional[str] = None,
    trusted_execution_id: Optional[str] = None,
) -> TurnAudience:
    """Which turn did this call come from, and who is that turn for?

    Never raises: a provenance lookup must not fail the side effect it
    describes, and every failure lands on the owner-only side.
    """
    if not actor_is_agent:
        return NOBODY

    if trusted_execution_id:
        trusted = _validated(trusted_execution_id, agent_name)
        if trusted is not None:
            return _log(agent_name, "trusted", trusted, audience_of(trusted))

    claimed = _validated(claimed_execution_id, agent_name)
    if claimed is None:
        return _log(agent_name, "no-evidence", None, COULD_NOT_TELL)
    if _is_delegated(claimed):
        return _log(agent_name, "cited-delegated-child", claimed, NOBODY)

    channel = getattr(claimed, "source_channel", None)
    chat_id = getattr(claimed, "source_channel_chat_id", None)
    if not (channel and chat_id):
        # No conversation to look in: a schedule, an operator chat, an MCP call.
        if _is_running(claimed):
            return _log(agent_name, "cited-live-turn", claimed, audience_of(claimed))
        return _log(agent_name, "stale-no-conversation", claimed, COULD_NOT_TELL)

    from database import db
    try:
        running = db.get_running_in_conversation(agent_name, channel, str(chat_id))
    except Exception as e:  # noqa: BLE001
        logger.warning("[turn-audience] conversation lookup failed (%s) — owner-only", type(e).__name__)
        return COULD_NOT_TELL
    turns = [row for row in running if not _is_delegated(row)]
    people = {audience_of(row) for row in turns}
    if len(people) != 1:
        return _log(agent_name, f"conversation-{len(turns)}-turns-{len(people)}-people", claimed, COULD_NOT_TELL)
    return _log(agent_name, "conversation", turns[0], people.pop())


def _log(agent_name: str, rule: str, execution: Any, audience: TurnAudience) -> TurnAudience:
    """Which rule fired, for the person who has to explain a row later. The
    addressee itself is never logged — a boolean is enough to debug with."""
    logger.info(
        "[turn-audience] agent=%s rule=%s execution=%s source=%s addressed=%s channel=%s",
        agent_name, rule, getattr(execution, "id", None), audience.source,
        audience.email is not None, audience.channel is not None,
    )
    return audience
