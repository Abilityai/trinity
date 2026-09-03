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


# ------------------------------------------------------- the deferral (crux)

def test_roster_awaits_no_briefing_even_when_every_agent_hangs(svc, monkeypatch):
    """THE test. Every agent's briefing hangs forever; the roster still returns.

    Under the old code this could not finish at all: `get_roster` awaited
    `gather` over all twelve. A duration assertion would not have proved it —
    on a fixture whose stub returns instantly, the old code is fast too — so the
    stub is one that can never complete, and the call is itself wrapped in a
    timeout so a regression fails the test rather than hanging the suite.
    """
    s, _briefed, _counted = svc
    started: list[str] = []
    monkeypatch.setattr(s, "_agent_briefing", _hanging_briefing(started))

    async def run():
        return await asyncio.wait_for(s.get_roster(EMAIL), 1.0)

    roster = asyncio.run(run())

    assert len(roster.agents) == len(FLEET)
    assert started == []                       # not merely "did not await" — never called
    assert all(c.briefing_state == "pending" for c in roster.agents)
    assert all(c.description is None and list(c.playbooks) == [] for c in roster.agents)


def test_the_roster_still_makes_its_one_docker_call(svc, monkeypatch):
    """Deferring the briefing must not disturb #2196's single batch read — the
    availability chip is a roster fact and stays on the roster."""
    s, _briefed, counted = svc
    monkeypatch.setattr(s, "_agent_briefing", _hanging_briefing([]))

    async def run():
        return await asyncio.wait_for(s.get_roster(EMAIL), 1.0)

    roster = asyncio.run(run())

    assert counted["batch"] == 1 and counted["single"] == 0
    assert all(c.availability == "ready" for c in roster.agents)


def test_the_roster_source_no_longer_fans_out():
    """A source pin, because the behavioural test above can be satisfied by a
    stub-shaped accident. `get_roster` must contain no fan-out at all — and the
    docstring must not carry the call either, or this reads it as a call site.
    """
    from client_portal import service as s

    src = inspect.getsource(s.get_roster)
    assert "gather(" not in src
    assert "_agent_briefing(" not in src


# --------------------------------------------------------------- the batch

def test_one_hung_agent_does_not_delay_the_other_briefings(svc, monkeypatch):
    """The bound doing its job on the batch: the wedged agent costs its budget,
    the healthy eleven are unaffected."""
    s, _briefed, _counted = svc
    monkeypatch.setattr(s, "_BRIEFING_BUDGET_SECONDS", 0.05)

    async def one_hangs(name, availability="ready"):
        if name == "agent-3":
            await asyncio.Event().wait()
        return ("desc-" + name, [])

    monkeypatch.setattr(s, "_agent_briefing", one_hangs)

    started = time.perf_counter()
    result = asyncio.run(s.get_briefings(EMAIL, None))
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5
    assert result.briefings["agent-3"].state == "unavailable"
    assert result.briefings["agent-3"].description is None
    assert result.briefings["agent-7"].state == "ready"
    assert result.briefings["agent-7"].description == "desc-agent-7"


