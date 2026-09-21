"""Journey J10: my agents can call each other, and I can see what they said (#2349).

**Promise:** a playbook call from agent A — `chat_with_agent`, `fan_out`,
`run_agent_loop` — reaches agent B and comes back with B's result; a call the
permission model forbids is refused and the caller is told why; a stopped
callee fails fast with an honest reason instead of hanging; a fan-out and a
loop stay inside the bounds they were given; and the platform records who
called whom, so a person can read what was said.

**How it is driven.** Every call is made the way a real playbook makes it:
through the MCP server, authenticated with A's OWN agent-scoped key, read from
A's container because no API returns it (by design). That is where
`checkAgentAccess` lives (P-02), so a harness that talked to the backend as
admin would prove nothing about the boundary. The admin JWT is used only to
arrange things (edges, stop/start, a disposable callee) and to read back what
the platform recorded.

**Where the boundary lives — and where it does not.** The permission edge is
enforced by the MCP server. An agent key used raw against the backend resolves
to its owner carrying the owner's role (architecture.md Invariant #8), so the
REST dispatch routes admit a sibling with no edge. Whether that is design or a
gap is abilityai/trinity-enterprise#629's ruling; by decision at the #2349 plan
gate this public file carries no reproducer for it.

**Credential-free vs keyed.** Attribution (IA-01, AC-01), the denial, the
stopped callee (IA-03), the fan-out bound (IA-02), the loop budget and the
delete cascade (L-03, P-01) all hold on a keyless stack, because the platform
writes the execution row and the collaboration activity BEFORE it calls the
container. Only "what B actually said" needs a model, and skips with the
allowlisted reason where no key exists — journey-smoke and the nightly are
deliberately credential-free (`integration-nightly.yml` states why).

**Finding carried as a `strict=True` xfail, with its own issue:**
- #2806 — no chain-depth guard on agent-to-agent chat chains

**Closed here:** #2807 — a refused call was audited as a successful tool call.
The deny sites now stamp the call context and the MCP audit wrapper records the
refusal (`src/mcp-server/src/access.ts::accessDenied`, read by
`audit.ts::withAudit`); the row reads `success: false`, `denied: true`, and is
asserted below. Also closed: abilityai/trinity-enterprise#628 — `run_agent_loop` skipped the
permission gate. Since that fix the MCP server gates it at registration
(`src/mcp-server/src/access.ts`, `TOOL_ACCESS_POLICY`), and the loop-id tools
resolve the loop's agent before answering; both are asserted below. The gate is
a tool-surface gate — the REST route behind it is still owner-equivalent, which is
the ent#629 question above.

Invariants cited, never restated: P-01, P-02, AC-01, L-03, IA-01, IA-02, IA-03.
"""
import json
import os
import time
import uuid

import pytest

from .conftest import (
    RESULT_DEADLINE_S,
    add_edge,
    agent_activities,
    agent_executions,
    agent_status,
    clear_edges,
    ensure_running,
    permitted_agents,
    poll_until,
    skip_unless_agent_can_answer,
    stop_agent_and_wait,
)

pytestmark = pytest.mark.journey

# #2812: the model gate is decided per CALLEE by the instance, in
# `conftest.skip_unless_agent_can_answer`, and is called at the top of each
# keyed test — a collection-time `skipif` cannot ask a live stack about an
# agent that does not exist yet.

# IA-03's signal is 5 s server-side (measured 0.16 s on a live instance). This is
# end to end through the MCP hop on whatever runner we are on; the elapsed time
# goes into the failure message so a slow runner reads as a slow runner.
FAIL_FAST_DEADLINE_S = float(os.getenv("JOURNEY_FAIL_FAST_DEADLINE_S", "10"))
ROW_DEADLINE_S = 60.0
AUDIT_DEADLINE_S = 30.0
LOOP_DEADLINE_S = float(os.getenv("JOURNEY_LOOP_DEADLINE_S", "200"))
FAN_OUT_TASKS = 12
FAN_OUT_CAP = 50
LOOP_RUNS = 3


def _unique(text: str) -> str:
    """The MCP idempotency key is deterministic over (caller, target, route,
    model, message): an identical message across tests would replay a stored
    snapshot or 409 in flight (#525), so every dispatch carries its own token."""
    return f"{text} [j10 {uuid.uuid4().hex[:8]}]"


