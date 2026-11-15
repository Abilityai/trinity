"""trinity-enterprise#736 — a deprecated library skill is marked on the set surfaces and the CLI.

ent#672 marked a deprecated skill on the Library card and the Skills tab; the
skill-set member lists, the set-assign notes, the runner picker and the CLI's
table did not. What these tests pin on the backend + CLI side, each by
EXECUTING the path:

  * GET `/api/skills/library/sets` — each member carries `deprecated`, through
    the response model, read from the library listing; an unreadable listing
    reads "not deprecated" and is never remembered, and the cached resolver
    dict (`library_sets`, which also feeds the start path) is never mutated;
  * an agent's set status (GET `/agents/{n}/skill-sets` and the POST assign
    response) — each member carries `deprecated` and the ent#672 code, so an
    assign names the deprecated member even when nothing new was added;
  * `trinity skills list` — the table's `deprecated` column. The CI unit job
    does not install the CLI's deps, so the row rule lives in a stdlib-only
    module run here by path; the command itself runs where click/rich exist.

No real Redis, no real Docker, no real agent.
Related flow: docs/memory/feature-flows/skill-assignment.md
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

AGENT = "acme-bot"
REPO = Path(__file__).resolve().parents[2]
_CLI = REPO / "src" / "cli"

LISTING = [
    {"name": "old", "deprecated": True, "superseded_by": "backlog"},
    {"name": "prose", "deprecated": True, "superseded_by": "another catalog's exporter"},
    {"name": "live", "deprecated": False, "superseded_by": None},
]


# ---- a fake library under skill_set_service ----------------------------------

class _Clone:
    def __init__(self, source_id, sets):
        self.source_id, self._sets = source_id, sets

    def declared_sets(self):
        return self._sets

    def tree_shas(self):
        return {"old": "o-sha", "prose": "p-sha", "live": "l-sha"}


@pytest.fixture
def library(monkeypatch):
    from services import skill_set_service as svc_mod
    from services.skill_sets import SetDef
    clone = _Clone("default", [SetDef("pack", ["old", "prose", "live"], [], [], [], [])])
    resolution = {n: {"source_id": "default", "clone": clone} for n in ("old", "prose", "live")}
    state = {"listing": list(LISTING)}

    def _listing():
        if isinstance(state["listing"], Exception):
            raise state["listing"]
        return state["listing"]

    fake = SimpleNamespace(
        _clones=lambda enabled_only=True: [clone],
        _library_fingerprint=lambda clones: "fp-1",            # the resolver DOES cache here
        _source_names=lambda: {"default": "Default"},
        _resolution=lambda: resolution,
        list_skills=_listing,
        _probe_dependencies=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(svc_mod, "_skill_service", lambda: fake)
    monkeypatch.setattr(svc_mod, "_cache", (None, {}))
    fake.state = state
    return fake


@pytest.fixture
def client(library, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import skills
    from database import db

    held = [{"set_name": "pack", "source_id": "default", "assigned_by": "alice",
             "assigned_by_agent": None, "assigned_at": "2026-11-04T00:00:00Z"}]
    rows = [SimpleNamespace(skill_name=n, delivery_status=None) for n in ("old", "prose", "live")]
    monkeypatch.setattr(db, "list_agent_skill_sets", lambda a: held, raising=False)
    monkeypatch.setattr(db, "agent_skill_set_names", lambda a: ["pack"], raising=False)
    monkeypatch.setattr(db, "get_agent_skills", lambda a: rows, raising=False)
    monkeypatch.setattr(skills, "can_manage_agent_skills", lambda user, agent: True)

    app = FastAPI()
    app.include_router(skills.router)
    human = SimpleNamespace(id=1, username="alice", email="a@example.com", role="admin",
                            mcp_scope=None, agent_name=None, connector_agent=None, mcp_key_id=None)
    app.dependency_overrides[skills.get_current_user] = lambda: human
    app.dependency_overrides[skills.get_skill_managed_agent_by_name] = lambda agent_name: agent_name
    app.dependency_overrides[skills.get_authorized_agent_by_name] = lambda agent_name: agent_name
    return TestClient(app)


def _members(body):
    return {m["name"]: m for m in body["members"]}


# ---- the library's set listing -------------------------------------------------

def test_the_set_listing_marks_a_deprecated_member_through_the_response_model(client):
    r = client.get("/api/skills/library/sets")
    assert r.status_code == 200
    members = _members(r.json()[0])
    assert members["old"]["deprecated"] is True
    assert members["prose"]["deprecated"] is True
    assert members["live"]["deprecated"] is False


def test_an_unreadable_listing_reads_not_deprecated_and_is_not_remembered(client, library):
    from services import skill_set_service as svc
    library.state["listing"] = RuntimeError("git timed out")
    r = client.get("/api/skills/library/sets")
    assert r.status_code == 200
    assert {n: m["deprecated"] for n, m in _members(r.json()[0]).items()} == {
        "old": False, "prose": False, "live": False}

    library.state["listing"] = list(LISTING)                # the next read works
    assert _members(client.get("/api/skills/library/sets").json()[0])["old"]["deprecated"] is True
    # The resolver's cached dict (it also feeds the start path) was never decorated.
    assert all("deprecated" not in m for m in svc.library_sets()["pack"]["members"])


def test_a_member_the_listing_does_not_know_reads_not_deprecated(client, library):
    library.state["listing"] = [LISTING[0]]                 # only `old` is listed
    members = _members(client.get("/api/skills/library/sets").json()[0])
    assert members["old"]["deprecated"] is True
    assert members["live"]["deprecated"] is False


# ---- an agent's set status ------------------------------------------------------

def test_an_agents_set_status_carries_the_flag_and_the_code(client):
    r = client.get(f"/api/agents/{AGENT}/skill-sets")
    assert r.status_code == 200
    members = _members(r.json()[0])
    assert (members["old"]["deprecated"], members["old"]["deprecation"]) == (True, "deprecated:backlog")
    # A prose successor is not a skill name: the bare code (the ent#672 grammar).
    assert (members["prose"]["deprecated"], members["prose"]["deprecation"]) == (True, "deprecated")
    assert (members["live"]["deprecated"], members["live"]["deprecation"]) == (False, None)


def test_the_status_reads_the_library_listing_once(client, library):
    calls = []
    real = library.list_skills
    library.list_skills = lambda: calls.append(1) or real()
    client.get(f"/api/agents/{AGENT}/skill-sets")
    assert len(calls) == 1


def test_a_re_assign_names_the_deprecated_member_although_nothing_was_added(client, monkeypatch):
    """`delivery` is None when the assign added no row (the set, or the member,
    was already held), so the note cannot read it; the status the same response
    carries names the deprecated member either way."""
    from routers import skills
    from services import skill_set_service as svc
    from services import skill_assignment_service as sas
    monkeypatch.setattr(svc, "assign", lambda *a, **k: {
        "created": False, "added": [], "removed": [], "entry": {"schedules": []}})
    monkeypatch.setattr(sas, "conflict_names", lambda a: set())
    monkeypatch.setattr(sas, "sync_gates", AsyncMock(return_value=None))
    monkeypatch.setattr(skills, "broadcast_skills_changed", AsyncMock())
    monkeypatch.setattr(svc, "_is_running", AsyncMock(return_value=False))

    r = client.post(f"/api/agents/{AGENT}/skill-sets/pack")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["delivery"] is None and body["members_added"] == []
    assert _members(body["status"])["old"]["deprecation"] == "deprecated:backlog"


# ---- the CLI table --------------------------------------------------------------

def _rows_module():
    spec = importlib.util.spec_from_file_location(
        "_ent736_skill_rows", _CLI / "trinity_cli" / "skill_rows.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_table_rows_gain_a_deprecated_column():
    rows = _rows_module().skill_table_rows([
        {"name": "old", "description": "d" * 90, "category": "ops",
         "deprecated": True, "superseded_by": "backlog"},
        {"name": "prose", "description": "x", "category": None,
         "deprecated": True, "superseded_by": "s" * 80},
        {"name": "bare", "description": None, "deprecated": True, "superseded_by": None},
        {"name": "live", "description": "y", "category": "dev", "deprecated": False},
        {"name": "legacy"},                                   # an older backend: no lifecycle keys
    ])
    assert [r["deprecated"] for r in rows] == ["→ backlog", "→ " + "s" * 40, "yes", "", ""]
    assert rows[0]["description"] == "d" * 60
    assert [r["category"] for r in rows] == ["ops", "", "", "dev", ""]   # None no longer prints "None"
    assert list(rows[0]) == ["name", "description", "category", "deprecated"]


def test_the_list_command_builds_its_table_from_that_rule():
    """Pin on the call site, layered on the execution test above: the unit job
    cannot import the command (no click), so this is what keeps it wired."""
    tree = ast.parse((_CLI / "trinity_cli" / "commands" / "skills.py").read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "list_skills")
    calls = {c.func.id for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    assert "skill_table_rows" in calls


@pytest.fixture
def cli(monkeypatch):
    pytest.importorskip("click")
    pytest.importorskip("rich")
    if str(_CLI) not in sys.path:
        monkeypatch.syspath_prepend(str(_CLI))
    from click.testing import CliRunner
    from trinity_cli.commands import skills as cmd
    return CliRunner(), cmd


def test_the_command_prints_the_column_end_to_end(cli, monkeypatch):
    runner, cmd = cli
    monkeypatch.setattr(cmd, "TrinityClient", lambda: SimpleNamespace(get=lambda path: [
        {"name": "old", "description": "retired", "category": "ops",
         "deprecated": True, "superseded_by": "backlog"},
        {"name": "live", "description": "fine", "category": "dev", "deprecated": False},
    ]))
    out = runner.invoke(cmd.skills, ["list"], terminal_width=200)
    assert out.exit_code == 0, out.output
    assert "deprecated" in out.output and "→ backlog" in out.output


def test_author_text_is_printed_never_parsed_as_markup(cli, monkeypatch):
    """Rich parses `[...]` in a table cell: `[/]` raised MarkupError and took the
    whole listing down. The description and the successor are author text."""
    runner, cmd = cli
    monkeypatch.setattr(cmd, "TrinityClient", lambda: SimpleNamespace(get=lambda path: [
        {"name": "old", "description": "use [/] carefully", "category": "[bold]x",
         "deprecated": True, "superseded_by": "[/]"},
    ]))
    out = runner.invoke(cmd.skills, ["list"], terminal_width=200)
    assert out.exit_code == 0, out.output
    assert "use [/] carefully" in out.output and "[bold]x" in out.output and "→ [/]" in out.output


def test_a_single_skill_view_prints_author_text_too(cli, monkeypatch):
    """`trinity skills get` prints a dict (`output._print_dict`) — the same class."""
    runner, cmd = cli
    monkeypatch.setattr(cmd, "TrinityClient", lambda: SimpleNamespace(get=lambda path: {
        "name": "old", "description": "use [/] carefully", "superseded_by": "[/]"}))
    out = runner.invoke(cmd.skills, ["get", "old"], terminal_width=200)
    assert out.exit_code == 0, out.output
    assert "use [/] carefully" in out.output
