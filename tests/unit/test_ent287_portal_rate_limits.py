"""Per-client rate limits on portal chat and document upload (ent#287).

`/tts` and `/stt` already carried a per-(client, agent) limiter. `/chat` and
`/documents` — the two portal surfaces that spend **money** and **disk** — did
not. Chat was bounded only indirectly by the agent's capacity/backlog inside
`execute_task`, which bounds *concurrency*, not spend: one client email could
serialise requests forever and consume an agent's entire model budget without
ever tripping it. Upload was bounded per file (25 MiB) and per inbox (100 MiB
per client per agent), but the inbox write is an **overwrite** — re-uploading
the same filename never grows the quota's `used`, so the quota never trips and
the per-request work (two docker execs + a 25 MiB tar) was unbounded.

What is pinned here:
  * both surfaces reject past the limit, with a retryable 429 + `Retry-After`
  * chat is scoped per (client, agent); upload is scoped per client ACROSS
    agents — the two different answers to "whose budget is this?"
  * the off-roster 404 happens BEFORE the limiter, so a caller cannot mint
    unbounded limiter keys from an unvalidated path param
  * the upload limit is charged before the body is `read()` into RAM and before
    any docker work (NOT before the transfer — Starlette has already spooled it)
  * the sustained (hourly) tier is actually binding, and chat is never tighter
    than the voice surfaces it is paired with in voice mode

The limiter is exercised through its real in-process fallback path (Redis
forced unavailable), not a mock: `_check_inprocess` is production code on the
Redis-outage branch, and the accept/reject boundary is identical to the Redis
branch.
"""
from __future__ import annotations

import asyncio

import pytest


# The voice surfaces' per-(client, agent) limit, set by ent#78. Chat must never
# be tighter than this: one voice turn is one /chat AND one /tts, so a chat
# limit below the tts limit would 429 voice mode on the chat leg first.
_VOICE_SURFACE_LIMIT = 20


@pytest.fixture()
def portal(tmp_path, monkeypatch):
    """Router + limiter with Redis forced off and a one-agent-per-name roster.

    Forcing `_get_redis()` to None is deliberate and not a shortcut: it makes
    the window state process-local and therefore deterministic, instead of
    depending on whether a dev Redis happens to be up (and polluting it if so).
    """
    # Keep the OSS import chain off the container's /data path. Nothing here
    # touches the DB — the roster and both services are stubbed.
    monkeypatch.setenv("TRINITY_DB_PATH", str(tmp_path / "trinity-test.db"))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(tmp_path / "trinity-test.db"))

    from services import rate_limiter
    from client_portal import router as portal_router
    from client_portal import service

    monkeypatch.setattr(rate_limiter, "_get_redis", lambda: None)
    rate_limiter.clear_inprocess()

    # Everything is on-roster except names starting with "off-".
    monkeypatch.setattr(
        service, "agent_on_roster",
        lambda agent_name, email, include_owned=False: not agent_name.startswith("off-")
    )

    calls: list[tuple] = []

    async def _fake_chat(agent_name, message, email, session_id=None, include_owned=False, **kw):
        calls.append(("chat", agent_name, email))
        return {"response": "ok", "session_id": "s1", "cost": 0.0}

    async def _fake_upload(agent_name, email, filename, data, include_owned=False):
        calls.append(("upload", agent_name, email))
        return {"filename": filename, "size_bytes": len(data), "path": f"/inbox/{filename}"}

    monkeypatch.setattr(service, "portal_chat", _fake_chat)
    monkeypatch.setattr(service, "portal_upload_document", _fake_upload)

    try:
        yield portal_router, calls
    finally:
        rate_limiter.clear_inprocess()


class _Body:
    """Minimal PortalChatRequest stand-in (the fields the router reads)."""

    def __init__(self, message="hi", session_id=None, new_thread=False):
        self.message = message
        self.session_id = session_id
        # ent#451 — the router forwards this to both turn entry points. A
        # stand-in missing it fails with AttributeError rather than a useful
        # assertion, which is the cost of hand-rolling a model double.
        self.new_thread = new_thread


