"""
Public User Memory Router (MEM-001 write path).

Provides a write endpoint agents can call (via the write_user_memory MCP tool)
to persist facts about the user they are currently serving.

Security model:
- The caller never supplies a user email.
- The backend resolves the email from the execution record identified by
  execution_id, after verifying the execution belongs to the calling agent
  and was triggered by a user-facing channel (public/slack/telegram/whatsapp).
- This prevents an agent from writing memory for an arbitrary user.
"""

import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from database import db
from dependencies import get_current_user
from db_models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["user-memory"])

# Channels where a human user identity (verified email) is present.
_USER_FACING_TRIGGERS = {"public", "slack", "telegram", "whatsapp"}

# Lightweight email sanity check — not RFC-exhaustive, just catches obvious garbage.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class WriteUserMemoryRequest(BaseModel):
    execution_id: str = Field(..., min_length=1, max_length=200)
    memory_text: str = Field(..., max_length=8000)


@router.post("/{agent_name}/user-memory")
async def write_user_memory(
    agent_name: str,
    body: WriteUserMemoryRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Write the per-user memory blob for the user currently being served.

    The caller supplies:
    - agent_name (path): the agent whose memory store to write to.
    - execution_id (body): the current execution, used to resolve the user email server-side.
    - memory_text (body): the complete updated memory blob (replaces previous content).

    The user email is never accepted from the caller — it is looked up from the
    execution record to prevent cross-user memory poisoning.
    """
    if not db.can_user_access_agent(current_user.username, agent_name):
        raise HTTPException(status_code=403, detail="Not authorized")

    execution = db.get_execution(body.execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")

    if execution.agent_name != agent_name:
        raise HTTPException(status_code=403, detail="Execution does not belong to this agent")

    triggered_by = (execution.triggered_by or "").lower()
    if triggered_by not in _USER_FACING_TRIGGERS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"write_user_memory is only available during user-facing sessions "
                f"(public, slack, telegram, whatsapp). This execution was triggered by '{triggered_by}'."
            ),
        )

    user_email = execution.source_user_email
    if not user_email or not _EMAIL_RE.match(user_email):
        raise HTTPException(
            status_code=422,
            detail="No verified user email associated with this execution.",
        )

    # #895: write only the agent_notes section so the background
    # conversation summarizer can't clobber deliberate agent writes (and
    # vice versa). The row is created on demand inside the helper.
    db.update_public_user_memory_agent_notes(
        agent_name, user_email, body.memory_text
    )

    logger.info(
        f"[UserMemory] Updated agent_notes for {user_email} on {agent_name} "
        f"({len(body.memory_text)} chars, execution={body.execution_id})"
    )
    return {"success": True, "agent_name": agent_name, "user_email": user_email}
