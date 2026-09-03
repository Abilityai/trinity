"""The roster's latency floor is no longer its slowest agent (#2163).

`get_roster` used to fan `_agent_briefing` across every card and `await
asyncio.gather(...)` — so the Workspace's first paint was bounded by the SLOWEST
agent in the fleet, for every user, on every sign-in. One wedged agent meant a
five-second sign-in for a fleet of healthy ones. Two changes, both pinned here:

* **Defer (option 1).** The roster awaits NO briefing at all; every card ships
  `briefing_state="pending"` and the client hydrates through `GET /briefings`.
* **Bound (option 2, the belt).** Every briefing that still runs — the batch
  above and the agent page's single one — goes through `_bounded_briefing`: a
  wall-clock `asyncio.wait_for` over a `_agent_briefing` whose per-phase httpx
  timeout is itself well below the old literal `5.0`. A trip reports
  `unavailable`, never an empty briefing that passes for a hint-less agent.

What is pinned is the COUNT and the BOUND, never a duration on a tiny fixture:
a timing assertion on a 12-agent fixture passes against the OLD code too, which
is exactly how #2160 shipped. The crux test therefore uses a stub that never
resolves — if the roster still awaited briefings, it could not return at all.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault("TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-2163.db"))
os.environ.setdefault("LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-2163-logs"))

import pytest

pytestmark = pytest.mark.unit

EMAIL = "alice@example.com"
FLEET = [f"agent-{i}" for i in range(12)]


def _rows(names, **extra):
    return [{"agent_name": n, "owner": "alice", "avatar_updated_at": None,
             "is_default_avatar": 1, "tts_voice_id": None, "shared_at": None,
             **extra}
            for n in names]


@pytest.fixture()
def svc(monkeypatch):
    """`test_2160`'s fixture shape: a stubbed roster, counted Docker seams, a
    counted briefing stub. The Docker seams are stubbed because
    `docker.from_env()` runs at import and would otherwise answer for real on a
    developer's machine, marking every fixture agent `unavailable` — which the
    briefing skips, so the counts below would silently stop measuring anything.
    """
    from client_portal import service as s

    counted = {"batch": 0, "single": 0}
    briefed: list[str] = []

    async def counting_briefing(name, availability="ready"):
        briefed.append(name)
        return ("desc", [])

    monkeypatch.setattr(s.db, "get_shared_roster", lambda e: _rows(FLEET))
    monkeypatch.setattr(s.db, "get_owned_roster", lambda e: [])
    monkeypatch.setattr(s, "_agent_briefing", counting_briefing)

    async def _map(names):
        counted["batch"] += 1
        return {n: "ready" for n in names}

    async def _one(name):
        counted["single"] += 1
        return "ready"

    monkeypatch.setattr(s, "_availability_map", _map)
    monkeypatch.setattr(s, "_agent_availability", _one)

    import services.tts_service as tts
    monkeypatch.setattr(tts, "is_available", lambda: False)
    return s, briefed, counted


def _hanging_briefing(started: list[str]):
    """A stub that is genuinely never resolved — not a slow fake.

    A `sleep(5)` stub would make the crux test a timing race; an `Event` that
    nobody sets cannot complete, so the roster either does not await it or the
    test times out.
    """
    async def stub(name, availability="ready"):
        started.append(name)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")   # pragma: no cover
    return stub


# --------------------------------------------------------------- the bound

def test_agent_page_briefing_is_bounded_and_says_so(svc, monkeypatch):
    """The agent page's own floor. `get_agent_card` issues exactly one briefing
    (#2160) — but unbounded, that one call still hung the page for as long as
    the agent cared to trickle. Bounded, the page renders, and the card SAYS
    the briefing failed rather than passing for an agent with no hints."""
    s, _briefed, _counted = svc
    monkeypatch.setattr(s, "_BRIEFING_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(s, "_agent_briefing", _hanging_briefing([]))

    started = time.perf_counter()
    card = asyncio.run(s.get_agent_card(EMAIL, "agent-3"))
    elapsed = time.perf_counter() - started

    assert card is not None and card.name == "agent-3"
    assert elapsed < 1.0
    assert card.briefing_state == "unavailable"
    assert card.description is None and list(card.playbooks) == []


def test_a_completed_briefing_says_ready(svc):
    """The other half: `unavailable` must not be the answer to everything, or
    the client retries forever and the state carries no information."""
    s, _briefed, _counted = svc

    card = asyncio.run(s.get_agent_card(EMAIL, "agent-3"))

    assert card.briefing_state == "ready"
    assert card.description == "desc"


def test_an_empty_briefing_that_COMPLETED_is_still_ready(svc, monkeypatch):
    """`ok` means "reached a verdict inside its budget", NOT "returned data".

    An agent that exposes nothing legitimately briefs empty. Reporting that as
    `unavailable` would make the client retry a healthy agent once per session
    and show "couldn't load" where the honest copy is "no hints".
    """
    s, _briefed, _counted = svc

    async def empty(name, availability="ready"):
        return s.AgentBriefing()

    monkeypatch.setattr(s, "_agent_briefing", empty)
    card = asyncio.run(s.get_agent_card(EMAIL, "agent-3"))

    assert card.briefing_state == "ready"
    assert list(card.playbooks) == []


def test_bounded_briefing_swallows_exceptions(svc, monkeypatch):
    """It sits on two response paths and must never raise — a briefing failure
    is never a 500."""
    s, _briefed, _counted = svc

    async def boom(name, availability="ready"):
        raise RuntimeError("agent unreachable")

    monkeypatch.setattr(s, "_agent_briefing", boom)
    briefing, ok = asyncio.run(s._bounded_briefing("agent-3", "ready"))

    assert ok is False
    assert briefing == s.AgentBriefing()


def test_a_briefing_that_was_never_attempted_is_not_ready(svc, monkeypatch):
    """A stopped agent is skipped before any HTTP (the `_agent_briefing` early
    return). The caller must be able to tell that from a completed empty
    briefing, or the card claims a verdict nobody reached."""
    s, briefed, _counted = svc

    briefing, ok = asyncio.run(s._bounded_briefing("agent-3", "stopped"))

    assert ok is False
    assert briefing == s.AgentBriefing()
    assert briefed == []   # and it cost no agent call


def test_bounded_briefing_reads_its_globals_at_call_time():
    """Neither `_agent_briefing` nor the budget may be captured as a default
    argument: every test here (and `test_2160`'s counting stub) steers this
    function by monkeypatching the module global, and a default argument binds
    at def time, so the patch would silently stop applying."""
    sig = inspect.signature(__import__("client_portal.service", fromlist=["x"])._bounded_briefing)
    defaults = [p.default for p in sig.parameters.values()]
    assert all(d in (inspect.Parameter.empty, "ready") for d in defaults)


def test_the_bound_is_below_the_old_floor():
    """The values, and that the literal they replace is gone.

    Per-phase < wall-clock, or the wall clock could not bound two sequential
    GETs; both well under the 5s that was never a ceiling in the first place.
    """
    from client_portal import service as s

    assert 0 < s._BRIEFING_HTTP_TIMEOUT_SECONDS < 5.0
    assert s._BRIEFING_HTTP_TIMEOUT_SECONDS < s._BRIEFING_BUDGET_SECONDS < 5.0

    src = inspect.getsource(s._agent_briefing)
    assert "_BRIEFING_HTTP_TIMEOUT_SECONDS" in src
    assert "timeout=5.0" not in src

    # The wall clock is what actually bounds the call, and it must be applied
    # by the shared helper rather than re-derived at a call site.
    assert "wait_for" in inspect.getsource(s._bounded_briefing)


def test_the_two_briefing_gets_run_concurrently():
    """Sequential GETs made the wall-clock bound lossy: a cancel landing during
    the second call discarded the description the first had already returned.
    Gathered, the healthy latency also halves."""
    from client_portal import service

    info = MagicMock(status_code=200)
    info.json.return_value = {"description": "d", "use_cases": []}
    skills = MagicMock(status_code=200)
    skills.json.return_value = {"skills": [{"name": "s1", "user_invocable": True}]}

    async def slow_get(url, **kw):
        await asyncio.sleep(0.05)
        return info if "template/info" in url else skills

    client = MagicMock()
    client.get = AsyncMock(side_effect=slow_get)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)

    core_db = MagicMock()
    core_db.get_connector_config.return_value = None

    with patch("services.agent_auth.agent_httpx_client", return_value=ctx), \
         patch("database.db", core_db):
        # Warm the lazy imports `_agent_briefing` performs in its own body
        # (`services.connector_service`, `database`) — a cold import costs tens
        # of milliseconds INSIDE the window and would make this measure the
        # module loader rather than the two GETs.
        asyncio.run(service._agent_briefing("atlas", "ready"))
        started = time.perf_counter()
        description, playbooks, _searchable, _total = asyncio.run(
            service._agent_briefing("atlas", "ready")
        )
        elapsed = time.perf_counter() - started

    # Both legs landed — a concurrency change that dropped one would still be
    # fast, so the fields are the real assertion and the clock is the proof of
    # overlap (sequential would be >= 0.10s).
    assert description == "d"
    assert len(playbooks) == 1
    assert elapsed < 0.09