class _Upload:
    """UploadFile stand-in that records whether its body was ever read.

    `read()` is what materialises the spooled body in RAM, so a rejected upload
    must not call it. Note what this does NOT prove: Starlette parses the
    multipart form before the endpoint function runs, so the bytes have already
    been transferred and spooled by then. This pins the handler's ordering, not
    a transfer saving — no in-handler gate can deliver one.
    """

    def __init__(self, filename="report.pdf"):
        self.filename = filename
        self.read_count = 0

    async def read(self):
        self.read_count += 1
        return b"x" * 10


# ent#358: the gated endpoints take the PRINCIPAL, not a bare email — the scope
# decision needs `is_platform` (a platform session's roster includes the agents
# it owns; a client's does not). These tests exercise the client side, so
# is_platform=False, which is also the value that keeps the old expectations
# meaningful: an off-roster agent must still 404 before the limiter runs.
def _principal(email):
    from client_portal.portal_auth import PortalPrincipal
    return PortalPrincipal(email=email, is_platform=False)


def _chat(portal_router, agent="atlas", email="bob@example.com"):
    return asyncio.run(portal_router.portal_chat(agent, _Body(), principal=_principal(email)))


def _upload(portal_router, agent="atlas", email="bob@example.com", upload=None):
    upload = upload or _Upload()
    return asyncio.run(portal_router.portal_upload(agent, file=upload, principal=_principal(email)))


def _raises_429(fn, *args, **kwargs):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        fn(*args, **kwargs)
    assert exc.value.status_code == 429, f"expected 429, got {exc.value.status_code}"
    return exc.value


# ---------------------------------------------------------------------------
# Chat — the money surface
# ---------------------------------------------------------------------------

def test_chat_rejects_past_the_burst_limit_with_a_retryable_429(portal):
    portal_router, calls = portal
    limit = portal_router.PORTAL_CHAT_BURST_LIMIT

    for _ in range(limit):
        _chat(portal_router)
    assert len(calls) == limit, "every turn within the limit must reach the service"

    exc = _raises_429(_chat, portal_router)
    # Retryable, not a dead end: the client must be told when to come back.
    assert exc.headers.get("Retry-After"), "429 must carry Retry-After"
    assert int(exc.headers["Retry-After"]) > 0
    assert len(calls) == limit, "a rejected turn must never reach the service (or the model)"


def test_chat_budget_is_per_agent_not_global(portal):
    """Exhausting one agent must not silence the client's other agents."""
    portal_router, calls = portal
    for _ in range(portal_router.PORTAL_CHAT_BURST_LIMIT):
        _chat(portal_router, agent="atlas")
    _raises_429(_chat, portal_router, agent="atlas")

    _chat(portal_router, agent="borealis")
    assert ("chat", "borealis", "bob@example.com") in calls


def test_chat_budget_is_per_client_not_shared(portal):
    """One noisy client must not lock every other client out of the agent."""
    portal_router, calls = portal
    for _ in range(portal_router.PORTAL_CHAT_BURST_LIMIT):
        _chat(portal_router, email="bob@example.com")
    _raises_429(_chat, portal_router, email="bob@example.com")

    _chat(portal_router, email="carol@example.com")
    assert ("chat", "atlas", "carol@example.com") in calls


def test_chat_is_never_tighter_than_the_voice_surfaces(portal):
    """Voice mode is one /chat + one /tts per turn (ent#78).

    If chat were limited below the voice limit, voice mode would 429 on the chat
    leg before /tts ever hit its own — the feature would break at a limit chosen
    for a different surface.
    """
    portal_router, _ = portal
    assert portal_router.PORTAL_CHAT_BURST_LIMIT >= _VOICE_SURFACE_LIMIT


def test_the_sustained_tier_actually_binds(portal):
    """A per-minute limit alone is not a budget bound.

    20/min permits 1200 turns/hour — so a burst-only control would not achieve
    what this issue asked for. The hourly ceiling must be strictly below what
    the burst tier would allow over the same hour, or it is decoration.
    """
    portal_router, _ = portal
    assert (
        portal_router.PORTAL_CHAT_HOURLY_LIMIT < portal_router.PORTAL_CHAT_BURST_LIMIT * 60
    ), "the hourly tier must bind before an hour of uninterrupted bursting"
    assert (
        portal_router.PORTAL_UPLOAD_HOURLY_LIMIT < portal_router.PORTAL_UPLOAD_BURST_LIMIT * 60
    )


