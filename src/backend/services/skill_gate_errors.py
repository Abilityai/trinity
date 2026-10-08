"""What a gated-skill check raises (trinity-enterprise#751).

A leaf module — no imports from the dispatch stack — so `error_handlers.py`,
the routers and every producer that catches these can import it cheaply.

Both are exceptions, never a result status: every existing caller of
`execute_task` reads an unknown status as success (a paid call would settle,
a channel would reply with empty text). An exception nobody catches fails
CLOSED — the request is not run and no success is reported.
"""
from typing import Any, Dict, List, Optional

APPROVAL_PENDING_CODE = "approval_pending"

# How the requester will learn the outcome (trinity#3233): a platform task to a
# requesting agent, an Inbox notice to a person, or nothing at all. Decided by
# `skill_gate_service.outcome_delivery`, the one rule `_notify` also follows.
OUTCOME_DELIVERY_AGENT_TASK = "agent_task"
OUTCOME_DELIVERY_INBOX = "inbox"
OUTCOME_DELIVERY_NONE = "none"


class SkillGateError(Exception):
    """Base: the request names a gated skill and was not dispatched."""

    status_code = 500
    code = "skill_gate_error"

    def detail(self) -> Dict[str, Any]:  # pragma: no cover - overridden
        return {"status": "refused", "code": self.code, "message": str(self)}


class SkillApprovalRequired(SkillGateError):
    """An approval was raised; nothing ran. The outcome is delivered later."""

    status_code = 202
    code = APPROVAL_PENDING_CODE

    def __init__(self, *, request_id: str, agent_name: str, skills: List[str],
                 approver_role: str, expires_at: Optional[str],
                 outcome_delivery: str = OUTCOME_DELIVERY_NONE):
        self.request_id = request_id
        self.agent_name = agent_name
        self.skills = list(skills)
        self.approver_role = approver_role
        self.expires_at = expires_at
        # Defaults to "none": an answer that does not know never promises.
        self.outcome_delivery = outcome_delivery
        super().__init__(self.message)

    @property
    def message(self) -> str:
        # No slash: this text is echoed into rows, events and channel replies,
        # and must never read as a request to run a skill.
        noun = "the skill" if len(self.skills) == 1 else "the skills"
        names = f"{noun} {', '.join(self.skills)}"
        # No role either: the requester needs to know it is waiting, not who
        # decides (`approver_role` stays in `detail()` for API callers). And no
        # promise of a reply — not every requester has a channel back; one that
        # has none is told so, and where the outcome will show (trinity#3233).
        text = (f"Not run: {names} on {self.agent_name} needs approval before it "
                f"can run. Request {self.request_id} is waiting for a decision.")
        if self.outcome_delivery == OUTCOME_DELIVERY_NONE:
            text += (" Nothing will be sent back when it is decided: if it is approved, "
                     f"it runs as a new execution on {self.agent_name}; if not, nothing runs.")
        return text

    def detail(self) -> Dict[str, Any]:
        return {
            "status": "pending_approval",
            "code": self.code,
            "request_id": self.request_id,
            "agent": self.agent_name,
            "skills": self.skills,
            "approver_role": self.approver_role,
            "expires_at": self.expires_at,
            "outcome_delivery": self.outcome_delivery,
            "message": self.message,
        }


class SkillGateRefused(SkillGateError):
    """The request names a gated skill and could not be put up for approval.
    Nothing ran and nothing was raised."""

    def __init__(self, status_code: int, code: str, message: str, **extra: Any):
        self.status_code = status_code
        self.code = code
        self.extra = extra
        super().__init__(message)

    def detail(self) -> Dict[str, Any]:
        return {"status": "refused", "code": self.code, "message": str(self), **self.extra}
