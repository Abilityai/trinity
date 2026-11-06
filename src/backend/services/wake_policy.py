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
2. ``agent_ownership.operator_resume_enabled`` (ent#329, tri-state since
   ent#1108) — the owner's decision on the older, narrower switch. A 1 is an
   explicit opt-in made by hand before the wider control existed, and it keeps
   meaning "on" even if the instance default is later turned off, because
   revoking a decision a person made is worse than one extra turn. A 0 is an
   explicit opt-OUT and keeps meaning "off" for the same reason. An explicit
   tri-state value wins over either: that is the same owner, speaking more
   recently.
3. ``system_settings['wake_on_ending_default']`` — the instance default, **ON**
   (ruling D4). ent#790's flip lives here rather than in a ``DEFAULT 1`` on the
   column, so it is one admin decision rather than a bulk rewrite of intent.

A 0 can be read as "off" only because ent#1108 made it distinguishable: the
column shipped ``DEFAULT 0``, so before that migration an owner who turned it
off was byte-identical to one who never saw it. The ent#1108 migration moved
every undecided 0 to NULL (keeping a 0 only where the audit log shows the
owner's latest toggle said off), so a 0 that survives IS the owner's voice, and
the whole pre-upgrade fleet reads NULL and follows the instance default. The
read is therefore RAW (``get_operator_resume_override``), never the COALESCEd
boolean getter — that one folds NULL into the ent#329 default and would make
the instance default below unreachable.

Fail-closed: an empty agent name, an agent with no live ``agent_ownership`` row
(unknown or soft-deleted — nobody owns it, so a wake would be spend on nothing,
the ent#1108 getter's rule; source ``no_owner``), or ANY of the three reads
raising, resolves to OFF. Never spend a turn on a flag you could not read (the ent#329 rule) — which
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
from db.agent_settings.operator_resume import NO_LIVE_ROW

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
SOURCE_NO_OWNER = "no_owner"


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

    if override is NO_LIVE_ROW:
        # Nobody owns this agent (no row, or soft-deleted): nothing to bill a
        # turn to, so no wake — never the instance default.
        return WakeVerdict(enabled=False, source=SOURCE_NO_OWNER)
    if override is not None:
        return WakeVerdict(enabled=bool(override), source=SOURCE_AGENT)

    try:
        legacy = db.get_operator_resume_override(agent_name)
    except Exception:  # noqa: BLE001
        # Fail CLOSED, like the read above it. This path used to swallow the
        # error and carry on to an instance default of ON, i.e. a read failure
        # became a billed turn — the exact trade the docstring above forbids.
        logger.warning(
            "wake_policy: operator_resume_enabled unreadable for %s; resolving OFF",
            agent_name,
        )
        return WakeVerdict(enabled=False, source=SOURCE_UNREADABLE)

    if legacy is not None:
        return WakeVerdict(enabled=bool(legacy), source=SOURCE_LEGACY_RESUME)

    try:
        default_on = instance_default()
    except Exception:  # noqa: BLE001
        logger.warning(
            "wake_policy: %s unreadable; resolving OFF for %s", SETTING_DEFAULT, agent_name
        )
        return WakeVerdict(enabled=False, source=SOURCE_UNREADABLE)

    return WakeVerdict(enabled=default_on, source=SOURCE_INSTANCE)
