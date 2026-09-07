"""Assignment gate — the OSS seam a registered module plugs role assignments into.

An *assignment* records which human fills which business role for an agent, and
which of those humans the agent primarily serves. Trinity core has no such
record: ``agent_ownership.owner_id`` is creator/infrastructure and
``agent_sharing`` is a flat per-email access grant. A registered module can own
that record; this module is the single hook point that lets the edition-agnostic
prompt path (``services/platform_prompt_service.py``) render it.

* No provider registered → :func:`resolve_assignment` returns ``None`` → the
  execution-context block renders exactly as before. Zero behavioural change.
* A provider registered → it answers with the resolved display name, the role
  id, the stakeholder list, and whether proactive contact is on file.

The provider holds the record and the policy. This module knows only the
*protocol*, and it is deliberately **synchronous**: ``compose_system_prompt`` is
a sync ``def`` called from async request handlers, so a provider that blocks on
I/O here stalls the worker's event loop. Providers are expected to answer from
memory.

    provider.assignment_for(agent_name: str, triggered_by: str | None)
        -> dict | None

with the answer shaped as::

    {
        "primary_user_display": str | None,   # display name, NEVER an email
        "role_id":              str | None,
        "stakeholders":         list[str] | None,
        "proactive_consent":    bool | None,
    }

**The provider owns "never an email", not this module.** The validation below
checks *types*, not contents — a display name that happens to be an address is a
`str` and passes. The property has to be established where the value is resolved.

``triggered_by`` is passed through so a provider can suppress the answer for
audiences that must not see staff identities (anonymous public links, paid
chat, external portal turns). The seam does not decide that policy — it carries
the trigger label so the provider can.

**This module owns the failure handling**, not the provider (the ``mfa_gate`` /
``a2a_gate`` precedent): ``compose_system_prompt`` has no exception handler of
its own, and all three of its callers lose the execution-context block if this
raises — one of them loses the whole platform prompt. So a provider that raises,
**or that answers with a malformed shape**, degrades to ``None`` plus a warning.
A malformed shape needs its own check because ``try``/``except`` cannot see it:
a ``str`` where a list was promised iterates into single characters and silently
produces the *wrong* answer without raising.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional, Protocol

logger = logging.getLogger(__name__)

# The keys a provider answer may carry. Anything else is dropped — a provider
# cannot smuggle an unrendered field into the prompt path by adding a key.
_ANSWER_KEYS = ("primary_user_display", "role_id", "stakeholders", "proactive_consent")


class AssignmentProvider(Protocol):
    def assignment_for(
        self, agent_name: str, triggered_by: Optional[str]
    ) -> Optional[dict]:
        ...


_provider: Optional[AssignmentProvider] = None


def register_provider(provider: AssignmentProvider) -> None:
    """Register the assignment provider. Idempotent (last wins)."""
    global _provider
    _provider = provider
    logger.info("[assignment_provider] provider registered: %s", type(provider).__name__)


def get_provider() -> Optional[AssignmentProvider]:
    return _provider


def clear_provider() -> None:
    """Drop the provider — used by tests to restore the OSS no-op path.

    Mandatory rather than convenient: the test suite runs under
    ``pytest-randomly``, so a provider left registered by one test leaks into
    whichever test happens to run next.
    """
    global _provider
    _provider = None


def _validated(answer: Any, agent_name: str) -> Optional[Dict[str, Any]]:
    """Coerce a provider answer to the documented shape, or ``None``.

    Shape defects take the SAME degrade path as a raised error, because the
    alternative is worse: a wrong-typed field renders a wrong prompt line
    silently, and a silently-wrong prompt is harder to notice than a missing
    one.
    """
    if answer is None:
        return None
    if not isinstance(answer, Mapping):
        logger.warning(
            "[assignment_provider] provider returned %s (expected mapping/None); "
            "ignoring for %s",
            type(answer).__name__,
            agent_name,
        )
        return None

    out: Dict[str, Any] = {}
    for key in _ANSWER_KEYS:
        value = answer.get(key)
        if value is None:
            continue
        if key == "stakeholders":
            if not isinstance(value, (list, tuple)):
                logger.warning(
                    "[assignment_provider] provider returned %s for 'stakeholders' "
                    "(expected list/None); ignoring for %s",
                    type(value).__name__,
                    agent_name,
                )
                continue
            out[key] = [str(v) for v in value]
        elif key == "proactive_consent":
            out[key] = bool(value)
        else:
            if not isinstance(value, str):
                logger.warning(
                    "[assignment_provider] provider returned %s for %r "
                    "(expected str/None); ignoring for %s",
                    type(value).__name__,
                    key,
                    agent_name,
                )
                continue
            out[key] = value
    return out or None


def resolve_assignment(
    agent_name: Optional[str], triggered_by: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Resolve the assignment facts to render for ``agent_name``, or ``None``.

    ``None`` covers every degraded case identically — no provider, no
    assignment, a provider error, a malformed answer — because the renderer's
    contract is already "omit any field that is None or empty". There is no
    caller that could act on the difference.
    """
    if not agent_name:
        return None
    provider = _provider
    if provider is None:
        return None  # core build — no assignment record
    try:
        answer = provider.assignment_for(agent_name, triggered_by)
    except Exception:  # noqa: BLE001 — never let a provider bug break every prompt
        logger.warning(
            "[assignment_provider] provider.assignment_for failed for %s; "
            "rendering without assignment",
            agent_name,
            exc_info=True,
        )
        return None
    return _validated(answer, agent_name)
