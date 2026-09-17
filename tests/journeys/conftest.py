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


def create_agent_and_wait(client, name: str) -> dict:
    """POST /api/agents and poll until the agent reports `running`.

    The body of `journey_agent`, lifted so J10's two-agent fixture (#2349) makes
    the same requests with the same failure wording — one create path, not two
    that drift.
    """
    resp = client.post(
        "/api/agents", json={"name": name}, timeout=AGENT_CREATE_CALL_TIMEOUT_S,
    )
    if resp.status_code not in (200, 201):
        raise AssertionError(
            f"creating agent '{name}' through POST /api/agents answered "
            f"{resp.status_code}: {resp.text[:400]}. Creating an agent is the "
            f"first thing anyone does with Trinity."
        )

    def is_running():
        check = client.get(f"/api/agents/{name}")
        if check.status_code != 200:
            return None
        state = check.json()
        return state if state.get("status") == "running" else None

    return poll_until(
        is_running,
        deadline_s=AGENT_RUNNING_DEADLINE_S,
        describe=f"agent '{name}' was created but never reached 'running'",
    )


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
        yield create_agent_and_wait(journey_client, name)
    finally:
        delete_agent_idempotent(journey_client, name)


# ---------------------------------------------------------------------------
# J10 — agent calls agent (#2349). Primitives shared by the two-agent journey.
#
# Everything an agent can do to another agent goes through the MCP server with
# the caller's OWN agent-scoped key: that is where `checkAgentAccess` (P-02)
# lives, so a harness that talked to the backend as admin would prove nothing
# about the boundary. Two consequences shape this section.
#
# * The key exists only inside the caller's container (`TRINITY_MCP_API_KEY`
#   in its env). No API returns it, by design — `POST /api/mcp/keys` refuses
#   `scope=agent`, and the regenerate route "deliberately carries no
#   plaintext". So the tier reads it through the Docker socket, the same host
#   the stack itself runs on. A run pointed at a remote `TRINITY_API_URL` with
#   no Docker socket FAILS here, naming the requirement — it does not skip.
# * The MCP server is stateful (`Mcp-Session-Id` from `initialize`), answers
#   in SSE or JSON, and renders a thrown backend error as a tool error. The
#   helper below normalises all of that into one result shape, and it never
#   reprs its headers: the key must not reach a failure message, a poll
#   observation, or an assert operand.
# ---------------------------------------------------------------------------
import concurrent.futures
import json
from typing import Any, Dict, List, Optional

import httpx

MCP_URL = os.getenv("TRINITY_MCP_URL", "http://localhost:8080/mcp")
# A synchronous chat_with_agent holds the HTTP call until the backend answers or
# the MCP server's own MCP_CHAT_TIMEOUT_MS (25 s) turns it into a receipt; a
# fan_out holds longer. Generous, and still inside the tier's 300 s per test.
MCP_CALL_TIMEOUT_S = float(os.getenv("JOURNEY_MCP_CALL_TIMEOUT_S", "180"))
# How long a dispatched turn may take to reach a terminal row when the harness
# is handed a receipt instead of an answer (#914 / #946).
RESULT_DEADLINE_S = float(os.getenv("JOURNEY_RESULT_DEADLINE_S", "240"))


def agent_status(client, name: str) -> Optional[str]:
    resp = client.get(f"/api/agents/{name}")
    return resp.json().get("status") if resp.status_code == 200 else None


def ensure_running(client, name: str) -> None:
    """Idempotent: a running agent is left alone, a stopped one is started and
    waited for. Used to restore the shared callee after a test stopped it."""
    if agent_status(client, name) == "running":
        return
    start = client.post(f"/api/agents/{name}/start")
    assert start.status_code in (200, 202), (
        f"starting agent '{name}' answered {start.status_code}: {start.text[:300]}"
    )
    poll_until(
        lambda: agent_status(client, name) == "running",
        deadline_s=AGENT_RUNNING_DEADLINE_S,
        describe=f"agent '{name}' was asked to start but never reached 'running'",
    )


