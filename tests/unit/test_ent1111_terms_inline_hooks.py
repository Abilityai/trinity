"""Inline acceptance hooks (abilityai/trinity-enterprise#1111, T1).

Related flow: docs/memory/feature-flows/terms-of-use.md

Every acceptance consumer ran through FastAPI `BackgroundTasks` — i.e. AFTER
the response. A consumer whose work the person is about to look for (a share
that has to be on their roster when the Workspace re-reads it at the redirect)
therefore always lost that race: the Workspace fetches its roster once, right
after accepting, and nothing re-reads it.

`register_acceptance_hook(fn, inline=True)` opts a consumer into being awaited
BEFORE the response, under one ceiling (`TERMS_INLINE_HOOK_BUDGET_S`, 5 s).
Three properties are the whole design, and each has a test here:

* **in time** — the hook's work is done before the response body exists;
* **past the ceiling** — the acceptance answers 200 anyway, and the hook is
  NOT cancelled: it runs on as a shielded background task and reaches its own
  compensation. A `wait_for` over the work itself would cancel a consumer
  mid-mutation, which is exactly what this shape exists to avoid;
* **it raises** — logged and audited, acceptance unaffected.

A consumer never fails or delays an acceptance beyond the ceiling.
"""
from __future__ import annotations

import asyncio
import pathlib
import time

import pytest
from fastapi import APIRouter, Depends, FastAPI
from starlette.testclient import TestClient

pytestmark = pytest.mark.unit

BACKEND = pathlib.Path(__file__).resolve().parents[2] / "src" / "backend"

ADMIN = {"id": 41, "username": "admin", "email": "admin@example.com",
         "role": "admin", "suspended_at": None}


@pytest.fixture(autouse=True)
def clean_terms():
    """No terms configured, no acceptance rows, no provider, no hooks."""
    from database import db
    from db.engine import get_engine
    from services import terms_gate, terms_service

    def reset():
        terms_gate.clear_provider()
        terms_service.clear_acceptance_hooks()
        for key in ("terms_required", "terms_version", "terms_text", "terms_title"):
            try:
                db.delete_setting(key)
            except Exception:  # noqa: BLE001
                pass
        with get_engine().begin() as conn:
            conn.exec_driver_sql("DELETE FROM terms_acceptances")

    reset()
    yield
    reset()


@pytest.fixture
def audits(monkeypatch):
    """Every audit row the acceptance writes, by action."""
    from services import platform_audit_service as pas

    seen = []

    async def _log(**kwargs):
        seen.append(kwargs)
        return "audit-1"

    monkeypatch.setattr(pas.platform_audit_service, "log", _log)
    return seen


@pytest.fixture
def jwt_as(monkeypatch):
    """A real platform JWT whose `sub` resolves to `row`."""
    import database
    from config import ALGORITHM, SECRET_KEY
    from jose import jwt

    def make(row):
        monkeypatch.setattr(
            database.db, "get_user_by_username",
            lambda name, *_a, **_k: dict(row, username=name))
        return jwt.encode(
            {"sub": row["username"], "exp": int(time.time()) + 300, "interactive": True},
            SECRET_KEY, algorithm=ALGORITHM)

    return make


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _app():
    """The real terms router through the real `get_current_user`."""
    import dependencies as deps
    from routers.terms import router as terms_router

    probe = APIRouter(prefix="/api/probe")

    @probe.get("/plain")
    async def plain(user=Depends(deps.get_current_user)):
        return {"who": user.username}

    app = FastAPI()
    app.include_router(terms_router)
    app.include_router(probe)
    return TestClient(app)


def _accept(client, token):
    version = client.get("/api/terms/current", headers=_bearer(token)).json()["version"]
    return client.post("/api/terms/accept", json={"version": version}, headers=_bearer(token))


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #

class TestRegistry:
    def test_the_two_kinds_are_separate_lists(self):
        from services import terms_service as ts

        def bg(*_a):
            pass

        async def inline(*_a):
            pass

        ts.register_acceptance_hook(bg)
        ts.register_acceptance_hook(inline, inline=True)

        assert ts.acceptance_hooks() == (bg,)
        assert ts.inline_acceptance_hooks() == (inline,)

    def test_registration_is_idempotent(self):
        from services import terms_service as ts

        async def inline(*_a):
            pass

        ts.register_acceptance_hook(inline, inline=True)
        ts.register_acceptance_hook(inline, inline=True)
        assert ts.inline_acceptance_hooks() == (inline,)

    def test_clearing_drops_both(self):
        from services import terms_service as ts

        ts.register_acceptance_hook(lambda *_a: None)
        ts.register_acceptance_hook(lambda *_a: None, inline=True)
        ts.clear_acceptance_hooks()
        assert ts.acceptance_hooks() == () and ts.inline_acceptance_hooks() == ()

    def test_the_ceiling_is_five_seconds(self):
        from services import terms_service as ts

        assert ts.TERMS_INLINE_HOOK_BUDGET_S == 5