def _ids(rows):
    return {r.get("id") for r in rows}


def _details(activity) -> dict:
    d = activity.get("details")
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except ValueError:
            d = {}
    return d or {}


def _visible_agents(mcp) -> set:
    res = mcp.call("list_agents")
    assert res.ok, f"list_agents as the caller failed: {res.error}"
    rows = res.data if isinstance(res.data, list) else (res.data or {}).get("agents", [])
    return {r.get("name") for r in rows if isinstance(r, dict)}


def _new_rows(client, name, before):
    return [r for r in agent_executions(client, name) if r.get("id") not in before]


def _terminal(status) -> bool:
    return status in ("success", "failed", "cancelled", "skipped")


def _read_turn(mcp, callee, res):
    """Answer-or-receipt → (status, response).

    A synchronous `chat_with_agent` becomes a `queued_timeout` receipt past the
    MCP server's own chat timeout (#914), and the pull-routed sequential path
    returns a receipt immediately (#946). The promise is asserted on the
    execution the platform ran, never on the inline body alone.
    """
    if not res.ok:
        return "error", res.error
    body = res.data if isinstance(res.data, dict) else {}
    exec_id = body.get("execution_id")
    if body.get("status") in ("queued_timeout", "accepted", "queued", "running") and exec_id:
        def done():
            r = mcp.call("get_execution_result", agent_name=callee, execution_id=exec_id)
            b = r.data if isinstance(r.data, dict) else {}
            return b if _terminal(b.get("status")) else None

        body = poll_until(
            done, deadline_s=RESULT_DEADLINE_S,
            describe=(f"execution {exec_id} on '{callee}' never reached a terminal "
                      f"state after the caller was handed a receipt"),
        )
    return body.get("status", "success"), body.get("response")


def _audit_rows_for_call(client, caller, target):
    """MCP tool-call audit rows about `caller` acting on `target`. The MCP server
    posts every tool call through `withAudit`; `details.tool` names the tool."""
    resp = client.get(f"/api/audit-log?source=mcp&target_id={target}&limit=100")
    assert resp.status_code == 200, (
        f"reading the audit log answered {resp.status_code}: {resp.text[:300]}"
    )
    rows = resp.json().get("entries", [])
    return [
        e for e in rows
        if (e.get("details") or {}).get("tool") == "chat_with_agent"
        and e.get("actor_id") == caller
    ]


# ---------------------------------------------------------------------------
# With permission: the call goes through, and the platform says who called whom
# ---------------------------------------------------------------------------

def test_with_permission_a_call_reaches_the_other_agent_and_is_recorded(
    pair, mcp_as_caller, journey_client,
):
    """A is allowed to talk to B, so B shows up in A's world, A's call lands on B
    as B's execution attributed to A (IA-01), and A's own activity stream shows
    the collaboration (AC-01). Holds without a model: all of it is written before
    B's container is called."""
    a, b = pair
    add_edge(journey_client, a, b)
    visible = _visible_agents(mcp_as_caller)
    assert b in visible, (
        f"'{a}' was granted permission to call '{b}' but list_agents as '{a}' "
        f"shows {sorted(visible)} — the edge exists in the DB and the MCP layer "
        f"does not see it"
    )

    rows_before = _ids(agent_executions(journey_client, b))
    collab_before = _ids(agent_activities(journey_client, a, "agent_collaboration"))

    res = mcp_as_caller.call(
        "chat_with_agent", agent_name=b, message=_unique("Reply with the single word: pong"),
    )
    denied = res.ok and isinstance(res.data, dict) and res.data.get("error") == "Access denied"
    assert not denied, (
        f"'{a}' has an edge to '{b}' and was still refused: {res.data.get('reason')!r}"
    )

    row = poll_until(
        lambda: _new_rows(journey_client, b, rows_before) or None,
        deadline_s=ROW_DEADLINE_S,
        describe=f"'{a}' called '{b}' with permission but no execution appeared on '{b}'",
    )[0]
    assert row.get("source_agent_name") == a and row.get("triggered_by") == "agent", (
        f"'{b}' ran the call but the record does not say '{a}' made it: "
        f"source_agent_name={row.get('source_agent_name')!r} "
        f"triggered_by={row.get('triggered_by')!r} — nobody could tell who called whom"
    )

    poll_until(
        lambda: [
            x for x in agent_activities(journey_client, a, "agent_collaboration")
            if x.get("id") not in collab_before and _details(x).get("target_agent") == b
        ] or None,
        deadline_s=ROW_DEADLINE_S,
        describe=(f"'{a}' called '{b}' but '{a}'s activity stream never showed the "
                  f"collaboration — the caller's side of 'who called whom' is missing"),
    )