def stop_agent_and_wait(client, name: str) -> None:
    stop = client.post(f"/api/agents/{name}/stop")
    assert stop.status_code in (200, 202), (
        f"stopping agent '{name}' answered {stop.status_code}: {stop.text[:300]}"
    )
    poll_until(
        lambda: agent_status(client, name) in ("stopped", "exited"),
        deadline_s=AGENT_STOPPED_DEADLINE_S,
        describe=f"agent '{name}' was asked to stop but never left 'running'",
    )


def agent_mcp_key(name: str) -> str:
    """The agent's own Trinity MCP key, read from its container env.

    Looks the container up by the platform's identity label
    (`trinity.agent-name`, Invariant #11), extracts ONLY `TRINITY_MCP_API_KEY`
    — the env blob also carries the provider key and any PAT — and never logs
    the value. Fails, never skips: the tier already needs the stack's Docker
    host (agents are containers), so a run without the socket is misconfigured
    rather than credential-poor.
    """
    try:
        import docker  # noqa: WPS433 — test-only dependency, declared in requirements-test.txt
        containers = docker.from_env().containers.list(
            all=True, filters={"label": f"trinity.agent-name={name}"},
        )
    except Exception as e:  # noqa: BLE001 — turn any SDK failure into the finding
        raise AssertionError(
            f"J10 needs the Docker socket of the host running the stack to read "
            f"agent '{name}'s MCP key from its container ({type(e).__name__}). "
            f"Run this tier where the stack runs; a remote TRINITY_API_URL alone "
            f"cannot drive the agent-to-agent journey."
        ) from e
    if not containers:
        raise AssertionError(
            f"no container carries label trinity.agent-name={name}; the agent was "
            f"reported running by the API but the Docker host has no container "
            f"for it."
        )
    env = containers[0].attrs.get("Config", {}).get("Env") or []
    for kv in env:
        if kv.startswith("TRINITY_MCP_API_KEY="):
            key = kv.split("=", 1)[1]
            if key:
                return key
    raise AssertionError(
        f"agent '{name}'s container has no TRINITY_MCP_API_KEY in its env — the "
        f"platform mints one at creation, so the agent could not call anyone."
    )


class McpToolResult:
    """One normalised tool outcome.

    `ok` is False for a JSON-RPC error object AND for a tool result flagged
    `isError` (how the MCP server surfaces a thrown backend error, e.g. an
    `API error (503): …`). `data` is the text parsed as JSON when it parses,
    else None; `text` is always the raw text.
    """

    def __init__(self, ok: bool, text: str, error: Optional[str] = None):
        self.ok = ok
        self.text = text or ""
        self.error = error
        try:
            self.data: Any = json.loads(self.text) if self.text else None
        except ValueError:
            self.data = None

    def __repr__(self) -> str:  # never carries headers or keys
        head = self.text[:200].replace("\n", " ")
        return f"<McpToolResult ok={self.ok} error={self.error!r} text={head!r}>"


