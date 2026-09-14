"""The voice call's tool manifest — what the model may call, decided once (ent#535).

A Workspace voice call lets the agent **act as itself**, and the surface a
realtime model can reach has to be a decision rather than an accident. Three
rules, all pure and all here:

1. **The manifest is a fixed set, resolved once at session start.** The browser
   never sends a tool list — the config is built server-side — but "the browser
   cannot widen it" must stay true by construction rather than by the current
   shape of one function, which is why the allowed NAMES travel with the session
   and the dispatcher refuses anything outside them (defence in depth, the Brain
   Orb `/action` shape).

2. **A per-agent declaration may only NARROW.** `template.yaml` is
   agent-writable, so a declaration that could ADD a tool would be a
   privilege-escalation primitive: an agent that can edit its own template could
   grant itself a capability the platform never offered. Intersection only. When
   the platform later defines an allowlist of direct tools, additions come from
   THAT list, not from the file.

3. **Fleet tools are not in v1** — no `list_agents`, `chat_with_agent`,
   `fan_out`. Recorded as a decision (ent#535 AC 4), and enforced by the fact
   that this module is the only place a name can enter the manifest.

Pure and dependency-free on purpose: the decision is testable without a Gemini
session, a container or a socket.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Set

logger = logging.getLogger(__name__)

#: The one tool that runs the agent. In a Workspace call it runs the agent IN
#: THE CHAT'S OWN SESSION (ent#535), which is what makes a call different from
#: a stateless one-shot against the container.
RUN_TASK = "run_task"

#: The canvas tools. Available only in `workspace_mode` — a phone call (VoIP)
#: has no canvas to draw on, so offering them there would advertise a surface
#: the caller cannot see.
PANEL_TOOLS: frozenset = frozenset({
    "show_markdown", "update_panel", "append_to_panel", "clear_panel",
    "show_diagram", "show_image",
})

#: Everything the platform is willing to offer a voice session today. A name
#: that is not in here cannot be reached by any declaration, which is what makes
#: rule 2 above enforceable rather than advisory.
PLATFORM_VOICE_TOOLS: frozenset = frozenset({RUN_TASK}) | PANEL_TOOLS


def platform_default_tools(*, workspace_mode: bool) -> frozenset:
    """The manifest before any per-agent narrowing."""
    return frozenset({RUN_TASK}) | (PANEL_TOOLS if workspace_mode else frozenset())


def normalize_declared_tools(declared: Any) -> Optional[Set[str]]:
    """Read a `template.yaml` `voice: tools: [...]` declaration, tolerantly.

    The ent#89 reader shape: never raises, and answers **None** for "the agent
    declared nothing" — which is different from `[]`, an explicit declaration
    that the agent wants no tools at all. Collapsing those two would make an
    empty list unexpressible and a malformed one silently disarming.

    Unknown names are dropped with a warning rather than failing the call: a
    typo, or a tool from a newer platform, must not cost the agent its voice.
    """
    if declared is None:
        return None
    # A SEQUENCE, explicitly — not "any Iterable". A mapping is iterable, so a
    # `voice: tools: {run_task: true}` typo would silently narrow to its KEYS,
    # i.e. a malformed declaration would take effect. A string is iterable too,
    # and would narrow to its characters.
    if not isinstance(declared, (list, tuple, set, frozenset)):
        logger.warning("[ent#535] voice.tools is not a list — ignoring the declaration")
        return None
    names: Set[str] = set()
    unknown: Set[str] = set()
    for raw in declared:
        if not isinstance(raw, str):
            continue
        name = raw.strip()
        if not name:
            continue
        (names if name in PLATFORM_VOICE_TOOLS else unknown).add(name)
    if unknown:
        logger.warning(
            "[ent#535] voice.tools names %s, which the platform does not offer — dropped",
            sorted(unknown),
        )
    return names


def resolve_manifest(declared: Any = None, *, workspace_mode: bool) -> frozenset:
    """The names this session may call. Narrowing only, never widening.

    An agent that declares nothing gets the platform default. An agent that
    declares a subset gets that subset. An agent that declares a tool the
    platform does not offer does not get it — the intersection is the whole
    mechanism, and it is why a compromised or self-editing agent cannot grant
    itself a capability by writing a file.
    """
    default = platform_default_tools(workspace_mode=workspace_mode)
    names = normalize_declared_tools(declared)
    if names is None:
        return default
    return frozenset(names) & default