def test_i_can_read_what_they_said(pair, mcp_as_caller, journey_client):
    """B's real answer comes back to A, and the operator can read it afterwards
    on B's execution record — the words, not just a 200."""
    a, b = pair
    # B is the one that has to produce words, so B is what the gate asks about.
    skip_unless_agent_can_answer(journey_client, b)
    add_edge(journey_client, a, b)
    rows_before = _ids(agent_executions(journey_client, b))

    res = mcp_as_caller.call(
        "chat_with_agent", agent_name=b,
        message=_unique("Reply with exactly the single word: pong"),
    )
    status, answer = _read_turn(mcp_as_caller, b, res)
    assert status == "success" and (answer or "").strip(), (
        f"'{a}' asked '{b}' a question and got status={status!r}, answer="
        f"{(answer or '')[:200]!r} — a blank or failed turn is indistinguishable "
        f"from a working agent in every status field, which is why this asserts "
        f"the words"
    )

    row = poll_until(
        lambda: [r for r in _new_rows(journey_client, b, rows_before)
                 if _terminal(r.get("status"))] or None,
        deadline_s=ROW_DEADLINE_S,
        describe=f"'{b}' answered '{a}' but no finished execution appeared on '{b}'",
    )[0]
    detail = journey_client.get(f"/api/agents/{b}/executions/{row['id']}")
    assert detail.status_code == 200, (
        f"reading execution {row['id']} of '{b}' answered {detail.status_code}: "
        f"{detail.text[:300]}"
    )
    recorded = (detail.json().get("response") or "").strip()
    assert recorded, (
        f"'{b}' answered '{a}' but the execution record holds no response text — "
        f"the operator cannot see what they said"
    )


# ---------------------------------------------------------------------------
# Without permission: refused, explained, and nothing dispatched
# ---------------------------------------------------------------------------

def test_without_permission_a_call_is_refused_and_the_caller_is_told_why(
    pair, mcp_as_caller, journey_client,
):
    """No edge from A to B: B is not in A's world, A's call is refused with a
    reason that names both agents (P-02), and nothing ran on B. Granting the
    edge makes B visible without any dispatch — the positive half of P-02."""
    a, b = pair
    visible = _visible_agents(mcp_as_caller)
    assert b not in visible and a in visible, (
        f"with no edge, list_agents as '{a}' shows {sorted(visible)} — it should "
        f"show '{a}' and not '{b}'"
    )

    rows_before = _ids(agent_executions(journey_client, b))
    acts_before = _ids(agent_activities(journey_client, b))

    res = mcp_as_caller.call(
        "chat_with_agent", agent_name=b, message=_unique("You should never receive this"),
    )
    body = res.data if isinstance(res.data, dict) else {}
    assert res.ok and body.get("error") == "Access denied", (
        f"'{a}' has no permission to call '{b}' and the call was not refused: "
        f"ok={res.ok} error={res.error!r} body={res.text[:300]!r}"
    )
    reason = body.get("reason") or ""
    assert a in reason and b in reason, (
        f"the refusal does not tell '{a}' what was refused: {reason!r}"
    )

    # Nothing reached B — no execution, no activity. The refusal happens in the
    # MCP server before any dispatch, so once the tool has answered there is
    # nothing in flight that could still write: check now, no waiting.
    assert not _new_rows(journey_client, b, rows_before), (
        f"'{b}' ran something for '{a}' even though the call was refused"
    )
    assert not [x for x in agent_activities(journey_client, b) if x.get("id") not in acts_before], (
        f"'{b}' recorded activity for a call that was refused"
    )

    add_edge(journey_client, a, b)
    assert b in _visible_agents(mcp_as_caller), (
        f"after granting '{a}' → '{b}', list_agents as '{a}' still hides '{b}'"
    )


