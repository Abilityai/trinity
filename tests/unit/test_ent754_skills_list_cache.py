"""trinity-enterprise#754 — a stopped agent's Skills tab shows its last-known list.

`GET /api/agents/{name}/playbooks` proxies the RUNNING agent's `/api/skills`
and answered 503 the moment the agent stopped, so the merged Skills tab would
have had nothing to show. The user's ruling (Q1): keep the last successful
listing in Redis with no TTL, `agent:skills_list:{name}`, and serve it on the
explicit opt-in `?last_known=true`.

Properties pinned here, each through the REAL route (a TestClient over the
real router) with only the container, the agent's HTTP answer and Redis faked:

1. A live success refreshes the copy; a live EMPTY list replaces it; a failed
   read or a body that is not a listing never overwrites it.
2. `last_known=true` serves the copy only when the agent is stopped or
   unreachable, labelled with when it was captured and why; with no copy the
   original error stands. Without the param, every answer is today's.
3. Redis is fail-open: down or raising, the live read still answers.
4. An oversized listing is not kept, and drops an older copy rather than
   leaving it to be served as current.
5. The copy is cleared on the teardown paths (`clear_agent_runtime_state`)
   and on the create path, never by a stop.
6. The public link strips the three new per-skill fields; the connector read
   refreshes the copy too.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytest.importorskip("fastapi", reason="backend venv required")
httpx = pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import redis_breaker_util  # noqa: E402
import routers.agent_files as files_route  # noqa: E402
import routers.public as public_route  # noqa: E402
from services import agent_skills_listing as listing  # noqa: E402

AGENT = "skills-agent"
KEY = f"agent:skills_list:{AGENT}"

LIVE = {
    "skills": [
        {"name": "daily-report", "description": "d", "path": ".claude/skills/daily-report/SKILL.md",
         "user_invocable": True, "automation": "autonomous", "allowed_tools": None,
         "argument_hint": "[date]", "has_schedule": False,
         "source": "agent", "dir": "daily-report", "approval": None},
        {"name": "publish-site", "description": "p", "path": ".claude/skills/publish-site/SKILL.md",
         "user_invocable": True, "automation": "gated", "allowed_tools": None,
         "argument_hint": None, "has_schedule": False,
         "source": "platform", "dir": "publish-site", "approval": "recommended"},
    ],
    "count": 2,
    "skill_paths": [".claude/skills", "~/.claude/skills"],
}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRedis:
    """The slice of a decode_responses=True client the listing service uses."""

    def __init__(self):
        self.store = {}
        self.fail_set = False

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value):
        if self.fail_set:
            raise ConnectionError("redis write failed")
        assert isinstance(value, str)
        self.store[key] = value

    def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)


class FakeContainer:
    def __init__(self, status):
        self.status = status


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        return self._body


class FakeAgent:
    """What the agent answers: a response, or an exception to raise."""

    def __init__(self):
        self.answer = FakeResponse(200, LIVE)
        self.calls = 0

    def client(self, agent_name, **_kw):
        agent = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url):
                agent.calls += 1
                assert url == f"http://agent-{agent_name}:8000/api/skills"
                if isinstance(agent.answer, Exception):
                    raise agent.answer
                return agent.answer

        return _Client()


@pytest.fixture
def env(monkeypatch):
    redis = FakeRedis()
    agent = FakeAgent()
    state = {"container": FakeContainer("running"), "redis": redis}

    async def _reload(_c):
        return None

    monkeypatch.setattr(redis_breaker_util, "get_breaker_redis", lambda: state["redis"])
    monkeypatch.setattr(listing, "get_agent_container", lambda name: state["container"])
    monkeypatch.setattr(listing, "container_reload", _reload)
    monkeypatch.setattr(listing, "agent_httpx_client", agent.client)

    app = FastAPI()
    app.include_router(files_route.router)

    # Override the access dependency the ROUTE captured (not a re-import): the
    # handler's own behaviour is under test, the uniform 404 has its own suite.
    def _override(dependant):
        for dep in getattr(dependant, "dependencies", []):
            if getattr(dep.call, "__name__", "") == "get_authorized_agent_by_name":
                app.dependency_overrides[dep.call] = lambda agent_name: agent_name
            _override(dep)

    for r in app.routes:
        _override(getattr(r, "dependant", None))

    class Ctx:
        client = TestClient(app, raise_server_exceptions=False)

    ctx = Ctx()
    ctx.redis = redis
    ctx.agent = agent
    ctx.state = state
    return ctx


def _get(ctx, **params):
    return ctx.client.get(f"/api/agents/{AGENT}/playbooks", params=params)


def _cached(ctx):
    raw = ctx.redis.store.get(KEY)
    return json.loads(raw) if raw is not None else None


# ---------------------------------------------------------------------------
# 1. The copy follows the live list
# ---------------------------------------------------------------------------


def test_live_success_answers_unchanged_and_keeps_a_copy(env):
    r = _get(env)

    assert r.status_code == 200
    assert r.json() == LIVE                      # the live contract is untouched
    copy = _cached(env)
    assert copy["skills"] == LIVE["skills"]
    assert copy["skill_paths"] == LIVE["skill_paths"]
    assert copy["captured_at"].endswith("Z")


def test_a_live_empty_list_replaces_the_copy(env):
    _get(env)
    env.agent.answer = FakeResponse(200, {"skills": [], "count": 0, "skill_paths": [".claude/skills"]})

    assert _get(env).status_code == 200
    assert _cached(env)["skills"] == []          # removal is news too


def test_a_failed_read_never_overwrites_the_copy(env):
    _get(env)
    env.agent.answer = FakeResponse(500, None, text="boom")

    r = _get(env)

    assert r.status_code == 500
    assert _cached(env)["skills"] == LIVE["skills"]


def test_a_200_that_is_not_a_listing_is_answered_but_not_kept(env):
    _get(env)
    env.agent.answer = FakeResponse(200, {"error": "half-started"})

    r = _get(env)

    assert r.status_code == 200 and r.json() == {"error": "half-started"}
    assert _cached(env)["skills"] == LIVE["skills"]


# ---------------------------------------------------------------------------
# 2. last_known is opt-in, and only for a stopped / unreachable agent
# ---------------------------------------------------------------------------


def test_stopped_with_last_known_serves_the_copy_labelled(env):
    _get(env)
    captured = _cached(env)["captured_at"]
    env.state["container"] = FakeContainer("exited")

    r = _get(env, last_known="true")

    assert r.status_code == 200
    body = r.json()
    assert body["skills"] == LIVE["skills"]
    assert body["skill_paths"] == LIVE["skill_paths"]
    assert body["count"] == 2
    assert body["last_known"] == {"captured_at": captured, "reason": "stopped"}


def test_stopped_without_the_param_keeps_todays_503(env):
    _get(env)
    env.state["container"] = FakeContainer("exited")

    r = _get(env)

    assert r.status_code == 503
    assert "last_known" not in r.text


def test_stopped_with_no_copy_keeps_the_503(env):
    env.state["container"] = FakeContainer("exited")

    r = _get(env, last_known="true")

    assert r.status_code == 503
    assert env.agent.calls == 0


@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
def test_running_but_unreachable_serves_the_copy_as_unreachable(env, exc):
    _get(env)
    env.agent.answer = exc

    r = _get(env, last_known="true")

    assert r.status_code == 200
    assert r.json()["last_known"]["reason"] == "unreachable"


def test_an_agent_error_is_not_masked_by_the_copy(env):
    """A running agent that ANSWERS with an error is not 'unreachable': the
    honest answer is the error, not yesterday's list."""
    _get(env)
    env.agent.answer = FakeResponse(500, None, text="boom")

    assert _get(env, last_known="true").status_code == 500


