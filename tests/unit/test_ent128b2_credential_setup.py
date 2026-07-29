"""ent#128 PR-B — the credential declaration standard.

Covers the four defects PR-A deferred here plus the `credential_setup:`
declaration standard itself:

  * `crud._resolve_local_template` reaching straight through `credentials:`
    (a malformed block silently cost the agent its `runtime:` and
    `shared_folders:` config too);
  * the same uncaught crash on the agent image's `GET /api/template/info`,
    with a BEHAVIOURAL parity guard over the duplicated accessor (W10 — no
    parity test covered `info.py`, so the two copies could diverge freely);
  * MCP-server precedence (`credentials:` outranked the template's own
    `mcp_servers:`, Defect D) and the dead `required_credentials` badge
    (Defect C / W6 — `platform_injected` vars must not be counted);
  * `normalize_credential_requirements()` — base-set-plus-overlay, never
    raises, never mutates its input.

Harness notes:
  * `services.agent_service.crud` is imported lazily inside each test body —
    same rationale as `test_1793_unknown_local_template.py`: importing the crud
    chain at collection time trips the documented tests/utils-shadows-
    backend-utils `sys.modules` race.
  * Local-template roots are monkeypatched via the module attribute, so no
    `sys.modules[...] =` assignment is introduced (keeps
    `tests/lint_sys_modules.py` green).

Target: src/backend/services/template_service.py,
        src/backend/services/agent_service/crud.py,
        docker/base-image/agent_server/routers/info.py
Issue:  Abilityai/trinity-enterprise#128 (AC #1-4)
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Env prerequisites before any backend import (repo test convention).
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
_TMP_DB = Path(tempfile.gettempdir()) / "trinity_test_ent128b2.db"
os.environ.setdefault("TRINITY_DB_PATH", str(_TMP_DB))

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = str(_PROJECT_ROOT / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit


# ===========================================================================
# Defect 2 — crud.py's raw reach-through through `credentials:`
# ===========================================================================
#
# The reach-through sat FIRST in a run of `config` mutations wrapped in one
# broad `except Exception`, so an AttributeError there skipped every mutation
# AFTER it. A malformed `credentials:` therefore cost the agent its `runtime:`
# and `shared_folders:` config as well — three unrelated features lost to one
# bad key, with only a WARNING to show for it.


def _config(template: str):
    from models import AgentConfig

    return AgentConfig(name="t-ent128b", template=template)


def _write_template(root: Path, name: str, body: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "template.yaml").write_text(body)


def _patch_roots(monkeypatch, curated: Path, deployed: Path) -> None:
    from services.agent_service import crud

    monkeypatch.setattr(
        crud, "_LOCAL_TEMPLATE_ROOTS", (curated.resolve(), deployed.resolve())
    )


_MALFORMED_CREDENTIALS = [
    pytest.param('credentials: "OPENAI_API_KEY"', id="string"),
    pytest.param("credentials:", id="null"),
    pytest.param("credentials:\n  - OPENAI_API_KEY", id="list"),
    pytest.param("credentials:\n  mcp_servers: nope", id="mcp_servers-string"),
    pytest.param(
        "credentials:\n  mcp_servers:\n    - a\n    - b", id="mcp_servers-list"
    ),
]


@pytest.mark.parametrize("credentials_block", _MALFORMED_CREDENTIALS)
def test_malformed_credentials_does_not_cost_runtime_and_shared_folders(
    monkeypatch, tmp_path, credentials_block
):
    """The mutations AFTER the credentials read must still be applied."""
    from services.agent_service import crud

    curated = tmp_path / "curated"
    deployed = tmp_path / "deployed"
    deployed.mkdir()
    _write_template(
        curated,
        "cred-shape",
        "name: cred-shape\n"
        "type: research-agent\n"
        "resources:\n  cpu: '4'\n  memory: '8g'\n"
        f"{credentials_block}\n"
        "runtime:\n  type: codex\n  model: gpt-5.5\n"
        "shared_folders:\n  expose: true\n  consume: false\n",
    )
    _patch_roots(monkeypatch, curated, deployed)

    config = _config("local:cred-shape")
    template_data, shared_folders = crud._resolve_local_template(config)

    # The template still resolves and the agent is still created...
    assert template_data["name"] == "cred-shape"
    # ...and the two settings that used to be collateral damage survive.
    assert config.runtime == "codex", "runtime: lost to the credentials reach-through"
    assert config.runtime_model == "gpt-5.5"
    assert shared_folders == {
        "expose": True,
        "consume": False,
    }, "shared_folders: lost to the credentials reach-through"


def test_wellformed_credentials_still_drive_mcp_servers(monkeypatch, tmp_path):
    """The happy path is unchanged — declared servers still reach `config`."""
    from services.agent_service import crud

    curated = tmp_path / "curated"
    deployed = tmp_path / "deployed"
    deployed.mkdir()
    _write_template(
        curated,
        "cred-ok",
        "name: cred-ok\n"
        "resources:\n  cpu: '2'\n  memory: '4g'\n"
        "credentials:\n"
        "  mcp_servers:\n"
        "    stripe:\n      env_vars: [STRIPE_API_KEY]\n"
        "    linear:\n      env_vars: [LINEAR_API_KEY]\n",
    )
    _patch_roots(monkeypatch, curated, deployed)

    config = _config("local:cred-ok")
    crud._resolve_local_template(config)

    assert sorted(config.mcp_servers) == ["linear", "stripe"]
