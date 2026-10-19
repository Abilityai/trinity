"""Which tier is a running turn acting for? (trinity-enterprise#926)

An agent acts for whoever chats with it. When the person is only shared the
agent, the agent's own file tools must not do for them what the API refuses
them. The guard hook in the agent image asks this question, per turn, before
it lets a Write/Edit/Read reach a path the shared policy keeps at owner tier.

The answer comes from the execution row (who started the turn), never from the
agent: the hook identifies the turn by `TRINITY_EXECUTION_ID` in the claude
process's launch environment, which the agent cannot change once it is running.

  owner    the person is the agent's owner or an admin, or the turn was started
           by something the owner configured (a schedule, a reminder, a loop)
  share    anyone else: a shared person, or a turn from a channel, a public
           link or another agent that carries no platform user
  unknown  the execution does not exist, or belongs to another agent
"""
from typing import Optional

OWNER = "owner"
SHARE = "share"
UNKNOWN = "unknown"

# Turns with no platform user that the OWNER configured. Anything else without
# a user (a public link, a channel, a paid call, another agent, voice) is an
# outside caller and gets the restricted tier.
OWNER_CONFIGURED_TRIGGERS = frozenset({
    "schedule", "system", "reminder", "loop", "self_task", "validation",
    "webhook", "manual", "event",
})


def turn_tier(db, agent_name: str, execution_id: str) -> str:
    execution = db.get_execution(execution_id)
    if execution is None or execution.agent_name != agent_name:
        return UNKNOWN
    user_id: Optional[int] = getattr(execution, "source_user_id", None)
    if user_id is None:
        trigger = (getattr(execution, "triggered_by", "") or "").lower()
        return OWNER if trigger in OWNER_CONFIGURED_TRIGGERS else SHARE
    try:
        user = db.get_user_by_id(int(user_id))
    except (TypeError, ValueError):
        user = None
    if not user:
        return SHARE
    if user.get("role") == "admin":
        return OWNER
    if db.can_user_share_agent(user.get("username") or "", agent_name):
        return OWNER
    return SHARE
