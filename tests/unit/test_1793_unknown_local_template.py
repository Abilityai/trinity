"""
Regression for #1793 — an unresolvable `local:` template must fail, not
silently produce a templateless agent.

Before this fix, `_resolve_local_template` returned an empty `template_data`
when the name matched no `template.yaml` under either root. The caller carried
on and provisioned a running container with no CLAUDE.md, no template.yaml and
no skills, and `POST /api/agents` reported a normal 200 creation. The
`github:` path already failed fast on an unknown repo; these tests pin the
`local:` path to the same contract.

`services.agent_service.crud` is imported lazily inside each test body rather
than at module level — same rationale as `tests/unit/test_deploy_writable_templates.py`
and `tests/unit/test_local_templates_listing.py` (importing the crud chain at
collection time trips the documented tests/utils-shadows-backend-utils
sys.modules race). Roots are monkeypatched via the module attribute, so no
`sys.modules[...] =` assignment is introduced (keeps `tests/lint_sys_modules.py`
green).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

TEMPLATE_YAML = """\
name: fixture-template
type: research-agent
runtime:
  type: claude-code
resources:
  cpu: "2"
  memory: "4g"
"""


def _config(template: str):
    from models import AgentConfig

    return AgentConfig(name="t1793", template=template)


def _patch_roots(monkeypatch, curated: Path, deployed: Path) -> None:
    from services.agent_service import crud

    monkeypatch.setattr(
        crud, "_LOCAL_TEMPLATE_ROOTS", (curated.resolve(), deployed.resolve())
    )


def test_unknown_local_template_raises_404(monkeypatch, tmp_path):
    """A name present under neither root is rejected before any side effect."""
    from services.agent_service import crud

    curated = tmp_path / "curated"
    deployed = tmp_path / "deployed"
    curated.mkdir()
    deployed.mkdir()
    _patch_roots(monkeypatch, curated, deployed)

    with pytest.raises(HTTPException) as exc:
        crud._resolve_local_template(_config("local:does-not-exist"))

    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "UNKNOWN_LOCAL_TEMPLATE"
    # The message must name the template so the caller can spot a typo.
    assert "does-not-exist" in exc.value.detail["error"]


def test_directory_without_template_yaml_raises_404(monkeypatch, tmp_path):
    """A directory that exists but carries no template.yaml is still unusable.

    This is the shape that produced the empty agent: the path resolved, so the
    old code proceeded with an empty template_data.
    """
    from services.agent_service import crud

    curated = tmp_path / "curated"
    deployed = tmp_path / "deployed"
    (curated / "hollow").mkdir(parents=True)
    deployed.mkdir()
    _patch_roots(monkeypatch, curated, deployed)

    with pytest.raises(HTTPException) as exc:
        crud._resolve_local_template(_config("local:hollow"))

    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "UNKNOWN_LOCAL_TEMPLATE"


@pytest.mark.parametrize("root_index", [0, 1])
def test_resolvable_template_still_loads(monkeypatch, tmp_path, root_index):
    """The happy path is unchanged under both roots (curated and deploy-local)."""
    from services.agent_service import crud

    roots = [tmp_path / "curated", tmp_path / "deployed"]
    for r in roots:
        r.mkdir()
    target = roots[root_index] / "fixture-template"
    target.mkdir()
    (target / "template.yaml").write_text(TEMPLATE_YAML)
    _patch_roots(monkeypatch, roots[0], roots[1])

    config = _config("local:fixture-template")
    template_data, shared_folders = crud._resolve_local_template(config)

    assert template_data["name"] == "fixture-template"
    # Template fields are still projected onto the config.
    assert config.type == "research-agent"
    assert config.runtime == "claude-code"
    assert shared_folders is None


def test_invalid_name_still_400_not_404(monkeypatch, tmp_path):
    """Traversal-ish names keep their existing 400 — #1793 must not mask it."""
    from services.agent_service import crud

    curated = tmp_path / "curated"
    deployed = tmp_path / "deployed"
    curated.mkdir()
    deployed.mkdir()
    _patch_roots(monkeypatch, curated, deployed)

    with pytest.raises(HTTPException) as exc:
        crud._resolve_local_template(_config("local:../etc"))

    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "INVALID_LOCAL_TEMPLATE_NAME"
