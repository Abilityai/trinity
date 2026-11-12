"""Execution-attribution seam: an edition may attribute an execution to a project.

Every place that creates a task execution calls :func:`record_row` right after
the row exists. A request that names a ``project_id`` is checked by
:func:`check` BEFORE the row is created. A read of one execution may be
annotated by :func:`annotate`. The census guard
(``tests/unit/test_1105_execution_attribution.py``) fails the build when a new
``create_task_execution(`` call site neither records nor is listed with a reason.

In an OSS build no attributor is registered, so the seam is inert:

* :func:`check` refuses a request that names a ``project_id`` with
  422 ``project_attribution_unavailable`` — never a silent drop;
* :func:`resolve`, :func:`record_row` and :func:`annotate` do nothing.

Rules every attributor relies on:

* **The platform decides, never the request alone.** The attributor reads the
  stored row and the authenticated principal; a requested ``project_id`` is a
  claim it must check, not a fact.
* **Frozen at creation.** Attribution is decided once, when the row is made.

Fail-open (Trinity's availability bias): ``resolve``/``record``/``annotate``
that raise are logged and treated as "no attribution"; they never fail the
dispatch or the read. Only ``check`` can refuse, and only before anything ran.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol, Tuple

logger = logging.getLogger(__name__)

UNAVAILABLE_CODE = "project_attribution_unavailable"

# What record_row reports back to a caller that asked for a project explicitly.
RECORDED = "recorded"
FAILED = "failed"


class AttributionRefused(Exception):
    """A requested ``project_id`` was refused before the execution was created."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message

    def detail(self) -> Dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class ExecutionCreated:
    """The facts of one created execution an attributor may decide on."""

    execution_id: str
    agent_name: str
    triggered_by: Optional[str] = None
    source_channel: Optional[str] = None
    source_channel_chat_id: Optional[str] = None
    source_channel_agent: Optional[str] = None
    schedule_id: Optional[str] = None
    caller_agent: Optional[str] = None          # the agent principal that dispatched it
    acting_execution_id: Optional[str] = None   # the caller's platform-stamped turn
    requested_project_id: Optional[str] = None  # named on the request (checked first)
    inherited_project_id: Optional[str] = None  # resolved at request time and carried
    principal: Any = field(default=None, compare=False, repr=False)


class Attributor(Protocol):
    def check(self, requested_project_id: str, *, principal: Any,
              agent_name: str) -> Optional[Tuple[int, str, str]]: ...

    def resolve(self, created: ExecutionCreated) -> Optional[str]: ...

    def record(self, created: ExecutionCreated) -> Optional[str]: ...

    def annotate(self, execution: Any, principal: Any) -> Dict[str, Any]: ...


_attributor: Optional[Attributor] = None


def register_attributor(attributor: Attributor) -> None:
    """Register the edition's attributor. The last registration wins."""
    global _attributor
    _attributor = attributor
    logger.info("[execution_attribution] attributor registered: %s",
                type(attributor).__name__)


def clear_attributor() -> None:
    """Drop the attributor — used by tests to restore the OSS no-op path."""
    global _attributor
    _attributor = None


def is_available() -> bool:
    return _attributor is not None


def check(requested_project_id: Optional[str], *, principal: Any, agent_name: str) -> None:
    """Refuse a requested ``project_id`` before the execution is created.

    No ``project_id`` → nothing to check. Raises :class:`AttributionRefused`.
    """
    if not requested_project_id:
        return
    if _attributor is None:
        raise AttributionRefused(
            422, UNAVAILABLE_CODE,
            "This platform does not attribute executions to projects; omit project_id.")
    refusal = _attributor.check(requested_project_id, principal=principal,
                                agent_name=agent_name)
    if refusal:
        status_code, code, message = refusal
        raise AttributionRefused(status_code, code, message)


def _principal_facts(principal: Any) -> Tuple[Optional[str], Optional[str]]:
    if principal is None:
        return None, None
    return (getattr(principal, "agent_name", None),
            getattr(principal, "_acting_execution_id", None))


def created_from_row(row: Any, *, principal: Any = None,
                     requested_project_id: Optional[str] = None,
                     inherited_project_id: Optional[str] = None) -> Optional[ExecutionCreated]:
    """Build :class:`ExecutionCreated` from a created execution row (or None)."""
    execution_id = getattr(row, "id", None)
    agent_name = getattr(row, "agent_name", None)
    if not execution_id or not agent_name:
        return None
    caller_agent, acting = _principal_facts(principal)
    return ExecutionCreated(
        execution_id=execution_id,
        agent_name=agent_name,
        triggered_by=getattr(row, "triggered_by", None),
        source_channel=getattr(row, "source_channel", None),
        source_channel_chat_id=getattr(row, "source_channel_chat_id", None),
        source_channel_agent=getattr(row, "source_channel_agent", None),
        schedule_id=getattr(row, "schedule_id", None),
        caller_agent=caller_agent,
        acting_execution_id=acting,
        requested_project_id=requested_project_id,
        inherited_project_id=inherited_project_id,
        principal=principal,
    )


def resolve(*, agent_name: str, principal: Any = None,
            requested_project_id: Optional[str] = None,
            triggered_by: Optional[str] = None) -> Optional[str]:
    """The project a dispatch that creates its rows LATER should carry.

    Fan-out children, loop iterations and gated replays are created after the
    request turn may have ended; they resolve once at request time and pass the
    answer down as ``inherited_project_id``. Fails open to None.
    """
    if _attributor is None:
        return None
    caller_agent, acting = _principal_facts(principal)
    created = ExecutionCreated(
        execution_id="", agent_name=agent_name, triggered_by=triggered_by,
        caller_agent=caller_agent, acting_execution_id=acting,
        requested_project_id=requested_project_id, principal=principal)
    try:
        return _attributor.resolve(created)
    except Exception:  # noqa: BLE001 — fail open
        logger.warning("[execution_attribution] resolve failed for %s; untagged",
                       agent_name, exc_info=True)
        return None


def record_row(row: Any, *, principal: Any = None,
               requested_project_id: Optional[str] = None,
               inherited_project_id: Optional[str] = None) -> Optional[str]:
    """Attribute a just-created execution. Never raises.

    Returns None when nothing was attributed (always, in OSS), ``RECORDED`` when
    it was, and ``FAILED`` when it should have been and the write failed — a
    caller that asked explicitly reports that, never a success.
    """
    if _attributor is None or row is None:
        return None
    created = created_from_row(row, principal=principal,
                               requested_project_id=requested_project_id,
                               inherited_project_id=inherited_project_id)
    if created is None:
        return None
    try:
        return _attributor.record(created)
    except Exception:  # noqa: BLE001 — fail open, never fail the dispatch
        logger.warning("[execution_attribution] record failed for execution %s; untagged",
                       created.execution_id, exc_info=True)
        return FAILED if (requested_project_id or inherited_project_id) else None


def annotate(execution: Any, principal: Any) -> Dict[str, Any]:
    """Extra fields for one execution read (e.g. ``project_id``). Fails open to {}."""
    if _attributor is None or execution is None:
        return {}
    try:
        return dict(_attributor.annotate(execution, principal) or {})
    except Exception:  # noqa: BLE001 — fail open, the read still answers 200
        logger.warning("[execution_attribution] annotate failed; field omitted",
                       exc_info=True)
        return {}
