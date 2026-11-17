"""#3966 — the pre-check hook is told which schedule is firing.

`~/.trinity/pre-check` runs before every cron schedule on an agent, and its
non-empty stdout replaces the firing schedule's message. Before this fix the
scheduler sent the backend only the agent name, and the backend exec'd the hook
with no arguments and no environment, so an agent with two cron schedules could
not gate one of them without rewriting the other's task (#955; a daily brief ran
the hourly refresh's `/update-dashboard` and was recorded as `success`).

Now the scheduler sends the firing schedule's id, name and message, and the
backend exports them to the hook as TRINITY_SCHEDULE_ID / TRINITY_SCHEDULE_NAME
/ TRINITY_SCHEDULE_MESSAGE. A request without them (an older scheduler, or any
other caller) runs the hook exactly as before.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
# `src.scheduler` imports as a package (relative imports), so the repo root
# must be importable too — appended, so `database` still resolves to the backend.
if str(_REPO) not in sys.path:
    sys.path.append(str(_REPO))

pytestmark = pytest.mark.unit

AGENT = "cos-agent"
HOURLY = SimpleNamespace(
    id="s-hourly",
    name="Hourly refresh",
    agent_name=AGENT,
    message="/update-dashboard",
)
BRIEF = SimpleNamespace(
    id="s-brief",
    name="Morning brief",
    agent_name=AGENT,
    message="/morning-brief",
)
_HDR = {"X-Internal-Secret": "unit-secret"}


# ---------------------------------------------------------------------------
# Scheduler → backend: the firing schedule travels with the pre-check request
# ---------------------------------------------------------------------------


def _scheduler():
    import src.scheduler.service as ssvc

    return ssvc, object.__new__(ssvc.SchedulerService)


def _backend_answering(body):
    response = MagicMock(status_code=200)
    response.json = MagicMock(return_value=body)
    post = AsyncMock(return_value=response)
    client = AsyncMock()
    client.__aenter__.return_value.post = post
    return post, client


class TestSchedulerSendsTheFiringSchedule:
    def test_each_cron_schedule_reaches_the_backend_with_its_own_identity(self):
        ssvc, svc = _scheduler()
        post, client = _backend_answering({"hook_present": False})
        with patch.object(ssvc.httpx, "AsyncClient", return_value=client):
            for schedule in (HOURLY, BRIEF):
                assert asyncio.run(svc._apply_pre_check_gate(schedule, "schedule")) == (
                    True,
                    schedule.message,
                )

        assert [c.kwargs["json"] for c in post.call_args_list] == [
            {
                "schedule_id": "s-hourly",
                "schedule_name": "Hourly refresh",
                "schedule_message": "/update-dashboard",
            },
            {
                "schedule_id": "s-brief",
                "schedule_name": "Morning brief",
                "schedule_message": "/morning-brief",
            },
        ]

    def test_a_call_without_a_schedule_sends_the_same_bodyless_request_as_before(self):
        ssvc, svc = _scheduler()
        post, client = _backend_answering({"hook_present": False})
        with patch.object(ssvc.httpx, "AsyncClient", return_value=client):
            assert asyncio.run(svc._run_pre_check(AGENT)) is None

        assert post.call_args.kwargs.get("json") is None

    def test_the_hooks_stdout_contract_is_unchanged(self):
        """Non-empty stdout still overrides; empty stdout still skips."""
        ssvc, svc = _scheduler()
        for stdout, expected in (
            ("/morning-brief\n", {"fire": True, "message": "/morning-brief"}),
            ("\n", {"fire": False, "reason": "pre-check returned empty stdout"}),
        ):
            _, client = _backend_answering(
                {"hook_present": True, "exit_code": 0, "stdout": stdout, "stderr": ""}
            )
            with patch.object(ssvc.httpx, "AsyncClient", return_value=client):
                assert asyncio.run(svc._run_pre_check(AGENT, BRIEF)) == expected


# ---------------------------------------------------------------------------
# Backend → hook: the schedule is exported in the exec's environment
# ---------------------------------------------------------------------------


def _hook_execs(monkeypatch, stdout="/morning-brief\n"):
    """Record every exec `run_pre_check` makes; the hook prints `stdout`."""
    from services import pre_check_service as pcs

    calls = []

    async def fake_exec(
        container_name, command, timeout=60, *, environment=None, user="developer"
    ):
        calls.append({"command": command, "environment": environment})
        if command == pcs.HOOK_PATH:
            return {"exit_code": 0, "output": stdout, "timed_out": False}
        return {"exit_code": 0, "output": "", "timed_out": False}  # `test -f`: present

    monkeypatch.setattr(pcs, "execute_command_in_container", fake_exec)
    monkeypatch.setattr(pcs, "get_agent_container", lambda name: object())
    return pcs, calls


def _hook_env(pcs, calls):
    hook = [c for c in calls if c["command"] == pcs.HOOK_PATH]
    assert len(hook) == 1, calls
    return hook[0]["environment"]


class TestBackendExportsTheSchedule:
    def test_the_hook_runs_with_the_firing_schedule_in_its_environment(
        self, monkeypatch
    ):
        pcs, calls = _hook_execs(monkeypatch)
        out = asyncio.run(
            pcs.run_pre_check(
                AGENT,
                schedule_id="s-brief",
                schedule_name="Morning brief",
                schedule_message="/morning-brief",
            )
        )

        assert _hook_env(pcs, calls) == {
            "TRINITY_SCHEDULE_ID": "s-brief",
            "TRINITY_SCHEDULE_NAME": "Morning brief",
            "TRINITY_SCHEDULE_MESSAGE": "/morning-brief",
        }
        assert out == {
            "hook_present": True,
            "exit_code": 0,
            "stdout": "/morning-brief\n",
            "stderr": "",
        }

    def test_without_a_schedule_the_hook_runs_exactly_as_before(self, monkeypatch):
        pcs, calls = _hook_execs(monkeypatch)
        asyncio.run(pcs.run_pre_check(AGENT))

        assert _hook_env(pcs, calls) is None

    def test_only_the_fields_the_caller_sent_are_exported(self, monkeypatch):
        pcs, calls = _hook_execs(monkeypatch)
        asyncio.run(pcs.run_pre_check(AGENT, schedule_id="s-brief"))

        assert _hook_env(pcs, calls) == {"TRINITY_SCHEDULE_ID": "s-brief"}

    def test_a_message_with_quotes_and_newlines_is_exported_verbatim(self, monkeypatch):
        """The environment is not a shell: nothing is quoted, split or trimmed."""
        message = 'Summarise "today"\n$(rm -rf ~) `x` ; done\n'
        pcs, calls = _hook_execs(monkeypatch)
        asyncio.run(pcs.run_pre_check(AGENT, schedule_message=message))

        assert _hook_env(pcs, calls) == {"TRINITY_SCHEDULE_MESSAGE": message}


# ---------------------------------------------------------------------------
# The internal endpoint carries the body from the scheduler to the service
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", "unit-secret")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers.internal import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def service_calls(monkeypatch):
    from services import pre_check_service as pcs

    calls = []

    async def fake_run_pre_check(agent_name, **schedule):
        calls.append({"agent_name": agent_name, **schedule})
        return {"hook_present": False}

    monkeypatch.setattr(pcs, "run_pre_check", fake_run_pre_check)
    return calls


class TestInternalEndpoint:
    def test_the_schedule_in_the_body_reaches_the_service(self, client, service_calls):
        r = client.post(
            f"/api/internal/agents/{AGENT}/pre-check",
            json={
                "schedule_id": "s-brief",
                "schedule_name": "Morning brief",
                "schedule_message": "/morning-brief",
            },
            headers=_HDR,
        )

        assert r.status_code == 200, r.text
        assert r.json() == {"hook_present": False}
        assert service_calls == [
            {
                "agent_name": AGENT,
                "schedule_id": "s-brief",
                "schedule_name": "Morning brief",
                "schedule_message": "/morning-brief",
            }
        ]

    def test_a_bodyless_request_from_an_older_scheduler_still_runs_the_hook(
        self,
        client,
        service_calls,
    ):
        r = client.post(f"/api/internal/agents/{AGENT}/pre-check", headers=_HDR)

        assert r.status_code == 200, r.text
        assert service_calls == [{"agent_name": AGENT}]
