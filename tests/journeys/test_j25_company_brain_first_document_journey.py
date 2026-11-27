"""Journey J25: my company brain takes in my first document (trinity-enterprise#1409).

**Promise:** after my first agent's first result, the assistant introduces
Cornelius as my company brain in one card. I say yes, it is set up without me
typing a command, and the card says when it is ready. I drop a document on
Cornelius's conversation in my Workspace, say "take this in", and it replies
with what it wrote; the brain's note count goes up and I can ask about it.

**A skeleton, deliberately.** The state read is proven in the unit island over
the template's real `Brain/` layout through the shipped agent-server files
router (`tests/unit/test_ent1409_brain_state.py`), and the offer, the decline,
the setup run and the ready card by a mount
(`src/frontend/tests/unit/assistantCompanyBrain1409.mount.spec.js`). None of
that is a real Cornelius container running `/first-run` on a real Claude
credential and taking in a real file. `strict=True` is the load-bearing half:
if this ever passes by accident the suite goes red.
"""
import pytest

pytestmark = pytest.mark.journey


@pytest.mark.xfail(
    strict=True,
    reason="J25 harness not built — abilityai/trinity-enterprise#1409. The "
           "walk needs the seeded Cornelius on a Claude credential, a person "
           "whose first agent has produced a result, and a Workspace upload.",
)
def test_j25_company_brain_takes_in_the_first_document():
    """What this has to assert when it is built, in the person's own terms:

    1. before the first result, `GET /api/onboarding/brain` says `not_set_up`
       and the assistant shows no brain card;
    2. after the first result there is exactly one card, "Already on your
       team: Cornelius, your company brain"; "Not now" in another session
       never brings it back;
    3. "Set it up" starts `/first-run --empty` on cornelius (an execution the
       person did not type), the card follows it, and once it ends
       `GET /api/onboarding/brain` says `ready` with 0 notes and the card says
       where documents go;
    4. a file dropped on Cornelius's Workspace conversation with "take this
       in" gets a reply naming the notes it wrote, and the note count is now
       above 0;
    5. delete cornelius: the state is `absent` and nothing is offered.
    """
    raise AssertionError("not built")
