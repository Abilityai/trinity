"""The company brain's state, read honestly (trinity-enterprise#1409).

The assistant introduces Cornelius as the company brain after the person's
first result and has to say where it stands: not there, not set up yet, ready
(with how many notes), or that it cannot tell. `GET /api/onboarding/brain`
answers that from the agent's own files: the setup marker the template's
bootstrap and `/first-run` write (`~/.trinity-bootstrap.done`), and the notes in
`Brain/` minus the scaffold the template ships with.

True unit tests: no Docker, no agent. The db seam and the agent door are faked;
the route is driven through the real router module.
"""
from __future__ import annotations

import asyncio

import pytest

import services.onboarding_service as onboarding_service
import services.system_seed_service as sss
from models import BrainState, User

pytestmark = pytest.mark.unit

ADMIN_ROW = {"id": 1, "username": "admin", "email": "admin@example.com", "role": "admin"}
USER_ROW = {"id": 2, "username": "bob", "email": "bob@example.com", "role": "user"}


@pytest.fixture()
def admin_user():
    return User(id=1, username="admin", email="admin@example.com", role="admin")


@pytest.fixture(autouse=True)
def no_manifest(monkeypatch):
    monkeypatch.delenv(sss.MANIFEST_ENV_VAR, raising=False)
    monkeypatch.setattr(sss.system_seed_service, "_resolve_manifest",
                        lambda override_raw: (None, "none", None))


def _patch_db(monkeypatch, *, user_row=ADMIN_ROW, metadata=None):
    fake = type("FakeDb", (), {})()
    fake.get_user_by_username = lambda username: user_row
    fake.get_all_agent_metadata = lambda email=None: metadata or {}
    monkeypatch.setattr(onboarding_service, "db", fake)


def _meta(owner_id=1):
    return {"owner_id": owner_id, "is_system": False, "is_shared_with_user": False}


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body


def _file(name, path):
    return {"name": name, "path": path, "type": "file"}


def _dir(name, children):
    return {"name": name, "path": name, "type": "directory", "children": children}


# The vault exactly as the template ships it: no note of the person's own.
SCAFFOLD = [
    _file("README.md", "Brain/README.md"),
    _file("CHANGELOG.md", "Brain/CHANGELOG.md"),
    _dir("00-Inbox", [_file("README.md", "Brain/00-Inbox/README.md")]),
    _dir("02-Permanent", [
        _file("README.md", "Brain/02-Permanent/README.md"),
        _file("_SAMPLE - Permanent Note Template.md", "Brain/02-Permanent/_SAMPLE - Permanent Note Template.md"),
    ]),
    _dir("03-MOCs", [_file(".gitkeep", "Brain/03-MOCs/.gitkeep")]),
    _dir("04-Output", [_dir("Articles", [_file("ARTICLE-INDEX.md", "Brain/04-Output/Articles/ARTICLE-INDEX.md")])]),
    _dir("05-Meta", [_dir("Templates", [_file("MOC-Template.md", "Brain/05-Meta/Templates/MOC-Template.md")])]),
]


def _with_notes(n):
    notes = [_file(f"note-{i}.md", f"Brain/Document Insights/note-{i}.md") for i in range(n)]
    return SCAFFOLD + [_dir("Document Insights", notes + [_file("scan.pdf", "Brain/Document Insights/scan.pdf")])]


class FakeAgent:
    """The agent door: a set-up marker (or not) and a vault listing."""

    def __init__(self, *, marker=True, tree=None, unreachable=False, vault_status=200):
        self.marker = marker
        self.tree = SCAFFOLD if tree is None else tree
        self.unreachable = unreachable
        self.vault_status = vault_status
        self.paths = []

    async def get(self, path, timeout=None, **kwargs):
        from services.agent_client.client import AgentNotReachableError
        self.paths.append(path)
        if self.unreachable:
            raise AgentNotReachableError("down")
        if ".trinity-bootstrap.done" in path:
            return _Resp(200 if self.marker else 404, {"tree": []})
        if path.endswith("/home/developer/Brain"):
            return _Resp(self.vault_status, {"tree": self.tree})
        return _Resp(404)


def _use_agent(monkeypatch, agent):
    seen = []

    def factory(name):
        seen.append(name)
        return agent

    monkeypatch.setattr(onboarding_service, "get_agent_client", factory)
    return seen


def _read(user):
    return asyncio.run(onboarding_service.get_brain_state(user))


