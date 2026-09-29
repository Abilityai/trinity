"""trinity-enterprise#527 / #663 — the role card in Agent details.

Files are truth and the card is a projection, so the tests drive
`build_role_card` with the agent door stubbed (a fake client serving
`template.yaml`, the canon files and `/api/metrics`) and assert what crosses:

* no `x-role` → no card (the panel is unchanged, AC 5);
* a role file that fails to load SAYS so (`role.error`), never an empty role;
* objectives are the ones the role owns or this agent supports, with metric
  value / target / freshness — stale is stale, never current (quality bar #4);
* readiness is the OWNER's stamp, never the template's word: a template that
  says `ready` with no stamp is `calibrating` + `unstamped_ready` (#663);
* the flip is owner-only, named 403 for everyone else, and the agent can never
  reach it; the stamp round-trips through a real SQLite file;
* both migration tracks carry the table.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
AGENT = "sales-companion"
EMAIL = "owner@example.com"

TEMPLATE = """
name: sales-companion
x-role:
  role: sales-lead
  status: calibrating
  seat: gary@example.com
x-canon:
  repo: git@example.com:fleet/canon.git
  clone_path: canon
"""
ROLE = """
schema_version: 1
id: sales-lead
title: Sales Lead
mission: Close ICP-fit pipeline at a cost the model can bear.
status: active
review_by: 2099-12-01
"""
OBJ_OWNED = """
schema_version: 1
id: q4-close-rate
statement: Raise close rate this quarter.
horizon: quarter
owner: role:sales-lead
metrics:
  - name: close_rate
    direction: up
    target: 0.3
    by: 2026-12-31
  - name: reply_rate
    direction: up
    target: 0.2
status: active
"""
OBJ_SUPPORTED = """
id: q4-icp-demand
statement: Raise ICP-fit inbound demand.
owner: role:marketing-lead
supporting_agents: [sales-companion]
metrics:
  - name: icp_fit_rate
    target: 0.65
