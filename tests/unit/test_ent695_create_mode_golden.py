"""Create-mode golden: a byte-level characterization of `POST /api/systems/deploy`
and of the first-run seeder (trinity-enterprise#695, acceptance criterion 6).

Why this exists
---------------
trinity-enterprise#695 adds a reconcile ("apply") verb for system manifests next
to today's create-only deploy. Its AC6 is that create mode stays unchanged in
every respect: existing callers see byte-identical behaviour. This module is the
tripwire for that. It records, BEFORE any apply code existed, what create mode
returns, what fleet state it writes and what it hands to the agent-create and
agent-start seams, and it fails on any difference. It imports no apply code and
must keep passing, unmodified, once apply lands.

What is pinned (fixture: `fixtures/ent695_create_mode_golden.json`)
------------------------------------------------------------------
* `POST /api/systems/deploy`, the raw response bytes, status code and content
  type, for: dry run `valid`; dry run `invalid` (an absent `local:` template and
  an unusable cpu value); the `_2` conflict preview against a seeded agent;
  `deployed`; `partial`; `failed` (the 500 body); the `strict` abort (status +
  detail); the named 400 for an empty `agents: {}`; and a non-admin creator's
  deploy over a base name another user's agent holds. For each, also the
  fleet-state rows the request wrote, the `AgentConfig` fields handed to create
  and the start calls, in order.
* The route's OpenAPI operation and the `SystemDeployRequest`,
  `SystemDeployResponse`, `SystemDeployFailure` and `SystemSchedulePreview`
  component schemas, in their rendered key order, minus their free-text
  `description` annotations (the handler's and the models' docstrings).
* The "other shape" (fresh install): the first-run seed pass on an install with
  only the admin account (no agents, no tags, no schedules, no settings, no
  repository: the bundled manifest uses `local:` templates). The real
  `ensure_first_run_seeded` runs the real `deploy_manifest` over the real
  bundled `config/manifests/default-system.yaml`; pinned are the pass result,
  the deploy result, the flag writes (`first_run_fresh`, `default_system_seeded`,
  `default_system_seed_info`) and the fleet rows.

What is real and what is a seam
-------------------------------
Real: the router (`require_role` + `capability_fence`, mounted on a bare FastAPI
app: `main.py`'s middleware and exception handlers are not in the path, and
none of the pinned cases reaches them), `deploy_manifest` and
every function it calls (`parse_manifest`, `validate_manifest`, name
resolution against the real `db`, the dry-run preflight through the create
path's own `_resolve_local_template` over the in-repo template catalog,
`configure_folders` / `configure_permissions` / `create_schedules` /
`configure_tags` / `create_system_view` / `start_all_agents`), the seeder, and
the database (`sqlite_only_backend`: the full production schema on a fresh
SQLite file per test). Seams: the authenticated principal (a dependency
override), and the two `routers.agents` facade functions create mode resolves
at call time, `create_agent_internal` (records the call, raises where a case
says so, otherwise registers the ownership row the real create leaves behind)
and `start_agent_internal` (records the call, raises where a case says so).
Those two need Docker; everything create mode decides itself runs for real.

Stability rules the fixture follows
-----------------------------------
* Generated ids are substituted with placeholders and nothing else is: the
  System View id (it appears in the `deployed` body) and schedule ids.
* Timestamp columns are not recorded; the clock the seeder stamps into
  `default_system_seed_info.seeded_at` is frozen at its binding in
  `system_seed_service` (`utc_now_iso`), so the fixture pins a date and never
  compares a literal to the current time.
* Set-valued data is compared as a set: `system_views.filter_tags` is stored
  from a Python `set`, so it is recorded sorted; rows are recorded sorted.
* `manifest_sha256` in the seed info is recorded as a placeholder that the test
  computes from the bundled file, so a comment edit to that manifest is not
  mistaken for a code change. Any edit that changes what the seed DEPLOYS still
  turns this red, deliberately.
* The OpenAPI fragment is the contract a create-mode client reads: paths,
  operation id, request and response schemas, field names, types, defaults
  and required sets. Prose is not contract, so a docstring edit does not turn
  it red. A dependency upgrade (FastAPI / pydantic) that renders the contract
  differently is still a change for those clients and does.

Generation rule (read this before touching the fixture)
-------------------------------------------------------
Generated once, on commit f754dfb02 (`dev` before any apply code existed),
with the clock frozen at
2026-10-27T09:00:00.000000Z. NEVER regenerate it to make a red run green. A red
run here means create mode changed, which is a behaviour change for every
existing caller (UI, MCP `deploy_system`, the first-run seeder, scripts) and
needs its own issue. Only once such an issue rules the change intended,
regenerate in that issue's PR, serially (no xdist, no shuffle):

    TRINITY_ENT695_REGENERATE_GOLDEN=<issue-ref> \\
        python -m pytest -p no:randomly -p no:xdist \\
        tests/unit/test_ent695_create_mode_golden.py

A regenerating run rewrites the cases it executes, records the issue in
`_meta.regenerated_by`, and then FAILS every case on purpose, so it can never
pass for a normal run. Review the fixture diff, then run the module again
without the variable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

import db_harness
import routers.agents as agents_router
import routers.systems as systems
import services.system_seed_service as sss
import services.system_service as system_service
from database import db
from db.engine import get_engine
from db_harness import seed_agent, seed_user
from models import User

pytestmark = pytest.mark.unit

# The full production schema on a fresh SQLite file per test. Bound by
# assignment, not by a bare import, so no unused-import autofix can drop it.
sqlite_only_backend = db_harness.sqlite_only_backend

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "ent695_create_mode_golden.json"
)
BUNDLED_MANIFEST = REPO_ROOT / "config" / "manifests" / "default-system.yaml"

BASE_COMMIT = "f754dfb02"
FROZEN_NOW = "2026-10-27T09:00:00.000000Z"
REGENERATE_ENV = "TRINITY_ENT695_REGENERATE_GOLDEN"

# Every case the fixture holds. A case in the fixture that no test produces is
# an orphan and fails `test_golden_holds_exactly_the_exercised_cases`.
CASES = (
    "dry_run_valid",
    "dry_run_invalid",
    "dry_run_name_conflict",
    "deployed",
    "partial",
    "failed",
    "strict_abort",
    "empty_agents_400",
    "deployed_by_creator_over_foreign_name",
    "openapi",
    "first_run_seed_fresh_install",
)

ADMIN = User(id=1, username="admin", email=None, role="admin")
# A non-admin creator, and the other user whose agent already holds a base name.
CREATOR = User(id=2, username="maker", email=None, role="creator")
OTHER_OWNER_ID, OTHER_OWNER = 3, "someone-else"


# --- manifests --------------------------------------------------------------
# One manifest per behaviour, each touching as many create-mode branches as it
# can without depending on anything outside this repository: an unknown
# top-level key and an unknown per-agent key (the two validation warnings), a
# platform prompt, resources, partial `folders`, schedules with and without
# their optional keys, mixed-case tags, `kind`, an explicit permission matrix
# with a target-only agent (cleared first) and an explicit self-target (the
# resolver keeps it and counts it; the permission writer drops it), default
# tags and a private System View.

GOLD_MANIFEST = """
name: gold
description: ent#695 create-mode golden
prompt: You are part of the gold system.
trinity_prompt: a typo for prompt, kept to pin the unknown-key warning
agents:
  lead:
    template: local:default
    resources:
      cpu: "4"
      memory: "8g"
    folders:
      expose: true
    schedules:
      - name: daily-report
        cron: "0 9 * * *"
        message: Write the daily report
      - name: weekly-sync
        cron: "30 8 * * 1"
        message: Run the weekly sync
        enabled: false
        timezone: Europe/Berlin
        description: Monday sync
    tags: [Research, lead-only]
    kind: agent
  helper:
    template: local:scout
    folders:
      consume: true
    credentials: {}
    kind: deployment
  watcher:
    template: local:default
