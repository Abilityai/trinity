"""Bundled-manifest catalog endpoints (ent#126).

  GET /api/systems/manifests
  GET /api/systems/manifests/{manifest_id}

True unit tests: the router is mounted on a bare FastAPI app and the catalog is
pointed at a tmp_path via TRINITY_MANIFESTS_DIR, so nothing here depends on the
repo's real `config/manifests` (that directory has its own suite,
`test_ent126_bundled_manifests.py`) or on the process CWD.

The two groups that earn their keep:

* **Route collisions (Invariant #4).** BOTH new routes sit under a prefix that
  already has parameterized siblings, and there are TWO distinct collisions —
  `/manifests` vs `GET /{system_name}`, and `/manifests/manifest` vs
  `GET /{system_name}/manifest`. Declared in the wrong order, each fails
  *silently and plausibly* ("system 'manifests' not found"), which is the worst
  kind of routing bug. Asserted through real requests, distinguishing the
  handlers by response shape rather than just the status code.

* **Path confinement.** Probed against the traversal shapes the guard is built
  for. Note the regex `^[A-Za-z0-9._-]+$` does NOT reject `..` — dots are inside
  the character class — so the explicit dot-segment rejection is a separate,
  load-bearing layer (#1759: a guard tested against 9 leak shapes missed 8).
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.systems as systems
import services.system_service as system_service
from dependencies import get_current_user

pytestmark = pytest.mark.unit


GOOD_MANIFEST = """
name: catalog-demo
description: A demo system
agents:
  alpha:
    template: local:default
    resources:
      cpu: "2"
      memory: "4g"
    schedules:
      - name: nightly
        cron: "0 3 * * *"
        message: "/run"
  beta:
    template: local:default
permissions:
  preset: full-mesh
"""

PROMPT_MANIFEST = """
name: prompt-setter
prompt: |
  I overwrite the platform-wide trinity_prompt.
agents:
  solo:
    template: local:default
"""

BAD_CPU_MANIFEST = """
name: badcpu
agents:
  solo:
    template: local:default
    resources:
      cpu: 1.0
"""

UNPARSEABLE = "name: [this is not\n  valid yaml: ::::\n"

INVALID_NAME_MANIFEST = """
name: Not_A_Valid_Name
agents:
  solo:
    template: local:default
"""


def _user(role="admin"):
    return types.SimpleNamespace(
        id=1, username=role, role=role, email=f"{role}@example.com",
        agent_name=None, connector_agent=None, mcp_scope=None,
    )


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Router alone, catalog pointed at tmp_path, `local:` templates resolvable."""
    monkeypatch.setenv(system_service.MANIFESTS_DIR_ENV, str(tmp_path))
    # The curated template catalog root does not exist under pytest; stub the
    # resolver seam on the crud MODULE because the preflight lazy-imports it at
    # call time (a module-level import there would close a cycle).
    import services.agent_service.crud as crud
    monkeypatch.setattr(crud, "_resolve_local_template", lambda config: ({}, None))
    # Hermetic already_deployed: no shared-SQLite reads.
    monkeypatch.setattr(system_service, "agent_exists", lambda name: False)

    app = FastAPI()
    app.include_router(systems.router)
    holder = {"user": _user("admin")}
    app.dependency_overrides[get_current_user] = lambda: holder["user"]
    client = TestClient(app, raise_server_exceptions=False)
    return types.SimpleNamespace(
        client=client, dir=tmp_path, holder=holder,
        write=lambda name, body: (tmp_path / name).write_text(body, encoding="utf-8"),
    )


# ------------------------------------------------------------------- listing

def test_list_empty_directory_is_empty_not_an_error(env):
    r = env.client.get("/api/systems/manifests")
    assert r.status_code == 200
    assert r.json() == []