"""
OBJ_FOREIGN = """
id: other
owner: role:cfo
metrics: [{name: burn}]
"""


class FakeClient:
    """The agent door: files by path, a listing, and the metrics endpoint."""

    def __init__(self, files: dict, *, metrics=None, last_updated=None, broken=()):
        self.files = files
        self.metrics = metrics or {}
        self.last_updated = last_updated
        self.broken = set(broken)

    async def read_file(self, path):
        if path in self.broken:
            raise RuntimeError("transport")
        if path not in self.files:
            return {"success": True, "content": None, "not_found": True}
        return {"success": True, "content": self.files[path]}

    async def get(self, path, **kw):
        resp = SimpleNamespace(status_code=200)
        if path == "/api/metrics":
            resp.json = lambda: {"has_metrics": True, "values": self.metrics, "last_updated": self.last_updated}
        elif path.startswith("/api/files?path=/home/developer/"):
            directory = path.split("/home/developer/", 1)[1]
            names = sorted(p.split("/")[-1] for p in self.files if p.startswith(directory + "/"))
            resp.json = lambda: {"tree": [{"name": n, "type": "file"} for n in names]}
        else:
            resp.status_code = 404
            resp.json = lambda: {}
        return resp


def _wire(monkeypatch, client, *, state="running", stamp=None, owner=True, ratings=None, messages=None):
    import client_portal.role_card as rc
    from services import docker_utils
    from services import agent_client as ac_mod
    monkeypatch.setattr(docker_utils, "agent_container_state_async", AsyncMock(return_value=state))
    monkeypatch.setattr(ac_mod, "get_agent_client", lambda name: client)
    monkeypatch.setattr(rc.db, "get_agent_role_readiness", lambda a: stamp)
    monkeypatch.setattr(rc.portal_db, "get_owned_roster",
                        lambda email: [{"agent_name": AGENT}] if owner else [])
    monkeypatch.setattr(rc.portal_db, "get_main_portal_session_id", lambda a, e: "main-1")
    monkeypatch.setattr(rc.portal_db, "get_portal_messages",
                        lambda a, e, limit=100, session_id=None: messages or [])
    monkeypatch.setattr(rc.db, "list_workspace_ratings_for_targets",
                        lambda ev, kind, ids: ratings or {})
    return rc


def _card(rc, *, is_platform=True):
    return asyncio.run(rc.build_role_card(AGENT, EMAIL, is_platform=is_platform))


def _full_client(**kw):
    return FakeClient({
        "template.yaml": TEMPLATE,
        "canon/roles/sales-lead.yaml": ROLE,
        "canon/objectives/q4-close-rate.yaml": OBJ_OWNED,
        "canon/objectives/q4-icp-demand.yaml": OBJ_SUPPORTED,
        "canon/objectives/other.yaml": OBJ_FOREIGN,
    }, **kw)


# --- no role → no card ----------------------------------------------------------

def test_an_agent_with_no_x_role_has_no_card(monkeypatch):
    rc = _wire(monkeypatch, FakeClient({"template.yaml": "name: plain\n"}))
    assert _card(rc) == {"agent_name": AGENT, "role": None}


def test_an_unreadable_template_is_also_no_card(monkeypatch):
    rc = _wire(monkeypatch, FakeClient({}, broken={"template.yaml"}))
    assert _card(rc)["role"] is None


# --- the role, and a role file that fails to load --------------------------------

def test_the_role_reads_from_the_canon_file_through_the_agent(monkeypatch):
    rc = _wire(monkeypatch, _full_client())
    card = _card(rc)
    role = card["role"]
    assert role["id"] == "sales-lead"
    assert role["title"] == "Sales Lead"
    assert role["mission"].startswith("Close ICP-fit")
    assert role["status"] == "active"
    assert role["path"] == "canon/roles/sales-lead.yaml"
    assert role["error"] is None
    assert card["seat"] == "gary@example.com"


@pytest.mark.parametrize("files,expected", [
    ({"template.yaml": TEMPLATE}, "role_file_not_found"),
    ({"template.yaml": TEMPLATE, "canon/roles/sales-lead.yaml": "- not\n- a mapping\n"}, "role_file_invalid"),
], ids=["missing", "not-a-mapping"])
def test_a_role_file_that_fails_to_load_says_so(monkeypatch, files, expected):
    rc = _wire(monkeypatch, FakeClient(files))
    role = _card(rc)["role"]
    assert role["id"] == "sales-lead"
    assert role["error"] == expected
    assert role["title"] is None                        # never an empty role dressed as one


def test_a_transport_failure_on_the_role_file_is_named_too(monkeypatch):
    rc = _wire(monkeypatch, FakeClient({"template.yaml": TEMPLATE}, broken={"canon/roles/sales-lead.yaml"}))
    assert _card(rc)["role"]["error"] == "role_file_unreadable"


def test_a_traversal_shaped_canon_path_or_role_id_never_reaches_a_read(monkeypatch):
    import client_portal.role_card as rc
    assert rc.canon_root({"x-canon": {"clone_path": "../etc"}}) is None
    assert rc.canon_root({"x-canon": {"clone_path": "canon/../x"}}) is None
    assert rc.canon_root({}) == "canon"
    assert rc.canon_root({"x-canon": {"clone_path": "shared/canon"}}) == "shared/canon"
    bad = TEMPLATE.replace("role: sales-lead", "role: ../../secrets")
    rc2 = _wire(monkeypatch, FakeClient({"template.yaml": bad}))
    assert _card(rc2)["role"]["error"] == "role_id_invalid"


# --- objectives + freshness -------------------------------------------------------

def test_objectives_are_the_ones_the_role_owns_or_this_agent_supports(monkeypatch):
    fresh = datetime.now(timezone.utc).isoformat()
    rc = _wire(monkeypatch, _full_client(metrics={"close_rate": 0.27, "icp_fit_rate": 0.6}, last_updated=fresh))
    objs = {o["id"]: o for o in _card(rc)["objectives"]}
    assert set(objs) == {"q4-close-rate", "q4-icp-demand"}      # `other` (cfo's) is not on this card
    assert objs["q4-close-rate"]["owned"] is True
    assert objs["q4-icp-demand"]["owned"] is False
    close = next(m for m in objs["q4-close-rate"]["metrics"] if m["name"] == "close_rate")
    assert close == {"name": "close_rate", "direction": "up", "target": 0.3, "by": "2026-12-31",
                     "value": 0.27, "as_of": fresh, "stale": False}


def test_a_stale_or_missing_metric_is_marked_stale_never_current(monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    rc = _wire(monkeypatch, _full_client(metrics={"close_rate": 0.27}, last_updated=old))
    objs = {o["id"]: o for o in _card(rc)["objectives"]}
    rows = {m["name"]: m for m in objs["q4-close-rate"]["metrics"]}
    assert rows["close_rate"]["stale"] is True and rows["close_rate"]["value"] == 0.27   # last value shown, as stale
    assert rows["reply_rate"]["stale"] is True and rows["reply_rate"]["value"] is None  # never pushed


def test_a_metric_with_no_freshness_stamp_at_all_is_stale(monkeypatch):
    rc = _wire(monkeypatch, _full_client(metrics={"close_rate": 0.27}, last_updated=None))
    rows = {m["name"]: m for o in _card(rc)["objectives"] for m in o["metrics"]}
    assert rows["close_rate"]["stale"] is True


def test_the_staleness_rule_directly():
    import client_portal.role_card as rc
    now = datetime(2026, 9, 21, tzinfo=timezone.utc)
    assert rc.is_stale(None, now=now)
    assert rc.is_stale("garbage", now=now)
    assert rc.is_stale("2026-08-01T00:00:00Z", now=now)          # 51 days
    assert not rc.is_stale("2026-09-20T00:00:00Z", now=now)
    assert not rc.is_stale("2026-09-20T00:00:00", now=now)       # naive → UTC


# --- readiness (#663) -------------------------------------------------------------

def test_without_an_owner_stamp_the_template_word_is_calibrating_whatever_it_says(monkeypatch):
    import client_portal.role_card as rc
    assert rc.effective_readiness("calibrating", None)["status"] == "calibrating"
    r = rc.effective_readiness("ready", None)
    assert r["status"] == "calibrating" and r["unstamped_ready"] is True and r["source"] == "template"


def test_the_owners_stamp_wins_and_names_who_and_when(monkeypatch):
    stamp = {"status": "ready", "changed_at": "2026-09-21T10:00:00Z", "changed_by": EMAIL}
    rc = _wire(monkeypatch, _full_client(), stamp=stamp)
    r = _card(rc)["readiness"]
    assert r == {"status": "ready", "changed_at": "2026-09-21T10:00:00Z", "changed_by": EMAIL,
                 "source": "owner", "unstamped_ready": False}


def test_only_the_owner_may_flip_and_the_refusal_is_named(monkeypatch):
    rc = _wire(monkeypatch, _full_client(), owner=False)
    with pytest.raises(rc.RoleCardRefused) as ei:
        rc.flip_readiness(AGENT, "shared@example.com", is_platform=True, status="ready")
    assert (ei.value.code, ei.value.status_code) == ("readiness_owner_only", 403)
    # A portal-token principal is never an owner, even for an agent it can reach.
    with pytest.raises(rc.RoleCardRefused):
        rc.flip_readiness(AGENT, EMAIL, is_platform=False, status="ready")
    with pytest.raises(rc.RoleCardRefused) as ei2:
        rc.flip_readiness(AGENT, EMAIL, is_platform=True, status="done")
    assert ei2.value.code == "readiness_unknown_state"


def test_the_card_tells_the_viewer_whether_they_may_flip(monkeypatch):
    assert _card(_wire(monkeypatch, _full_client(), owner=True))["can_flip_readiness"] is True
    assert _card(_wire(monkeypatch, _full_client(), owner=False))["can_flip_readiness"] is False
    assert _card(_wire(monkeypatch, _full_client(), owner=True), is_platform=False)["can_flip_readiness"] is False


# --- walkthrough + stopped agent ---------------------------------------------------

def test_walkthrough_counts_the_viewers_own_asks_capped_at_ten_and_their_thumbs_down(monkeypatch):
    msgs = [{"id": f"u{i}", "role": "user"} for i in range(14)] + \
           [{"id": f"a{i}", "role": "assistant"} for i in range(14)]
    rc = _wire(monkeypatch, _full_client(), messages=msgs, ratings={"a1": 0.0, "a2": 1.0, "a5": 0.0})
    w = _card(rc)["walkthrough"]
    assert w == {"asks": 10, "target": 10, "rated_down": 2, "unavailable": False}


def test_a_stopped_agent_says_so_and_still_shows_the_owner_stamp(monkeypatch):
    stamp = {"status": "ready", "changed_at": "t", "changed_by": EMAIL}
    rc = _wire(monkeypatch, _full_client(), state="stopped", stamp=stamp)
    card = _card(rc)
    assert card["unavailable"] == "agent_stopped"
    assert card["role"] is None
    assert card["readiness"]["status"] == "ready"


# --- the stamp, against a real SQLite file ---------------------------------------

@pytest.fixture
def real_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    import db.engine as engine_module
    import db.tables as tables
    import db.role_readiness as rr
    engine = create_engine(f"sqlite:///{tmp_path / 'r.db'}")
    tables.agent_role_readiness.create(engine)
    monkeypatch.setattr(engine_module, "get_engine", lambda: engine)
    monkeypatch.setattr(rr, "get_engine", lambda: engine)
    return rr.RoleReadinessOperations()


def test_the_stamp_round_trips_and_a_second_flip_overwrites(real_db):
    assert real_db.get_role_readiness(AGENT) is None
    first = real_db.set_role_readiness(AGENT, "ready", EMAIL)
    assert real_db.get_role_readiness(AGENT) == first
    second = real_db.set_role_readiness(AGENT, "calibrating", EMAIL)
    got = real_db.get_role_readiness(AGENT)
    assert got["status"] == "calibrating" and got["changed_at"] >= first["changed_at"]
    with pytest.raises(ValueError):
        real_db.set_role_readiness(AGENT, "done", EMAIL)


# --- both migration tracks ------------------------------------------------------

def test_the_table_is_on_both_tracks_and_in_the_cleanup_registry():
    schema = (REPO / "src/backend/db/schema.py").read_text()
    tables = (REPO / "src/backend/db/tables.py").read_text()
    mig = (REPO / "src/backend/db/migrations.py").read_text()
    cleanup = (REPO / "src/backend/db/agent_cleanup.py").read_text()
    rev = (REPO / "src/backend/migrations/versions/0067_agent_role_readiness.py").read_text()
    assert "CREATE TABLE IF NOT EXISTS agent_role_readiness" in schema
    assert "agent_role_readiness = Table(" in tables
    assert '("agent_role_readiness_table", _migrate_agent_role_readiness_table)' in mig
    assert 'AgentRef("agent_role_readiness"' in cleanup
    # Chained off #2924's revision (the three 2026-09-21 schema PRs are stacked).
    assert 'down_revision = "0066_public_user_memory_writes"' in rev
    assert 'has_table("agent_role_readiness")' in rev


def test_the_migration_graph_still_has_exactly_one_head():
    import subprocess
    out = subprocess.run(["python3", str(REPO / "scripts/ci/check_alembic_heads.py"),
                          str(REPO / "src/backend/migrations/versions")], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
