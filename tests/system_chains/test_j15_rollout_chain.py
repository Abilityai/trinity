"""J15 — a fleet rolls out from one definition (trinity-enterprise#794).

Skeleton. The chain runs against the reference company (trinity-enterprise#793),
which does not exist yet, so it reports NOT RUN with that reason rather than
passing on an empty fleet. When ent#793 lands, the steps are:

1. deploy the reference company's manifest to a fresh install;
2. every agent holds its role's skills — `GET /api/agents/{n}/skills` and the
   platform-written `~/.claude/skills/<name>/.trinity-skill.json` per agent;
3. every agent's canon is readable and verified — the canon roles read
   (`canon_roles_service`) answers with roles, not a named empty;
4. the orchestrator's picture of the fleet matches what was deployed — the
   platform's agent list and assignments, compared with the manifest.
"""
import pytest

from .conftest import not_run


@pytest.mark.chain("J15", "A fleet rolls out from one definition")
def test_a_fleet_rolls_out_from_one_definition(chain):
    not_run("blocked by abilityai/trinity-enterprise#793 — the reference company it rolls out "
            "does not exist yet")
