"""Every bundled manifest must actually deploy (ent#126).

This is the test whose absence let a broken manifest ship. `acme-consulting.yaml`
carried `cpu: 1.0` — an unquoted YAML float, which `normalize_cpu` rejects because
it compares against the STRING set ("1","2","4","8","16") — so every deploy of it
failed 100% of its agents and returned HTTP 500. Nothing caught it because nothing
read that file: `default-system.yaml` (the first-run seed) was the only manifest
any code path touched, and it uses the quoted `"1"`.

trinity-enterprise#126 puts a UI card in front of each of these files, which
promotes them from zero-consumer samples to the primary one-click install path on
a fresh install. So they are inside the blast radius, and this table-driven test
walks the real directory rather than naming files: a manifest added later is
covered automatically.

Deliberately checks the things a "does it parse" test would miss:
  * merged resources pass the create path's OWN validators;
  * no two manifests declare the same system name (they would fight over
    `{system}-{short}` names and produce `_N` duplicates of each other);
  * no unrecognised top-level keys (the `trinity_prompt:`-for-`prompt:` class);
  * the dry-run reports `valid`, through the real service.
"""
from __future__ import annotations

import asyncio
import types
from pathlib import Path

import pytest

import services.system_service as system_service
from services.agent_service.capabilities import normalize_cpu, normalize_memory

pytestmark = pytest.mark.unit

# The repo's real bundled directory, located from THIS file rather than the CWD —
# pytest's working directory is not guaranteed to be the repo root.
MANIFESTS_DIR = Path(__file__).resolve().parents[2] / "config" / "manifests"

MANIFEST_FILES = sorted(MANIFESTS_DIR.glob("*.yaml")) + sorted(
    MANIFESTS_DIR.glob("*.yml")
)
# Fail loudly rather than vacuously passing an empty parametrization if the
# directory ever moves.
IDS = [p.name for p in MANIFEST_FILES]


def _user():
    return types.SimpleNamespace(
        id=1, username="admin", role="admin", email="admin@example.com",
        agent_name=None, connector_agent=None, mcp_scope=None,
    )


def test_bundled_manifest_directory_is_not_empty():
    assert MANIFEST_FILES, f"no bundled manifests found under {MANIFESTS_DIR}"


@pytest.mark.parametrize("path", MANIFEST_FILES, ids=IDS)
def test_bundled_manifest_parses(path: Path):
    manifest = system_service.parse_manifest(path.read_text(encoding="utf-8"))
    assert manifest.name
    assert manifest.agents, "a manifest with no agents deploys nothing"


@pytest.mark.parametrize("path", MANIFEST_FILES, ids=IDS)
def test_bundled_manifest_validates(path: Path):
    manifest = system_service.parse_manifest(path.read_text(encoding="utf-8"))
    # Raises ValueError on a bad name / template prefix / preset / schedule / tag.
    system_service.validate_manifest(manifest)


@pytest.mark.parametrize("path", MANIFEST_FILES, ids=IDS)
def test_bundled_manifest_has_no_unknown_top_level_keys(path: Path):
    """Catches the `trinity_prompt:`-instead-of-`prompt:` class.

    A key the parser does not read is silently dropped, so the author's intent
    (here: install a system prompt) vanishes with no signal at all.
    """
    manifest = system_service.parse_manifest(path.read_text(encoding="utf-8"))
    assert manifest.unknown_keys == [], (
        f"{path.name} declares unrecognized top-level key(s) "
        f"{manifest.unknown_keys}, which are silently ignored at deploy"
    )


@pytest.mark.parametrize("path", MANIFEST_FILES, ids=IDS)
def test_bundled_manifest_resources_pass_create_path_validators(path: Path):
    """THE regression test for `cpu: 1.0`.

    Runs the create path's own normalizers over each agent's MERGED resources,
    with the create path's own precedence: `_resolve_local_template` replaces
    config.resources when the template declares a block, so a manifest value only
    survives when the template is silent — which is the case that failed.
    """
    from models import AgentConfig
    from services.agent_service.crud import _get_default_resource, _resolve_local_template

    manifest = system_service.parse_manifest(path.read_text(encoding="utf-8"))
    for short_name, config in manifest.agents.items():
        merged = AgentConfig(
            name=f"{manifest.name}-{short_name}",
            template=config.template,
            resources=dict(config.resources or {}),
        )
        if config.template and config.template.startswith("local:"):
            _resolve_local_template(merged)

        # Each raises ValueError with an actionable message on a bad value.
        normalize_cpu(merged.resources.get("cpu"), _get_default_resource("cpu"))
        normalize_memory(merged.resources.get("memory"), _get_default_resource("memory"))


@pytest.mark.parametrize("path", MANIFEST_FILES, ids=IDS)
def test_bundled_manifest_dry_runs_clean(path: Path):
    """End-to-end through the real service: the preview must report `valid`.

    `invalid` here means a real deploy would fail — the preflight resolves
    `local:` templates and validates merged resources.
    """
    result = asyncio.run(
        system_service.deploy_manifest(
            path.read_text(encoding="utf-8"), _user(), None, dry_run=True
        )
    )
    assert result.status == "valid", (
        f"{path.name} previews as {result.status}: "
        f"{[(f.short_name, f.reason) for f in result.failed]}"
    )


def test_bundled_manifests_declare_distinct_system_names():
    """Two manifests sharing a system name collide on every agent name.

    `acme-consulting.yaml` and `default-system.yaml` both declared `name: acme`
    with the same three short names. Since default-system.yaml is seeded on every
    fresh install, installing the other one resolved to acme-scout_2 / acme-sage_2
    / acme-scribe_2 — a duplicate fleet whose recovery is manual and per-agent.
    On a fresh install that was the DEFAULT path, not an edge case.
    """
    by_name: dict[str, list[str]] = {}
    for path in MANIFEST_FILES:
        manifest = system_service.parse_manifest(path.read_text(encoding="utf-8"))
        by_name.setdefault(manifest.name, []).append(path.name)

    collisions = {name: files for name, files in by_name.items() if len(files) > 1}
    assert not collisions, f"bundled manifests share a system name: {collisions}"


@pytest.mark.parametrize("path", MANIFEST_FILES, ids=IDS)
def test_bundled_manifest_is_within_the_size_cap(path: Path):
    from models import MANIFEST_MAX_BYTES

    assert path.stat().st_size <= MANIFEST_MAX_BYTES


def test_default_system_manifest_stays_schedule_free_and_prompt_free():
    """Pins the first-run seed's two deliberate omissions (ent#124).

    A zero-credential fresh install must not accumulate failing cron executions,
    and `prompt:` would overwrite the platform-wide trinity_prompt on first boot.
    Both are stated in that file's own header; this makes them enforced rather
    than merely documented, since the ent#126 picker now makes it clickable too.
    """
    path = MANIFESTS_DIR / "default-system.yaml"
    manifest = system_service.parse_manifest(path.read_text(encoding="utf-8"))
    assert manifest.prompt is None
    for short_name, config in manifest.agents.items():
        assert not config.schedules, f"{short_name} gained a schedule"
