"""Journey-tier fixtures (#2335, Rail R1).

A *journey* is a promise the platform makes to a person, tested the way that
person meets it: through the public API, against a live stack, end to end. The
unit island cannot hold these — `tests/unit/pytest.ini` sets
`norecursedirs = ..`, which is exactly why 96 root-level live-backend tests are
invisible to the merge gate.

Three rules this tier does not bend:

**A journey FAILS; it does not skip.** `tests/conftest.py::created_agent` calls
`pytest.skip` when an agent will not start — reasonable for a fixture whose
tests are about something else, and precisely the blind spot #2336 exists to
close: on 2026-08-14 five of six stopped agents could not be started at all, and
no automated test went red. Here, "the agent never reached running" IS the
finding.

**Preconditions are checked once, loudly.** A tier that cannot run is a failure,
never a silent pass (`run-full.sh`'s stated rule 1). If the stack is unreachable
the whole tier fails with the URL it tried, not with 40 confusing errors.

**Nothing is touched that this tier did not create.** Every agent is named
`pytest-ephemeral-journey-<random>` and torn down by that name (`tests/README.md`
prefix rule, #1558). Teardown is idempotent, so a crashed run leaves the tier
re-runnable without manual cleanup.
"""
import os
import time
import uuid

import pytest
import requests

# Deadline polling is the only supported synchronisation primitive in this tier
# (#2335 AC 5). `sleep(n)` as synchronisation is what makes a suite simultaneously
# slow and flaky: it pays the worst case every run and still loses the race on a
# loaded runner.
POLL_INTERVAL_S = 2.0

# Named so a failure message can quote the promise rather than the number.
AGENT_RUNNING_DEADLINE_S = float(os.getenv("JOURNEY_AGENT_RUNNING_DEADLINE_S", "90"))
AGENT_STOPPED_DEADLINE_S = float(os.getenv("JOURNEY_AGENT_STOPPED_DEADLINE_S", "60"))
# The CREATE call itself provisions a container before it answers.
AGENT_CREATE_CALL_TIMEOUT_S = float(os.getenv("JOURNEY_AGENT_CREATE_CALL_TIMEOUT_S", "180"))

EPHEMERAL_PREFIX = "pytest-ephemeral-journey-"


def poll_until(predicate, *, deadline_s: float, describe: str,
               interval_s: float = POLL_INTERVAL_S):
    """Poll `predicate` until it returns a truthy value or the deadline passes.

    Returns the truthy value. Raises `AssertionError` naming the broken promise
    and how long it waited — never a bare `assert 200 == 500` (#2336 AC 6).
    """
    started = time.monotonic()
    last = None
    while time.monotonic() - started < deadline_s:
        last = predicate()
        if last:
            return last
        time.sleep(interval_s)
    waited = time.monotonic() - started
    raise AssertionError(
        f"{describe} — waited {waited:.0f}s of {deadline_s:.0f}s. "
        f"Last observation: {last!r}"
    )


@pytest.fixture(scope="session")
def journey_client(api_client):
    """The live-stack client, with the tier's precondition checked ONCE.

    Reuses `tests/conftest.py`'s authenticated client rather than building a
    second one — R1 says reuse what works, and a parallel harness is how two
    auth paths drift.
    """
    url = os.getenv("TRINITY_API_URL", "http://localhost:8000")
    try:
        health = requests.get(f"{url}/health", timeout=10)
    except requests.RequestException as e:
        pytest.fail(
            f"journey tier requires a live stack at {url} and could not reach it "
            f"({type(e).__name__}). Start one with ./scripts/deploy/start.sh, or "
            f"point TRINITY_API_URL at a dev instance. A tier that cannot run is "
            f"a failure, not a skip."
        )
    if health.status_code != 200:
        pytest.fail(
            f"journey tier requires a healthy stack at {url}; /health answered "
            f"{health.status_code}. The stack is up but not serving — that is a "
            f"finding, not a reason to skip."
        )
    return api_client


@pytest.fixture()
def journey_agent_name() -> str:
    """A unique name under the reserved prefix, so teardown can never reach an
    agent this tier did not create."""
    return f"{EPHEMERAL_PREFIX}{uuid.uuid4().hex[:8]}"


def delete_agent_idempotent(client, name: str) -> None:
    """Teardown that is safe to run twice, and on an agent that never existed.

    The tier must be re-runnable after a crash without manual cleanup (#2335
    AC 3), which means teardown cannot assume the create succeeded.
    """
    try:
        client.delete(f"/api/agents/{name}")
    except Exception:  # noqa: BLE001 — teardown must never mask the real failure
        pass


@pytest.fixture()
def journey_agent(journey_client, journey_agent_name):
    """An agent created through the public API, reached `running`, torn down.

    This fixture IS the first half of J03: "create an agent from a template and
    have it come up". A failure here is reported as the broken promise, not as
    a skipped test.
    """
    name = journey_agent_name
    # Creating an agent provisions a container; the shared client's 30s default
    # is a request timeout for ordinary calls and is genuinely too short here —
    # it surfaced as `httpx.ReadTimeout` at fixture setup, which reads as harness
    # breakage rather than as the platform being slow. The deadline that matters
    # is still the poll below; this only stops the CALL from giving up first.
    def is_running():
        check = journey_client.get(f"/api/agents/{name}")
        if check.status_code != 200:
            return None
        state = check.json()
        return state if state.get("status") == "running" else None

    # The CREATE is inside the try, so the finally reaches a partial one.
    #
    # It used to sit above, with its `raise AssertionError` outside the block —
    # so a create that provisioned the ownership row and THEN failed (a 500
    # after the row is written, or a read timeout on a request the backend went
    # on to complete) left an agent teardown never touched. That breaks this
    # tier's own rule that it is re-runnable after a crash with no manual
    # cleanup, and it breaks it on the path where cleanup matters most: the
    # failure case. Moving it in costs nothing — `delete_agent_idempotent` is
    # already safe on an agent that was never created.
    try:
        resp = journey_client.post(
            "/api/agents", json={"name": name}, timeout=AGENT_CREATE_CALL_TIMEOUT_S,
        )
        if resp.status_code not in (200, 201):
            raise AssertionError(
                f"creating agent '{name}' through POST /api/agents answered "
                f"{resp.status_code}: {resp.text[:400]}. Creating an agent is the "
                f"first thing anyone does with Trinity."
            )

        agent = poll_until(
            is_running,
            deadline_s=AGENT_RUNNING_DEADLINE_S,
            describe=f"agent '{name}' was created but never reached 'running'",
        )
        yield agent
    finally:
        delete_agent_idempotent(journey_client, name)