permissions:
  explicit:
    lead: [helper, watcher, lead]
    helper: [lead]
default_tags: [Gold-Team]
system_view:
  name: Gold System
  icon: G
  color: "#d4af37"
  shared: false
"""

# Two different preview blockers beside a member that previews clean: an absent
# `local:` template (the create path's own 404) and an unusable cpu on a
# `github:` template (validated as declared, since the template is never probed).
BROKEN_MANIFEST = """
name: broken
agents:
  ghost:
    template: local:no-such-template-ent695
  heavy:
    template: github:example-org/example-agent
    resources:
      cpu: 1.0
  fine:
    template: local:default
"""

PARTIAL_MANIFEST = """
name: part
prompt: Partial-deploy prompt.
agents:
  orchestrator:
    template: local:default
  worker-a:
    template: local:default
    folders:
      expose: true
      consume: true
    schedules:
      - name: nightly
        cron: "0 2 * * *"
        message: Run the nightly job
  worker-b:
    template: local:default
    tags: [Ops]
permissions:
  preset: orchestrator-workers
default_tags: [fleet]
"""

FAILED_MANIFEST = """
name: doomed
prompt: Must not be written when nothing is created.
agents:
  one:
    template: local:default
  two:
    template: github:example-org/missing-repo
  three:
    template: local:default
