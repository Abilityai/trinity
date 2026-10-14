"""J17 — a fact that lands at the wrong agent reaches its owner (trinity-enterprise#794).

Skeleton. Needs the reference company (trinity-enterprise#793) and the fact
routing protocol (trinity-enterprise#804); `dev` has no routing or receipt
record for a fact. When they land, the steps are:

1. a fact is handed to an agent that does not own it;
2. it arrives at the owning agent — a platform record of the hand-off;
3. the sender holds a receipt;
4. the fact exists in exactly one place afterwards (the canon's git history).
"""
import pytest

from .conftest import not_run


@pytest.mark.chain("J17", "A fact reaches its owner")
def test_a_fact_reaches_its_owner(chain):
    not_run("blocked by abilityai/trinity-enterprise#793 (reference company) and #804 "
            "(fact routing) — no routing or receipt record exists on dev")