def test_empty_env_var_falls_back_to_the_default_directory(monkeypatch):
    """`TRINITY_MANIFESTS_DIR=""` must mean "use the default", not `Path("")`.

    Load-bearing since ent#126 wired the var into both compose files as
    `${TRINITY_MANIFESTS_DIR:-}`: every deployment now sets it to the EMPTY STRING
    unless an operator overrides it. `os.getenv(name, default)` would return `""`
    there — `Path("")` is the CWD, so the catalog would list nothing on every
    install while looking perfectly configured. The `or` is what prevents it.

    Exactly the trap #1759 hit one seam over, where an empty `HOST_TEMPLATES_PATH`
    turned `Path("") / name` into an empty NAMED VOLUME.
    """
    monkeypatch.setenv(system_service.MANIFESTS_DIR_ENV, "")
    assert system_service._manifests_dir() == Path("config/manifests")

    monkeypatch.setenv(system_service.MANIFESTS_DIR_ENV, "   ")
    # Whitespace is a real (if odd) path; only truly empty falls back. Pinned so
    # the behaviour is a decision rather than an accident.
    assert system_service._manifests_dir() != Path("config/manifests")

    monkeypatch.delenv(system_service.MANIFESTS_DIR_ENV, raising=False)
    assert system_service._manifests_dir() == Path("config/manifests")


def test_list_missing_directory_is_empty_not_an_error(env, monkeypatch, tmp_path):
    monkeypatch.setenv(system_service.MANIFESTS_DIR_ENV, str(tmp_path / "nope"))
    r = env.client.get("/api/systems/manifests")
    assert r.status_code == 200
    assert r.json() == []


def test_list_reports_summary_fields(env):
    env.write("catalog-demo.yaml", GOOD_MANIFEST)
    r = env.client.get("/api/systems/manifests")
    assert r.status_code == 200
    (entry,) = r.json()
    assert entry["id"] == "catalog-demo"
    assert entry["filename"] == "catalog-demo.yaml"
    assert entry["name"] == "catalog-demo"
    assert entry["description"] == "A demo system"
    assert entry["agent_count"] == 2
    assert entry["templates"] == ["local:default"]
    assert entry["schedule_count"] == 1
    assert entry["sets_prompt"] is False
    assert entry["permissions_preset"] == "full-mesh"
    assert entry["valid"] is True
    assert entry["reason"] is None
    assert entry["already_deployed"] is False


def test_list_flags_a_manifest_that_overwrites_the_platform_prompt(env):
    """`sets_prompt` gates the UI's acknowledgement checkbox, so it must be
    reported from the LISTING — before the user commits to a card."""
    env.write("prompt-setter.yaml", PROMPT_MANIFEST)
    (entry,) = env.client.get("/api/systems/manifests").json()
    assert entry["sets_prompt"] is True


def test_list_marks_already_deployed(env, monkeypatch):
    monkeypatch.setattr(
        system_service, "agent_exists", lambda name: name == "catalog-demo-alpha"
    )
    env.write("catalog-demo.yaml", GOOD_MANIFEST)
    (entry,) = env.client.get("/api/systems/manifests").json()
    assert entry["already_deployed"] is True


def test_list_survives_a_db_error_when_checking_deployed_state(env, monkeypatch):
    def boom(name):
        raise RuntimeError("db down")
    monkeypatch.setattr(system_service, "agent_exists", boom)
    env.write("catalog-demo.yaml", GOOD_MANIFEST)
    r = env.client.get("/api/systems/manifests")
    assert r.status_code == 200
    (entry,) = r.json()
    # The manifest is still described; only the deployed marker degrades.
    assert entry["valid"] is True
    assert entry["already_deployed"] is False


# -------------------------------------------------------------- fail-soft

def test_unparseable_manifest_is_listed_as_invalid_not_a_500(env):
    env.write("broken.yaml", UNPARSEABLE)
    r = env.client.get("/api/systems/manifests")
    assert r.status_code == 200
    (entry,) = r.json()
    assert entry["id"] == "broken"
    assert entry["valid"] is False
    assert entry["reason"]
    assert entry["name"] is None


