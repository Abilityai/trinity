"""
#3237: the system agent is deployed once /setup has created the admin.

The only other deploy attempt runs at backend startup, which on a fresh install
is before the admin exists, so it fails with "Admin user 'admin' not found" and
nothing retried it until the backend next restarted.

Module: src/backend/routers/setup.py
"""

import asyncio

import pytest
from fastapi import BackgroundTasks

from test_setup_operator_profile import _get_setup, _req, _run, _task_funcs, patched  # noqa: F401

pytestmark = pytest.mark.unit


def test_setup_schedules_system_agent_deploy(patched):
    bg = BackgroundTasks()
    _run(_req(), bg)
    setup = _get_setup()
    funcs = _task_funcs(bg)
    assert setup._deploy_system_agent in funcs
    # After the seed pass: Cornelius and the starter agents come first.
    assert funcs.index(setup.ensure_first_run_seeded) < funcs.index(setup._deploy_system_agent)


def test_system_agent_deploy_failure_never_raises(monkeypatch):
    setup = _get_setup()

    async def boom():
        raise RuntimeError("docker unreachable")

    monkeypatch.setattr(setup.system_agent_service, "ensure_deployed", boom)
    asyncio.run(setup._deploy_system_agent())


def test_system_agent_deploy_calls_ensure_deployed(monkeypatch):
    setup = _get_setup()
    calls = []

    async def ok():
        calls.append(1)
        return {"action": "created", "status": "success", "message": "ok"}

    monkeypatch.setattr(setup.system_agent_service, "ensure_deployed", ok)
    asyncio.run(setup._deploy_system_agent())
    assert calls == [1]