def _refused_call_audit_row(pair, mcp_as_caller, journey_client):
    a, b = pair
    seen = {e.get("event_id") for e in _audit_rows_for_call(journey_client, a, b)}
    res = mcp_as_caller.call(
        "chat_with_agent", agent_name=b, message=_unique("You should never receive this"),
    )
    assert res.ok and isinstance(res.data, dict) and res.data.get("error") == "Access denied", (
        f"expected a refusal, got ok={res.ok} body={res.text[:200]!r}"
    )

    def new_rows():
        # The MCP server posts audit rows asynchronously, so the row is polled for.
        return [e for e in _audit_rows_for_call(journey_client, a, b)
                if e.get("event_id") not in seen] or None

    rows = poll_until(
        new_rows, deadline_s=AUDIT_DEADLINE_S,
        describe=(f"'{a}'s refused call to '{b}' left no audit row — the MCP server "
                  f"audits every tool call, so the refusal should be there"),
    )
    return rows[0]


def test_a_refused_call_still_leaves_an_audit_trail(pair, mcp_as_caller, journey_client):
    """The operator's side of 'the denial is observable', as it stands today:
    the refused tool call IS in the audit log."""
    row = _refused_call_audit_row(pair, mcp_as_caller, journey_client)
    assert (row.get("details") or {}).get("tool") == "chat_with_agent"


def test_the_operator_can_see_that_a_call_was_refused(pair, mcp_as_caller, journey_client):
    """The audit row for a refused call says it was refused: `success: false`
    and `denied: true`. Before #2807 it read `success: true` with no marker —
    the MCP audit wrapper labelled by throw/no-throw and every gate on the
    surface RETURNS its denial; the deny site now stamps the call context."""
    row = _refused_call_audit_row(pair, mcp_as_caller, journey_client)
    details = row.get("details") or {}
    assert details.get("denied") is True, (
        f"the audit row for a refused call carries no refusal marker: {details!r}"
    )
    assert details.get("success") is False, (
        f"the audit row for a refused call still reads as a success: {details!r}"
    )


# ---------------------------------------------------------------------------
# A stopped callee fails fast, honestly, and leaves nothing behind (IA-03)
# ---------------------------------------------------------------------------

def test_a_stopped_callee_fails_fast_with_an_honest_reason(pair, mcp_as_caller, journey_client):
    """B is stopped. A's call comes back within seconds saying exactly that —
    not after the execution timeout, and without an execution row on B."""
    a, b = pair
    add_edge(journey_client, a, b)
    stop_agent_and_wait(journey_client, b)
    try:
        rows_before = _ids(agent_executions(journey_client, b))
        started = time.monotonic()
        res = mcp_as_caller.call(
            "chat_with_agent", agent_name=b, message=_unique("Are you there?"),
        )
        elapsed = time.monotonic() - started
        text = f"{res.error or ''} {res.text}"
        assert not res.ok and "503" in text and "Agent is not running" in text, (
            f"'{a}' called stopped agent '{b}' and got ok={res.ok} after {elapsed:.1f}s: "
            f"{text[:300]!r} — the honest answer is a 503 'Agent is not running'"
        )
        assert elapsed < FAIL_FAST_DEADLINE_S, (
            f"'{a}' called stopped agent '{b}' and the refusal took {elapsed:.1f}s "
            f"(limit {FAIL_FAST_DEADLINE_S:.0f}s) — a stopped callee must fail fast, "
            f"not drift toward the execution timeout"
        )
        assert not _new_rows(journey_client, b, rows_before), (
            f"a call to stopped agent '{b}' left an execution row behind"
        )
    finally:
        ensure_running(journey_client, b)


# ---------------------------------------------------------------------------
# A fan-out is bounded and lands as one batch on one agent (IA-02)
# ---------------------------------------------------------------------------