def test_one_bad_manifest_does_not_hide_the_good_ones(env):
    """The whole point of fail-soft: a broken file hiding its neighbours is how a
    broken bundled manifest stays invisible."""
    env.write("broken.yaml", UNPARSEABLE)
    env.write("catalog-demo.yaml", GOOD_MANIFEST)
    entries = {e["id"]: e for e in env.client.get("/api/systems/manifests").json()}
    assert set(entries) == {"broken", "catalog-demo"}
    assert entries["catalog-demo"]["valid"] is True
    assert entries["broken"]["valid"] is False


def test_manifest_failing_validation_is_invalid_with_a_reason(env):
    env.write("badname.yaml", INVALID_NAME_MANIFEST)
    (entry,) = env.client.get("/api/systems/manifests").json()
    assert entry["valid"] is False
    assert "Invalid system name" in entry["reason"]


def test_manifest_with_unusable_resources_is_invalid(env):
    """`valid` runs the resource preflight too, not just parse+validate — this is
    the `cpu: 1.0` class, which parses and validates perfectly well."""
    env.write("badcpu.yaml", BAD_CPU_MANIFEST)
    (entry,) = env.client.get("/api/systems/manifests").json()
    assert entry["valid"] is False
    assert "Invalid cpu" in entry["reason"]


def test_oversized_manifest_is_invalid_not_a_500(env):
    from models import MANIFEST_MAX_BYTES
    env.write("huge.yaml", "# " + ("x" * (MANIFEST_MAX_BYTES + 10)))
    r = env.client.get("/api/systems/manifests")
    assert r.status_code == 200
    (entry,) = r.json()
    assert entry["valid"] is False
    assert "larger than" in entry["reason"]


# --------------------------------------------------- reason hygiene (exit point)

# Every `reason` leaves `_assess_manifest` through `_failure_reason` — the same
# exit point the deploy report's `failed[].reason` uses. Both inputs it reports on
# are hostile-shaped: PyYAML parse errors ECHO the offending source line, and
# `validate_manifest` interpolates manifest-supplied values into its message. A
# `creator` can already read the raw YAML via `GET /manifests/{id}`, so this is not
# a disclosure fix today; it is a hygiene invariant on the field a reader trusts,
# on the surface most likely to be widened to a looser role later.

# Deliberately SHORT. A 40-char `ghp_` PAT "passes" these assertions for the wrong
# reason: PyYAML truncates its own echoed context line, chopping the PAT below the
# pattern's 36-char floor, so nothing has to redact anything. AKIA+16 survives it.
_SHORT_SECRET = "AKIA" + "B" * 16


def test_parse_error_reason_is_credential_sanitized(env):
    """The parse error quotes the offending line back — secrets included."""
    env.write("leaky.yaml", f"name: [unclosed\n  k: {_SHORT_SECRET}\n  x: ::::\n")
    (entry,) = env.client.get("/api/systems/manifests").json()
    assert entry["valid"] is False
    assert _SHORT_SECRET not in entry["reason"]
    assert "REDACTED" in entry["reason"]


def test_validation_error_reason_is_credential_sanitized(env):
    """`validate_manifest` interpolates the manifest's own value into the message."""
    env.write(
        "leaky.yaml",
        f"name: BAD_{_SHORT_SECRET}\nagents:\n  s:\n    template: local:default\n",
    )
    (entry,) = env.client.get("/api/systems/manifests").json()
    assert entry["valid"] is False
    assert _SHORT_SECRET not in entry["reason"]
    assert "REDACTED" in entry["reason"]