def test_a_bound_trip_is_reported_unavailable_not_hintless(svc, monkeypatch):
    """The #2163 honesty rule, on the batch path: a wedged agent must never
    render as one that simply has nothing to offer."""
    s, _briefed, _counted = svc
    monkeypatch.setattr(s, "_BRIEFING_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(s, "_agent_briefing", _hanging_briefing([]))

    result = asyncio.run(s.get_briefings(EMAIL, ["agent-3"]))

    assert result.briefings["agent-3"].state == "unavailable"
    assert result.briefings["agent-3"].playbooks == []


def test_briefings_are_roster_scoped_and_iterate_the_roster(svc):
    """The access boundary AND the SSRF shape in one: only roster names are
    answered, and the name that reaches the agent comes from the DB row, never
    from the caller's string (`AGENT-1` is not `agent-1`)."""
    s, briefed, _counted = svc

    result = asyncio.run(s.get_briefings(EMAIL, ["agent-1", "not-mine", "AGENT-1"]))

    assert briefed == ["agent-1"]
    assert set(result.briefings) == {"agent-1"}


def test_briefings_with_no_filter_cover_the_whole_roster(svc):
    """The background-batch arm. A literal `n in None` would 500 here on every
    roster load, which is the shape this test exists to catch."""
    s, briefed, _counted = svc

    result = asyncio.run(s.get_briefings(EMAIL, None))

    assert sorted(briefed) == sorted(FLEET)
    assert set(result.briefings) == set(FLEET)


def test_briefings_make_no_docker_read(svc, monkeypatch):
    """The batch runs moments after the roster made the fleet Docker call, so
    repeating it buys nothing — and `_agent_briefing` attempts `unknown` by
    design, so a container-state read cannot change the outcome anyway. Both
    seams are counted, for one name and for many."""
    s, _briefed, counted = svc
    seen: list[tuple] = []

    async def recording(name, availability="ready"):
        seen.append((name, availability))
        return ("d", [])

    monkeypatch.setattr(s, "_agent_briefing", recording)
    asyncio.run(s.get_briefings(EMAIL, ["agent-1"]))
    asyncio.run(s.get_briefings(EMAIL, None))

    assert counted["batch"] == 0 and counted["single"] == 0
    assert {availability for _n, availability in seen} == {"unknown"}


def test_semaphore_is_per_request_and_acquired_outside_the_bound(svc, monkeypatch):
    """Two properties one test can prove together.

    40 agents against 16 permits with a stub that takes 0.02s and a 0.05s
    budget: if the permit wait happened INSIDE the wall clock, rounds 2 and 3
    would burn their budget queueing and come back `unavailable`. And running
    it twice through `asyncio.run` proves the semaphore is created per request
    — a module-level one binds to the first loop and raises on the second.
    """
    s, _briefed, _counted = svc
    big = [f"a{i}" for i in range(40)]
    monkeypatch.setattr(s.db, "get_shared_roster", lambda e: _rows(big))
    monkeypatch.setattr(s, "_BRIEFING_BUDGET_SECONDS", 0.05)

    async def slow(name, availability="ready"):
        await asyncio.sleep(0.02)
        return ("d", [])

    monkeypatch.setattr(s, "_agent_briefing", slow)

    first = asyncio.run(s.get_briefings(EMAIL, None))
    second = asyncio.run(s.get_briefings(EMAIL, None))

    assert len(first.briefings) == 40
    assert all(b.state == "ready" for b in first.briefings.values())
    assert all(b.state == "ready" for b in second.briefings.values())
    assert "asyncio.Semaphore(" in inspect.getsource(s.get_briefings)


def test_briefings_respect_include_owned(svc, monkeypatch):
    """Membership resolves by exactly the roster's rule (ent#357/#2198). A
    second rule here would brief an agent the sidebar does not list."""
    s, _briefed, _counted = svc
    monkeypatch.setattr(s.db, "get_shared_roster", lambda e: [])
    monkeypatch.setattr(s.db, "get_owned_roster", lambda e: _rows(["mine"]))

    assert asyncio.run(s.get_briefings(EMAIL, None)).briefings == {}
    owned = asyncio.run(s.get_briefings(EMAIL, None, include_owned=True))
    assert set(owned.briefings) == {"mine"}


def test_briefings_tolerate_two_tuple_stubs(svc):
    """`_agent_briefing` is monkeypatched with the pre-#2213 2-tuple by several
    test modules; a 4-field unpack against one raises inside the response build
    — a stale double becoming a 500 rather than a failed assertion."""
    s, _briefed, _counted = svc

    result = asyncio.run(s.get_briefings(EMAIL, ["agent-1"]))

    entry = result.briefings["agent-1"]
    assert entry.description == "desc"
    assert entry.playbooks == [] and entry.playbooks_total == 0


def test_briefings_never_raise_when_the_agent_read_explodes(svc, monkeypatch):
    """Fail-soft moved with the work: a raising agent leaves an `unavailable`
    entry, never a 500 on the hydration call."""
    s, _briefed, _counted = svc

    async def boom(name, availability="ready"):
        raise RuntimeError("agent unreachable")

    monkeypatch.setattr(s, "_agent_briefing", boom)
    result = asyncio.run(s.get_briefings(EMAIL, None))

    assert set(result.briefings) == set(FLEET)
    assert all(b.state == "unavailable" for b in result.briefings.values())


def test_no_email_briefs_nothing(svc, monkeypatch):
    """Same boundary as the roster: no identity, no rows, no agent contacted.

    The roster stub is made email-sensitive here (the fixture's ignores it), so
    this exercises the real path rather than the double.
    """
    s, briefed, _counted = svc
    monkeypatch.setattr(s.db, "get_shared_roster",
                        lambda e: _rows(FLEET) if e else [])

    assert asyncio.run(s.get_briefings(None, None)).briefings == {}
    assert asyncio.run(s.get_briefings("", None)).briefings == {}
    assert briefed == []


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


def test_a_shutdown_cancel_propagates_rather_than_reading_as_a_bound_trip(svc, monkeypatch):
    """`except Exception`, never `except BaseException` — and that distinction
    is invisible in the source to anyone "hardening" the swallow later.

    `asyncio.CancelledError` has been a BaseException since 3.8. This helper sits
    on two response paths and swallows everything else by design, so widening the
    clause one word would make a backend shutdown read as a failed briefing:
    the cancellation is absorbed, the coroutine returns a value, and the task
    that was being torn down carries on.
    """
    s, _briefed, _counted = svc

    async def cancelled(name, availability="ready"):
        raise asyncio.CancelledError()

    monkeypatch.setattr(s, "_agent_briefing", cancelled)

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await s._bounded_briefing("agent-3", "ready")

    asyncio.run(run())


def test_the_wall_clock_cancel_closes_the_agent_connection(svc, monkeypatch):
    """The bound must RELEASE the socket it gave up on, not merely stop waiting.

    `_agent_briefing` holds `agent_httpx_client` open across both GETs, so the
    only thing that closes it when the wall clock fires is the cancellation
    unwinding through `async with`. `asyncio.wait_for` cancels the inner
    awaitable and AWAITS it before raising, which is what makes that true — a
    refactor that stopped awaiting the cancellation (or moved the client outside
    the bound) would leak one connection per trip, under exactly the condition
    the bound exists for.
    """
    s, _briefed, _counted = svc
    monkeypatch.setattr(s, "_BRIEFING_BUDGET_SECONDS", 0.02)
    exits: list = []

    class _Client:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, exc_type, exc, tb):
            exits.append(exc_type)
            return False

    async def hangs_inside_the_client(name, availability="ready"):
        async with _Client():
            await asyncio.Event().wait()

    monkeypatch.setattr(s, "_agent_briefing", hangs_inside_the_client)
    briefing, ok = asyncio.run(s._bounded_briefing("agent-3", "ready"))

    assert ok is False and briefing == s.AgentBriefing()
    assert exits == [asyncio.CancelledError], "the client was not closed on the bound trip"


