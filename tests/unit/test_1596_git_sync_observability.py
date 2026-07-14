"""#1596 — git-sync bloat mitigations (backend slice).

Covers the observability chain (agent .git size persisted to agent_sync_state)
and the default-.gitignore conventions that stop bulk data/deps/caches from being
auto-committed. The agent-side maintenance repack + .git-size measurement live in
the base image (docker/base-image/agent_server/routers/git.py) and are exercised
by the image build / live sync, not here.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend  # noqa: E402

pytestmark = pytest.mark.unit


def _ops():
    from db.sync_state import SyncStateOperations
    return SyncStateOperations()


class TestGitDirBytesRoundTrip:
    def test_upsert_and_read_git_dir_bytes(self, db_backend):
        ops = _ops()
        ops.upsert("a1", last_sync_status="success", git_dir_bytes=47244640256)  # ~44 GiB
        row = ops.get("a1")
        assert row["git_dir_bytes"] == 47244640256

    def test_partial_update_preserves_git_dir_bytes(self, db_backend):
        ops = _ops()
        ops.upsert("a1", last_sync_status="success", git_dir_bytes=1000)
        # A later failed sync that doesn't re-measure must keep the last value.
        ops.upsert("a1", last_sync_status="failed", last_error_summary="push failed")
        row = ops.get("a1")
        assert row["git_dir_bytes"] == 1000
        assert row["last_sync_status"] == "failed"

    def test_list_all_surfaces_the_field(self, db_backend):
        ops = _ops()
        ops.upsert("a1", last_sync_status="success", git_dir_bytes=42)
        rows = {r["agent_name"]: r for r in ops.list_all()}
        assert rows["a1"]["git_dir_bytes"] == 42


class TestDefaultGitignoreConventions:
    def test_bulk_data_patterns_present(self):
        from services.git_service import _GITIGNORE_PATTERNS
        for pat in (
            "node_modules/", ".venv/", "venv/", "__pycache__/",
            "*.pyc", ".pytest_cache/", "*.sqlite", "*.sqlite3", "*.db",
        ):
            assert pat in _GITIGNORE_PATTERNS, f"{pat} missing from default .gitignore (#1596)"

    def test_still_ignores_credentials_and_content(self):
        # Guard against a copy-paste that drops the pre-existing safety rules.
        from services.git_service import _GITIGNORE_PATTERNS
        for pat in (".env", ".mcp.json", "content/", "*.pem"):
            assert pat in _GITIGNORE_PATTERNS
