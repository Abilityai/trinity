"""Pipeline-state stage advances are PUSHED, not polled (trinity-enterprise#533).

ent#525 shipped the Work card's steps behind a 12 s poll, so a stage advance
could lag a full poll behind the file the agent had already written. The agent
server now notices its own write and tells the backend; the backend publishes a
thin ``/ws`` trigger and the existing access-controlled read does the rest.

What is proven here, and why each is worth a test:

1. **The door is the heartbeat's door.** The notice is an agent talking about
   *itself*, so it carries the agent's OWN agent-scoped MCP key — never the
   internal secret (an agent is never given one), never an admin gate. A user
   key, a system key, another agent's key and no key at all are the SAME 403
   with the SAME detail: the route is not an oracle for which agents exist.
2. **The trigger is thin (#918) and agent-keyed (ent#467).** ``/ws`` is
   SCOPE_ALL; anything on the payload reaches every connected browser that
   passes the roster filter, so the event carries identifiers only — no
   ``health``, no ``blockers``, no file body — and ``changed_at`` is stamped by
   the backend, never taken from the agent. ``agent_name`` is top-level, which
   is what makes ent#467 scope it with no event-bus change.
3. **Ids are grammar-checked before anything is published.** The ids ride into
   a download path on the read side; a rejected id must 422 and must not
   produce a broadcast. ``stage`` is the opposite rule — normalised to null
   rather than rejected, because a stage id is free-form by schema and a weird
   one must not cost the notice.
4. **A burst is coalesced, and a refused notice is a 200.** A pathological
   writer is bounded server-side; the refusal is not a 429 because a burst is
   the agent's normal and the 12 s poll already covers it.
5. **The cache generation is what makes this work on more than one worker.**
   ``_cache`` is a per-process dict and prod runs ``uvicorn --workers 2``, so an
   in-memory invalidation refreshes the worker that got the notice and leaves
   the other serving a stale card for up to 10 s — passing green on dev's
   single worker. With Redis gone, both sides read ``None`` and the cache is
   exactly today's TTL-only cache (fail-open).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault("TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent533.db"))
os.environ.setdefault("LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent533-logs"))

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

AGENT = "scout"
OTHER = "sage"
ROUTE = f"/api/agents/{AGENT}/pipeline-state/changed"
BODY = {"pipeline_id": "digest", "instance_id": "2026-09-10T09", "stage": "synthesis"}
DENIED = "Pipeline-state notices require the agent's own MCP key"


def _run(coro):
    return asyncio.run(coro)


class _FakeManager:
    """The #918 fake: `/ws` receives a JSON string, `/ws/events` a dict."""

    def __init__(self):
        self.messages = []

    async def broadcast(self, message):
        self.messages.append(message)

    async def broadcast_filtered(self, event):
        self.messages.append(event)


@pytest.fixture
def ps(monkeypatch):
    from client_portal.work import pipeline_state as mod
    mod.clear_cache()
    # Default: no Redis. The real `_get_redis` would reach routers.auth and pay
    # a connect timeout per call; the generation test installs fakeredis itself.
    monkeypatch.setattr(mod, "_get_redis", lambda: None)
    mod.set_websocket_manager(None)
    mod.set_filtered_websocket_manager(None)
    yield mod
    mod.clear_cache()
    mod.set_websocket_manager(None)
    mod.set_filtered_websocket_manager(None)


@pytest.fixture(autouse=True)
def _limiter(monkeypatch):
    """Force the limiter's in-process fallback: deterministic and no 1 s
    connect timeout per call."""
    from services import rate_limiter
    monkeypatch.setattr(rate_limiter, "_get_redis", lambda: None)
    rate_limiter.clear_inprocess()
    yield
    rate_limiter.clear_inprocess()


@pytest.fixture
def managers(ps):
    main, filtered = _FakeManager(), _FakeManager()
    ps.set_websocket_manager(main)
    ps.set_filtered_websocket_manager(filtered)
    return main, filtered


def _key(scope, agent_name=None):
    return {"scope": scope, "agent_name": agent_name, "user_id": 1}