def test_a_fan_out_is_bounded_and_lands_as_one_batch(pair, mcp_as_caller, journey_client):
    """More than 50 tasks is refused — by the tool A calls, and by the backend
    behind it. A batch of 12 comes back as one fan-out whose 12 executions all
    ran on B (IA-02). Whether they *complete* needs a model: the next test."""
    a, b = pair
    add_edge(journey_client, a, b)

    too_many = [{"id": f"t{i}", "message": _unique(f"task {i}")} for i in range(FAN_OUT_CAP + 1)]
    res = mcp_as_caller.call("fan_out", agent_name=b, tasks=too_many)
    assert not res.ok or (isinstance(res.data, dict) and res.data.get("error")), (
        f"a fan-out of {FAN_OUT_CAP + 1} tasks was accepted by the tool: {res.text[:300]!r}"
    )
    assert str(FAN_OUT_CAP) in f"{res.error or ''} {res.text}", (
        f"the refusal of a {FAN_OUT_CAP + 1}-task fan-out does not name the cap: "
        f"{(res.error or res.text)[:300]!r}"
    )
    backend = journey_client.post(f"/api/agents/{b}/fan-out", json={"tasks": too_many, "agent": "self"})
    assert backend.status_code == 422 and str(FAN_OUT_CAP) in backend.text, (
        f"the backend accepted a {FAN_OUT_CAP + 1}-task fan-out: "
        f"{backend.status_code} {backend.text[:300]!r}"
    )

    rows_a_before = _ids(agent_executions(journey_client, a))
    tasks = [{"id": f"t{i}", "message": _unique(f"Reply with the number {i}")} for i in range(FAN_OUT_TASKS)]
    res = mcp_as_caller.call("fan_out", agent_name=b, tasks=tasks, max_concurrency=3)
    assert res.ok and isinstance(res.data, dict), (
        f"'{a}' fanned {FAN_OUT_TASKS} tasks out to '{b}' and the tool failed: {res.error!r}"
    )
    fan_out_id = res.data.get("fan_out_id")
    assert fan_out_id, f"the fan-out answered without a fan_out_id: {res.text[:300]!r}"

    def settled():
        r = journey_client.get(f"/api/agents/{b}/fan-out/{fan_out_id}")
        if r.status_code != 200:
            return None
        batch = r.json()
        return batch if batch.get("status") != "running" else None

    batch = poll_until(
        settled, deadline_s=RESULT_DEADLINE_S,
        describe=f"fan-out {fan_out_id} on '{b}' never settled",
    )
    results = batch.get("results") or []
    assert batch.get("total") == FAN_OUT_TASKS and len(results) == FAN_OUT_TASKS, (
        f"fan-out {fan_out_id} should hold {FAN_OUT_TASKS} tasks, the batch reads "
        f"total={batch.get('total')} results={len(results)}"
    )
    on_b = _ids(agent_executions(journey_client, b))
    exec_ids = {r.get("execution_id") for r in results if r.get("execution_id")}
    assert exec_ids and exec_ids <= on_b, (
        f"fan-out {fan_out_id} was sent to '{b}' but {len(exec_ids - on_b)} of its "
        f"executions are not '{b}'s"
    )
    assert not _new_rows(journey_client, a, rows_a_before), (
        f"fan-out {fan_out_id} aimed at '{b}' also created executions on the caller '{a}'"
    )


def test_every_fan_out_subtask_completes(pair, mcp_as_caller, journey_client):
    """With a real model behind B, all 12 subtasks of A's fan-out come back
    completed. Its own test rather than a conditional inside the batch test, so
    that on a keyless stack it shows up as the allowlisted SKIP the skip audit
    can see — a silently-skipped assertion is the shape that audit exists to catch."""
    a, b = pair
    skip_unless_agent_can_answer(journey_client, b)
    add_edge(journey_client, a, b)
    tasks = [{"id": f"t{i}", "message": _unique(f"Reply with the number {i}")} for i in range(FAN_OUT_TASKS)]
    res = mcp_as_caller.call("fan_out", agent_name=b, tasks=tasks, max_concurrency=3)
    assert res.ok and isinstance(res.data, dict) and res.data.get("fan_out_id"), (
        f"'{a}' fanned {FAN_OUT_TASKS} tasks out to '{b}' and the tool failed: {res.error or res.text[:300]!r}"
    )
    fan_out_id = res.data["fan_out_id"]
    batch = poll_until(
        lambda: (lambda r: r.json() if r.status_code == 200 and r.json().get("status") != "running" else None)(
            journey_client.get(f"/api/agents/{b}/fan-out/{fan_out_id}")
        ),
        deadline_s=RESULT_DEADLINE_S,
        describe=f"fan-out {fan_out_id} on '{b}' never settled",
    )
    assert batch.get("status") == "completed" and batch.get("completed") == FAN_OUT_TASKS, (
        f"with a real model, fan-out {fan_out_id} ended {batch.get('status')!r} with "
        f"{batch.get('completed')}/{FAN_OUT_TASKS} completed and {batch.get('failed')} failed"
    )


