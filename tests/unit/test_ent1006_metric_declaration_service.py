"""Declaring metrics from the UI — the write path and its fences (trinity-enterprise#1006).

The UI writes the agent's `template.yaml` and then reconciles the registry from
the file it wrote, so the template stays the one source of truth. Pinned here:

* a declaration lands in the FILE and the registry agrees (source `ui`);
* compare-and-swap: a stale hash, or a file that changed mid-write, writes nothing;
* an unreadable template or a stopped agent writes nothing and retires nothing (#2196);
* invalid fields come back keyed by field, with nothing written;
* retiring a metric still in use — or whose use could not be checked — needs an
  explicit acknowledgement; points survive the retire;
* the routes take the owner gate + the `instructions.manage` capability, and
  `template.yaml` is an instruction file in the Files path policy, so the
  generic Files route is not a back door around them.

The agent container is faked at the two seams the service crosses: the
template read (`metric_registry._read_template_from_container`) and the Files
write (`update_agent_file_logic`).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend  # noqa: E402,F401

pytest.importorskip("docker", reason="backend venv required")

from fastapi import HTTPException  # noqa: E402
from database import db  # noqa: E402
from routers import agent_files  # noqa: E402
import services.agent_service as agent_service_pkg  # noqa: E402
from services import file_path_policy as policy  # noqa: E402
from services import metric_declaration_editor as editor  # noqa: E402
from services import metric_declaration_service as svc  # noqa: E402
from services import metric_points_service  # noqa: E402
from services import metric_references, metric_registry  # noqa: E402

AGENT = "declare-agent"

TEMPLATE = """\
name: declare-agent
# keep me
metrics:
  - name: close_rate
    type: percentage
    label: Close rate
    cadence: 1w
    x-owner: ops
"""


class FakeAgent:
    """The container's `template.yaml`, as the two seams see it."""

    def __init__(self, text=TEMPLATE):
        self.text = text
        self.running = True
        self.writes = []
        self.on_read = None          # optional hook: called before each read

    async def read(self, agent_name):
        if self.on_read:
            self.on_read(self)
        if not self.running:
            raise metric_registry.RefreshUnavailable("agent_not_running", "stopped")
        return self.text

    async def write(self, agent_name, path, content, current_user, request):
        assert path == "template.yaml"
        self.writes.append(content)
        self.text = content
        return {"success": True}


@pytest.fixture
def agent(db_backend, monkeypatch):
    fake = FakeAgent()
    monkeypatch.setattr(metric_registry, "_read_template_from_container", fake.read)
    monkeypatch.setattr(agent_service_pkg, "update_agent_file_logic", fake.write)
    # Reference sources that cross into the agent default to "none found".
    monkeypatch.setattr(svc, "_objectives", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "_widgets", AsyncMock(return_value=[]))
    import importlib
    audit = importlib.import_module("services.platform_audit_service")
    monkeypatch.setattr(audit.platform_audit_service, "log", AsyncMock(return_value="evt"))
    metric_references.clear_providers()
    asyncio.run(metric_registry.refresh_from_running_agent(AGENT, source="refresh"))
    yield fake
    metric_references.clear_providers()


def _req():
    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={},
                           scope={"path": "/"}, state=SimpleNamespace(request_id="r1"),
                           method="POST")


USER = SimpleNamespace(id=1, username="owner", email="owner@example.com", role="user",
                       mcp_scope=None, agent_name=None, connector_agent=None)

NEW = {"name": "proposals_sent", "type": "counter", "label": "Proposals sent",
       "cadence": "1w", "direction": "up_good", "recorded_by": "the weekly skill"}


def _hash(fake):
    return editor.template_hash(fake.text)


def _active():
    return {d["name"]: d for d in db.list_metric_definitions(AGENT, include_retired=True)}


def _declare(fake, fields=NEW, base=None):
    return asyncio.run(svc.declare(AGENT, dict(fields), base or _hash(fake),
                                   current_user=USER, request=_req()))


def _refused(call):
    with pytest.raises(svc.DeclarationRefused) as e:
        call()
    return e.value


# ---------------------------------------------------------------------------
# declare
# ---------------------------------------------------------------------------


def test_declare_writes_the_template_and_the_registry_follows(agent):
    out = _declare(agent)
    assert len(agent.writes) == 1
    assert "# keep me" in agent.text
    assert "  - name: proposals_sent\n" in agent.text
    assert "x-recorded-by: the weekly skill" in agent.text
    assert out["template_hash"] == editor.template_hash(agent.text)
    assert out["registry_refreshed"] is True
    assert out["registry"]["created"] == ["proposals_sent"]
    row = _active()["proposals_sent"]
    assert row["status"] == "active" and row["source"] == "ui"
    assert row["cadence_seconds"] == 7 * 86400
    assert row["extensions"] == {"x-recorded-by": "the weekly skill"}


