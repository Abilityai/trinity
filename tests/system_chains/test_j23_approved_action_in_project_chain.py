"""J23 — an approved action inside a project (trinity-enterprise#795).

Skeleton. A project task whose work is a gated action should wait for the
approval, run after it, and leave the approval and its outcome in the
project's log. The two halves exist and are proven apart — the gate by J21,
the project by J22 — but nothing joins them: a gate request carries no
project, and no platform writer puts an approval or its outcome into a
project's log. The only way the log could show it today is an agent writing it
there, which this tier does not accept as evidence. So the chain reports NOT
RUN with that reason rather than passing on the agent's word.

When the join exists, the steps are:

1. a project with the executor agent active and a task whose work is the gated skill;
2. the agent's request for the skill is held (`skill_gate_requests` pending, no run);
3. the approver approves in their own Inbox; the skill runs exactly once;
4. the project's log carries the approval (who decided, when) and the run's
   outcome, written by the platform — readable by a member who took no part.
"""
import pytest

from .conftest import not_run


@pytest.mark.chain("J23", "An approved action inside a project")
def test_an_approved_action_inside_a_project(chain):
    not_run("no platform writer joins a gate request to a project: the request carries no "
            "project, and nothing writes the approval or its outcome to the project's log "
            "(abilityai/trinity-enterprise#880)")
