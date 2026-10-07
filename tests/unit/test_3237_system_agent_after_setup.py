"""#3237 — a fresh install that creates its admin through /setup gets its system agent.

The only deploy attempt used to run at backend boot, BEFORE /setup had created
the admin, and failed with "Admin user 'admin' not found". Nothing retried it,
so `trinity-system` stayed absent until something restarted the backend — on
every one-click / marketplace path (DigitalOcean, AWS, the hosted script).

Under test:

* a successful setup schedules `system_agent_service.ensure_deployed()` as a
  background task, and it runs AFTER the admin row exists;
* it is scheduled BEFORE first-run seeding — the boot order, and the seeder
  hosts its failure alerts on `trinity-system`;
* the deploy task never raises (Starlette runs background tasks in sequence, so
  a raise would also skip the seed and the intake) and is skipped without Docker;
* a refused setup schedules nothing.

Import isolation: `routers.setup` is imported lazily (see
test_setup_operator_profile.py for why).
"""
import asyncio
import sys
import types

import pytest
from fastapi import BackgroundTasks, HTTPException

pytestmark = pytest.mark.unit

PW = "Sup3rSecret!!"
HASH = "$2b$12$abcdefghijklmnopqrstuv"


def _setup():
    import routers.setup as m
    return m


class FakeDB:
    def __init__(self, users=None):
        self.settings = {"setup_completed": "false"}
        self.users = users or {}

    def get_user_by_username(self, username):
        return self.users.get(username)

    def get_setting_value(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def update_user_password(self, username, hashed):
        self.users.setdefault(username, {"username": username})["password"] = hashed
        return True

    def update_user(self, username, updates):
        self.users.setdefault(username, {"username": username}).update(updates)
        return self.users[username]


class FakeSystemAgentService:
    """Mirrors `_create_system_agent`'s precondition: the admin row must exist."""

    def __init__(self, db, raises=None):
        self.db, self.raises, self.calls = db, raises, []

    async def ensure_deployed(self):
        self.calls.append(self.db.get_user_by_username("admin") is not None)
        if self.raises:
            raise self.raises
        return {"action": "created", "status": "running", "message": "System agent created and started"}


@pytest.fixture
def env(monkeypatch):
    def _apply(users=None, docker=True, raises=None):
        setup = _setup()
        db = FakeDB(users)
        svc = FakeSystemAgentService(db, raises)
        monkeypatch.delenv("ADMIN_PASSWORD_SOURCE", raising=False)
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
        monkeypatch.delenv("ADMIN_USERNAME", raising=False)
        monkeypatch.setattr(setup, "db", db)
        monkeypatch.setattr(setup, "validate_password_strength", lambda p: [])
        monkeypatch.setattr(setup, "hash_password", lambda p: "hashed:" + p)
        # The helper imports both lazily; stub the modules it reaches for.
        monkeypatch.setitem(
            sys.modules, "services.system_agent_service",
            types.SimpleNamespace(system_agent_service=svc),
        )
        monkeypatch.setitem(
            sys.modules, "services.docker_service",
            types.SimpleNamespace(docker_client=object() if docker else None),
        )
        return db, svc
    return _apply


def _post(bg):
    setup = _setup()
    data = setup.SetAdminPasswordRequest(
        password=PW, confirm_password=PW, email="me@example.com",
    )
    return asyncio.run(setup.set_admin_password(data, object(), bg))


def _run(task):
    result = task.func(*task.args, **task.kwargs)
    if asyncio.iscoroutine(result):
        asyncio.run(result)


def _names(bg):
    return [t.func.__name__ for t in bg.tasks]


def test_setup_schedules_the_system_agent_deploy_before_seeding(env):
    env()
    bg = BackgroundTasks()
    assert _post(bg)["success"] is True
    names = _names(bg)
    assert "_deploy_system_agent_after_setup" in names, (
        "/setup must deploy the system agent — the boot attempt ran before the admin existed"
    )
    assert names.index("_deploy_system_agent_after_setup") < names.index("ensure_first_run_seeded"), (
        "boot order: the system agent before the seed, which hosts its alerts on trinity-system"
    )


def test_the_deploy_runs_after_the_admin_exists(env):
    _, svc = env()
    bg = BackgroundTasks()
    _post(bg)
    _run(next(t for t in bg.tasks if t.func.__name__ == "_deploy_system_agent_after_setup"))
    assert svc.calls == [True], "ensure_deployed ran once, with the admin row already present"


def test_a_failed_deploy_never_raises(env):
    _, svc = env(raises=RuntimeError("docker unavailable"))
    asyncio.run(_setup()._deploy_system_agent_after_setup())
    assert svc.calls == [False]


def test_the_deploy_is_skipped_without_docker(env):
    _, svc = env(docker=False)
    asyncio.run(_setup()._deploy_system_agent_after_setup())
    assert svc.calls == [], "demo mode (no Docker client): nothing to deploy, as at boot"


def test_a_refused_setup_schedules_nothing(env):
    env(users={"admin": {"username": "admin", "password": HASH}})
    bg = BackgroundTasks()
    with pytest.raises(HTTPException) as ei:
        _post(bg)
    assert ei.value.status_code == 403
    assert bg.tasks == []