class McpSession:
    """A minimal MCP client over the server's streamable-HTTP transport.

    initialize → notifications/initialized → tools/list probe → tools/call.
    ~40 lines of a public wire protocol rather than a new test dependency; the
    probe at open() is what turns an unreachable or misconfigured server into
    ONE failure naming the URL instead of a confusing one per test.
    """

    def __init__(self, api_key: str, url: str = MCP_URL):
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self.url = url
        self.session_id: Optional[str] = None
        self._next_id = 0
        self.tools: List[str] = []
        self._open()

    def __repr__(self) -> str:  # never the headers
        return f"<McpSession url={self.url} session={'yes' if self.session_id else 'no'}>"

    def _post(self, method: str, params: Optional[dict], *, with_id: bool):
        body: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if with_id:
            self._next_id += 1
            body["id"] = self._next_id
        headers = dict(self._headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        resp = httpx.post(self.url, headers=headers, json=body, timeout=MCP_CALL_TIMEOUT_S)
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        if not with_id:
            return resp.status_code, None
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            # A frame that is not JSON, or not an object, is skipped rather than
            # thrown: the caller turns "no message" into a named failure, which is
            # more useful than a raw ValueError from inside the transport.
            msgs = []
            for line in resp.text.splitlines():
                if not line.startswith("data: "):
                    continue
                try:
                    parsed = json.loads(line[6:])
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    msgs.append(parsed)
            mine = [m for m in msgs if m.get("id") == body["id"]]
            return resp.status_code, (mine[-1] if mine else (msgs[-1] if msgs else None))
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {"error": {"message": resp.text[:300]}}

    def _open(self) -> None:
        status, init = self._post("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "trinity-journey-j10", "version": "1"},
        }, with_id=True)
        if status != 200 or not isinstance(init, dict) or "result" not in init:
            raise AssertionError(
                f"MCP initialize against {self.url} answered HTTP {status}: "
                f"{str(init)[:300]} — the agent-to-agent journey needs the MCP "
                f"server the agents themselves use (TRINITY_MCP_URL)."
            )
        self._post("notifications/initialized", {}, with_id=False)
        status, listed = self._post("tools/list", {}, with_id=True)
        tools = (listed or {}).get("result", {}).get("tools") if isinstance(listed, dict) else None
        if status != 200 or not tools:
            raise AssertionError(
                f"MCP tools/list against {self.url} answered HTTP {status} with no "
                f"tools ({str(listed)[:300]}); the session authenticated but the "
                f"server advertises nothing to this key."
            )
        self.tools = [t.get("name") for t in tools]

    def call(self, tool: str, **arguments: Any) -> McpToolResult:
        status, msg = self._post("tools/call", {"name": tool, "arguments": arguments}, with_id=True)
        if not isinstance(msg, dict):
            return McpToolResult(False, "", f"HTTP {status} with no JSON-RPC message")
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {"message": str(msg["error"])}
            return McpToolResult(False, "", f"{err.get('code')}: {err.get('message')}")
        result = msg.get("result") or {}
        text = "".join(
            c.get("text", "") for c in (result.get("content") or []) if c.get("type") == "text"
        )
        if result.get("isError"):
            return McpToolResult(False, text, text[:300] or "tool error")
        return McpToolResult(True, text)


# ---- permission edges (P-01 / P-02 arrangement, as the owner) ---------------

def permitted_agents(client, source: str) -> List[str]:
    resp = client.get(f"/api/agents/{source}/permissions")
    assert resp.status_code == 200, (
        f"reading agent '{source}'s permissions answered {resp.status_code}: {resp.text[:300]}"
    )
    # Shape: {"source_agent": ..., "permitted_agents": [name, ...],
    # "available_agents": [{name, status, permitted}, ...]} — the raw edge set is
    # `permitted_agents`, a list of names (`agent_service/permissions.py`).
    rows = resp.json().get("permitted_agents") or []
    return [(r.get("name") if isinstance(r, dict) else r) for r in rows]


def add_edge(client, source: str, target: str) -> None:
    resp = client.post(f"/api/agents/{source}/permissions/{target}")
    assert resp.status_code in (200, 201), (
        f"granting '{source}' → '{target}' answered {resp.status_code}: {resp.text[:300]}"
    )


def clear_edges(client, source: str) -> None:
    """Remove every edge `source` holds, and PROVE it — the module-scoped pair's
    order-independence rests on this reset, so a DELETE that quietly 404s or
    500s would leak an edge into the next test and make the permission test go
    red blaming the product for a harness leak (review of #2349, I1)."""
    for target in permitted_agents(client, source):
        resp = client.delete(f"/api/agents/{source}/permissions/{target}")
        assert resp.status_code in (200, 204), (
            f"harness reset: revoking '{source}' → '{target}' answered "
            f"{resp.status_code}: {resp.text[:200]}"
        )
    left = permitted_agents(client, source)
    assert not left, (
        f"harness reset: '{source}' still holds edges {left} after clear_edges — "
        f"a leaked edge here would make a later permission test fail for the "
        f"wrong reason"
    )


# ---- what the platform recorded (the "I can see what they said" half) -------

def agent_executions(client, name: str) -> List[dict]:
    resp = client.get(f"/api/agents/{name}/executions")
    assert resp.status_code == 200, (
        f"listing executions of '{name}' answered {resp.status_code}: {resp.text[:300]}"
    )
    return resp.json()