def test_an_empty_filter_briefs_nobody(svc):
    """An empty list is "brief nobody"; only `None` means the whole roster.

    The two are opposite answers to one call, and the empty list is the one that
    must never fan out — see the route test of the same name for the query-string
    half.
    """
    s, briefed, _counted = svc

    assert asyncio.run(s.get_briefings(EMAIL, [])).briefings == {}
    assert briefed == []


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


# ------------------------------------------------------------------ the route

@pytest.fixture()
def route(tmp_path, monkeypatch):
    """The route with Redis forced off (`test_ent287`'s shape) and the service
    stubbed, so what is exercised is the router's own work: parse, cap, limiter
    key, and the two arguments it must thread."""
    monkeypatch.setenv("TRINITY_DB_PATH", str(tmp_path / "trinity-test.db"))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(tmp_path / "trinity-test.db"))

    from services import rate_limiter
    from client_portal import router as portal_router
    from client_portal import service
    from client_portal.models import PortalBriefings

    monkeypatch.setattr(rate_limiter, "_get_redis", lambda: None)
    rate_limiter.clear_inprocess()

    seen: list[tuple] = []

    async def fake_get_briefings(email, requested=None, include_owned=False):
        seen.append((email, requested, include_owned))
        return PortalBriefings(briefings={})

    monkeypatch.setattr(service, "get_briefings", fake_get_briefings)
    try:
        yield portal_router, seen, rate_limiter
    finally:
        rate_limiter.clear_inprocess()


def _principal(email="bob@example.com", is_platform=False):
    from client_portal.portal_auth import PortalPrincipal
    return PortalPrincipal(email, is_platform)


def test_the_filter_is_split_stripped_and_deduped(route):
    portal_router, seen, _rl = route

    asyncio.run(portal_router.portal_briefings(
        agents=" agent-1 , agent-2,agent-1,, ", principal=_principal()))

    assert seen == [("bob@example.com", ["agent-1", "agent-2"], False)]


def test_an_absent_filter_reaches_the_service_as_none(route):
    """`None` is the whole-roster arm; an empty list would brief nobody, which
    is a different (and silently wrong) answer."""
    portal_router, seen, _rl = route

    asyncio.run(portal_router.portal_briefings(agents=None, principal=_principal()))

    assert seen == [("bob@example.com", None, False)]