def test_the_editor_read_returns_form_fields_and_the_hash(agent):
    _declare(agent)
    body = asyncio.run(svc.read_editor(AGENT))
    assert body["template_hash"] == _hash(agent)
    assert body["editable"] is True
    assert body["declared_count"] == 2 and body["cap"] == 50
    new = body["declarations"][1]
    assert new["name"] == "proposals_sent" and new["recorded_by"] == "the weekly skill"
    assert new["valid"] is True and new["errors"] == []


def test_a_stale_hash_writes_nothing(agent):
    e = _refused(lambda: _declare(agent, base="0" * 64))
    assert (e.code, e.status) == ("template_changed", 409)
    assert agent.writes == []


def test_a_file_that_changes_mid_write_writes_nothing(agent):
    reads = {"n": 0}

    def change_on_second_read(fake):
        reads["n"] += 1
        if reads["n"] == 2:
            fake.text = fake.text + "extra: 1\n"

    agent.on_read = change_on_second_read
    base = _hash(agent)
    e = _refused(lambda: _declare(agent, base=base))
    assert e.code == "template_changed"
    assert agent.writes == []


def test_a_stopped_agent_writes_nothing(agent):
    agent.running = False
    e = _refused(lambda: _declare(agent, base="0" * 64))
    assert (e.code, e.status) == ("agent_not_running", 409)
    assert agent.writes == []


def test_an_unreadable_template_writes_nothing_and_retires_nothing(agent):
    agent.text = "name: [unclosed\n"
    e = _refused(lambda: _declare(agent))
    assert (e.code, e.status) == ("template_unreadable", 503)
    assert agent.writes == []
    assert _active()["close_rate"]["status"] == "active"


def test_invalid_fields_come_back_keyed_by_field_and_write_nothing(agent):
    e = _refused(lambda: _declare(agent, {**NEW, "cadence": "30s", "name": "Bad Name"}))
    assert (e.code, e.status) == ("metric_invalid", 422)
    assert {x["field"] for x in e.extra["errors"]} >= {"cadence", "name"}
    assert agent.writes == []


def test_a_duplicate_name_is_refused(agent):
    e = _refused(lambda: _declare(agent, {**NEW, "name": "close_rate"}))
    assert e.extra["errors"][0]["code"] == "metric_duplicate"


def test_a_layout_the_editor_cannot_change_is_refused_by_name(agent):
    agent.text = "name: a\nmetrics: [{name: x, type: gauge, cadence: 1d}]\n"
    e = _refused(lambda: _declare(agent))
    assert (e.code, e.status) == ("template_not_editable", 409)
    assert agent.writes == []
    body = asyncio.run(svc.read_editor(AGENT))
    assert body["editable"] is False and body["reason"]


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


def test_update_changes_the_entry_and_keeps_extension_keys(agent):
    fields = {"name": "close_rate", "type": "percentage", "label": "Close rate",
              "cadence": "1d", "direction": "up_good"}
    out = asyncio.run(svc.update(AGENT, "close_rate", fields, _hash(agent),
                                 current_user=USER, request=_req()))
    assert out["registry"]["updated"] == ["close_rate"]
    assert "cadence: 1d" in agent.text and "x-owner: ops" in agent.text
    assert _active()["close_rate"]["cadence_seconds"] == 86400


def test_update_cannot_change_the_type(agent):
    fields = {"name": "close_rate", "type": "gauge", "cadence": "1d"}
    e = _refused(lambda: asyncio.run(svc.update(AGENT, "close_rate", fields, _hash(agent),
                                                current_user=USER, request=_req())))
    assert e.extra["errors"][0]["code"] == "metric_type_immutable"
    assert agent.writes == []


def test_update_of_an_unknown_metric_is_404(agent):
    e = _refused(lambda: asyncio.run(svc.update(AGENT, "nope", {**NEW, "name": "nope"}, _hash(agent),
                                                current_user=USER, request=_req())))
    assert (e.code, e.status) == ("metric_not_found", 404)


# ---------------------------------------------------------------------------
# retire
# ---------------------------------------------------------------------------


def _retire(fake, ack=False):
    return asyncio.run(svc.retire(AGENT, "close_rate", _hash(fake), acknowledge_references=ack,
                                  current_user=USER, request=_req()))


def _point(ts):
    return {"metric": "close_rate", "ts": ts,
            "idempotency_key": metric_points_service.point_identity("close_rate", ts, None),
            "value_numeric": 31.4, "value_text": None, "dims": None,
            "execution_id": None, "created_at": ts}


