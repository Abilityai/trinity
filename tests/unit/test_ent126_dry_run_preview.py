"""Dry-run preview: topology, schedules and resource blockers (ent#126).

AC #2 requires the preview to show agents-to-create, permission topology AND
schedules. This suite drives the real service through the router and pins:

* `permission_edges` across all three presets, `explicit`, `explicit: {}` and
  `permissions: None` — the same behaviour matrix the writer characterization
  suite pins, asserted here at the RESPONSE level so a preview cannot silently
  disagree with the writer it claims to describe;
* `schedules_preview`, including `enabled` defaulting to True (the reason the UI
  needs an acknowledgement gate);
* `permissions_configured` / `schedules_created` still reporting 0 on a dry run —
  they mean "written", and repurposing them would mislead existing consumers;
* a bad `cpu`/`memory` surfacing as `status: "invalid"` rather than a clean
  preview followed by a 100%-failed deploy;
* the additive response contract — the new keys are always present.
"""
from __future__ import annotations

import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.systems as systems
import services.agent_service.crud as crud
import services.system_service as system_service
from dependencies import get_current_user

pytestmark = pytest.mark.unit


def _user(role="admin"):
    return types.SimpleNamespace(
        id=1, username=role, role=role, email=f"{role}@example.com",
        agent_name=None, connector_agent=None,
    )


@pytest.fixture
def env(monkeypatch):
    # `local:` templates resolve fine; the catalog root does not exist under
    # pytest. Patched on the crud MODULE because the preflight lazy-imports it.
    monkeypatch.setattr(crud, "_resolve_local_template", lambda config: ({}, None))
    # Hermetic name resolution: no `_N` suffixing, no shared-SQLite reads.
    monkeypatch.setattr(
        system_service,
        "resolve_agent_names",
        lambda system_name, agents: ({s: f"{system_name}-{s}" for s in agents}, []),
    )
    app = FastAPI()
    app.include_router(systems.router)
    app.dependency_overrides[get_current_user] = lambda: _user("admin")
    return TestClient(app, raise_server_exceptions=False)


def _dry_run(client, manifest):
    r = client.post(
        "/api/systems/deploy", json={"manifest": manifest, "dry_run": True}
    )
    assert r.status_code == 200, r.text[:300]
    return r.json()


def _manifest(*, agents="", extra=""):
    return f"name: prev\nagents:\n{agents}{extra}"


TWO_PLAIN = """  a:
    template: local:default
  b:
    template: local:default
"""

THREE_PLAIN = TWO_PLAIN + """  c:
    template: local:default
"""


# ------------------------------------------------------------ topology matrix

def test_full_mesh_topology(env):
    body = _dry_run(env, _manifest(
        agents=THREE_PLAIN, extra="permissions:\n  preset: full-mesh\n"
    ))
    assert body["permission_edges"] == {
        "prev-a": ["prev-b", "prev-c"],
        "prev-b": ["prev-a", "prev-c"],
        "prev-c": ["prev-a", "prev-b"],
    }


def test_full_mesh_single_agent_has_no_edges(env):
    body = _dry_run(env, _manifest(
        agents="  solo:\n    template: local:default\n",
        extra="permissions:\n  preset: full-mesh\n",
    ))
    # `if targets:` — a lone agent is skipped, not written as an empty list.
    assert body["permission_edges"] == {}


def test_orchestrator_workers_topology(env):
    body = _dry_run(env, _manifest(
        agents="""  orchestrator:
    template: local:default
  w1:
    template: local:default
  w2:
    template: local:default
""",
        extra="permissions:\n  preset: orchestrator-workers\n",
    ))
    assert body["permission_edges"] == {
        "prev-orchestrator": ["prev-w1", "prev-w2"],
        # Workers are explicitly cleared — visible in the preview as empty lists.
        "prev-w1": [],
        "prev-w2": [],
    }


def test_orchestrator_workers_without_orchestrator_has_no_edges_but_warns(env):
    body = _dry_run(env, _manifest(
        agents=TWO_PLAIN, extra="permissions:\n  preset: orchestrator-workers\n"
    ))
    assert body["permission_edges"] == {}
    assert any("orchestrator" in w for w in body["warnings"])


def test_preset_none_shows_every_agent_cleared(env):
    body = _dry_run(env, _manifest(
        agents=TWO_PLAIN, extra="permissions:\n  preset: none\n"
    ))
    assert body["permission_edges"] == {"prev-a": [], "prev-b": []}


