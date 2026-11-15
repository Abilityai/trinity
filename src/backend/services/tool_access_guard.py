"""The send block, refused by Trinity (trinity-enterprise#341, GUARD-008).

Removing a send tool from Claude Code's `--disallowedTools` hides it from the
model, but it is not a boundary: the agent's shell holds its MCP key and can
call the backend directly, its own `.mcp.json` can add a second copy of the
Trinity server under another name, and Codex and Gemini ignore the list. So
each backend route behind a Trinity send tool (and `report`) calls
`assert_tool_allowed` first, and an agent-scoped caller whose ENFORCED tool
list names that tool is refused there — on every runtime, whatever path the
call took, from the moment the profile is saved.

Only agent principals are checked (`User.agent_name` is set for agent-scoped
keys only): a person, the system key and a Workspace visitor are never
refused here. An absent `agent_name` reads as "not an agent" on purpose:
every agent identity reaching these routes is a `models.User`, which always
declares the field, and the one principal without it is the Workspace visitor
(`WorkspacePrincipal`, rooms), a person by construction. The list read is the
enforced one (`guardrails_config.disallowed_tools`), so a dormant Custom list,
or a legacy read-only agent whose send block was never applied, is not refused.

Failure direction: OPEN on a malformed row, like every other guardrails reader
(`get_guardrails_config` reads it as `{}`, the platform baseline) — only
validated owner writes reach the column. A database error is not swallowed:
it propagates and the send fails with it.
"""
from fastapi import HTTPException

from database import db
from services import tool_access_profiles as tap


def assert_tool_allowed(principal, tool: str, as_agent: str = None) -> None:
    """403 when `principal` is an agent whose tool access removes `tool`.

    `as_agent` is the agent the route sends AS (its path agent), when that can
    differ from the caller: an agent with a permission edge to a sibling may
    reach the sibling's send routes, and a message sent in a Strict agent's
    name is that agent sending. Both lists are checked; a person is never.
    """
    if tool not in tap.SINK_TOOLS:
        raise ValueError(f"{tool!r} is not a Trinity send tool")
    caller = getattr(principal, "agent_name", None)
    if not caller:
        return
    for agent_name in dict.fromkeys(a for a in (caller, as_agent) if a):
        cfg = db.get_guardrails_config(agent_name)
        if not tap.denies_tool(cfg, tool):
            continue
        # A list without a stored profile is a Custom list (`resolve` derives
        # it so whatever the read-only layer), so the label needs no second read.
        profile = tap.label(tap.resolve(cfg, False)["profile"])
        whose = "this agent's" if agent_name == caller else f"{agent_name}'s"
        raise HTTPException(
            status_code=403,
            detail=(
                f"{tool} is blocked by {whose} tool access ({profile}). "
                "Only its owner can change that, in Settings › Guardrails › Tool access."
            ),
        )