def test_validation_error_reason_redacts_url_userinfo(env):
    """PAT-bearing remote URLs are the git/GitHub shape (learnings 2026-07-14)."""
    env.write(
        "leaky.yaml",
        "name: https://user:p4ssw0rd@example.com/x\n"
        "agents:\n  s:\n    template: local:default\n",
    )
    (entry,) = env.client.get("/api/systems/manifests").json()
    assert "p4ssw0rd" not in entry["reason"]
    assert "***@example.com" in entry["reason"]


_MANY_BAD_AGENTS = "name: many\nagents:\n" + "".join(
    f"  agent{i}:\n    template: local:default\n    resources:\n      cpu: 1.0\n"
    for i in range(40)
)


@pytest.mark.parametrize("body", [
    # The validate branch interpolates an unbounded manifest-supplied value.
    pytest.param(
        "name: " + "X" * 4000 + "\nagents:\n  s:\n    template: local:default\n",
        id="validate",
    ),
    # The blockers branch: each `failure.reason` is already capped, but the JOIN
    # of N of them was not — this manifest yields one blocker per agent.
    pytest.param(_MANY_BAD_AGENTS, id="blockers"),
])
def test_a_reason_that_grows_with_the_manifest_is_capped(env, body):
    """`reason` is a response field, so it must not scale with the input file.

    Asserted as `== _REASON_MAX_LEN` rather than `<=`: both bodies are built to
    overflow, so an exact match is what proves the cap actually fired — `<=` would
    keep passing if the underlying message ever shrank enough to make the case
    vacuous. The parse branch is deliberately absent: PyYAML truncates its own
    context line, so that reason lands around 252 chars and could never test this.
    """
    env.write("big.yaml", body)
    (entry,) = env.client.get("/api/systems/manifests").json()
    assert entry["valid"] is False
    assert len(entry["reason"]) == system_service._REASON_MAX_LEN


# ------------------------------------------------------------- file selection

def test_non_yaml_files_are_ignored(env):
    env.write("notes.txt", "not a manifest")
    env.write("README.md", "# docs")
    env.write("catalog-demo.yaml", GOOD_MANIFEST)
    entries = env.client.get("/api/systems/manifests").json()
    assert [e["id"] for e in entries] == ["catalog-demo"]


def test_yml_suffix_is_listed(env):
    env.write("shortsuffix.yml", GOOD_MANIFEST)
    (entry,) = env.client.get("/api/systems/manifests").json()
    assert entry["filename"] == "shortsuffix.yml"


def test_mixed_case_suffix_is_listed(env):
    env.write("shouty.YAML", GOOD_MANIFEST)
    (entry,) = env.client.get("/api/systems/manifests").json()
    assert entry["filename"] == "shouty.YAML"


def test_symlinked_manifest_is_not_served(env, tmp_path):
    real = tmp_path / "real-target.yaml"
    real.write_text(GOOD_MANIFEST, encoding="utf-8")
    (tmp_path / "linked.yaml").symlink_to(real)
    ids = [e["id"] for e in env.client.get("/api/systems/manifests").json()]
    assert "linked" not in ids
    assert "real-target" in ids


# ----------------------------------------------------------------- read one

def test_read_returns_raw_yaml_and_summary(env):
    env.write("catalog-demo.yaml", GOOD_MANIFEST)
    r = env.client.get("/api/systems/manifests/catalog-demo")
    assert r.status_code == 200
    body = r.json()
    assert body["manifest"] == GOOD_MANIFEST
    assert body["id"] == "catalog-demo"
    assert body["valid"] is True


def test_read_tolerates_an_explicit_extension(env):
    env.write("catalog-demo.yaml", GOOD_MANIFEST)
    r = env.client.get("/api/systems/manifests/catalog-demo.yaml")
    assert r.status_code == 200
    assert r.json()["id"] == "catalog-demo"


def test_read_unknown_id_is_404(env):
    r = env.client.get("/api/systems/manifests/nope")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]


