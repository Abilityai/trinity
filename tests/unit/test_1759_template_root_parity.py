"""Parity guard: the CREATE resolver and the LISTING surface must agree on
where curated local templates live (#1759).

Two surfaces resolve `local:` templates and they disagreed outside a container:

* `services/template_service.py::_local_templates_dir()` (LISTING) has a
  repo-relative fallback, added by #843.
* `services/agent_service/crud.py::_LOCAL_TEMPLATE_ROOTS` (CREATE) had none —
  two absolute container paths resolved at import.

Before #1759 that asymmetry was merely latent (an unresolvable template
silently produced a blank agent). Once create HARD-FAILS on an absent
template, the asymmetry becomes: `GET /api/templates` lists `local:scout`
while `POST /api/agents` 400s it in every source-run backend (dev shell,
source-run CI, unit tests).

**This file must NOT use the `_load_crud` MagicMock harness.** In that harness
`services.template_service` is a MagicMock, so a gate that called
`_local_templates_dir()` would be satisfied by a truthy mock and the #1484
characterization tests would stay green on the OLD behaviour — silently. The
parity is therefore pinned here, against BOTH real modules, in the same
byte-parity-mirror spirit as `test_credential_paths_parity.py` /
`test_model_context_parity.py` (Invariant #5).

The specific bug this exists to kill: `crud.py` sits ONE directory deeper than
`template_service.py` (`services/agent_service/` vs `services/`), so a
copy-pasted `.parent * 4` fallback yields `<repo>/src/config/agent-templates`,
which does not exist — and every `local:` create would 400 on a source run.

Target: src/backend/services/agent_service/crud.py
Issue:  abilityai/trinity#1759
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Env prerequisites before any backend import (repo test convention). These
# modules are imported FOR REAL here, so the backend's import-time bootstrap
# (config + init_database) must find a usable environment.
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
_TMP_DB = Path(tempfile.gettempdir()) / "trinity_test_1759_root_parity.db"
os.environ.setdefault("TRINITY_DB_PATH", str(_TMP_DB))

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = str(_PROJECT_ROOT / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

_CONTAINER_CURATED_ROOT = Path("/agent-configs/templates")


@pytest.fixture(scope="module")
def real_modules():
    """The two REAL modules — no mocks, no `_load_crud`."""
    import services.agent_service.crud as crud
    import services.template_service as template_service

    return crud, template_service


def test_crud_repo_fallback_points_at_the_real_catalog(real_modules):
    """The strongest, least tautological assertion: the fallback must resolve
    to a directory that actually contains a shipped template."""
    crud, _ = real_modules
    repo_dir = crud._repo_local_templates_dir()

    assert repo_dir.is_dir(), (
        f"crud's repo-relative template fallback does not exist: {repo_dir}. "
        f"`parents[4]`, not `.parent * 4` — crud.py is one directory deeper "
        f"than template_service.py."
    )
    assert (
        repo_dir / "scout" / "template.yaml"
    ).is_file(), f"{repo_dir} is not the curated catalog (no scout/template.yaml)."
    # The exact wrong answer that `.parent * 4` produces.
    assert repo_dir.parent.name != "src", (
        f"{repo_dir} looks like the `.parent * 4` off-by-one "
        f"(<repo>/src/config/agent-templates)."
    )


def test_crud_and_template_service_fallbacks_agree(real_modules):
    """Both fallbacks are derived from their own module file, so this pins the
    DEPTH RELATIONSHIP between the two modules rather than copying a constant.
    If either module moves directories, this fails."""
    crud, template_service = real_modules
    ts_fallback = (
        Path(template_service.__file__).resolve().parents[3]
        / "config"
        / "agent-templates"
    )
    assert crud._repo_local_templates_dir() == ts_fallback


def test_curated_root_matches_listing_surface(real_modules):
    """End-to-end parity: the root CREATE resolves against is the directory
    LISTING enumerates — in a container (bind mount present) and out of one."""
    crud, template_service = real_modules
    listing_dir = template_service._local_templates_dir()
    curated_root = crud._LOCAL_TEMPLATE_ROOTS[0]

    assert curated_root == listing_dir.resolve()
    if not _CONTAINER_CURATED_ROOT.exists():
        assert curated_root == crud._repo_local_templates_dir()


def test_deploy_local_root_is_unchanged(real_modules):
    """Root[1] is a runtime writable store with no repo equivalent (#950) —
    it must stay the absolute container path, byte-identical to
    `deploy.DEPLOYED_TEMPLATES_DIR_IN_BACKEND`."""
    crud, _ = real_modules
    assert crud._LOCAL_TEMPLATE_ROOTS[1] == Path("/data/deployed-templates")
    assert len(crud._LOCAL_TEMPLATE_ROOTS) == 2


def test_every_shipped_template_resolves_through_the_create_gate(real_modules):
    """No template in the shipped catalog may 400 on create.

    `hidden: true` fixtures are included on purpose — they are excluded from
    the user-facing listing but remain creatable by id, which the test/canary
    harness depends on.
    """
    crud, _ = real_modules
    catalog = crud._repo_local_templates_dir()
    names = sorted(
        d.name
        for d in catalog.iterdir()
        if d.is_dir() and (d / "template.yaml").is_file()
    )
    assert names, f"no templates found under {catalog}"

    unresolvable = [
        n
        for n in names
        if not (
            crud._safe_local_template_path(n, crud._LOCAL_TEMPLATE_ROOTS[0])
            / "template.yaml"
        ).exists()
    ]
    assert (
        not unresolvable
    ), f"shipped templates unreachable by the create resolver: {unresolvable}"
    # AC#2: `local:default` is referenced by the live-server suites and by the
    # onboarding/manifest docs as a real, deployable template id.
    assert "default" in names


# ===========================================================================
# Seam 2, container branch — the byte-identity claim D1-A3 rests on
# ===========================================================================
#
# Widening the `/template` bind decision was justified by "the delta in the
# verified container path is provably zero". Every other test in this branch
# runs on a source checkout, where `/agent-configs/templates` does NOT exist —
# so they only ever exercise the repo-fallback branch, and the container branch
# (the one that actually ships) had no coverage at all. `.exists()` is read
# from the module global at call time, so pointing that global at a real
# directory exercises the in-container branch without a container.


def test_container_branch_host_base_is_byte_identical_to_pre_1759(
    real_modules, monkeypatch, tmp_path
):
    """Inside a Trinity container the bind-source base must be the literal
    pre-#1759 default, character for character.

    If this drifts, every containerised `local:` create silently changes where
    `/template` is mounted from — the failure mode would be a wrong-but-present
    template, which is strictly worse than the blank agent #1759 set out to
    kill.
    """
    crud, _ = real_modules
    fake_container_root = tmp_path / "agent-configs" / "templates"
    fake_container_root.mkdir(parents=True)
    monkeypatch.setattr(crud, "_CONTAINER_CURATED_TEMPLATES", fake_container_root)

    assert crud._default_host_templates_base() == "./config/agent-templates"
    # ...and the curated root follows the bind mount, not the repo.
    assert crud._curated_templates_root() == fake_container_root.resolve()


def test_host_branch_base_is_absolute_so_docker_accepts_it(
    real_modules, monkeypatch, tmp_path
):
    """Outside a container the repo path IS a host path, so the fallback must
    be absolute — Docker rejects a relative bind source outright."""
    crud, _ = real_modules
    monkeypatch.setattr(
        crud, "_CONTAINER_CURATED_TEMPLATES", tmp_path / "definitely-absent"
    )

    base = crud._default_host_templates_base()
    assert Path(base).is_absolute(), base
    assert base == str(crud._repo_local_templates_dir())


def test_1900_local_template_name_regex_parity(real_modules):
    """The read-path allowlist must stay byte-identical to the create path's.

    `template_service._LOCAL_TEMPLATE_NAME_RE` (#1900) is DUPLICATED from
    `crud._LOCAL_TEMPLATE_NAME_RE` (#950), not imported, in both directions:

      * crud → template_service is forbidden — `services.template_service` is
        MagicMocked by the #1484 characterization harness, so a gate calling
        into it would be satisfied by a truthy mock and those tests would stay
        green on the OLD behaviour (the same reasoning that makes
        `crud._repo_local_templates_dir` hand-rolled, crud.py:73-87).
      * template_service → crud would close an import cycle (crud imports
        template_service).

    Duplication is only safe while something pins the copies equal. This is it.
    Note the shared `$`-matches-before-a-trailing-newline edge is deliberate on
    both sides (D5): "fixing" one copy to `\\Z` silently breaks this guard.
    """
    crud, template_service = real_modules

    assert (
        template_service._LOCAL_TEMPLATE_NAME_RE.pattern
        == crud._LOCAL_TEMPLATE_NAME_RE.pattern
    )


def test_1900_read_and_create_barriers_agree_on_the_same_names(real_modules, tmp_path):
    """Same input, same verdict, across two independently-implemented barriers.

    The read path returns `None` (→ 404, no oracle) where the create path
    raises `HTTPException(400)`; the *decision* must not diverge, or a name
    creatable by id would 404 on detail (or worse, vice-versa).
    """
    crud, template_service = real_modules
    from fastapi import HTTPException

    root = tmp_path / "templates"
    root.mkdir()

    hostile = ["..", "../evil", "/etc", ".hidden", "-lead", "_lead", "a/b", "a..b",
               "sp ace", "", "a\\b"]
    for name in hostile:
        assert template_service.contained_template_dir(name, root) is None, name
        with pytest.raises(HTTPException) as exc:
            crud._safe_local_template_path(name, root)
        assert exc.value.status_code == 400, name

    for name in ["sage", "dd-compliance", "a.b", "a_b", "9lives", "trinity-system"]:
        assert template_service.contained_template_dir(name, root) is not None, name
        assert crud._safe_local_template_path(name, root) is not None, name