def test_hourly_tier_rejects_once_its_ceiling_is_reached(monkeypatch, portal):
    """Drive the sustained tier directly by lowering it under the burst tier."""
    portal_router, calls = portal
    monkeypatch.setattr(portal_router, "PORTAL_CHAT_HOURLY_LIMIT", 3)

    for _ in range(3):
        _chat(portal_router)
    exc = _raises_429(_chat, portal_router)
    # An hourly rejection must still be retryable, and its wait is the long one.
    assert int(exc.headers["Retry-After"]) > 60
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# Upload — the disk / work surface
# ---------------------------------------------------------------------------

def test_upload_rejects_past_the_burst_limit(portal):
    portal_router, calls = portal
    limit = portal_router.PORTAL_UPLOAD_BURST_LIMIT

    for _ in range(limit):
        _upload(portal_router)
    assert len(calls) == limit

    exc = _raises_429(_upload, portal_router)
    assert exc.headers.get("Retry-After")
    assert len(calls) == limit


def test_upload_budget_is_shared_across_agents(portal):
    """Upload is keyed on email alone — the bounded cost is the operator's disk
    and container work, which a client can spread over every rostered agent.

    This is the deliberate difference from chat: chat spends a *particular*
    agent's budget, so it is scoped per (client, agent); upload spends the
    installation's, so scoping it per agent would let a client with N agents do
    N times the damage.
    """
    portal_router, _ = portal
    for _ in range(portal_router.PORTAL_UPLOAD_BURST_LIMIT):
        _upload(portal_router, agent="atlas")

    _raises_429(_upload, portal_router, agent="borealis")


def test_a_rejected_upload_never_reads_the_request_body(portal):
    """A rejected upload must not materialise the spooled body in RAM.

    (The transfer itself already happened during multipart parsing — see
    `_Upload`. This pins ordering inside the handler, nothing upstream of it.)
    """
    portal_router, _ = portal
    for _ in range(portal_router.PORTAL_UPLOAD_BURST_LIMIT):
        _upload(portal_router)

    spy = _Upload()
    _raises_429(_upload, portal_router, upload=spy)
    assert spy.read_count == 0, "the limiter must be charged before the body is read"


# ---------------------------------------------------------------------------
# The gate ordering that keeps the limiter from becoming an amplifier
# ---------------------------------------------------------------------------

def test_off_roster_agent_is_404_before_the_limiter_runs(portal):
    """Keying a limiter on an unvalidated path param is an amplifier, not a
    control: each distinct `agent_name` mints its own key, holding memory for
    its window while never tripping a limit. The roster gate must come first,
    and the miss must stay a uniform 404 (no existence oracle).
    """
    from fastapi import HTTPException
    from services import rate_limiter

    portal_router, calls = portal

    for i in range(portal_router.PORTAL_CHAT_BURST_LIMIT * 3):
        with pytest.raises(HTTPException) as exc:
            _chat(portal_router, agent=f"off-{i}")
        assert exc.value.status_code == 404

    assert not calls, "an off-roster turn must never reach the service"
    assert not rate_limiter._inprocess_buckets, (
        "an off-roster request must create no limiter state at all"
    )


def test_off_roster_upload_is_404_and_reads_no_body(portal):
    from fastapi import HTTPException
    from services import rate_limiter

    portal_router, calls = portal
    spy = _Upload()

    with pytest.raises(HTTPException) as exc:
        _upload(portal_router, agent="off-nope", upload=spy)
    assert exc.value.status_code == 404
    assert spy.read_count == 0
    assert not calls
    assert not rate_limiter._inprocess_buckets


def test_a_rejected_burst_does_not_also_burn_the_hourly_budget(monkeypatch, portal):
    """Ordering: burst is charged first and returns on rejection, so a client
    held at the per-minute limit still has their hourly allowance intact when
    the minute rolls over."""
    from services import rate_limiter

    portal_router, _ = portal
    monkeypatch.setattr(portal_router, "PORTAL_CHAT_BURST_LIMIT", 2)

    _chat(portal_router)
    _chat(portal_router)
    for _ in range(5):
        _raises_429(_chat, portal_router)

    hourly = rate_limiter._inprocess_buckets["portal_chat_hourly:bob@example.com:atlas"]
    assert len(hourly) == 2, (
        "only the two accepted turns may count against the hourly window; "
        f"{len(hourly)} recorded"
    )