def test_point_usage_counts_to_a_bound_and_reports_the_newest(db_backend):
    db.insert_metric_points(AGENT, [_point("2026-10-01T00:00:00.000000Z"),
                                    _point("2026-10-02T00:00:00.000000Z"),
                                    _point("2026-10-03T00:00:00.000000Z")])
    assert db.metric_point_usage(AGENT, "close_rate", 100) == (3, "2026-10-03T00:00:00.000000Z")
    assert db.metric_point_usage(AGENT, "close_rate", 2)[0] == 2
    assert db.metric_point_usage(AGENT, "other", 100) == (0, None)


def test_retiring_a_metric_with_points_needs_acknowledgement(agent):
    db.insert_metric_points(AGENT, [_point("2026-10-01T00:00:00.000000Z")])
    e = _refused(lambda: _retire(agent))
    assert (e.code, e.status) == ("metric_in_use", 409)
    assert e.extra["references"][0]["kind"] == "points"
    assert e.extra["references"][0]["label"] == "1 recorded points"
    assert agent.writes == []

    out = _retire(agent, ack=True)
    assert out["registry"]["retired"] == ["close_rate"]
    assert "close_rate" not in agent.text and "# keep me" in agent.text
    assert _active()["close_rate"]["status"] == "retired"
    assert db.metric_point_usage(AGENT, "close_rate", 10)[0] == 1    # points are kept


def test_an_unused_metric_retires_without_acknowledgement(agent):
    out = _retire(agent)
    assert out["success"] is True
    assert "metrics: []" in agent.text


def test_a_source_that_cannot_be_checked_still_needs_acknowledgement(agent, monkeypatch):
    monkeypatch.setattr(svc, "_objectives", AsyncMock(side_effect=RuntimeError("agent_stopped")))
    e = _refused(lambda: _retire(agent))
    assert e.code == "metric_in_use"
    assert e.extra["unchecked"] == ["objectives"]
    assert agent.writes == []


def test_registered_providers_are_named_and_a_failing_one_is_unchecked(agent):
    metric_references.register_provider(
        "linked", lambda a, m: [{"kind": "project", "label": "Q4 pipeline", "detail": "linked metric"}])

    def broken(a, m):
        raise RuntimeError("down")

    metric_references.register_provider("broken", broken)
    body = asyncio.run(svc.references(AGENT, "close_rate"))
    assert {"kind": "project", "label": "Q4 pipeline", "detail": "linked metric"} in body["references"]
    assert body["unchecked"] == ["broken"]


def test_objective_and_widget_references_are_listed(agent, monkeypatch):
    monkeypatch.setattr(svc, "_objectives", AsyncMock(return_value=[
        {"kind": "objective", "label": "q4-close-rate", "detail": "Lift close rate"}]))
    monkeypatch.setattr(svc, "_widgets", AsyncMock(return_value=[
        {"kind": "widget", "label": "Close rate", "detail": "dashboard.yaml"}]))
    kinds = [r["kind"] for r in asyncio.run(svc.references(AGENT, "close_rate"))["references"]]
    assert kinds == ["objective", "widget"]


_REAL_WIDGETS = svc._widgets  # the `agent` fixture stubs it; these tests run the real one


def _dashboard_answers(monkeypatch, body):
    """Fake the agent's GET /api/dashboard at the HTTP seam, so `_widgets` itself runs."""
    from services.agent_service import helpers

    async def fake_request(agent_name, method, path, **kw):
        assert path == "/api/dashboard"
        return SimpleNamespace(status_code=200, json=lambda: body)

    monkeypatch.setattr(helpers, "agent_http_request", fake_request)
    monkeypatch.setattr(svc, "_widgets", _REAL_WIDGETS)


def test_a_widget_bound_to_the_metric_is_found_in_the_real_dashboard_read(agent, monkeypatch):
    _dashboard_answers(monkeypatch, {"has_dashboard": True, "error": None, "config": {
        "title": "Sales", "sections": [{"widgets": [
            {"type": "metric", "metric": "close_rate", "label": "Close rate"},
            {"type": "metric", "metric": "other", "label": "Other"}]}]}})
    body = asyncio.run(svc.references(AGENT, "close_rate"))
    assert body["references"] == [{"kind": "widget", "label": "Close rate", "detail": "dashboard.yaml"}]
    assert body["unchecked"] == []


def test_a_broken_dashboard_is_unchecked_not_unused(agent, monkeypatch):
    # The agent answers 200 with config None and the parse error when the file
    # exists but cannot be read; that is a source that could not be checked (#2196).
    _dashboard_answers(monkeypatch, {"has_dashboard": False, "config": None,
                                     "error": "YAML parse error at line 3, column 1: oops"})
    body = asyncio.run(svc.references(AGENT, "close_rate"))
    assert body["references"] == []
    assert body["unchecked"] == ["dashboard"]