# ---------------------------------------------------------------------------
# Loops: the permission boundary, and the budget
# ---------------------------------------------------------------------------

def test_without_permission_an_agent_cannot_start_a_loop_on_another(
    pair, mcp_as_caller, journey_client,
):
    """No edge from A to B: A cannot start a loop on B, and is told why — the same
    boundary `chat_with_agent` enforces."""
    a, b = pair
    res = mcp_as_caller.call(
        "run_agent_loop", agent_name=b, message=_unique("Reply with the single word: pong"),
        max_runs=1, timeout_per_run=30, max_duration_seconds=60,
    )
    body = res.data if isinstance(res.data, dict) else {}
    loop_id = body.get("loop_id")
    try:
        assert body.get("success") is not True and not loop_id, (
            f"'{a}' has no permission to call '{b}' and still started loop "
            f"{loop_id} on it: {res.text[:200]!r}"
        )
        assert "denied" in f"{res.error or ''} {res.text}".lower(), (
            f"'{a}' was not told the loop on '{b}' was refused: {res.text[:200]!r}"
        )
    finally:
        if loop_id:
            journey_client.post(f"/api/loops/{loop_id}/stop")


def test_without_permission_an_agent_cannot_read_or_stop_a_loop_on_another(
    pair, mcp_as_caller, journey_client,
):
    """A loop is addressed by id, so the agent it belongs to is known only after the
    resolve. With an edge A starts a loop on B; the edge is removed; A can neither
    read nor stop it, and the refusal does not say whose loop it is — the id was
    A's only input. The owner still can: that is the escape hatch the refusal names."""
    a, b = pair
    add_edge(journey_client, a, b)
    res = mcp_as_caller.call(
        "run_agent_loop", agent_name=b, message=_unique("Reply with the single word: pong"),
        max_runs=1, timeout_per_run=30, max_duration_seconds=60, on_failure="continue",
    )
    body = res.data if isinstance(res.data, dict) else {}
    loop_id = body.get("loop_id")
    assert res.ok and body.get("success") is True and loop_id, (
        f"'{a}' could not start a loop on '{b}' with permission: {res.error or res.text[:300]!r}"
    )
    try:
        clear_edges(journey_client, a)
        for tool in ("get_loop_status", "stop_loop"):
            r = mcp_as_caller.call(tool, loop_id=loop_id)
            rb = r.data if isinstance(r.data, dict) else {}
            assert rb.get("success") is not True, (
                f"'{a}' lost its edge to '{b}' and {tool} still answered: {r.text[:200]!r}"
            )
            assert "not found or not accessible" in r.text.lower(), (
                f"{tool} did not refuse with the compound reason: {r.text[:200]!r}"
            )
            assert b not in r.text, (
                f"{tool}'s refusal names the loop's agent '{b}' — the caller supplied only an id: "
                f"{r.text[:200]!r}"
            )
        stop = journey_client.post(f"/api/loops/{loop_id}/stop")
        assert stop.status_code == 200, (
            f"the owner could not stop loop {loop_id}: {stop.status_code} {stop.text[:200]}"
        )
    finally:
        journey_client.post(f"/api/loops/{loop_id}/stop")