def test_read_an_invalid_manifest_still_returns_its_text(env):
    """A card marked invalid must still open in the editor — that is how the user
    fixes it."""
    env.write("broken.yaml", UNPARSEABLE)
    r = env.client.get("/api/systems/manifests/broken")
    assert r.status_code == 200
    assert r.json()["valid"] is False
    assert r.json()["manifest"] == UNPARSEABLE


# --------------------------------------------------------- path confinement

@pytest.mark.parametrize("bad_id", [
    "...",          # dot-only segment the regex happily matches
    "..yaml",       # extension-stripping leaves a bare ".."
    "..yml",
    "a..b",         # embedded dot segment
    "%2e%2e",       # FastAPI percent-decodes BEFORE the guard sees it -> ".."
    "x" * 129,      # over the length cap
])
def test_malformed_id_reaching_the_handler_is_400(env, bad_id):
    """Ids that actually reach the handler must be rejected by the guard.

    Every case here survives URL normalisation, so the guard is the only thing
    standing between it and a file read. `..yaml` matters because the extension
    tolerance strips the suffix and would otherwise hand a bare ".." onward, and
    `%2e%2e` matters because it proves the decode happens upstream of the regex.
    """
    r = env.client.get(f"/api/systems/manifests/{bad_id}")
    assert r.status_code == 400, f"{bad_id!r} -> {r.status_code} {r.text[:120]}"


@pytest.mark.parametrize("bad_id", [
    "..",                       # URL-normalised to /api/systems/ (list_systems)
    ".",                        # URL-normalised to /api/systems/manifests (list)
    "...",                      # reaches the guard -> 400
    "..yaml",                   # reaches the guard -> 400
    "a..b",                     # reaches the guard -> 400
    "%2e%2e",                   # decoded then caught by the guard -> 400
    "../../etc/passwd",         # normalised away at routing -> 404
    "../default-system",
    "..%2f..%2fetc%2fpasswd",
    "%2E%2E%2Fsecret",
    "foo/bar",
    "/etc/passwd",
])
def test_no_traversal_shape_ever_returns_manifest_content(env, bad_id):
    """The property that actually matters, asserted across every shape.

    Two different layers stop these and it is worth being explicit about which:
    dot segments like `..` and `.` are collapsed by URL normalisation before
    routing (so `..` lands on `GET /api/systems/` and `.` on the catalog listing —
    both harmless, neither a traversal), while anything that survives
    normalisation is rejected by the guard. Asserting `400` uniformly would be
    asserting the wrong thing and would fail for reasons that are not bugs; what
    must hold is that no response ever carries manifest content from outside the
    configured directory.
    """
    env.write("catalog-demo.yaml", GOOD_MANIFEST)
    r = env.client.get(f"/api/systems/manifests/{bad_id}")
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    assert not (isinstance(body, dict) and "manifest" in body), (
        f"{bad_id!r} leaked manifest content: {r.text[:160]}"
    )


def test_symlink_escape_is_refused(env, tmp_path):
    """A symlink INSIDE the directory pointing outside it. `.resolve()` +
    `is_relative_to` is the layer that catches this; the character allowlist
    cannot see it."""
    outside = tmp_path.parent / "outside-secret.yaml"
    outside.write_text(GOOD_MANIFEST, encoding="utf-8")
    (tmp_path / "escape.yaml").symlink_to(outside)
    r = env.client.get("/api/systems/manifests/escape")
    # Refused as a symlink (O_NOFOLLOW) or as escaping confinement — never 200.
    assert r.status_code != 200, r.text[:200]