def test_no_dashboard_file_means_no_widget_uses_the_metric(agent, monkeypatch):
    _dashboard_answers(monkeypatch, {"has_dashboard": False, "config": None,
                                     "error": "No dashboard.yaml found at /home/developer/dashboard.yaml"})
    body = asyncio.run(svc.references(AGENT, "close_rate"))
    assert body["references"] == []
    assert body["unchecked"] == []


# ---------------------------------------------------------------------------
# routes: the HTTP shape and the gate
# ---------------------------------------------------------------------------


def test_a_refusal_reaches_the_client_as_a_named_detail(agent):
    agent.running = False
    with pytest.raises(HTTPException) as e:
        asyncio.run(agent_files.get_metric_declarations(AGENT))
    assert e.value.status_code == 409
    assert e.value.detail["code"] == "agent_not_running"


def _routes():
    return {(r.path, m): r for r in agent_files.router.routes for m in (getattr(r, "methods", ()) or ())}


def _calls(route) -> set:
    seen, stack = set(), list(route.dependant.dependencies)
    while stack:
        d = stack.pop()
        seen.add(getattr(d.call, "__name__", repr(d.call)))
        stack.extend(d.dependencies)
    return seen


DECLARATION_ROUTES = [
    ("/api/agents/{agent_name}/metrics/declarations", "GET"),
    ("/api/agents/{agent_name}/metrics/declarations", "POST"),
    ("/api/agents/{agent_name}/metrics/declarations/{metric_name}", "PUT"),
    ("/api/agents/{agent_name}/metrics/declarations/{metric_name}", "DELETE"),
    ("/api/agents/{agent_name}/metrics/declarations/{metric_name}/references", "GET"),
]


@pytest.mark.parametrize("key", DECLARATION_ROUTES)
def test_every_declaration_route_takes_the_owner_gate_and_the_capability(key):
    calls = _calls(_routes()[key])
    assert "require_person_or_instructions_manage" in calls, key
    assert "get_owned_agent_by_name" in calls, key


def test_an_agent_key_without_the_grant_is_refused(monkeypatch):
    route = _routes()[DECLARATION_ROUTES[1]]
    gate = next(d.call for d in route.dependant.dependencies
                if getattr(d.call, "__name__", "") == "require_person_or_instructions_manage")
    monkeypatch.setattr(db, "agent_has_capability", lambda agent, cap: False)
    import importlib
    audit = importlib.import_module("services.platform_audit_service")
    monkeypatch.setattr(audit.platform_audit_service, "log", AsyncMock(return_value="evt"))
    key = SimpleNamespace(id=1, username="owner", email="o@example.com", role="admin",
                          mcp_scope="agent", agent_name="sibling", connector_agent=None,
                          mcp_key_id="k1", vouched_source_agent=None)
    req = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), method="POST",
                          scope={"path": "/"}, state=SimpleNamespace(request_id="r1"),
                          path_params={"agent_name": AGENT})
    with pytest.raises(HTTPException) as e:
        asyncio.run(gate(req, key))
    assert e.value.status_code == 403
    assert e.value.detail["capability"] == "instructions.manage"


# ---------------------------------------------------------------------------
# the Files back door is closed
# ---------------------------------------------------------------------------


def test_template_yaml_is_an_instruction_file():
    assert policy.INSTRUCTIONS in policy.classes("template.yaml")
    assert policy.INSTRUCTIONS in policy.classes("/home/developer/template.yaml")
    # Only the agent's own top-level template, like CLAUDE.md.
    assert policy.INSTRUCTIONS not in policy.classes("docs/template.yaml")


def test_a_share_holder_cannot_write_template_yaml_through_files():
    assert policy.decide(policy.SHARE, policy.WRITE, "template.yaml").allowed is False
    assert policy.decide(policy.OWNER, policy.WRITE, "template.yaml").allowed is True
    # A share holder may still read it.
    assert policy.decide(policy.SHARE, policy.READ, "template.yaml").allowed is True


# ---------------------------------------------------------------------------
# the note reaches the read the tiles render
# ---------------------------------------------------------------------------


def test_recorded_by_is_on_the_metric_read(agent):
    from services import metric_read_service
    _declare(agent)
    body = metric_read_service.read_agent_metrics(AGENT)
    by_name = {m["name"]: m for m in body["metrics"]}
    assert by_name["proposals_sent"]["recorded_by"] == "the weekly skill"
    assert by_name["close_rate"]["recorded_by"] is None


def test_the_missing_dashboard_wording_matches_the_agent_server():
    # `_widgets` tells "no file" from "broken file" by this prefix; if the agent
    # server rewords it, every agent with no dashboard would read as unchecked.
    src = (_BACKEND.parent.parent / "docker" / "base-image" / "agent_server"
           / "routers" / "dashboard.py").read_text()
    assert f'"error": "{svc._NO_DASHBOARD_PREFIX}' in src
