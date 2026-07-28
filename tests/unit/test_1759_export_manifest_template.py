"""`export_manifest` must emit a template id that actually redeploys (#1759).

`system_service.export_manifest` built each agent entry as

    {"template": agent.get('template', 'local:business-assistant')}

which was broken twice over:

1. `dict.get(key, default)` returns the default only when the key is ABSENT.
   Every agent dict from `routers/agents.py` carries the key with value `None`
   for a template-less ("Blank") agent — the platform's most common agent type
   — so the fallback was **unreachable dead code** and those agents exported
   `template: null`. `SystemAgentConfig.template` is a non-Optional `str`, so
   redeploying that manifest failed Pydantic validation *before* this issue.
2. `config/agent-templates/business-assistant` has never existed, so even when
   the fallback was reachable it named a template that would 400 once the
   create-time gate landed.

This is therefore a pre-existing broken round-trip being fixed, NOT a
regression the #1759 create gate introduces.

Target: src/backend/services/system_service.py::export_manifest
Issue:  abilityai/trinity#1759
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
_TMP_DB = Path(tempfile.gettempdir()) / "trinity_test_1759_export_manifest.db"
os.environ.setdefault("TRINITY_DB_PATH", str(_TMP_DB))

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = str(_PROJECT_ROOT / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import services.system_service as system_service  # noqa: E402
from models import SystemManifest  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def quiet_db(monkeypatch):
    """Silence the per-agent db enrichment; only `template` is under test."""
    db = MagicMock()
    db.get_agent_folder_config.return_value = None
    db.list_agent_schedules.return_value = []
    db.get_agent_tags.return_value = []
    db.get_agent_permissions.return_value = []
    # Unstubbed, this returns a truthy MagicMock that lands in the manifest and
    # blows up `yaml.dump`.
    db.get_setting_value.return_value = None
    monkeypatch.setattr(system_service, "db", db)
    return db


def _export(system_name, agents):
    return yaml.safe_load(system_service.export_manifest(system_name, agents))


def test_templateless_agent_exports_a_resolvable_template(quiet_db):
    """The regression the `.get` default silently allowed: `template: null`."""
    manifest = _export("sys", [{"name": "sys-blank", "template": None}])

    entry = manifest["agents"]["blank"]
    assert entry["template"] is not None
    assert entry["template"] == "local:default"
    # The dead fallback must be gone: that template does not exist on disk.
    assert entry["template"] != "local:business-assistant"


def test_missing_template_key_also_covered(quiet_db):
    """The only case the old `.get` default actually handled."""
    manifest = _export("sys", [{"name": "sys-nokey"}])
    assert manifest["agents"]["nokey"]["template"] == "local:default"


def test_real_template_is_passed_through_untouched(quiet_db):
    manifest = _export("sys", [{"name": "sys-scout", "template": "local:scout"}])
    assert manifest["agents"]["scout"]["template"] == "local:scout"


def test_exported_manifest_validates_and_names_a_shipped_template(quiet_db):
    """End-to-end round-trip: the export must survive manifest validation AND
    name a template the create path can resolve."""
    manifest_yaml = system_service.export_manifest(
        "sys", [{"name": "sys-blank", "template": None}]
    )
    # Would previously raise: template=None fails the non-Optional `str`.
    parsed = SystemManifest(**yaml.safe_load(manifest_yaml))
    template_id = parsed.agents["blank"].template
    assert template_id.startswith("local:")

    catalog = _PROJECT_ROOT / "config" / "agent-templates"
    assert (catalog / template_id[len("local:"):] / "template.yaml").is_file(), (
        f"exported manifest names {template_id!r}, which does not exist "
        f"under {catalog} and would now 400 on redeploy"
    )


def test_templateless_agents_are_logged(quiet_db, caplog):
    """`export_manifest` returns a bare YAML string, so a log line is the only
    channel that does not change the response contract."""
    with caplog.at_level("WARNING"):
        system_service.export_manifest(
            "sys",
            [{"name": "sys-blank", "template": None},
             {"name": "sys-scout", "template": "local:scout"}],
        )
    warnings = [r.getMessage() for r in caplog.records
                if r.levelname == "WARNING"]
    assert any("sys-blank" in w and "local:default" in w for w in warnings)
    assert not any("sys-scout" in w for w in warnings)