class TestBrainState:
    def test_no_cornelius_is_absent_and_nothing_is_read(self, monkeypatch, admin_user):
        _patch_db(monkeypatch, metadata={"scout": _meta()})
        seen = _use_agent(monkeypatch, FakeAgent())
        assert _read(admin_user) == {"agent": None, "state": "absent", "notes": None}
        assert seen == []

    def test_the_visibility_read_runs_off_the_event_loop(self, monkeypatch, admin_user):
        """Validation of #4820: `_visible_agents` is sync DB I/O; on the loop it stalls every request."""
        import threading

        _patch_db(monkeypatch, metadata={"cornelius": _meta()})
        _use_agent(monkeypatch, FakeAgent(marker=False))
        real = onboarding_service._visible_agents
        threads = []

        def spy(user):
            threads.append(threading.current_thread())
            return real(user)

        monkeypatch.setattr(onboarding_service, "_visible_agents", spy)

        async def go():
            loop_thread = threading.current_thread()
            state = await onboarding_service.get_brain_state(admin_user)
            return loop_thread, state

        loop_thread, state = asyncio.run(go())
        assert state["state"] == "not_set_up"
        assert len(threads) == 1 and threads[0] is not loop_thread

    def test_a_cornelius_the_caller_cannot_see_is_absent(self, monkeypatch, admin_user):
        _patch_db(monkeypatch, user_row=USER_ROW, metadata={"cornelius": _meta(owner_id=1)})
        _use_agent(monkeypatch, FakeAgent())
        user = User(id=2, username="bob", email="bob@example.com", role="user")
        assert _read(user)["state"] == "absent"

    def test_without_the_marker_it_is_not_set_up(self, monkeypatch, admin_user):
        _patch_db(monkeypatch, metadata={"cornelius": _meta()})
        _use_agent(monkeypatch, FakeAgent(marker=False))
        assert _read(admin_user) == {"agent": "cornelius", "state": "not_set_up", "notes": 0}

    def test_ready_with_the_scaffold_only_counts_no_notes(self, monkeypatch, admin_user):
        _patch_db(monkeypatch, metadata={"cornelius": _meta()})
        _use_agent(monkeypatch, FakeAgent())
        assert _read(admin_user) == {"agent": "cornelius", "state": "ready", "notes": 0}

    def test_ready_counts_the_persons_notes_only(self, monkeypatch, admin_user):
        _patch_db(monkeypatch, metadata={"cornelius": _meta()})
        _use_agent(monkeypatch, FakeAgent(tree=_with_notes(4)))
        assert _read(admin_user) == {"agent": "cornelius", "state": "ready", "notes": 4}

    def test_an_unreachable_brain_is_unknown_never_not_set_up(self, monkeypatch, admin_user):
        _patch_db(monkeypatch, metadata={"cornelius": _meta()})
        _use_agent(monkeypatch, FakeAgent(unreachable=True))
        assert _read(admin_user) == {"agent": "cornelius", "state": "unknown", "notes": None}

    def test_an_unreadable_vault_keeps_the_state_and_drops_the_count(self, monkeypatch, admin_user):
        _patch_db(monkeypatch, metadata={"cornelius": _meta()})
        _use_agent(monkeypatch, FakeAgent(vault_status=500))
        assert _read(admin_user) == {"agent": "cornelius", "state": "ready", "notes": None}

    def test_the_marker_is_read_by_its_path(self, monkeypatch, admin_user):
        _patch_db(monkeypatch, metadata={"cornelius": _meta()})
        agent = FakeAgent()
        _use_agent(monkeypatch, agent)
        _read(admin_user)
        assert "/api/files/download?path=/home/developer/.trinity-bootstrap.done" in agent.paths


class TestBrainRoute:
    def test_the_route_answers_through_the_model(self, monkeypatch, admin_user):
        import routers.onboarding as onboarding_router
        _patch_db(monkeypatch, metadata={"cornelius": _meta()})
        _use_agent(monkeypatch, FakeAgent(tree=_with_notes(2)))
        out = asyncio.run(onboarding_router.get_brain(current_user=admin_user))
        assert BrainState.model_validate(out).model_dump() == {"agent": "cornelius", "state": "ready", "notes": 2}

    def test_the_route_is_registered_on_the_app_router(self):
        import routers.onboarding as onboarding_router
        paths = {(r.path, tuple(sorted(r.methods))) for r in onboarding_router.router.routes}
        assert ("/api/onboarding/brain", ("GET",)) in paths