@pytest.fixture
def client(ps, monkeypatch):
    """The route under TestClient, with `validate_mcp_api_key` in our hands.

    The handler resolves `database` at CALL time (the lazy-import discipline),
    so replacing the module attribute is enough — no real key rows needed.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import database
    from routers.agent_pipeline_state import router

    keys = {
        "own": _key("agent", AGENT),
        "other-agent": _key("agent", OTHER),
        "user": _key("user"),
        "system": _key("system"),
    }
    validations = []

    def _validate(token, track_usage=True):
        validations.append({"token": token, "track_usage": track_usage})
        return keys.get(token)

    monkeypatch.setattr(database, "db", SimpleNamespace(validate_mcp_api_key=_validate))
    app = FastAPI()
    app.include_router(router)
    test_client = TestClient(app)
    test_client.validations = validations
    return test_client


# ---------------------------------------------------------------------------
# 1. The router declares its surface and is actually mounted
# ---------------------------------------------------------------------------
def test_router_declares_its_mcp_surface_and_is_mounted():
    src = (_BACKEND / "routers" / "agent_pipeline_state.py").read_text()
    first = src.splitlines()[0]
    assert first.startswith("# mcp: none"), (
        "Invariant #13: a new router declares its MCP surface on line 1 — "
        f"got {first!r}"
    )

    main = (_BACKEND / "main.py").read_text()
    assert "agent_pipeline_state" in main, "main.py never imports the router"
    i_agents = main.index("app.include_router(agents_router)")
    i_new = main.index("app.include_router(agent_pipeline_state_router)")
    assert i_new > i_agents, (
        "Invariant #4: register after agents_router so nothing in the core "
        "agent router can shadow the notice path"
    )


# ---------------------------------------------------------------------------
# 2. The door
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Basic zzz"},
    {"Authorization": "Bearer nope"},
])
def test_a_missing_or_unknown_key_is_403(client, managers, headers):
    r = client.post(ROUTE, json=BODY, headers=headers)
    assert r.status_code == 403
    assert r.json()["detail"] == DENIED
    assert not managers[0].messages and not managers[1].messages


@pytest.mark.parametrize("token", ["user", "system", "other-agent"])
def test_a_key_that_is_not_this_agents_own_is_the_same_403(client, managers, token):
    r = client.post(ROUTE, json=BODY, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    # Uniform detail: a wrong-agent key must not read differently from a user
    # key, or the route becomes an oracle for which agents exist.
    assert r.json()["detail"] == DENIED
    assert not managers[0].messages and not managers[1].messages


# ---------------------------------------------------------------------------
# 3. The trigger
# ---------------------------------------------------------------------------
def test_own_key_publishes_a_thin_trigger(client, managers):
    main_mgr, filtered_mgr = managers
    r = client.post(ROUTE, json=BODY, headers={"Authorization": "Bearer own"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "published": True}

    main_event = json.loads(main_mgr.messages[0])
    filtered_event = filtered_mgr.messages[0]
    for event in (main_event, filtered_event):
        assert set(event) == {
            "type", "event", "agent_name", "pipeline_id", "instance_id",
            "stage", "changed_at",
        }, "the trigger grew a field — /ws is SCOPE_ALL (#918)"
        assert event["type"] == event["event"] == "pipeline_state_changed"
        assert event["agent_name"] == AGENT
        assert event["pipeline_id"] == "digest"
        assert event["instance_id"] == "2026-09-10T09"
        assert event["stage"] == "synthesis"
        # Server-stamped, never the agent's clock.
        assert event["changed_at"].endswith("Z")

    from services.event_bus import agent_names_in_payload
    assert agent_names_in_payload(main_event) == frozenset({AGENT}), (
        "ent#467 scopes by the names ON the payload — an event that names no "
        "agent is delivered fleet-wide"
    )


def test_the_key_is_validated_without_amplifying_its_usage_counter(client, managers):
    """The heartbeat's rule, for the same reason: a notice is not a use. With
    `track_usage=True` a chatty pipeline would inflate the key's usage_count
    and write to SQLite on every tick."""
    client.post(ROUTE, json=BODY, headers={"Authorization": "Bearer own"})
    assert client.validations == [{"token": "own", "track_usage": False}]


def test_a_notice_never_carries_the_file_body(client, managers):
    """The state file holds health, blockers, escalations and per-stage
    metrics. None of it may ride a fleet-wide channel."""
    body = dict(BODY, health="red", escalations=["paged eugene"], blockers=["quota"])
    r = client.post(ROUTE, json=body, headers={"Authorization": "Bearer own"})
    assert r.status_code == 200
    wire = json.dumps(managers[0].messages) + json.dumps(managers[1].messages)
    for leaked in ("red", "paged eugene", "quota", "health", "escalations", "blockers"):
        assert leaked not in wire


# ---------------------------------------------------------------------------
# 4. Ids are checked before anything is published
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    "..", "../../etc/passwd", "a/b", "%2e%2e", "", " ", "x" * 129, "a\x00b",
])
def test_traversal_ids_are_422_and_never_broadcast(client, managers, bad):
    for field in ("pipeline_id", "instance_id"):
        body = dict(BODY)
        body[field] = bad
        r = client.post(ROUTE, json=body, headers={"Authorization": "Bearer own"})
        assert r.status_code == 422, f"{field}={bad!r} was accepted"
    assert not managers[0].messages and not managers[1].messages


@pytest.mark.parametrize("stage, expected", [
    ("synthesis", "synthesis"),
    ("  synthesis  ", "synthesis"),
    ("", None),
    ("   ", None),
    (None, None),
    ("s" * 81, None),
    ("bad\nstage", None),
    ("bad\x07stage", None),
])
def test_stage_is_bounded_and_normalised_never_rejected(client, managers, stage, expected):
    body = dict(BODY, stage=stage)
    r = client.post(ROUTE, json=body, headers={"Authorization": "Bearer own"})
    assert r.status_code == 200, "a weird stage id must not cost the notice"
    assert json.loads(managers[0].messages[-1])["stage"] == expected


# ---------------------------------------------------------------------------
# 5. Coalescing
# ---------------------------------------------------------------------------
def test_notices_are_coalesced_per_agent(client, ps, managers):
    limit = ps.NOTIFY_LIMIT
    for _ in range(limit):
        r = client.post(ROUTE, json=BODY, headers={"Authorization": "Bearer own"})
        assert r.json()["published"] is True
    r = client.post(ROUTE, json=BODY, headers={"Authorization": "Bearer own"})
    # 200, not 429: a burst is the agent's normal and the poll already covers
    # it. The agent ignores the body either way; a 429 would only invite retry.
    assert r.status_code == 200
    assert r.json() == {"ok": True, "published": False}
    assert len(managers[0].messages) == limit, "a refused notice must not broadcast"

    # The budget is PER AGENT — one noisy agent cannot mute another.
    from client_portal.work import pipeline_state as mod
    assert _run(mod.notify_changed(OTHER, SimpleNamespace(
        pipeline_id="p", instance_id="i", stage=None))) is True


def test_a_refused_notice_does_not_bump_the_generation(ps, monkeypatch, managers):
    seen = []
    monkeypatch.setattr(ps, "mark_changed", lambda name: seen.append(name))
    payload = SimpleNamespace(pipeline_id="p", instance_id="i", stage=None)
    for _ in range(ps.NOTIFY_LIMIT):
        assert _run(ps.notify_changed(AGENT, payload)) is True
    assert _run(ps.notify_changed(AGENT, payload)) is False
    assert len(seen) == ps.NOTIFY_LIMIT


# ---------------------------------------------------------------------------
# 6. The generation — the part that makes this work on two workers
# ---------------------------------------------------------------------------
def _fake_steps(state="reported"):
    from client_portal.work.models import WorkSteps
    return WorkSteps(state=state)


def test_a_notice_bumps_the_generation_so_every_worker_rereads(ps, monkeypatch):
    import fakeredis
    shared = fakeredis.FakeStrictRedis()
    monkeypatch.setattr(ps, "_get_redis", lambda: shared)

    reads = []

    async def fake_read(agent_name, started_at, roster):
        reads.append(agent_name)
        return _fake_steps()

    monkeypatch.setattr(ps, "_read", fake_read)

    # Two workers = two independent per-process caches over ONE Redis.
    worker_a, worker_b = {}, {}

    monkeypatch.setattr(ps, "_cache", worker_a)
    _run(ps.read_pipeline_steps(AGENT))
    monkeypatch.setattr(ps, "_cache", worker_b)
    _run(ps.read_pipeline_steps(AGENT))
    assert len(reads) == 2 and worker_a and worker_b

    # Both are warm: inside the TTL neither reads again.
    monkeypatch.setattr(ps, "_cache", worker_a)
    _run(ps.read_pipeline_steps(AGENT))
    monkeypatch.setattr(ps, "_cache", worker_b)
    _run(ps.read_pipeline_steps(AGENT))
    assert len(reads) == 2

    # A notice lands on worker A only...
    monkeypatch.setattr(ps, "_cache", worker_a)
    ps.mark_changed(AGENT)

    # ...and worker B, which never saw it, still re-reads inside the TTL.
    monkeypatch.setattr(ps, "_cache", worker_b)
    _run(ps.read_pipeline_steps(AGENT))
    assert len(reads) == 3, (
        "the other worker served a stale card — an in-memory invalidation "
        "would pass on dev's single worker and fail in prod"
    )

    # A second read on B is warm again at the new generation.
    _run(ps.read_pipeline_steps(AGENT))
    assert len(reads) == 3


def test_a_notice_during_a_read_is_not_stamped_onto_the_entry_it_invalidates(ps, monkeypatch):
    """The generation is sampled BEFORE the read, not after.

    Sampling after would stamp a notice that arrived mid-read onto the very
    entry it invalidates — the read returns pre-notice data carrying the
    post-notice generation, and the card sits stale for the full TTL with
    nothing left to signal it. This is the one ordering the design depends on
    and the one a reader would most plausibly 'tidy up'.
    """
    import fakeredis
    shared = fakeredis.FakeStrictRedis()
    monkeypatch.setattr(ps, "_get_redis", lambda: shared)
    reads = []

    async def fake_read(agent_name, started_at, roster):
        reads.append(agent_name)
        if len(reads) == 1:
            ps.mark_changed(agent_name)      # the agent advanced WHILE we read
        return _fake_steps()

    monkeypatch.setattr(ps, "_read", fake_read)
    _run(ps.read_pipeline_steps(AGENT))
    _run(ps.read_pipeline_steps(AGENT))
    assert len(reads) == 2, (
        "the mid-read notice was stamped onto the entry as already-seen"
    )


def test_the_generation_is_per_agent(ps, monkeypatch):
    import fakeredis
    shared = fakeredis.FakeStrictRedis()
    monkeypatch.setattr(ps, "_get_redis", lambda: shared)
    reads = []

    async def fake_read(agent_name, started_at, roster):
        reads.append(agent_name)
        return _fake_steps()

    monkeypatch.setattr(ps, "_read", fake_read)
    _run(ps.read_pipeline_steps(AGENT))
    _run(ps.read_pipeline_steps(OTHER))
    ps.mark_changed(OTHER)
    _run(ps.read_pipeline_steps(AGENT))       # untouched agent stays cached
    assert reads == [AGENT, OTHER]
    _run(ps.read_pipeline_steps(OTHER))
    assert reads == [AGENT, OTHER, OTHER]


def test_redis_down_keeps_the_ttl_cache(ps, monkeypatch):
    """Fail-open: with no Redis the generation is None on both sides, which is
    exactly ent#525's TTL-only behaviour — never 'no cache at all'."""
    monkeypatch.setattr(ps, "_get_redis", lambda: None)
    reads = []

    async def fake_read(agent_name, started_at, roster):
        reads.append(agent_name)
        return _fake_steps()

    monkeypatch.setattr(ps, "_read", fake_read)
    first = _run(ps.read_pipeline_steps(AGENT))
    again = _run(ps.read_pipeline_steps(AGENT))
    assert again is first and len(reads) == 1

    # And a notice with Redis down still drops the LOCAL entry, so the worker
    # that received it is at least correct.
    ps.mark_changed(AGENT)
    _run(ps.read_pipeline_steps(AGENT))
    assert len(reads) == 2


def test_a_broken_redis_never_raises_into_the_read_or_the_notice(ps, monkeypatch):
    class _Broken:
        def get(self, *a, **k):
            raise RuntimeError("redis exploded")

        def setex(self, *a, **k):
            raise RuntimeError("redis exploded")

    monkeypatch.setattr(ps, "_get_redis", lambda: _Broken())
    reads = []

    async def fake_read(agent_name, started_at, roster):
        reads.append(agent_name)
        return _fake_steps()

    monkeypatch.setattr(ps, "_read", fake_read)
    assert _run(ps.read_pipeline_steps(AGENT)).state == "reported"
    ps.mark_changed(AGENT)          # must not raise
    assert _run(ps.read_pipeline_steps(AGENT)).state == "reported"


def test_the_managers_are_optional(ps):
    """Before main.py wires them the notice must still succeed — the wiring is
    what test_1483_ws_setters_wired.py guards, not a runtime precondition."""
    ps.set_websocket_manager(None)
    ps.set_filtered_websocket_manager(None)
    assert _run(ps.notify_changed(AGENT, SimpleNamespace(
        pipeline_id="p", instance_id="i", stage="s"))) is True
