"""Journey J20: a colleague joins the work — the pointer half (ent#630, ent#631).

**Promise:** I can tell a colleague about the work from inside the conversation,
and they are told — without being given anything they could not already see.

The epic named it "J12-a-colleague-joins-the-work"; J12 (and J13–J19) were
already taken, so it ships as **J20**. ent#631 is the pointer (a tag lands in
the colleague's Inbox); the seat (the colleague joins the room) is the epic's
incubating half and will extend this journey.

**A skeleton, deliberately.** The rules and the doors are proven in the unit
island (`tests/unit/test_ent631_person_mention.py`) and the picker and the
Inbox item by a mount (`src/frontend/tests/unit/portalPersonMention.mount.spec.js`);
none of that is two real people on one instance. `strict=True` is the
load-bearing half: if this ever passes by accident the suite goes red.
"""
import pytest

pytestmark = pytest.mark.journey


@pytest.mark.xfail(
    strict=True,
    reason="J20 harness not built — abilityai/trinity-enterprise#631. The walk "
           "needs two platform users on one instance, an agent shared with the "
           "second, and a room the first one runs.",
)
def test_j20_a_tag_reaches_the_colleague_and_grants_nothing():
    """What this has to assert when it is built, in the users' own terms:

    1. Alice opens a room with an agent and types `@bo` — Bob (who can reach the
       agent) is offered; Dave (who cannot) and a made-up name are not, and the
       made-up name is refused by name;
    2. Alice sends "@Bob Baker take a look" with Bob tagged — the agent's wake
       is exactly what it would have been without Bob's name;
    3. Bob's Inbox shows ONE Unread item, "Alice mentioned you in the room …";
       opening it says he is not in the room and that Alice can let him in, and
       shows nothing of the room — not even Alice's message;
    4. Alice's message shows "Bob · in their Inbox", then "Bob · read";
    5. Alice sends the same tag twice in one message — still one item; an
       agent posting "@Bob Baker" through MCP creates none.
    """
    raise NotImplementedError("J20 harness not built")