# --------------------------------------------------------------------------- #
# In time: the work is done before the response exists
# --------------------------------------------------------------------------- #

class TestInTime:
    @pytest.mark.asyncio
    async def test_the_hook_finishes_before_the_response_body_is_built(self, audits, jwt_as):
        """The route awaits the inline runner, then builds its return value —
        so by the time the body exists the hook's write has landed. The
        background hook has not run yet: it is only scheduled."""
        from fastapi import BackgroundTasks

        from models import TermsAccept
        from routers import terms as terms_router
        from services import terms_service as ts

        order = []

        async def inline(user_id, email, version, accepted_at):
            order.append(("inline", user_id, email))

        def bg(*_a):
            order.append(("background",))

        ts.register_acceptance_hook(inline, inline=True)
        ts.register_acceptance_hook(bg)

        user = _principal()
        background = BackgroundTasks()
        request = _request()

        out = await terms_router.accept_terms(
            TermsAccept(version=ts.current_terms()["version"]), request, background,
            current_user=user)

        assert out["created"] is True
        assert [step[0] for step in order] == ["inline"], (
            "the inline hook must have run, and the background one must not")
        assert order[0][2] == user.email
        # The background hook is scheduled, not run.
        assert len(background.tasks) == 1
        await background()
        assert [step[0] for step in order] == ["inline", "background"]

    def test_through_the_real_route_inline_precedes_background(self, audits, jwt_as):
        """Production wiring: the route the Workspace actually calls."""
        from services import terms_service as ts

        order = []

        async def inline(*_a):
            order.append("inline")

        def bg(*_a):
            order.append("background")

        ts.register_acceptance_hook(inline, inline=True)
        ts.register_acceptance_hook(bg)

        client = _app()
        r = _accept(client, jwt_as(ADMIN))

        assert r.status_code == 200, r.text
        # Through the real route: inline first, background after. (The response
        # lands between them; `TestClient` drains the background tasks before
        # handing the response back, so the split is asserted at the route
        # function in the test above, where the return value is observable.)
        assert order == ["inline", "background"]

    def test_a_sync_inline_hook_is_tolerated(self, audits, jwt_as):
        from services import terms_service as ts

        ran = []
        ts.register_acceptance_hook(lambda *_a: ran.append("sync"), inline=True)

        assert _accept(_app(), jwt_as(ADMIN)).status_code == 200
        assert ran == ["sync"]

    def test_a_repeat_acceptance_runs_neither_kind(self, audits, jwt_as):
        from services import terms_service as ts

        ran = []
        ts.register_acceptance_hook(lambda *_a: ran.append("bg"))
        ts.register_acceptance_hook(_appender(ran, "inline"), inline=True)

        client, token = _app(), jwt_as(ADMIN)
        assert _accept(client, token).status_code == 200
        assert ran == ["inline", "bg"]

        assert _accept(client, token).json()["created"] is False
        assert ran == ["inline", "bg"], "no new row ⇒ no hooks"


# --------------------------------------------------------------------------- #
# Past the ceiling: answered anyway, and never cancelled
# --------------------------------------------------------------------------- #