def test_no_container_at_all_is_a_404_even_with_a_copy(env):
    _get(env)
    env.state["container"] = None

    assert _get(env, last_known="true").status_code == 404


# ---------------------------------------------------------------------------
# 3. Redis is fail-open
# ---------------------------------------------------------------------------


def test_redis_down_on_the_live_path_still_answers(env):
    env.state["redis"] = None

    r = _get(env)

    assert r.status_code == 200 and r.json() == LIVE


def test_redis_write_failure_on_the_live_path_still_answers(env):
    env.redis.fail_set = True

    r = _get(env)

    assert r.status_code == 200 and r.json() == LIVE


def test_redis_down_when_stopped_is_the_original_503(env):
    env.state["container"] = FakeContainer("exited")
    env.state["redis"] = None

    assert _get(env, last_known="true").status_code == 503


def test_a_malformed_copy_is_treated_as_no_copy(env):
    env.redis.store[KEY] = "{not json"
    env.state["container"] = FakeContainer("exited")

    assert _get(env, last_known="true").status_code == 503


# ---------------------------------------------------------------------------
# 4. Size bound
# ---------------------------------------------------------------------------


def test_an_oversized_listing_is_not_kept_and_drops_the_older_copy(env):
    _get(env)
    huge = {"skills": [{"name": f"s{i}", "description": "x" * 2000} for i in range(200)],
            "count": 200, "skill_paths": []}
    env.agent.answer = FakeResponse(200, huge)

    r = _get(env)

    assert r.status_code == 200 and r.json()["count"] == 200
    assert KEY not in env.redis.store             # never serve the stale smaller list


