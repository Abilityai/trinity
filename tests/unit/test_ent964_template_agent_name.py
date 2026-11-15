"""The catalog names the agent a GitHub template declares — trinity-enterprise#964.

The Create Agent dialog prefills Name from the source the person picked: a
GitHub template's own `template.yaml` `name:` when it declares one, else its
repository name without the owner. `display_name` cannot carry that: it is the
admin override, then the template's `display_name`, then `name:` — a catalog
title, not the agent's name. So GitHub entries carry `agent_name`, the declared
`name:` alone, or None.

`template.yaml` is untrusted (#1900): `name:` may be missing, blank, or not a
string at all, and none of those may raise on the catalog path (one raise
empties `GET /api/templates`, the #1835 class). The value is only ever an
input's starting text; the backend still sanitizes the name at create time.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytestmark = pytest.mark.unit


def _build(metadata, override=None):
    from services import template_service
    return template_service._build_template("acme/trinity-pm", metadata, override)


def test_a_declared_name_is_the_agent_name():
    entry = _build({"name": "trinity-pm", "display_name": "Trinity PM — Product Manager"})
    assert entry["agent_name"] == "trinity-pm"
    assert entry["display_name"] == "Trinity PM — Product Manager"


def test_the_admin_title_does_not_replace_the_declared_name():
    entry = _build({"name": "trinity-pm"}, {"display_name": "Our PM"})
    assert entry["agent_name"] == "trinity-pm"
    assert entry["display_name"] == "Our PM"


def test_a_declared_name_is_trimmed():
    assert _build({"name": "  trinity-pm \n"})["agent_name"] == "trinity-pm"


@pytest.mark.parametrize("metadata", [
    {},
    {"name": ""},
    {"name": "   "},
    {"name": None},
    {"name": 42},
    {"name": ["trinity-pm"]},
    {"name": {"x": 1}},
    {"name": True},
])
def test_no_usable_declared_name_is_none(metadata):
    """The dialog then falls back to the repository name."""
    assert _build(metadata)["agent_name"] is None


def test_a_non_mapping_template_yaml_still_builds():
    assert _build(["not", "a", "mapping"])["agent_name"] is None


def test_an_overlong_declared_name_is_capped():
    entry = _build({"name": "a" * 500})
    assert len(entry["agent_name"]) == 128