system_view:
  name: Doomed
"""

# The shape a desired-state file has before anything is declared in it.
EMPTY_MANIFEST = """
name: blank
prompt: ""
agents: {}
"""

STRICT_MANIFEST = """
name: strict
prompt: Must not be written when strict aborts.
agents:
  first:
    template: local:default
  second:
    template: local:default
  third:
    template: local:default
permissions:
  preset: full-mesh
"""


# --- fleet state --------------------------------------------------------------
# The tables create mode writes, with the columns it decides. Timestamps and the
# autoincrement key are left out (clock- and order-dependent, not create-mode
# decisions); `agent_ownership` is left out because, in this harness, the create
# seam writes it, not create mode.
_FLEET_TABLES = {
    "agent_permissions": ("source_agent", "target_agent", "created_by"),
    "agent_shared_folder_config": ("agent_name", "expose_enabled", "consume_enabled"),
    "agent_tags": ("agent_name", "tag"),
    "agent_schedules": (
        "id",
        "agent_name",
        "name",
        "cron_expression",
        "message",
        "enabled",
        "timezone",
        "description",
        "owner_id",
        "deleted_at",
    ),
    "system_views": (
        "id",
        "name",
        "description",
        "icon",
        "color",
        "filter_tags",
        "owner_id",
        "is_shared",
    ),
    "system_settings": ("key", "value"),
    "operator_queue": (
        "id",
        "agent_name",
        "type",
        "status",
        "priority",
        "title",
        "question",
    ),
}


def _fleet_snapshot() -> dict:
    with get_engine().connect() as conn:
        return {
            table: [
                dict(row)
                for row in conn.execute(
                    text(f"SELECT {', '.join(cols)} FROM {table}")
                ).mappings()
            ]
            for table, cols in _FLEET_TABLES.items()
        }


def _generated_ids(*snapshots: dict) -> dict:
    """{actual generated id: stable placeholder}, named by the row's natural key.

    Takes every snapshot a case compares, so a row that only the BEFORE side
    holds (a removal) is reported with a placeholder too, never a raw id.
    """
    ids = {}
    for snapshot in snapshots:
        for row in snapshot["system_views"]:
            ids[row["id"]] = f"<generated system_views.id: {row['name']}>"
        for row in snapshot["agent_schedules"]:
            ids[row["id"]] = (
                f"<generated agent_schedules.id: {row['agent_name']}/{row['name']}>"
            )
    return ids


def _canonical_row(table: str, row: dict, ids: dict, replacements: dict) -> dict:
    out = {}
    for key, value in row.items():
        if key == "id" and value in ids:
            value = ids[value]
        if table == "system_views" and key == "filter_tags":
            value = sorted(json.loads(value))  # stored from a Python set
        if isinstance(value, str):
            for actual, placeholder in replacements.items():
                value = value.replace(actual, placeholder)
        out[key] = value
    return out


def _fleet_writes(
    before: dict, after: dict, ids: dict, replacements: dict | None = None
) -> dict:
    """Rows the request added and rows it removed, per table, canonicalized.

    A multiset difference, not a set one: two rows that canonicalize alike (two
    same-named schedules on one agent share a placeholder id) must count twice,
    or a writer that duplicated a row would read as unchanged.
    """
    replacements = replacements or {}

    def canon(snapshot, table):
        return Counter(
            json.dumps(_canonical_row(table, row, ids, replacements), sort_keys=True)
            for row in snapshot[table]
        )

    writes = {}
    for table in _FLEET_TABLES:
        b, a = canon(before, table), canon(after, table)
        writes[table] = {
            "added": [json.loads(r) for r in sorted((a - b).elements())],
            "removed": [json.loads(r) for r in sorted((b - a).elements())],
        }
    return writes


# --- the two Docker-bound seams ------------------------------------------------


class _Seams:
    """Stands in for `routers.agents.create_agent_internal` / `start_agent_internal`.

    Create records the `AgentConfig` fields create mode decides (name, template,
    resources, kind) and the call's other arguments, raises when the case says
    the create fails, and otherwise registers the live ownership row the real
    create leaves behind, which the later create-mode writers depend on (the
    `agent_tags` foreign key, `create_schedule`'s access and liveness gates).
    """

    def __init__(self, fail_create=None, fail_start=None):
        self.fail_create = dict(fail_create or {})
        self.fail_start = dict(fail_start or {})
        self.create_calls: list[dict] = []
        self.start_calls: list[str] = []

    async def create(
        self, config, current_user, request, skip_name_sanitization=False, **kwargs
    ):
        self.create_calls.append(
            {
                "name": config.name,
                "template": config.template,
                "resources": (
                    dict(config.resources) if config.resources is not None else None
                ),
                "kind": config.kind,
                "as_user": current_user.username,
                "skip_name_sanitization": skip_name_sanitization,
                "request_passed": request is not None,
                "other_kwargs": sorted(kwargs),
            }
        )
        exc = self.fail_create.get(config.name)
        if exc is not None:
            raise exc
        if not db.register_agent_owner(config.name, current_user.username):
            raise AssertionError(f"harness: could not register {config.name}")
        return {"status": "created"}  # deploy_manifest ignores the return value

    async def start(self, agent_name):
        self.start_calls.append(agent_name)
        exc = self.fail_start.get(agent_name)
        if exc is not None:
            raise exc
        return {"status": "started"}


@pytest.fixture
def install(sqlite_only_backend, monkeypatch):
    """A set-up install: the full schema, one admin account, nothing else."""
    # Own the lazily imported modules, so a sibling's sys.modules stub can never
    # reach the call-time `from routers.agents import ...` / `from
    # services.system_service import ...` inside create mode (learnings 2026-07-12).
    monkeypatch.setitem(sys.modules, "routers.agents", agents_router)
    monkeypatch.setitem(sys.modules, "services.system_service", system_service)
    for var in ("ANTHROPIC_API_KEY", "ADMIN_USERNAME", sss.MANIFEST_ENV_VAR):
        monkeypatch.delenv(var, raising=False)
    seed_user(user_id=ADMIN.id, username=ADMIN.username, role=ADMIN.role)
    return monkeypatch


def _wire(monkeypatch, seams: _Seams) -> None:
    monkeypatch.setattr(agents_router, "create_agent_internal", seams.create)
    monkeypatch.setattr(agents_router, "start_agent_internal", seams.start)


def _client(principal: User = ADMIN) -> TestClient:
    app = FastAPI()
    app.include_router(systems.router)
    # The route's own binding, so the override keys the exact function object
    # its `require_role` / `capability_fence` dependencies close over.
    app.dependency_overrides[systems.get_current_user] = lambda: principal
    return TestClient(app, raise_server_exceptions=False)


def _deploy_case(
    monkeypatch, body: dict, seams: _Seams | None = None, principal: User = ADMIN
) -> dict:
    seams = seams or _Seams()
    _wire(monkeypatch, seams)
    before = _fleet_snapshot()
    resp = _client(principal).post("/api/systems/deploy", json=body)
    after = _fleet_snapshot()
    ids = _generated_ids(before, after)
    raw = resp.content.decode("utf-8")
    for actual, placeholder in ids.items():
        raw = raw.replace(actual, placeholder)
    return {
        "status_code": resp.status_code,
        "content_type": resp.headers.get("content-type"),
        "body": raw,
        "create_calls": seams.create_calls,
        "start_calls": seams.start_calls,
        "fleet_writes": _fleet_writes(before, after, ids),
    }


# --- the golden ---------------------------------------------------------------


def _load_golden() -> dict:
    with GOLDEN_PATH.open(encoding="utf-8") as f:
        return json.load(f)


# A regeneration records the issue that ruled it; the first generation does not.
_GOLDEN_EXISTED_AT_IMPORT = GOLDEN_PATH.exists()


def _generation_meta() -> dict:
    import platform

    import fastapi
    import pydantic

    return {
        "issue": "abilityai/trinity-enterprise#695 (AC6)",
        "base_commit": BASE_COMMIT,
        "clock": FROZEN_NOW,
        "generated_with": {
            "python": platform.python_version(),
            "fastapi": fastapi.__version__,
            "pydantic": pydantic.VERSION,
        },
        "rule": (
            "Never regenerate to make a red run green: a difference is a create-mode "
            "behaviour change and needs its own issue. See the test module docstring."
        ),
    }


def _check(case: str, observed: dict) -> None:
    assert case in CASES, f"unregistered case {case!r}"
    regenerate_for = os.environ.get(REGENERATE_ENV, "").strip()
    if regenerate_for:
        if os.environ.get("PYTEST_XDIST_WORKER"):
            pytest.fail(
                f"{REGENERATE_ENV} must run serially (-p no:xdist): the fixture is one file"
            )
        if GOLDEN_PATH.exists():
            golden = _load_golden()
        else:
            golden = {"_meta": _generation_meta(), "cases": {}}
        if _GOLDEN_EXISTED_AT_IMPORT:
            regenerated_by = golden["_meta"].setdefault("regenerated_by", [])
            if regenerate_for not in regenerated_by:
                regenerated_by.append(regenerate_for)
        golden["cases"][case] = observed
        GOLDEN_PATH.write_text(
            json.dumps(golden, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        pytest.fail(
            f"regenerated golden case {case!r} for {regenerate_for}: review the fixture "
            "diff, then run again without the variable (a regenerating run never passes)"
        )
    expected = _load_golden()["cases"].get(case)
    assert (
        expected is not None
    ), f"golden case {case!r} is missing from {GOLDEN_PATH.name}"
    assert observed == expected, (
        f"create mode changed ({case}): this golden pins AC6 of "
        "trinity-enterprise#695. Do not regenerate it to pass; see the module docstring."
    )
    # Equal as values is not equal as bytes: also pin the key order the JSON
    # carries (the response bodies are already compared as raw strings).
    assert json.dumps(observed) == json.dumps(expected), f"key order changed ({case})"


# --- POST /api/systems/deploy ---------------------------------------------------


def test_dry_run_valid(install):
    _check(
        "dry_run_valid",
        _deploy_case(install, {"manifest": GOLD_MANIFEST, "dry_run": True}),
    )


def test_dry_run_invalid(install):
    _check(
        "dry_run_invalid",
        _deploy_case(install, {"manifest": BROKEN_MANIFEST, "dry_run": True}),
    )


def test_dry_run_name_conflict(install):
    """The `_2` preview: a base name owned by an existing agent is suffixed.

    The existing agent carries no tags, the shape of a member deployed before
    systems were tagged: create mode reads no membership and suffixes anyway.
    """
    seed_agent("gold-lead", owner_id=ADMIN.id)
    _check(
        "dry_run_name_conflict",
        _deploy_case(install, {"manifest": GOLD_MANIFEST, "dry_run": True}),
    )


def test_deployed(install):
    """A real deploy with the request's defaults (`dry_run` and `strict` omitted)."""
    _check("deployed", _deploy_case(install, {"manifest": GOLD_MANIFEST}))


def test_partial(install):
    seams = _Seams(
        fail_create={
            "part-orchestrator": HTTPException(
                status_code=400,
                detail={
                    "error": "Template could not be cloned",
                    "code": "CLONE_FAILED",
                },
            )
        },
        fail_start={"part-worker-a": RuntimeError("container exited (137)")},
    )
    _check("partial", _deploy_case(install, {"manifest": PARTIAL_MANIFEST}, seams))


def test_failed_returns_500_with_report(install):
    """Nothing created: HTTP 500 carrying the full report, reasons sanitized."""
    seams = _Seams(
        fail_create={
            "doomed-one": RuntimeError(
                "git clone failed: https://deploy-bot:placeholder-not-a-token@git.example.com/org/agent.git"
            ),
            "doomed-two": HTTPException(
                status_code=404, detail="Template repository not found"
            ),
            "doomed-three": ValueError("quota exceeded: " + "x" * 600),
        }
    )
    _check("failed", _deploy_case(install, {"manifest": FAILED_MANIFEST}, seams))


def test_strict_abort(install):
    """`strict: true` aborts on the first failure and keeps its status code."""
    seams = _Seams(
        fail_create={
            "strict-second": HTTPException(
                status_code=409, detail="Agent name already taken"
            ),
        }
    )
    _check(
        "strict_abort",
        _deploy_case(install, {"manifest": STRICT_MANIFEST, "strict": True}, seams),
    )


def _without_prose(node, in_properties=False):
    """The fragment minus its free-text `description` annotations.

    Prose (the handler's docstring, a model's docstring) is documentation, not
    contract, so editing it must not read as a create-mode change. A schema
    PROPERTY that happens to be named `description` (`SystemSchedulePreview`
    has one) is contract and is kept: only a key sitting directly under
    `properties` is a property name.
    """
    if isinstance(node, dict):
        return {
            key: _without_prose(value, in_properties=(key == "properties"))
            for key, value in node.items()
            if in_properties or not (key == "description" and isinstance(value, str))
        }
    if isinstance(node, list):
        return [_without_prose(item) for item in node]
    return node


def test_empty_agents_is_a_named_400(install):
    """`agents: {}` is refused by the parser, before any write or name lookup."""
    _check("empty_agents_400", _deploy_case(install, {"manifest": EMPTY_MANIFEST}))


def test_deployed_by_creator_over_foreign_name(install):
    """A non-admin creator deploys over a base name another user's agent holds.

    Create mode suffixes it (`gold-lead_2`), names the existing agent but never
    its owner, makes the caller the owner of everything it writes, and lets a
    creator overwrite the platform-wide `trinity_prompt`: a known gap, pinned
    here so that a change to it is a decision with its own issue.
    """
    seed_user(user_id=CREATOR.id, username=CREATOR.username, role=CREATOR.role)
    seed_user(user_id=OTHER_OWNER_ID, username=OTHER_OWNER, role="creator")
    seed_agent("gold-lead", owner_id=OTHER_OWNER_ID)
    _check(
        "deployed_by_creator_over_foreign_name",
        _deploy_case(install, {"manifest": GOLD_MANIFEST}, principal=CREATOR),
    )


def test_openapi_contract():
    raw = _client().get("/openapi.json").content
    spec = json.loads(raw)
    schemas = spec["components"]["schemas"]
    _check(
        "openapi",
        _without_prose(
            {
                "operation": spec["paths"]["/api/systems/deploy"]["post"],
                "schemas": {
                    name: schemas[name]
                    for name in (
                        "SystemDeployRequest",
                        "SystemDeployResponse",
                        "SystemDeployFailure",
                        "SystemSchedulePreview",
                    )
                },
            }
        ),
    )


# --- the other shape: fresh install ---------------------------------------------


def test_first_run_seed_on_fresh_install(install):
    """The first-run pass on an install that has only its admin account.

    The Cornelius seeder is the one collaborator replaced outright: it is not
    create mode, and it would clone a GitHub template. It records the verdict it
    was handed; the system seeder and `deploy_manifest` run for real.
    """
    monkeypatch = install
    seams = _Seams()
    _wire(monkeypatch, seams)
    monkeypatch.setattr(sss, "docker_client", object())  # Docker reachable
    monkeypatch.setattr(
        sss, "get_breaker_redis", lambda: None
    )  # no Redis: locks fail open
    monkeypatch.setattr(sss, "BUNDLED_MANIFEST_PATH", str(BUNDLED_MANIFEST))
    monkeypatch.setattr(
        sss, "utc_now_iso", lambda: FROZEN_NOW
    )  # the `seeded_at` binding

    cornelius_calls = []

    async def cornelius_seed(fresh):
        cornelius_calls.append({"fresh": fresh})
        return {
            "agent": "cornelius",
            "action": "none",
            "status": "skipped",
            "message": "replaced by the ent#695 golden harness",
        }

    monkeypatch.setattr(sss.cornelius_agent_service, "ensure_seeded", cornelius_seed)

    real_deploy = system_service.deploy_manifest
    deploy_calls = []

    async def deploy_spy(manifest_yaml, current_user, request=None, **kwargs):
        result = await real_deploy(manifest_yaml, current_user, request, **kwargs)
        deploy_calls.append(
            {
                "manifest_is_the_bundled_file": manifest_yaml
                == BUNDLED_MANIFEST.read_text(encoding="utf-8"),
                "as_user": current_user.model_dump(
                    include={"id", "username", "email", "role", "mcp_scope"}
                ),
                "request_passed": request is not None,
                "kwargs": kwargs,
                "result": result.model_dump_json(),
            }
        )
        return result

    monkeypatch.setattr(system_service, "deploy_manifest", deploy_spy)

    before = _fleet_snapshot()
    # The shape under test: nothing but the admin account exists yet.
    assert all(not rows for rows in before.values()), before
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM agent_ownership")).scalar() == 0

    result = asyncio.run(sss.ensure_first_run_seeded())

    after = _fleet_snapshot()
    sha = hashlib.sha256(
        BUNDLED_MANIFEST.read_text(encoding="utf-8").encode("utf-8")
    ).hexdigest()
    ids = _generated_ids(before, after)
    _check(
        "first_run_seed_fresh_install",
        {
            "seed_pass_result": result,
            "cornelius_calls": cornelius_calls,
            "deploy_calls": deploy_calls,
            "create_calls": seams.create_calls,
            "start_calls": seams.start_calls,
            "fleet_writes": _fleet_writes(
                before,
                after,
                ids,
                {sha: "<sha256 of config/manifests/default-system.yaml>"},
            ),
        },
    )


# --- the fixture itself ---------------------------------------------------------


def test_fleet_diff_counts_duplicate_rows_and_masks_removed_ids():
    """Two same-named schedules on one agent share a placeholder id, so their
    canonical rows are equal; the diff must still report two rows, not one. A
    row only the before side holds is reported removed, under its placeholder."""
    row = {
        "id": "s1",
        "agent_name": "a",
        "name": "nightly",
        "cron_expression": "0 2 * * *",
        "message": "m",
        "enabled": 1,
        "timezone": "UTC",
        "description": None,
        "owner_id": 1,
        "deleted_at": None,
    }
    empty = {table: [] for table in _FLEET_TABLES}
    one = {**empty, "agent_schedules": [row]}
    two = {**empty, "agent_schedules": [row, {**row, "id": "s2"}]}

    def added(after):
        return _fleet_writes(empty, after, _generated_ids(after))["agent_schedules"][
            "added"
        ]

    assert len(added(one)) == 1
    assert len(added(two)) == 2
    assert _fleet_writes(one, two, _generated_ids(two))["agent_schedules"]["added"] == [
        {**row, "id": "<generated agent_schedules.id: a/nightly>"}
    ]
    removed = _fleet_writes(two, one, _generated_ids(two, one))["agent_schedules"]
    assert removed == {
        "added": [],
        "removed": [{**row, "id": "<generated agent_schedules.id: a/nightly>"}],
    }


def test_golden_holds_exactly_the_exercised_cases():
    golden = _load_golden()
    assert sorted(golden["cases"]) == sorted(CASES)
    assert golden["_meta"]["base_commit"] == BASE_COMMIT
    assert golden["_meta"]["clock"] == FROZEN_NOW