def test_a_loop_stops_by_itself_within_its_budget(pair, mcp_as_caller, journey_client):
    """A starts a loop on B with a budget of `LOOP_RUNS` runs. It ends on its own
    with exactly that many, naming the budget as the reason. `on_failure="continue"` makes
    this hold on a keyless stack too: every run fails there, and the budget —
    not the abort policy — is what ends the loop."""
    a, b = pair
    add_edge(journey_client, a, b)
    res = mcp_as_caller.call(
        "run_agent_loop", agent_name=b,
        message=_unique("Reply with the single word: pong (run {{run}})"),
        max_runs=LOOP_RUNS, on_failure="continue", max_consecutive_failures=LOOP_RUNS + 1,
        timeout_per_run=45, max_duration_seconds=180, no_progress_threshold=0,
    )
    body = res.data if isinstance(res.data, dict) else {}
    loop_id = body.get("loop_id")
    assert res.ok and body.get("success") is True and loop_id, (
        f"'{a}' could not start a loop on '{b}' with permission: {res.error or res.text[:300]!r}"
    )
    try:
        def finished():
            r = journey_client.get(f"/api/loops/{loop_id}")
            if r.status_code != 200:
                return None
            loop = r.json()
            return loop if loop.get("status") not in ("pending", "queued", "running", "stopping") else None

        loop = poll_until(
            finished, deadline_s=LOOP_DEADLINE_S,
            describe=f"loop {loop_id} on '{b}' (max_runs={LOOP_RUNS}) never stopped on its own",
        )
    finally:
        journey_client.post(f"/api/loops/{loop_id}/stop")
    assert loop.get("runs_completed") == LOOP_RUNS and loop.get("stop_reason") == "max_runs_reached", (
        f"loop {loop_id} on '{b}' ended {loop.get('status')!r} after "
        f"{loop.get('runs_completed')} runs with stop_reason={loop.get('stop_reason')!r} "
        f"— the budget was {LOOP_RUNS} runs and should be the reason it stopped"
    )


# ---------------------------------------------------------------------------
# Deleting an agent leaves no dangling permission edge (L-03, P-01)
# ---------------------------------------------------------------------------

def test_deleting_an_agent_leaves_no_dangling_permission_edge(pair, journey_client, journey_agent):
    """A is granted access to a disposable agent C; deleting C removes the edge
    with it. Read from A's own permission list, which is the raw edge set — the
    fleet-wide edges view filters to agents the reader can still access and
    would hide exactly the dangling row this looks for."""
    a, _ = pair
    c = journey_agent["name"]
    add_edge(journey_client, a, c)
    assert c in permitted_agents(journey_client, a), (
        f"granting '{a}' → '{c}' did not show up in '{a}'s permission list"
    )
    resp = journey_client.delete(f"/api/agents/{c}")
    assert resp.status_code in (200, 202, 204), (
        f"deleting agent '{c}' answered {resp.status_code}: {resp.text[:300]}"
    )
    poll_until(
        lambda: agent_status(journey_client, c) is None,
        deadline_s=ROW_DEADLINE_S,
        describe=f"agent '{c}' was deleted but is still reported by the API",
    )
    left = permitted_agents(journey_client, a)
    assert c not in left, (
        f"'{c}' was deleted but '{a}' still holds a permission edge to it: {left}"
    )


# ---------------------------------------------------------------------------
# Runaway recursion — the promise the platform cannot keep yet (#2806)
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    strict=True,
    reason="#2806 — no chain-depth guard exists on agent-to-agent chat chains; "
           "the bound today is capacity parks and per-hop timeouts",
)
def test_two_agents_cannot_bounce_a_call_between_each_other_forever():
    """A and B may call each other. A call that A makes to B, that B passes
    back to A, that A passes back to B, ... is stopped by the platform with a
    named reason before it runs away — not by an execution timeout, a capacity
    park, or an operator noticing the bill.

    What this asserts when #2806 ships (the flip condition is written there):

    1. with the depth limit set low on a keyed stack, a real A→B→A→B chain is
       refused at the hop past the limit, with the named error, before any
       model work for that hop starts;
    2. the refusal is recorded on the platform, not only returned to the caller;
    3. no execution beyond the limit exists on either agent afterwards.

    It is a skeleton on purpose: the recursion needs B's model to decide to call
    A, so a live body would fail for the wrong reason on the keyless PR gate and
    the strict marker could never flip honestly. The issue carries the contract.
    """
    raise AssertionError("no inter-agent call-depth guard exists — see abilityai/trinity#2806")