def test_read_refuses_a_symlink_pointing_INSIDE_the_directory(env, tmp_path):
    """Parity with the listing, which skips symlinks outright.

    Confinement is not the issue here — the target is inside the directory. The
    issue is that the two routes must agree on what the catalog contains: a
    manifest absent from `GET /manifests` must not be readable via
    `GET /manifests/{id}`, or "not listed" stops meaning "not served".
    """
    env.write("real-target.yaml", GOOD_MANIFEST)
    (tmp_path / "inside-link.yaml").symlink_to(tmp_path / "real-target.yaml")

    ids = [e["id"] for e in env.client.get("/api/systems/manifests").json()]
    assert "inside-link" not in ids
    assert "real-target" in ids

    r = env.client.get("/api/systems/manifests/inside-link")
    assert r.status_code == 400, r.text[:200]
    assert "symlink" in r.json()["detail"].lower()
    # The real file is still addressable by its own id.
    assert env.client.get("/api/systems/manifests/real-target").status_code == 200


def test_absolute_path_id_does_not_escape(env, tmp_path):
    env.write("catalog-demo.yaml", GOOD_MANIFEST)
    r = env.client.get("/api/systems/manifests/%2Fetc%2Fpasswd")
    assert r.status_code in (400, 404)


# ------------------------------------------------- Invariant #4 route order

def test_list_route_is_not_captured_by_the_system_name_route(env):
    """`GET /api/systems/manifests` must reach the LIST endpoint.

    Declared after `GET /{system_name}` it is swallowed as a system named
    "manifests" and 404s from get_system — silently and plausibly.
    """
    env.write("catalog-demo.yaml", GOOD_MANIFEST)
    r = env.client.get("/api/systems/manifests")
    assert r.status_code == 200
    body = r.json()
    # get_system returns a dict {name, agent_count, agents}; the catalog a list.
    assert isinstance(body, list)
    assert [e["id"] for e in body] == ["catalog-demo"]


def test_detail_route_is_not_captured_by_the_system_manifest_export_route(env):
    """`GET /api/systems/manifests/manifest` ALSO matches
    `GET /{system_name}/manifest` with system_name="manifests".

    Distinguished by response shape: the export route is a PlainTextResponse, so
    a JSON `detail` body proves the detail route handled it.
    """
    r = env.client.get("/api/systems/manifests/manifest")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["detail"] == "Bundled manifest 'manifest' not found"


def test_a_real_system_named_like_the_catalog_still_exports(env, monkeypatch):
    """The guard must not have broken the sibling route it shadows."""
    monkeypatch.setattr(
        "routers.agents.get_accessible_agents",
        lambda user: [{"name": "realsys-alpha", "status": "running",
                       "template": "local:default", "created_at": "2026-01-01"}],
    )
    monkeypatch.setattr(
        system_service, "export_manifest", lambda name, agents: "name: realsys\n"
    )
    r = env.client.get("/api/systems/realsys/manifest")
    assert r.status_code == 200
    assert r.text == "name: realsys\n"


# ------------------------------------------------------------- authorization

@pytest.mark.parametrize("role", ["user", "operator"])
def test_below_creator_is_forbidden(env, role):
    """Both routes mirror POST /deploy's `require_role("creator")` (AC #6)."""
    env.write("catalog-demo.yaml", GOOD_MANIFEST)
    env.holder["user"] = _user(role)
    assert env.client.get("/api/systems/manifests").status_code == 403
    assert env.client.get("/api/systems/manifests/catalog-demo").status_code == 403


@pytest.mark.parametrize("role", ["creator", "admin"])
def test_creator_and_above_allowed(env, role):
    env.write("catalog-demo.yaml", GOOD_MANIFEST)
    env.holder["user"] = _user(role)
    assert env.client.get("/api/systems/manifests").status_code == 200
    assert env.client.get("/api/systems/manifests/catalog-demo").status_code == 200


def test_connector_principal_is_rejected(env):
    """require_role also runs _reject_connector_principal, so a connector-scoped
    key cannot browse the catalog even with a sufficient role."""
    env.write("catalog-demo.yaml", GOOD_MANIFEST)
    connector = _user("admin")
    connector.connector_agent = "some-agent"
    env.holder["user"] = connector
    assert env.client.get("/api/systems/manifests").status_code == 403