class TestTheCeiling:
    @pytest.mark.asyncio
    async def test_a_slow_hook_does_not_hold_the_acceptance(self, audits, monkeypatch):
        from services import terms_service as ts

        monkeypatch.setattr(ts, "TERMS_INLINE_HOOK_BUDGET_S", 0.05)
        started, finished = asyncio.Event(), asyncio.Event()

        async def slow(*_a):
            started.set()
            await asyncio.sleep(0.4)
            finished.set()

        ts.register_acceptance_hook(slow, inline=True)

        t0 = time.monotonic()
        await ts.run_inline_acceptance_hooks(1, "a@example.com", "v1", "2026-11-06T00:00:00Z")
        elapsed = time.monotonic() - t0

        assert started.is_set()
        assert elapsed < 0.3, f"the runner waited {elapsed:.2f}s past its 0.05s ceiling"
        assert not finished.is_set(), "the hook should still be running"
        # The wait is a CEILING, not a cancellation: the task runs to the end.
        await asyncio.wait_for(finished.wait(), 2)
        assert [a["event_action"] for a in audits] == ["terms_hook_slow"]
        assert audits[0]["details"]["user_id"] == 1

    @pytest.mark.asyncio
    async def test_the_slow_hook_is_never_cancelled(self, audits, monkeypatch):
        """A `wait_for` over the work itself would cancel a consumer
        mid-mutation. The shield is what makes that impossible."""
        from services import terms_service as ts

        monkeypatch.setattr(ts, "TERMS_INLINE_HOOK_BUDGET_S", 0.05)
        outcome = []

        async def slow(*_a):
            try:
                await asyncio.sleep(0.3)
                outcome.append("completed")
            except asyncio.CancelledError:
                outcome.append("cancelled")
                raise

        ts.register_acceptance_hook(slow, inline=True)
        await ts.run_inline_acceptance_hooks(1, "a@example.com", "v1", None)
        await asyncio.sleep(0.5)

        assert outcome == ["completed"]

    @pytest.mark.asyncio
    async def test_one_ceiling_covers_every_hook(self, audits, monkeypatch):
        """The budget is per acceptance, not per hook: three slow hooks cannot
        add up to three ceilings on one request."""
        from services import terms_service as ts

        monkeypatch.setattr(ts, "TERMS_INLINE_HOOK_BUDGET_S", 0.1)
        done = []

        async def slow(*_a):
            await asyncio.sleep(0.25)
            done.append(1)

        for _ in range(3):
            ts.register_acceptance_hook(_distinct(slow), inline=True)

        t0 = time.monotonic()
        await ts.run_inline_acceptance_hooks(1, "a@example.com", "v1", None)
        elapsed = time.monotonic() - t0

        assert elapsed < 0.25, f"three hooks spent {elapsed:.2f}s against one 0.1s ceiling"
        # All three were STARTED — past the ceiling they finish in the background.
        await asyncio.sleep(0.5)
        assert len(done) == 3

    def test_the_route_still_answers_200_when_the_ceiling_is_spent(
            self, audits, jwt_as, monkeypatch):
        from services import terms_service as ts

        monkeypatch.setattr(ts, "TERMS_INLINE_HOOK_BUDGET_S", 0.05)

        async def slow(*_a):
            await asyncio.sleep(0.3)

        ts.register_acceptance_hook(slow, inline=True)

        r = _accept(_app(), jwt_as(ADMIN))
        assert r.status_code == 200, r.text
        assert r.json()["created"] is True
        assert "terms_hook_slow" in [a["event_action"] for a in audits]


# --------------------------------------------------------------------------- #
# It raises
# --------------------------------------------------------------------------- #

class TestItRaises:
    @pytest.mark.asyncio
    async def test_a_raising_hook_is_audited_and_swallowed(self, audits):
        from services import terms_service as ts

        async def boom(*_a):
            raise RuntimeError("the spare went away")

        ts.register_acceptance_hook(boom, inline=True)
        await ts.run_inline_acceptance_hooks(7, "a@example.com", "v1", None)

        failed = [a for a in audits if a["event_action"] == "terms_hook_failed"]
        assert len(failed) == 1
        assert failed[0]["details"]["error"] == "RuntimeError"
        assert failed[0]["details"]["user_id"] == 7
        assert "boom" in failed[0]["details"]["hook"]

    @pytest.mark.asyncio
    async def test_a_raising_hook_does_not_stop_the_next_one(self, audits):
        from services import terms_service as ts

        ran = []

        async def boom(*_a):
            raise RuntimeError("no")

        async def after(*_a):
            ran.append("after")

        ts.register_acceptance_hook(boom, inline=True)
        ts.register_acceptance_hook(after, inline=True)
        await ts.run_inline_acceptance_hooks(1, "a@example.com", "v1", None)

        assert ran == ["after"]

    def test_the_acceptance_still_succeeds(self, audits, jwt_as):
        from services import terms_service as ts

        async def boom(*_a):
            raise RuntimeError("no")

        ts.register_acceptance_hook(boom, inline=True)

        r = _accept(_app(), jwt_as(ADMIN))
        assert r.status_code == 200, r.text
        assert r.json()["created"] is True

    @pytest.mark.asyncio
    async def test_no_inline_hook_is_a_no_op(self, audits):
        from services import terms_service as ts

        await ts.run_inline_acceptance_hooks(1, "a@example.com", "v1", None)
        assert audits == []


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _appender(sink, label):
    async def hook(*_a):
        sink.append(label)
    return hook


def _distinct(fn):
    """A fresh function object, so registration's idempotency does not collapse
    three registrations of the same coroutine function into one."""
    async def hook(*a):
        await fn(*a)
    return hook


def _principal():
    from models import User

    return User(id=41, username="admin", email="admin@example.com", role="admin")


def _request():
    from types import SimpleNamespace

    return SimpleNamespace(
        client=SimpleNamespace(host="10.0.0.9"),
        state=SimpleNamespace(request_id="req-1"),
        scope={"path": "/api/terms/accept"},
    )
