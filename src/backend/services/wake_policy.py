"""Who may be woken when something they are waiting on ends (ent#1102).

A wake spends a billed turn on an agent that did not schedule it, so exactly one
place decides whether it may: ``resolve``. Both wake paths read it —
``caller_wake_service`` (a delegated run ended) and ``operator_resume_service``
(an ask the agent parked was answered or ended, ent#329/ent#611) — so an owner
who silences one has silenced both, and there is no second copy of the policy to
drift.

Three inputs, in this order:

1. ``agent_ownership.wake_on_ending`` (tri-state, ent#1102) — the owner's current
   voice through the Settings control. NULL is *not* "off", it is "no opinion".
2. ``agent_ownership.operator_resume_enabled = 1`` (ent#329) — an explicit opt-in
   somebody made by hand before the tri-state existed. It keeps meaning "on"
   even if the instance default is later turned off, because revoking a decision
   a person made is worse than one extra turn. An explicit tri-state value wins
   over it: that is the same owner, speaking more recently.
3. ``system_settings['wake_on_ending_default']`` — the instance default, **ON**
   (ruling D4). ent#790's flip lives here rather than in a ``DEFAULT 1`` on the
   column, so it is one admin decision rather than a bulk rewrite of intent.

A legacy ``operator_resume_enabled = 0`` is NOT read as "off": the column shipped
``DEFAULT 0``, so an owner who turned it off is byte-identical to one who never
saw it, and reading that as an opt-out would silence the entire pre-upgrade
fleet. Those owners are covered by the instance default and can say "off" again
through the tri-state, where the answer is distinguishable.

Fail-closed: an empty agent name, or ANY of the three reads raising, resolves to
OFF. Never spend a turn on a flag you could not read (the ent#329 rule) — which
includes the instance default, whose read is as fallible as the other two.

The reads go through the module-level ``db``, never a function-local import. A
local import binds whatever ``sys.modules`` holds at CALL time and is invisible
to a test that substitutes its own ``db`` on the module under test, so the four
gates this resolver replaced lost their opt-out and fail-closed coverage the
moment they started calling it (the stale-fake class, found by the ent#1102
sweep). One module attribute means one place to point at.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from database import db

logger = logging.getLogger(__name__)

SETTING_DEFAULT = "wake_on_ending_default"
# Ruling D4 (hub retrospective 2026-11-04): the default is ON. The retro
# measured the cost of OFF — an agent that cannot be woken polls instead, which
# cost more than any wake storm would. Flipping this literal is the whole
# revert: nothing else reads a hardcoded default.
INSTANCE_DEFAULT_ON = True

_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})

# Resolution sources, as they appear in the receipt, the audit row and
# ``get_agent_info`` — so an operator reading "why was I woken" gets the layer
# that decided, not just the verdict.
SOURCE_AGENT = "agent"
SOURCE_LEGACY_RESUME = "legacy_resume"
SOURCE_INSTANCE = "instance"
SOURCE_UNREADABLE = "unreadable"


@dataclass(frozen=True)
class WakeVerdict:
    """The decision plus the layer that made it."""

    enabled: bool
    source: str

    @property
    def reason(self) -> Optional[str]:
        """A receipt-ready reason, or None when the wake is on."""
        if self.enabled:
            return None
        return f"wake_disabled_by_{self.source}"


def instance_default() -> bool:
    """The instance-wide default, ON unless an admin wrote otherwise.

    Raises if the setting cannot be READ. ``resolve`` turns that into OFF — the
    unreadable-flag rule, which is about the read failing and not about what the
    value says. A setting that reads fine but says something unrecognised is a
    different case and falls back to the built-in default below: ruling D4 is
    the policy for an absent or malformed VALUE, never for a broken read.
    """
    raw = db.get_setting_value(
        SETTING_DEFAULT, default="true" if INSTANCE_DEFAULT_ON else "false"
    )
    text = str(raw if raw is not None else "").strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return INSTANCE_DEFAULT_ON


def resolve(agent_name: Optional[str]) -> WakeVerdict:
    """Whether ``agent_name`` may be woken, and which layer decided.

    The only reader of the wake policy. Fail-closed on every error path: a wake
    that does not happen is recoverable (the ending stays readable through
    ``get_execution_result``), a wake billed on an unreadable flag is not.
    """
    if not agent_name:
        return WakeVerdict(enabled=False, source=SOURCE_UNREADABLE)

    try:
        override = db.get_wake_on_ending(agent_name)
    except Exception:  # noqa: BLE001
        logger.warning(
            "wake_policy: wake_on_ending unreadable for %s; resolving OFF", agent_name
        )
        return WakeVerdict(enabled=False, source=SOURCE_UNREADABLE)

    if override is not None:
        return WakeVerdict(enabled=bool(override), source=SOURCE_AGENT)

    try:
        legacy_optin = db.get_operator_resume_enabled(agent_name)
    except Exception:  # noqa: BLE001
        # Fail CLOSED, like the read above it. This path used to swallow the
        # error and carry on to an instance default of ON, i.e. a read failure
        # became a billed turn — the exact trade the docstring above forbids.
        logger.warning(
            "wake_policy: operator_resume_enabled unreadable for %s; resolving OFF",
            agent_name,
        )
        return WakeVerdict(enabled=False, source=SOURCE_UNREADABLE)

    if legacy_optin:
        return WakeVerdict(enabled=True, source=SOURCE_LEGACY_RESUME)

    try:
        default_on = instance_default()
    except Exception:  # noqa: BLE001
        logger.warning(
            "wake_policy: %s unreadable; resolving OFF for %s", SETTING_DEFAULT, agent_name
        )
        return WakeVerdict(enabled=False, source=SOURCE_UNREADABLE)

    return WakeVerdict(enabled=default_on, source=SOURCE_INSTANCE)
