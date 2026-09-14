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

    The restart is deliberately made to require a CONTAINER RECREATE, because
    that is the condition the regression needed. Without it this journey passes
    against the broken code (measured, not assumed).
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

    # Force the condition the 08-14 regression actually needed. #2186's title
    # says it exactly: "starting a stopped agent no longer 500s WHEN A RECREATE
    # IS NEEDED". A fresh agent stopped and started immediately has no config
    # drift, so `start_agent_internal` takes the plain-start path and the bug is
    # unreachable — verified empirically: replaying the regression with this
    # step absent left the gate GREEN, which is the whole reason AC #3 exists.
    #
    # Changing the resource limits while stopped makes `check_resource_limits_match`
    # false, so the next start must recreate the container — the exact path that
    # raised 500 for five of six agents.
    # The values are DERIVED from what the agent currently has, not hardcoded.
    # This step read `{"memory": "2g", "cpu": "1"}`, which forced a recreate only
    # because `AGENT_DEFAULT_MEMORY == "4g"` and `AGENT_DEFAULT_CPU == "2"` at the
    # time — constants this test never reads, and which an operator can move at
    # runtime through `PUT /api/settings/agent-defaults/resources`. Set the fleet
    # default to 2g/1 and the PUT becomes a no-op, `check_resource_limits_match`
    # stays true, no recreate happens, and this journey silently loses its teeth
    # in exactly the way it already lost them twice.
    before = journey_client.get(f"/api/agents/{name}/resources")
    assert before.status_code == 200, (
        f"could not read current resources for stopped agent '{name}': "
        f"{before.status_code} {before.text[:200]}"
    )
    cur = before.json()
    cur_mem = cur.get("memory") or cur.get("current_memory")
    cur_cpu = str(cur.get("cpu") or cur.get("current_cpu") or "")
    # Any other legal value will do; the point is only that it DIFFERS.
    want_mem = "1g" if str(cur_mem).startswith("2") else "2g"
    want_cpu = "2" if cur_cpu.startswith("1") else "1"

    drift = journey_client.put(
        f"/api/agents/{name}/resources", json={"memory": want_mem, "cpu": want_cpu},
    )
    assert drift.status_code in (200, 202), (
        f"could not change resources on stopped agent '{name}' to force a "
        f"recreate: {drift.status_code} {drift.text[:200]}"
    )

    # Assert the drift actually took, so "could not force a recreate" fails with
    # its own name instead of surfacing later as "the restart 500'd" — or worse,
    # as a green run that exercised the plain-start path.
    after = journey_client.get(f"/api/agents/{name}/resources").json()
    assert (after.get("memory"), str(after.get("cpu"))) == (want_mem, want_cpu), (
        f"resources did not change on '{name}': asked for {want_mem}/{want_cpu}, "
        f"read back {after.get('memory')}/{after.get('cpu')} (was {cur_mem}/{cur_cpu}). "
        f"Without a real change there is no config drift, `start_agent_internal` "
        f"takes the plain-start path, and the #2186 regression this journey "
        f"exists to catch is unreachable"
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
