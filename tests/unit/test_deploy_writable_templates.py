"""
Writability regression for deploy-local (#950 / #971).

PR #971 fixed the silent-fallback bug where the backend wrote the extracted
archive to a container-only path and the new agent came up empty. The fix
made the deployed-templates directory resolution **fail fast**: if
`/data/deployed-templates` cannot be created, the deploy returns
HTTP 500 with `code=DEPLOYED_TEMPLATES_DIR_UNWRITABLE` instead of silently
falling back. #971 deferred a dedicated regression test for that resolution —
this is it.

The test drives `deploy_local_agent_logic` far enough to reach the
`templates_dir.mkdir(...)` call, with `pathlib.Path.mkdir` monkeypatched to
raise OSError, and asserts the fail-fast 500.

`deploy.py` imports cleanly in the unit env (docker SDK is installed; the
heavy siblings import without a running daemon), so this monkeypatches the
module's own attribute references rather than mutating `sys.modules` — which
keeps it clear of `tests/lint_sys_modules.py` (no `sys.modules[...] =`).
"""

from __future__ import annotations

import asyncio
import base64
import io
import tarfile
import types
from pathlib import Path

import pytest
from fastapi import HTTPException

# NOTE: `services.agent_service.deploy` and `models` are imported lazily inside
# the test (not at module level) on purpose. Importing the deploy chain at
# collection time trips the documented tests/utils-shadows-backend-utils
# sys.modules race (ModuleNotFoundError: utils.url_validation) when this file
# is collected alongside the rest of the unit suite. The conftest's autouse
# restore fixtures run at test time, not collection time, so deferring the
# heavy import to the test body sidesteps the race entirely. Same lazy-load
# rationale as tests/unit/test_local_templates_listing.py.


def _make_archive() -> str:
    """A minimal, valid Trinity archive: template.yaml + non-empty CLAUDE.md.

    Valid so it clears extraction + is_trinity_compatible (incl. the new
    CLAUDE.md hard-fail) and the test can reach the mkdir step.
    """
    template_yaml = (
        "name: test-writable\n"
        "resources:\n"
        '  cpu: "1"\n'
        '  memory: "2g"\n'
    ).encode("utf-8")
    claude_md = b"# Test\nInstructions."

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in (("template.yaml", template_yaml), ("CLAUDE.md", claude_md)):
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buffer.seek(0)
    return base64.b64encode(buffer.read()).decode("utf-8")


def test_unwritable_templates_dir_fails_fast(monkeypatch):
    """mkdir failure on the deployed-templates dir → HTTP 500 with the
    DEPLOYED_TEMPLATES_DIR_UNWRITABLE code (no silent fallback)."""

    # Lazy import (see module-level note). Other unit-test files
    # (test_voice_auth / test_voice_tools) install bare `services.template_service`
    # and `services.docker_service` stubs into sys.modules at *collection* time
    # (pytest imports every test module before running any test). The conftest's
    # restore can't fully clean them — they're baseline keys, so its pop-loop
    # skips them — leaving a stub that lacks `is_trinity_compatible`. Evict the
    # deploy import chain with monkeypatch.delitem (linter-safe; auto-restored on
    # teardown) so `deploy` re-imports against the real modules from disk.
    import importlib
    import sys

    for _name in (
        "services.agent_service.deploy",
        "services.agent_service.helpers",
        "services.template_service",
        "services.docker_service",
        "services.docker_utils",
        "services.settings_service",
    ):
        monkeypatch.delitem(sys.modules, _name, raising=False)

    deploy = importlib.import_module("services.agent_service.deploy")
    from models import DeployLocalRequest, User

    # Stub the pre-mkdir collaborators so the logic reaches the mkdir step
    # without a DB/Docker daemon. is_trinity_compatible stays REAL — the
    # archive is valid, so this also confirms a good archive passes.
    monkeypatch.setattr(deploy, "get_agents_by_prefix", lambda base: [])
    monkeypatch.setattr(deploy, "get_agent_quota_for_role", lambda role: 0)
    monkeypatch.setattr(deploy, "get_next_version_name", lambda base: f"{base}-v1")
    monkeypatch.setattr(deploy, "get_latest_version", lambda base: None)
    monkeypatch.setattr(
        deploy, "db", types.SimpleNamespace(get_agents_by_owner=lambda username: [])
    )

    # The actual fault under test: the deployed-templates dir is unwritable.
    _real_mkdir = Path.mkdir

    def _boom(self, *args, **kwargs):
        if str(self) == deploy.DEPLOYED_TEMPLATES_DIR_IN_BACKEND:
            raise OSError(13, "Permission denied")
        return _real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _boom)

    body = DeployLocalRequest(archive=_make_archive(), name=None, credentials=None)
    user = User(id=1, username="tester", role="creator")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            deploy.deploy_local_agent_logic(
                body,
                user,
                request=types.SimpleNamespace(),
                create_agent_fn=None,  # never reached — mkdir raises first
            )
        )

    exc = exc_info.value
    assert exc.status_code == 500
    assert isinstance(exc.detail, dict)
    assert exc.detail.get("code") == "DEPLOYED_TEMPLATES_DIR_UNWRITABLE"