class TestBrainRouteIsForAPerson:
    """The brain's state is read for the person's assistant panel. An agent's
    key, a connector or a delegate never reads it (the #2996 route census)."""

    def _client(self, principal):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import routers.onboarding as onboarding_router

        app = FastAPI()
        app.include_router(onboarding_router.router)
        app.dependency_overrides[onboarding_router.get_current_user] = lambda: principal
        return TestClient(app)

    def test_an_agent_key_is_refused(self, monkeypatch):
        _patch_db(monkeypatch, metadata={"cornelius": _meta()})
        _use_agent(monkeypatch, FakeAgent(tree=_with_notes(2)))
        agent = User(id=1, username="admin", email="admin@example.com", role="admin",
                     mcp_scope="agent", agent_name="scout")
        assert self._client(agent).get("/api/onboarding/brain").status_code == 403

    def test_a_person_reads_it(self, monkeypatch, admin_user):
        _patch_db(monkeypatch, metadata={"cornelius": _meta()})
        _use_agent(monkeypatch, FakeAgent(tree=_with_notes(2)))
        r = self._client(admin_user).get("/api/onboarding/brain")
        assert r.status_code == 200
        assert r.json() == {"agent": "cornelius", "state": "ready", "notes": 2}


# --- through the shipped agent server, over the template's real layout -------
#
# The files the Cornelius template ships in `Brain/` (Abilityai/cornelius at
# 6044b3b, `find Brain -type f`), laid out under a temporary home. The agent
# door is the SHIPPED agent-server files router (loaded by path, #1795), with
# the container's `/home/developer` mapped onto that home.

import importlib.util as _ilu
import logging as _logging
from pathlib import Path as _Path

TEMPLATE_BRAIN_FILES = [
    "Brain/00-Inbox/README.md",
    "Brain/01-Sources/Books/_SAMPLE - Book Note Template.md",
    "Brain/02-Permanent/README.md",
    "Brain/02-Permanent/_SAMPLE - Permanent Note Template.md",
    "Brain/04-Output/Articles/ARTICLE-INDEX.md",
    "Brain/05-Meta/Changelogs/_SAMPLE - Changelog Template.md",
    "Brain/05-Meta/Templates/MOC-Template.md",
    "Brain/05-Meta/Templates/Permanent-Note-Template.md",
    "Brain/05-Meta/Templates/Source-Note-Template.md",
    "Brain/CHANGELOG.md",
    "Brain/README.md",
    "Brain/AI Extracted Notes/.gitkeep",
    "Brain/Document Insights/.gitkeep",
    "Brain/03-MOCs/.gitkeep",
]
_AGENT_FILES = _Path(__file__).resolve().parents[2] / "docker/base-image/agent_server/routers/files.py"


@pytest.fixture()
def cornelius_home(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    spec = _ilu.spec_from_file_location("_test1409_agent_files", str(_AGENT_FILES))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    base = (tmp_path / "home").resolve()
    for rel in TEMPLATE_BRAIN_FILES:
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / rel).write_text("scaffold\n")
    monkeypatch.setattr(mod, "_HOME", base)
    app = FastAPI()
    app.include_router(mod.router)
    client = TestClient(app)

    class Door:
        async def get(self, path, timeout=None, **kwargs):
            return client.get(path.replace("/home/developer", str(base)))

    _use_agent(monkeypatch, Door())
    _patch_db(monkeypatch, metadata={"cornelius": _meta()})
    return base


class TestAgainstTheTemplateLayout:
    def test_a_fresh_fork_is_not_set_up_with_no_notes(self, cornelius_home, admin_user):
        assert _read(admin_user) == {"agent": "cornelius", "state": "not_set_up", "notes": 0}

    def test_set_up_counts_only_the_persons_notes(self, cornelius_home, admin_user):
        (cornelius_home / ".trinity-bootstrap.done").write_text("ok\n")
        assert _read(admin_user) == {"agent": "cornelius", "state": "ready", "notes": 0}
        (cornelius_home / "Brain/Document Insights/Q3 pricing memo.md").write_text("# memo\n")
        (cornelius_home / "Brain/02-Permanent/Pricing follows value.md").write_text("# note\n")
        assert _read(admin_user) == {"agent": "cornelius", "state": "ready", "notes": 2}

    def test_reading_the_marker_logs_no_error_on_the_agent(self, cornelius_home, admin_user, caplog):
        (cornelius_home / ".trinity-bootstrap.done").write_text("ok\n")
        with caplog.at_level(_logging.WARNING):
            _read(admin_user)
        assert [r.getMessage() for r in caplog.records if r.levelno >= _logging.ERROR] == []
