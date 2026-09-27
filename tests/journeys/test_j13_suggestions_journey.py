"""Journey J13: an agent tells me what I can do with it and what is waiting on me (ent#465).

**Promise:** I open an agent in the Workspace and see a short list made for me —
a question it is waiting on me for, a schedule of mine that keeps failing, a
playbook I have never run — each saying why, each one click away, and nothing
padded in when there is nothing to say.

The issue asked for "J11-recommended-actions"; J11 and J12 were already taken,
so this ships as **J13**.

**A skeleton, deliberately.** The rules, the SQL and the doors are proven in
the unit island (`tests/unit/test_ent465_suggestions.py`) and the verbs by a
mount (`src/frontend/tests/unit/portalSuggestions.spec.js`); none of that is a
real schedule failing on a real agent and the owner being told. `strict=True`
is the load-bearing half: if this ever passes by accident the suite goes red.
"""
import pytest

pytestmark = pytest.mark.journey


@pytest.mark.xfail(
    strict=True,
    reason="J13 harness not built — abilityai/trinity-enterprise#465. The "
           "walk needs a running agent whose schedule really fails and a "
           "platform user and a magic-link client on the same instance.",
)
def test_j13_suggestions_are_mine_and_true():
    """What this has to assert when it is built, in the user's own terms:

    1. as the OWNER, create a schedule whose command fails, let it run three
       times — `GET .../agents/{name}/suggestions` names it ("Failed 3 runs in
       a row"), and its Accept opens `/agents/{name}?tab=schedules`;
    2. dismiss it, let it fail a fourth time — it stays dismissed; let it
       succeed and then fail three more times — it is back;
    3. as a SHARED platform user of the same agent — the failing schedule is
       not in their list, and their list differs from the owner's;
    4. as a MAGIC-LINK client of the same agent — the endpoint is a 404;
    5. stop the agent — capability items disappear and the response says
       capabilities are unavailable, while the schedule item stays.
    """
    raise NotImplementedError("J13 harness not built")