def test_an_empty_filter_briefs_nobody_and_takes_the_filtered_bucket(route):
    """`agents=` present but empty is "brief nobody", NEVER the whole roster.

    The router separates the two on `agents is not None`, and they are opposite
    answers: an empty list intersects the roster to nothing, `None` fans out to
    every agent on it. Tightening that to `if agents:` — the obvious
    simplification — would turn one empty query parameter into a whole-fleet
    fan-out AND charge it to the filtered bucket, which is six times looser
    precisely because a filtered call costs one agent.
    """
    portal_router, seen, rl = route

    asyncio.run(portal_router.portal_briefings(agents="", principal=_principal()))
    asyncio.run(portal_router.portal_briefings(agents=" , ", principal=_principal()))

    assert seen == [("bob@example.com", [], False)] * 2
    assert "portal_briefings:bob@example.com" in rl._inprocess_buckets
    assert "portal_briefings_all:bob@example.com" not in rl._inprocess_buckets


def test_include_owned_follows_the_principal(route):
    """ent#357/#2198: what a caller may DO equals what they may SEE. A platform
    session's roster includes owned agents; a client's must not."""
    portal_router, seen, _rl = route

    asyncio.run(portal_router.portal_briefings(
        agents=None, principal=_principal(is_platform=True)))

    assert seen[0][2] is True


def test_an_over_cap_filter_is_refused_by_name_and_costs_nothing(route):
    """Named 422 (Product Quality Bar #6), raised BEFORE the limiter so the
    rejected request cannot burn the caller's own bucket."""
    from fastapi import HTTPException

    portal_router, seen, rl = route
    many = ",".join(f"a{i}" for i in range(201))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(portal_router.portal_briefings(agents=many, principal=_principal()))

    assert exc.value.status_code == 422
    assert "at most 200 names" in str(exc.value.detail)
    assert seen == []
    assert not rl._inprocess_buckets       # no bucket touched


def test_the_two_forms_take_two_limiter_keys(route):
    """The unfiltered batch costs one bounded agent call PER ROSTERED AGENT, so
    it cannot share the filtered form's budget."""
    portal_router, _seen, rl = route

    asyncio.run(portal_router.portal_briefings(agents="agent-1", principal=_principal()))
    asyncio.run(portal_router.portal_briefings(agents=None, principal=_principal()))

    assert "portal_briefings:bob@example.com" in rl._inprocess_buckets
    assert "portal_briefings_all:bob@example.com" in rl._inprocess_buckets


def test_the_unfiltered_form_is_the_tighter_bucket(route):
    """Eleven whole-roster calls in a minute must 429; the filtered form has far
    more headroom because one call is one agent."""
    from fastapi import HTTPException

    portal_router, _seen, _rl = route

    for _ in range(10):
        asyncio.run(portal_router.portal_briefings(agents=None, principal=_principal()))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(portal_router.portal_briefings(agents=None, principal=_principal()))
    assert exc.value.status_code == 429

    # The filtered bucket is untouched by that exhaustion.
    asyncio.run(portal_router.portal_briefings(agents="agent-1", principal=_principal()))


def test_fastapi_can_build_the_route():
    """`client_portal/router.py` uses `from __future__ import annotations`, so
    every annotation is a STRING FastAPI must resolve at include time — a name
    missing from the module namespace fails there, at app startup, which
    importing the module would not catch (`test_2162`'s lesson)."""
    from fastapi import FastAPI

    from client_portal.router import router

    app = FastAPI()
    app.include_router(router)

    route_obj = next(r for r in app.routes if getattr(r, "path", "").endswith("/briefings"))
    params = {p.name: p for p in route_obj.dependant.query_params}
    assert "agents" in params
    assert params["agents"].field_info.default is None


def test_the_route_is_declared_in_the_viewer_scoped_block():
    """Invariant #4: `briefings` is a literal segment-1 and must stay in the
    no-agent-parameter block, where a future `/{param}` catch-all cannot
    capture it."""
    import inspect as _inspect
    from client_portal import router as portal_router

    src = _inspect.getsource(portal_router)
    assert src.index('@router.get("/sessions"') < src.index('@router.get("/briefings"')
    assert src.index('@router.get("/briefings"') < src.index('@router.get("/agents/{agent_name}/page"')
