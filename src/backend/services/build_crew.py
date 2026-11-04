"""Workspace Build's agents, which are the tools for making a first agent.

Nobody should see one counted as having made one. Each person who can build has
their own guide: ``build-guide`` for the first one ever deployed, ``atlas-`` and 8
hex digits (with ``-2`` to ``-9`` when the name was held) for anyone else. Instances
from before then may still run ``agent-builder``. The frontend keeps the same rule
in ``src/frontend/src/components/portal/builder/portalBuild.js`` (``isBuildCrew``).

Standard library only, so the rule can be tested without the backend.
"""

import re

_LEGACY_CREW = frozenset({"build-guide", "agent-builder"})
# A deploy adds `_2` to a name a live agent already has (system_service.get_next_agent_name).
_GUIDE_NAME = re.compile(r"^(atlas-[0-9a-f]{8}(-[2-9])?|build-guide)(_\d+)?$")


def is_build_crew(name) -> bool:
    """True for any person's guide and for the old Agent Builder."""
    if not isinstance(name, str) or not name:
        return False
    return name in _LEGACY_CREW or bool(_GUIDE_NAME.match(name))
