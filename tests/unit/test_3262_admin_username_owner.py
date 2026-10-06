"""#3262 — the system agent, Cornelius and the default system seed are owned by
the platform's admin account, whatever ADMIN_USERNAME names it.

All three hard-coded the username "admin". `_ensure_admin_user` and /setup
create the admin as `utils.admin_identity.admin_username()`, which honours
ADMIN_USERNAME, so on an `ADMIN_USERNAME=root` install:

* the system agent was never created ("Admin user 'admin' not found");
* the Cornelius seed and the default system seed deferred forever, silently,
  as if setup had not run yet.

#2381 fixed the same mismatch in routers/setup.py and named this class.

Existing installs are unaffected: an `agent_ownership` row that already exists
is never re-owned by `register_agent_owner` (IntegrityError → is_system only).
"""
from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

# Imported at collection time while sys.modules is clean, then pinned per test
# (the #762 cross-file contamination convention, see test_1816_*).
import services.cornelius_agent_service as cas
import services.system_agent_service as sas
import services.system_seed_service as sss

pytestmark = pytest.mark.unit

ROOT = {"id": 7, "username": "root", "email": "root@example.com", "role": "admin"}


@pytest.fixture(autouse=True)
def _root_admin(monkeypatch):
    for name, module in {
        "services.cornelius_agent_service": cas,
        "services.system_agent_service": sas,
        "services.system_seed_service": sss,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setenv("ADMIN_USERNAME", "root")


class _Users:
    """A user table holding only `root`; records every username asked for."""

    def __init__(self):
        self.asked = []

    def __call__(self, username):
        self.asked.append(username)
        return dict(ROOT) if username == "root" else None


class _Settings:
    def __init__(self):
        self.store = {}

    def get(self, key, default=None):
        return self.store.get(key, default)

    def set(self, key, value):
        self.store[key] = value


# --- system agent ---------------------------------------------------------------

def test_system_agent_create_looks_up_the_configured_admin(monkeypatch):
    users = _Users()
    db = MagicMock()
    db.get_user_by_username = users
    monkeypatch.setattr(sas, "db", db)
    # Stop right after the owner check: a missing template is the next failure.
    monkeypatch.setattr(sas, "Path", lambda p: types.SimpleNamespace(
        exists=lambda: False, __truediv__=lambda self, o: self,
    ))
    with pytest.raises(Exception) as ei:
        asyncio.run(sas.system_agent_service._create_system_agent())
    assert users.asked and users.asked[0] == "root"
    assert "not found" not in str(ei.value), "the owner check passed; only the template stub stopped it"


def test_system_agent_create_names_the_missing_admin(monkeypatch):
    db = MagicMock()
    db.get_user_by_username = lambda u: None
    monkeypatch.setattr(sas, "db", db)
    with pytest.raises(ValueError, match="'root' not found"):
        asyncio.run(sas.system_agent_service._create_system_agent())


def test_running_system_agent_is_registered_to_the_configured_admin(monkeypatch):
    container = types.SimpleNamespace(status="running")
    db = MagicMock()
    monkeypatch.setattr(sas, "db", db)
    monkeypatch.setattr(sas, "get_agent_container", lambda name: container)
    monkeypatch.setattr(sas, "container_reload", AsyncMock())
    monkeypatch.setattr(sas, "check_base_image_state", AsyncMock(return_value="match"))
    asyncio.run(sas.system_agent_service.ensure_deployed())
    db.register_agent_owner.assert_called_once_with(sas.SYSTEM_AGENT_NAME, "root", is_system=True)


# --- Cornelius seed -------------------------------------------------------------

def test_cornelius_seeds_under_the_configured_admin(monkeypatch):
    users, settings = _Users(), _Settings()
    monkeypatch.setattr(cas.db, "get_setting_value", settings.get)
    monkeypatch.setattr(cas.db, "set_setting", settings.set)
    monkeypatch.setattr(cas.db, "count_non_system_agents", lambda: 0)
    monkeypatch.setattr(cas.db, "get_user_by_username", users)
    monkeypatch.setattr(cas, "docker_client", object())
    monkeypatch.setattr(cas, "get_breaker_redis", lambda: None)
    provision = AsyncMock()
    monkeypatch.setattr(cas.cornelius_agent_service, "_provision", provision)

    result = asyncio.run(cas.cornelius_agent_service.ensure_seeded())

    assert result["action"] == "created", result
    assert users.asked == ["root"]
    provision.assert_awaited_once()
    assert provision.await_args.args[0].username == "root"


# --- default system seed --------------------------------------------------------

def test_system_seed_looks_up_the_configured_admin(monkeypatch):
    users, settings = _Users(), _Settings()
    monkeypatch.setattr(sss.db, "get_setting_value", settings.get)
    monkeypatch.setattr(sss.db, "set_setting", settings.set)
    monkeypatch.setattr(sss.db, "count_non_system_agents", lambda: 0)
    monkeypatch.setattr(sss.db, "get_user_by_username", users)
    monkeypatch.setattr(sss, "docker_client", object())
    monkeypatch.setattr(sss, "get_breaker_redis", lambda: None)
    # Stop right after the owner check: an unresolvable manifest is the next step.
    monkeypatch.setattr(
        sss.system_seed_service, "_resolve_manifest", lambda override: (None, "test", None),
    )

    result = asyncio.run(sss.system_seed_service.ensure_seeded(fresh=True))

    assert users.asked == ["root"]
    assert result.get("action") != "deferred", (
        "with ADMIN_USERNAME=root and a root admin, the seed must not wait for setup forever"
    )