def test_explicit_topology_shows_clears_and_grants(env):
    body = _dry_run(env, _manifest(
        agents=THREE_PLAIN,
        extra="permissions:\n  explicit:\n    a:\n      - b\n",
    ))
    assert body["permission_edges"] == {
        # b and c are not explicit SOURCES, so both are cleared; b is then
        # granted-to as a target.
        "prev-b": [],
        "prev-c": [],
        "prev-a": ["prev-b"],
    }


def test_explicit_empty_mapping_has_no_edges(env):
    """`explicit: {}` is falsy — nothing is cleared. NOT the `none` preset."""
    body = _dry_run(env, _manifest(
        agents=TWO_PLAIN, extra="permissions:\n  explicit: {}\n"
    ))
    assert body["permission_edges"] == {}


def test_no_permissions_block_has_no_edges(env):
    body = _dry_run(env, _manifest(agents=TWO_PLAIN))
    assert body["permission_edges"] == {}


# ----------------------------------------------------------------- schedules

def test_schedules_preview_defaults_enabled_true(env):
    body = _dry_run(env, _manifest(agents="""  a:
    template: local:default
    schedules:
      - name: nightly
        cron: "0 3 * * *"
        message: "/run"
"""))
    assert body["schedules_preview"] == [{
        "agent": "prev-a",
        "short_name": "a",
        "name": "nightly",
        "cron": "0 3 * * *",
        "message": "/run",
        # The load-bearing default: a manifest that merely LISTS a schedule
        # starts autonomous recurring executions the moment it deploys.
        "enabled": True,
        "timezone": "UTC",
        "description": None,
    }]


def test_schedules_preview_honours_explicit_fields(env):
    body = _dry_run(env, _manifest(agents="""  a:
    template: local:default
    schedules:
      - name: quiet
        cron: "0 9 * * *"
        message: "/x"
        enabled: false
        timezone: Europe/London
        description: a description
"""))
    (sched,) = body["schedules_preview"]
    assert sched["enabled"] is False
    assert sched["timezone"] == "Europe/London"
    assert sched["description"] == "a description"


def test_schedules_preview_spans_agents_in_order(env):
    body = _dry_run(env, _manifest(agents="""  a:
    template: local:default
    schedules:
      - name: a1
        cron: "0 1 * * *"
        message: "/a1"
      - name: a2
        cron: "0 2 * * *"
        message: "/a2"
  b:
    template: local:default
    schedules:
      - name: b1
        cron: "0 3 * * *"
        message: "/b1"
"""))
    assert [(s["agent"], s["name"]) for s in body["schedules_preview"]] == [
        ("prev-a", "a1"), ("prev-a", "a2"), ("prev-b", "b1"),
    ]


def test_no_schedules_is_an_empty_list_not_null(env):
    body = _dry_run(env, _manifest(agents=TWO_PLAIN))
    assert body["schedules_preview"] == []


# ------------------------------------------------- shipped counters unchanged

def test_written_counters_stay_zero_on_a_dry_run(env):
    """`permissions_configured` / `schedules_created` mean WRITTEN.

    A dry run writes nothing, so they stay 0 even though the preview describes
    three permission edges and a schedule. Callers count the new arrays.
    """
    body = _dry_run(env, _manifest(
        agents="""  a:
    template: local:default
    schedules:
      - name: s
        cron: "0 3 * * *"
        message: "/s"
  b:
    template: local:default
""",
        extra="permissions:\n  preset: full-mesh\n",
    ))
    assert body["permissions_configured"] == 0
    assert body["schedules_created"] == 0
    assert body["tags_configured"] == 0
    # ...while the preview is populated.
    assert body["permission_edges"]
    assert body["schedules_preview"]


# ----------------------------------------------------------------- blockers

def test_bad_cpu_makes_the_preview_invalid(env):
    """The B1 regression: this manifest previewed `valid` and then failed 100%
    of its agents at create, returning HTTP 500."""
    body = _dry_run(env, _manifest(agents="""  a:
    template: local:default
    resources:
      cpu: 1.0
      memory: 2g
"""))
    assert body["status"] == "invalid"
    (failure,) = body["failed"]
    assert failure["short_name"] == "a"
    assert "Invalid cpu" in failure["reason"]
    assert "1.0" in failure["reason"]


def test_bad_memory_makes_the_preview_invalid(env):
    body = _dry_run(env, _manifest(agents="""  a:
    template: local:default
    resources:
      cpu: "2"
      memory: 512Mi
"""))
    assert body["status"] == "invalid"
    assert "Invalid memory" in body["failed"][0]["reason"]


