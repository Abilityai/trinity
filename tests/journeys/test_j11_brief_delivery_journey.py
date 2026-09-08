"""Journey J11: a companion's brief reaches me where I already work (#2565).

**Promise:** a schedule names one Workspace user; when it fires, the output lands
as a turn in that person's Main chat with the agent, without them asking.

J11 is the first journey that reaches for the USER rather than the other way
round. J05 — "I can schedule work, walk away, and find out what happened" — ends
at the executions list, which is the operator's surface; a person who is not the
operator has never seen a scheduled run's output.

**A skeleton, deliberately.** The delivery half ships with
trinity-enterprise#498; the harness is #2565's own work. A `strict=True` xfail is
what Gate G1 asks a `new:` declaration to carry — a named, failing-on-success
claim standing in the tier — and `strict` is the load-bearing half: if this ever
passes by accident the suite goes RED, so the skeleton cannot quietly become a
test that asserts nothing.

Credential-bound like J03: "the brief arrives" means a real model answered, and
every PR-triggered workflow here is deliberately credential-free
(`integration-nightly.yml` states why in full).
"""
import pytest

pytestmark = pytest.mark.journey


@pytest.mark.xfail(
    strict=True,
    reason="J11 harness not built — abilityai/trinity#2565. The delivery path "
           "ships with trinity-enterprise#498; this asserts the promise end to "
           "end and needs a Workspace client, a shared agent and a real fire.",
)
def test_j11_a_scheduled_brief_lands_in_the_users_main_chat():
    """The promise, not the plumbing.

    What this has to assert when it is built, in the user's own terms:

    1. a schedule carrying `deliver_to_workspace_email` fires, and the output
       appears as a NEW assistant turn in that person's Main chat with the agent
       (ent#523) — read back through the Workspace API they would use;
    2. a re-delivered fire posts exactly ONCE (`report_completion`'s effect guard
       is keyed on the execution id, and a fire is one execution);
    3. delivery never interleaves with an in-flight turn on that thread;
    4. a target who has lost access, or an address the instance does not know,
       produces a VISIBLE failure on the execution row — never a silent no-op;
    5. the delivered message is rateable like any other agent message (#366).

    Invariants it rides on: E-01 (terminal-state closure), E-05 (dispatched rows
    have a session), L-03 (delete cascades leave no stranded thread).
    """
    raise AssertionError("J11 harness not implemented — see abilityai/trinity#2565")