# ---------------------------------------------------------------------------
# 5. Lifecycle
# ---------------------------------------------------------------------------


def test_the_keyspace_is_registered_as_cleared():
    from services import agent_runtime_state

    assert listing.KEY_PREFIX == "agent:skills_list:"
    assert listing.KEY_PREFIX in agent_runtime_state.CLEARED_KEYSPACES


@pytest.mark.asyncio
async def test_clear_agent_runtime_state_drops_the_copy(env, monkeypatch):
    from services import agent_runtime_state, slot_service

    class _Slots:
        async def force_clear_slots(self, _name):
            return None

    monkeypatch.setattr(agent_runtime_state, "clear_agent_breakers", lambda _n: None)
    monkeypatch.setattr(slot_service, "get_slot_service", lambda: _Slots())
    _get(env)
    env.redis.store["agent:skills_list:other-agent"] = "{}"

    await agent_runtime_state.clear_agent_runtime_state(AGENT)

    assert KEY not in env.redis.store
    assert "agent:skills_list:other-agent" in env.redis.store   # only its own


def test_a_stop_does_not_clear_the_copy():
    """`clear_agent_breakers` runs on every real START (lifecycle.py) and on
    create; the copy is exactly what a stopped-then-started agent still needs,
    so it must not be wired there."""
    src = (_BACKEND / "services" / "agent_runtime_state.py").read_text()
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "clear_agent_breakers")
    assert "skills_list" not in ast.unparse(fn) and "agent_skills_listing" not in ast.unparse(fn)


def test_the_create_path_clears_the_copy_beside_the_breakers():
    """Call-site guard (the behaviour of `forget` is proved above): the create
    path clears a predecessor's copy where it clears the breakers, so a read
    that was in flight during a delete cannot hand a recycled name its
    predecessor's list (#1560 class)."""
    src = (_BACKEND / "services" / "agent_service" / "crud.py").read_text()
    tree = ast.parse(src)
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            calls.append(ast.unparse(node.func) + "(" + ", ".join(ast.unparse(a) for a in node.args) + ")")
    assert "clear_agent_breakers(config.name)" in calls
    assert "agent_skills_listing.forget(config.name)" in calls


# ---------------------------------------------------------------------------
# 6. The other two readers
# ---------------------------------------------------------------------------


def _public_client(monkeypatch):
    monkeypatch.setattr(public_route, "check_public_link_rate_limit", lambda ip: None)
    monkeypatch.setattr(public_route, "_validate_public_link", lambda token: {"agent_name": AGENT})
    app = FastAPI()
    app.include_router(public_route.router)
    return TestClient(app, raise_server_exceptions=False)


def test_the_public_link_never_sees_the_new_fields(env, monkeypatch):
    client = _public_client(monkeypatch)

    r = client.get("/api/public/playbooks/tok")

    assert r.status_code == 200
    for skill in r.json()["skills"]:
        assert not {"source", "dir", "approval"} & set(skill), skill
        assert skill["name"] in {"daily-report", "publish-site"}
        assert "argument_hint" in skill and "user_invocable" in skill
    assert _cached(env)["skills"] == LIVE["skills"]   # and it refreshed the copy


def test_the_public_link_on_a_stopped_agent_keeps_its_503(env, monkeypatch):
    client = _public_client(monkeypatch)
    env.state["container"] = FakeContainer("exited")

    r = client.get("/api/public/playbooks/tok")

    assert r.status_code == 503
    assert r.json()["detail"] == "Agent is not running"


@pytest.mark.asyncio
async def test_the_connector_read_returns_the_list_and_refreshes_the_copy(env):
    from services import connector_service

    skills = await connector_service.fetch_live_playbooks(AGENT)

    assert [s["name"] for s in skills] == ["daily-report", "publish-site"]
    assert _cached(env)["skills"] == LIVE["skills"]


@pytest.mark.asyncio
async def test_the_connector_read_on_a_stopped_agent_keeps_its_503(env):
    from fastapi import HTTPException
    from services import connector_service

    env.state["container"] = FakeContainer("exited")

    with pytest.raises(HTTPException) as exc:
        await connector_service.fetch_live_playbooks(AGENT)

    assert exc.value.status_code == 503
    assert exc.value.detail == "Agent is not running."