def test_valid_resources_preview_clean(env):
    body = _dry_run(env, _manifest(agents="""  a:
    template: local:default
    resources:
      cpu: "4"
      memory: "8G"
"""))
    # Memory is case-folded by normalize_memory, so "8G" is fine.
    assert body["status"] == "valid"
    assert body["failed"] == []


def test_absent_resources_preview_clean(env):
    """No `resources:` block must not become a blocker — the normalizers fall
    back to the fleet defaults, exactly as create does."""
    body = _dry_run(env, _manifest(agents=TWO_PLAIN))
    assert body["status"] == "valid"
    assert body["failed"] == []


def test_template_resources_win_over_a_bad_manifest_value(env, monkeypatch):
    """Mirrors the create path's precedence: `_resolve_local_template` replaces
    config.resources when the template declares a block, so a bad manifest value
    is DISCARDED and must not be reported as a blocker."""
    def resolve_with_resources(config):
        config.resources = {"cpu": "2", "memory": "4g"}
        return ({}, None)
    monkeypatch.setattr(crud, "_resolve_local_template", resolve_with_resources)
    body = _dry_run(env, _manifest(agents="""  a:
    template: local:default
    resources:
      cpu: 1.0
"""))
    assert body["status"] == "valid", body["failed"]


def test_unresolvable_local_template_is_still_a_blocker(env, monkeypatch):
    """#1841's own guarantee must survive the resource work."""
    from fastapi import HTTPException

    def boom(config):
        raise HTTPException(status_code=404, detail="Unknown local template")
    monkeypatch.setattr(crud, "_resolve_local_template", boom)
    body = _dry_run(env, _manifest(agents=TWO_PLAIN))
    assert body["status"] == "invalid"
    assert len(body["failed"]) == 2
    assert body["failed"][0]["status_code"] == 404


# --------------------------------------------------- response contract + view

def test_dry_run_response_always_carries_the_new_keys(env):
    body = _dry_run(env, _manifest(agents=TWO_PLAIN))
    for key in ("permission_edges", "schedules_preview", "system_view_requested",
                "agents_to_create", "prompt_updated", "warnings", "failed"):
        assert key in body, f"missing {key}"


def test_agents_to_create_carries_resolved_names(env):
    body = _dry_run(env, _manifest(agents=TWO_PLAIN))
    assert body["agents_to_create"] == [
        {"name": "prev-a", "short_name": "a", "template": "local:default"},
        {"name": "prev-b", "short_name": "b", "template": "local:default"},
    ]


def test_prompt_updated_flags_a_platform_wide_mutation(env):
    body = _dry_run(env, _manifest(
        agents=TWO_PLAIN, extra='prompt: |\n  I replace trinity_prompt.\n'
    ))
    assert body["prompt_updated"] is True


def test_system_view_requested_reflects_the_manifest(env):
    without = _dry_run(env, _manifest(agents=TWO_PLAIN))
    assert without["system_view_requested"] is False

    with_view = _dry_run(env, _manifest(
        agents=TWO_PLAIN, extra="system_view:\n  name: My View\n"
    ))
    assert with_view["system_view_requested"] is True


def test_unknown_top_level_keys_warn_without_failing(env):
    """The `trinity_prompt:`-for-`prompt:` class: warned, never rejected."""
    body = _dry_run(env, _manifest(
        agents=TWO_PLAIN, extra="trinity_prompt: oops\nauto_start: true\n"
    ))
    assert body["status"] == "valid"
    warning = next(w for w in body["warnings"] if "unknown top-level" in w)
    assert "trinity_prompt" in warning
    assert "auto_start" in warning
    # The prompt was NOT installed under the wrong key.
    assert body["prompt_updated"] is False


# ------------------------------------------------------------ error mapping

def test_unparseable_manifest_is_400_with_a_string_detail(env):
    r = env.post("/api/systems/deploy",
                 json={"manifest": "name: [bad\n  ::::\n", "dry_run": True})
    assert r.status_code == 400
    assert isinstance(r.json()["detail"], str)


def test_validation_error_is_400_with_a_string_detail(env):
    r = env.post("/api/systems/deploy", json={
        "manifest": "name: Bad_Name\nagents:\n  a:\n    template: local:default\n",
        "dry_run": True,
    })
    assert r.status_code == 400
    assert "Invalid system name" in r.json()["detail"]


def test_oversized_manifest_is_422_with_a_list_detail(env):
    """The Field(max_length=...) cap. FastAPI reports request-model violations as
    a LIST of errors, which is a distinct shape the frontend normalizer handles."""
    from models import MANIFEST_MAX_BYTES
    r = env.post("/api/systems/deploy",
                 json={"manifest": "x" * (MANIFEST_MAX_BYTES + 1), "dry_run": True})
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)
