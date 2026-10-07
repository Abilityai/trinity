"""J16 — a fact published to the canon reaches its consumers (trinity-enterprise#794).

Skeleton. Needs the reference company (trinity-enterprise#793) and the change
propagation protocol (trinity-enterprise#804/#805/#806, #283). On `dev` nothing
records which canon version an output read and nothing serves a staleness
warning, so a pass today could only rest on what an agent says — which this
chain does not accept. When those land, the steps are:

1. one agent publishes a changed fact to its canon folder — the canon's git
   history holds the commit;
2. the consuming agent's next output uses the new value and names the canon
   version it read — from the execution record, not the reply text;
3. a fact older than its freshness bound is served with a staleness warning;
4. a write into another agent's folder ends as a pull request, not a direct change.
"""
import pytest

from .conftest import not_run


@pytest.mark.chain("J16", "A canon fact reaches its consumers")
def test_a_canon_fact_reaches_its_consumers(chain):
    not_run("blocked by abilityai/trinity-enterprise#793 (reference company) and #804 "
            "(change propagation) — no canon-version or staleness record exists on dev")
