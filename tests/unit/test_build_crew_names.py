"""Workspace Build: no guide counts as one of the person's own agents.

Each person who can build has their own guide (``build-guide`` for the first one ever
deployed, ``atlas-`` and 8 hex digits for anyone else), plus the Agent Builder that
phase 1 deployed. The same rule lives in the frontend's ``portalBuild.js``.
"""

import importlib.util
from pathlib import Path

# Loaded by path: importing it through the `services` package would pull in the whole
# backend, and this rule needs nothing but the standard library.
_PATH = Path(__file__).resolve().parents[2] / "src" / "backend" / "services" / "build_crew.py"
_spec = importlib.util.spec_from_file_location("build_crew", _PATH)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
is_build_crew = _module.is_build_crew


def test_guides_and_the_old_builder_are_the_crew():
    for name in ("build-guide", "agent-builder", "atlas-0a1b2c3d", "atlas-0a1b2c3d-2", "atlas-0a1b2c3d-9"):
        assert is_build_crew(name), name


def test_the_persons_agents_are_not():
    for name in ("researcher", "atlas-helper", "atlas-0a1b2c3d-10", "atlas-0A1B2C3D", "x-atlas-0a1b2c3d", "", None):
        assert not is_build_crew(name), name


def test_a_name_trinity_suffixed_is_still_a_guide():
    # The deploy adds _2 to a name a live agent already has (system_service.get_next_agent_name).
    for name in ("atlas-0a1b2c3d_2", "atlas-0a1b2c3d-2_3", "build-guide_2"):
        assert is_build_crew(name), name
    assert not is_build_crew("atlas-0a1b2c3d_x")
