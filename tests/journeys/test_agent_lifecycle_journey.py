"""Journey: an agent can be created, stopped, and started again (#2336).

**This is the regression of 2026-08-14 written down as a test.** On that day five
of six stopped agents could not be started — HTTP 500 on the single most
fundamental operation the platform offers — and it reached users. Nothing caught
it, because the per-PR gate collects `tests/unit/` only and no automated test
ever started a real agent.

Deliberately credential-free: nothing here asks an LLM for anything, so it runs
on every PR including from forks, where the repo will not (and should not)
expose a provider key. The 08-14 failure was in the START path, not the model.
"""
import pytest

from .conftest import (
    AGENT_RUNNING_DEADLINE_S,
    AGENT_STOPPED_DEADLINE_S,
    poll_until,
)

pytestmark = pytest.mark.journey


def _status(client, name):
    resp = client.get(f"/api/agents/{name}")
    return resp.json().get("status") if resp.status_code == 200 else None


def test_a_created_agent_reaches_running(journey_agent):
    """The `journey_agent` fixture performs create-and-wait; this asserts the
    promise it was waiting for actually held, so the journey has a named test
    rather than only a fixture."""
    assert journey_agent.get("status") == "running", (
        f"agent came up in state {journey_agent.get('status')!r} rather than "
        f"'running' — 'create an agent' is the first promise Trinity makes"
    )


def test_a_stopped_agent_can_be_started_again(journey_client, journey_agent):
    """Stop it, then start it. The 08-14 regression lived exactly here.

    Both directions are polled to a deadline, and each failure names which half
    of the round trip broke — "stopped but never restarted" and "never stopped"
    send you to different code.
    """
    name = journey_agent["name"]

    stop = journey_client.post(f"/api/agents/{name}/stop")
    assert stop.status_code in (200, 202), (
        f"stopping agent '{name}' answered {stop.status_code}: {stop.text[:300]}"
    )
    poll_until(
        lambda: _status(journey_client, name) in ("stopped", "exited"),
        deadline_s=AGENT_STOPPED_DEADLINE_S,
        describe=f"agent '{name}' was asked to stop but never left 'running'",
    )

    start = journey_client.post(f"/api/agents/{name}/start")
    assert start.status_code in (200, 202), (
        f"starting stopped agent '{name}' answered {start.status_code}: "
        f"{start.text[:300]} — this is the 2026-08-14 regression: five of six "
        f"stopped agents answered 500 here and it reached users"
    )
    poll_until(
        lambda: _status(journey_client, name) == "running",
        deadline_s=AGENT_RUNNING_DEADLINE_S,
        describe=(
            f"agent '{name}' was started after a stop but never reached "
            f"'running' again"
        ),
    )