def agent_activities(client, name: str, activity_type: Optional[str] = None) -> List[dict]:
    params = f"?activity_type={activity_type}" if activity_type else ""
    resp = client.get(f"/api/agents/{name}/activities{params}")
    assert resp.status_code == 200, (
        f"listing activities of '{name}' answered {resp.status_code}: {resp.text[:300]}"
    )
    body = resp.json()
    return body.get("activities", body) if isinstance(body, dict) else body


@pytest.fixture(scope="module")
def journey_agent_pair(journey_client):
    """Two agents, A (the caller) and B (the callee), provisioned once per
    module and torn down by name.

    Module scope because each provision is a container (13 s on the CI runner,
    more on a cold one) and the journey has eight tests; function scope would
    pay ~16 provisions. Order-independence comes from the `pair` fixture below,
    which resets edges and liveness before every test, not from test order.

    Provisioned concurrently: journey-smoke runs `--timeout=300
    --timeout-method=thread`, whose hard exit skips every `finally`, so the
    setup must stay far inside that budget even on a slow runner.
    """
    names = [f"{EPHEMERAL_PREFIX}{uuid.uuid4().hex[:8]}" for _ in range(2)]
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            agents = list(pool.map(lambda n: create_agent_and_wait(journey_client, n), names))
        yield agents[0]["name"], agents[1]["name"]
    finally:
        for n in names:
            delete_agent_idempotent(journey_client, n)


@pytest.fixture()
def pair(journey_client, journey_agent_pair):
    """The pair with a clean slate: no permission edges either way, both running."""
    a, b = journey_agent_pair
    clear_edges(journey_client, a)
    clear_edges(journey_client, b)
    ensure_running(journey_client, a)
    ensure_running(journey_client, b)
    return a, b


@pytest.fixture()
def mcp_as_caller(pair):
    """An MCP session authenticated as agent A — the caller's own key, so every
    call crosses the same `checkAgentAccess` gate a real playbook call does."""
    a, _ = pair
    return McpSession(agent_mcp_key(a))


# ---------------------------------------------------------------------------
# "Will a real model answer?" — decided by the INSTANCE, not the harness (#2812)
# ---------------------------------------------------------------------------
#
# The gate this replaces read `ANTHROPIC_API_KEY` from the **pytest process**.
# That is the harness host, not the instance under test. A stack whose agents
# authenticate by subscription (SUB-003: `CLAUDE_CODE_OAUTH_TOKEN` inside the
# container, rows in `subscription_credentials`) has no such variable on the
# host yet answers normally — so on that stack the keyed journeys skipped
# PERMANENTLY, and invisibly, because the reason is allowlisted in
# `tests/harness/audit_skips.py`. A gate that cannot fail is worse than no
# gate: #2336's per-PR journey-smoke and #2350's merge-enforced Journey Impact
# declaration were both green while asserting nothing about a real model.
#
# The gate is a COMPOSITE of two reads, because neither alone is sufficient —
# and the second was added after journey-smoke proved the first wrong:
#
#   1. The callee's OWN auth mode — `GET /api/subscriptions/agents/{name}/auth`
#      → `auth_mode` ∈ {"subscription", "api_key", "not_configured"}. This is
#      "what the callee will actually have" (#2812 AC 2) rather than what the
#      instance has somewhere, which is why it is keyed per agent and not on
#      `GET /api/subscriptions` being non-empty — a shape #2812 rejects by name,
#      because it would turn a permanent skip into a false FAILURE when a
#      freshly created ephemeral agent never gets a subscription assigned.
#
#   2. Whether the INSTANCE holds a usable Claude credential at all —
#      `claude_auth_configured` on `GET /api/settings/feature-flags`, i.e. a
#      non-empty platform Anthropic key OR any registered subscription
#      (`subscription_service.is_claude_auth_configured`).
#
# Read 1 alone is NOT enough, and journey-smoke is the proof: `get_agent_auth_mode`
# derives purely from DB state, and `use_platform_api_key` is a per-agent ROUTING
# FLAG, not evidence a key exists. On the credential-free CI stack (`.env.example`
# ships `ANTHROPIC_API_KEY=` empty) a fresh agent still reports `auth_mode="api_key"`
# — it is configured to USE the platform key, there just isn't one. Gating on that
# alone ran the keyed journeys against a keyless stack and failed them, which is
# the same defect as #2812 pointing the other way. Read 2 is what distinguishes
# "configured to use a credential" from "a credential exists".
#
# Both reads are instance-side. Neither reads the pytest host's environment, and
# neither discloses a credential value.
#
# Residual, stated rather than hidden: an instance holding a credential that is
# present but INVALID (a revoked key, or the literal `ANTHROPIC_API_KEY=placeholder`,
# which the backend does not special-case) passes both reads, so the journey runs
# and FAILS rather than skipping. The old host-side gate vetoed the placeholder
# sentinel by string comparison; an instance-side gate cannot see the value and
# should not. This tier's doctrine is that a stack which cannot deliver the promise
# is a finding, not a silent pass — so that direction is deliberate. Only a real
# turn could close it, which is #2812's other candidate signal (a session-scoped
# probe) and a heavier change than this gate warrants.

