"""The boot result sweeps must not hold agent-server startup (#4643).

Both sweeps sleep a random 0-60 s jitter (#1085) before delivering anything.
They used to be awaited inside a FastAPI startup handler, and uvicorn accepts no
connection until every startup handler returns, so `/health` answered up to 60 s
late on every boot of an agent whose pending folder exists. The jitter must delay
only the resend, not the server.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

_AGENT_SERVER = (
    Path(__file__).resolve().parent.parent.parent
    / "docker" / "base-image" / "agent_server"
)
if str(_AGENT_SERVER.parent) not in sys.path:
    sys.path.insert(0, str(_AGENT_SERVER.parent))

pull_worker = pytest.importorskip("agent_server.services.pull_worker")
result_callback = pytest.importorskip("agent_server.services.result_callback")


class _FakeApp:
    def __init__(self):
        self.handlers: Dict[str, Any] = {}

    def on_event(self, name):
        def _register(fn):
            self.handlers[name] = fn
            return fn
        return _register


def test_result_callback_startup_returns_before_the_sweep_finishes(monkeypatch):
    finished: list = []

    monkeypatch.setattr(result_callback, "_callbacks_configured", lambda: True)

    async def _drive():
        gate, began = asyncio.Event(), asyncio.Event()

        async def _sweep():  # stands in for the 0-60 s jitter + delivery
            began.set()
            await gate.wait()
            finished.append(True)

        monkeypatch.setattr(result_callback, "resend_pending_results", _sweep)
        app = _FakeApp()
        result_callback.schedule_pending_result_resend(app)
        await asyncio.wait_for(app.handlers["startup"](), timeout=1)
        assert finished == [], "startup returned only after the sweep finished"
        await asyncio.wait_for(began.wait(), timeout=1)  # the sweep still runs
        gate.set()
        for _ in range(50):
            await asyncio.sleep(0)
            if finished:
                break
        assert finished == [True]

    asyncio.run(_drive())


def test_pull_startup_returns_before_the_sweep_and_the_tick_follows_it(monkeypatch):
    monkeypatch.setattr(pull_worker, "_worker_creds_present", lambda: True)
    monkeypatch.setattr(pull_worker, "_RESEND_TICK_SECONDS", 0.0)
    pull_worker._RESEND_TASKS.clear()
    calls: list = []

    async def _drive():
        gate = asyncio.Event()

        async def _sweep():
            calls.append("sweep")
            if len(calls) == 1:  # the boot sweep is the slow (jittered) one
                await gate.wait()

        monkeypatch.setattr(pull_worker, "resend_pending_pull_results", _sweep)
        app = _FakeApp()
        pull_worker.schedule_pending_pull_result_resend(app)
        await asyncio.wait_for(app.handlers["startup"](), timeout=1)
        assert len(pull_worker._RESEND_TASKS) == 1
        task = pull_worker._RESEND_TASKS[0]
        for _ in range(20):
            await asyncio.sleep(0)
        assert calls == ["sweep"], "the tick must wait for the boot sweep"
        gate.set()
        for _ in range(200):
            await asyncio.sleep(0)
            if len(calls) >= 3:
                break
        assert len(calls) >= 3, "the periodic tick must start after the boot sweep"
        await app.handlers["shutdown"]()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert pull_worker._RESEND_TASKS == []

    asyncio.run(_drive())
