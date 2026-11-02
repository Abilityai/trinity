"""Pre-admission policy seam (trinity-enterprise#1059).

One place where a policy may stop an agent's turn before it is admitted, so a
limit holds on every door rather than on the ones someone remembered. Called
from the three admission points — ``dispatch_admission_service.admit_chat_request``
(``/chat``), ``chat_execution_service.dispatch_parallel_task`` (``/task``) and
``TaskExecutionService.execute_task`` (every other door: schedules, webhooks,
loops, fan-out, reminders, voice, the Workspace, approved gated runs) — after
the skill gate and before any idempotency claim or capacity acquire.

The registry is empty in a build that registers nothing, and then nothing
changes: ``enforce`` returns and ``notice`` returns None. The first policy is an
agent's share of its subscription's usage, registered by an optional module.

A policy is any object with:

  * ``enforce(agent_name, *, triggered_by, source)`` — sync or async; raise
    ``chat_signals.AdmissionRefused`` to stop the turn, return to admit.
    ``source`` names the seam: ``"chat"``, ``"task"`` or ``"execute_task"``.
  * ``notice(agent_name) -> Optional[str]`` (optional) — one plain-words
    sentence for the agent's Execution Context (``- **Usage**: …``), or None.

Fail-open: a policy that raises anything other than ``AdmissionRefused`` is
logged and skipped, like ``ensure_serviceable_subscription`` — a policy that
cannot read its own state must not take every dispatch down with it.
"""
from __future__ import annotations

import inspect
import logging
from typing import List, Optional

from services.chat_signals import AdmissionRefused

logger = logging.getLogger(__name__)

_POLICIES: List[object] = []


def register(policy) -> None:
    """Add a policy. Registering the same object twice keeps one entry."""
    if not any(p is policy for p in _POLICIES):
        _POLICIES.append(policy)


def unregister(policy) -> None:
    _POLICIES[:] = [p for p in _POLICIES if p is not policy]


async def enforce(agent_name: str, *, triggered_by: Optional[str], source: str) -> None:
    """Run every registered policy; the first refusal propagates."""
    for policy in list(_POLICIES):
        try:
            outcome = policy.enforce(agent_name, triggered_by=triggered_by, source=source)
            if inspect.isawaitable(outcome):
                await outcome
        except AdmissionRefused:
            raise
        except Exception:
            logger.warning(
                "[admission_policy] %s failed for %s at %s; admitting",
                type(policy).__name__, agent_name, source, exc_info=True,
            )


def notice(agent_name: Optional[str]) -> Optional[str]:
    """The policies' notices for this agent, joined; None when there are none."""
    if not agent_name:
        return None
    lines = []
    for policy in list(_POLICIES):
        read = getattr(policy, "notice", None)
        if read is None:
            continue
        try:
            text = read(agent_name)
        except Exception:
            logger.warning(
                "[admission_policy] %s notice failed for %s",
                type(policy).__name__, agent_name, exc_info=True,
            )
            continue
        if text:
            lines.append(str(text).strip())
    return " ".join(line for line in lines if line) or None