# Kept VERBATIM: `tests/harness/audit_skips.py` allowlists this exact substring
# ("journey needs a real provider key"). Rewording it makes every skip here
# unallowlisted and turns the skip audit red.
MODEL_SKIP_REASON = "journey needs a real provider key"

# One GET per agent per session, plus one for the instance read. The auth mode
# is set at create and changed only by the auto-switch service, so re-reading it
# per test buys nothing — and this tier is wall-clock budgeted.
_AUTH_MODE_CACHE: dict = {}


def agent_auth_mode(client, agent_name: str) -> str:
    """This agent's Claude auth mode, as the instance reports it.

    Returns `"unknown"` when the endpoint cannot be read, which the caller
    treats as "cannot answer" — the same direction as the old missing-key gate,
    so a harness that loses access degrades to a visible skip rather than a
    confusing assertion failure deep inside a chat turn.
    """
    if agent_name in _AUTH_MODE_CACHE:
        return _AUTH_MODE_CACHE[agent_name]
    mode = "unknown"
    try:
        resp = client.get(f"/api/subscriptions/agents/{agent_name}/auth")
        if resp.status_code == 200:
            mode = (resp.json() or {}).get("auth_mode") or "unknown"
    except Exception:  # noqa: BLE001 — a gate must never mask the test's own failure
        mode = "unknown"
    _AUTH_MODE_CACHE[agent_name] = mode
    return mode


def instance_has_claude_credential(client) -> bool:
    """Whether this INSTANCE holds a usable Claude credential.

    `claude_auth_configured` = a non-empty platform Anthropic key OR any
    registered subscription — the single definition behind the feature flag and
    ent#582's first-credential check, so "configured" cannot mean two things.
    Unreadable ⇒ False, i.e. skip, matching the rest of this gate's direction.
    """
    if "instance" in _AUTH_MODE_CACHE:
        return _AUTH_MODE_CACHE["instance"]
    ok = False
    try:
        resp = client.get("/api/settings/feature-flags")
        if resp.status_code == 200:
            ok = bool((resp.json() or {}).get("claude_auth_configured"))
    except Exception:  # noqa: BLE001 — a gate must never mask the test's own failure
        ok = False
    _AUTH_MODE_CACHE["instance"] = ok
    return ok


def skip_unless_agent_can_answer(client, agent_name: str) -> str:
    """Skip with the allowlisted reason unless `agent_name` can reach a model.

    The one helper both J03 and J10 use (#2812 AC 1), so there is a single
    definition of "a real model will answer on this stack". J10 passes the
    CALLEE — the agent that has to produce the words — not the caller.

    Requires BOTH halves of the composite described above: the agent must be
    routed to some credential, AND the instance must actually hold one.
    """
    mode = agent_auth_mode(client, agent_name)
    if mode not in ("subscription", "api_key"):
        pytest.skip(f"{MODEL_SKIP_REASON} — agent '{agent_name}' reports auth_mode={mode!r}")
    if not instance_has_claude_credential(client):
        pytest.skip(
            f"{MODEL_SKIP_REASON} — agent '{agent_name}' is routed to {mode!r} but the "
            f"instance reports claude_auth_configured=False (no platform key, no subscription)"
        )
    return mode
