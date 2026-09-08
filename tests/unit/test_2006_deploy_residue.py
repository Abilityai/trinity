"""#2006 — a rejected `.mcp.json` must leave nothing on disk.

ent#213 added the archive `.mcp.json` guard, but it ran **32 lines after**
`shutil.copytree(extract_root, dest_path)`. The 400 aborted the deploy and
nothing removed the directory, so the refused config stayed under
`/data/deployed-templates/<version_name>/` — a live member of
`crud._LOCAL_TEMPLATE_ROOTS` — and remained reachable via a subsequent
`POST /api/agents {"template": "local:<version_name>"}` by the same caller who
had just been told the deploy failed. A guard that runs after the persist is
not a gate.

ent#213's own suite exercises `_validate_archive_mcp_config` in isolation ("the
full deploy flow needs Docker" — `test_213_deploy_mcp_validation.py:17-19`),
which is exactly why the ordering was never covered: the guard was always
correct, its POSITION was not. So these tests drive the **real**
`deploy_local_agent_logic` and assert on the filesystem.

No Docker is needed, and that is a property of the fix rather than of the
harness: the guard now runs before the quota lookup, before the
stop-previous-version step, and before the copy, so nothing external is reached
on the rejection path. The valid-archive test mocks the db/docker lookups that
follow, and stops at `create_agent_fn`.
"""

from __future__ import annotations

import base64
import io
import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from models import AgentStatus
from services.agent_service import deploy as deploy_mod
from services.agent_service.deploy import deploy_local_agent_logic

pytestmark = pytest.mark.unit


# The `${LD_PRELOAD}` reserved env-ref from the issue's reproduction: the
# validator refuses it, and #1929's `${VAR:-default}` blanking on the CREATE
# path would later launder it to `""` — which is why "validate the generated
# output" is not a fix for this and the residue must simply not exist.
REJECTED_MCP = {
    "mcpServers": {
        "ebook-mcp": {
            "command": "python3",
            "args": ["--directory", "${LD_PRELOAD}", "run", "ebook-mcp"],
            "env": {"LD_PRELOAD_REF": "${LD_PRELOAD}"},
        }
    }
}

VALID_MCP = {"mcpServers": {"ok": {"command": "python3", "args": ["-m", "server"]}}}


def _archive(name: str, mcp: dict | None) -> str:
    """A minimal Trinity-compatible archive, base64 tar.gz, as the API takes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(arcname: str, content: str):
            data = content.encode()
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        add(
            f"{name}/template.yaml",
            f"name: {name}\ntype: business-assistant\n"
            "resources:\n  cpu: '2'\n  memory: 4g\n",
        )
        add(f"{name}/CLAUDE.md", "# agent\n")
        if mcp is not None:
            add(f"{name}/.mcp.json", json.dumps(mcp))
    return base64.b64encode(buf.getvalue()).decode()


@pytest.fixture
def templates_dir(tmp_path, monkeypatch):
    """Point the deployed-templates root at tmp_path.

    Patched as a module attribute because the function reads the constant at
    call time (`Path(DEPLOYED_TEMPLATES_DIR_IN_BACKEND)`), which is what makes
    the real flow testable without touching /data.
    """
    d = tmp_path / "deployed-templates"
    monkeypatch.setattr(
        deploy_mod, "DEPLOYED_TEMPLATES_DIR_IN_BACKEND", str(d), raising=True
    )
    return d


@pytest.fixture
def user():
    return SimpleNamespace(
        username="creator", email="creator@example.com", role="creator", id=7
    )


def _agent_status(name: str) -> AgentStatus:
    """A minimal real AgentStatus — DeployLocalResponse validates the field, so
    a stand-in namespace would fail for reasons unrelated to what is tested."""
    return AgentStatus(
        name=name, type="business-assistant", status="running", port=2222,
        created=datetime(2026, 8, 5, tzinfo=timezone.utc),
        resources={"cpu": "2", "memory": "4g"},
    )


def _body(archive: str, name: str):
    # Mirrors DeployLocalRequest, incl. the #2060 require_manifest field —
    # deploy.py reads it strictly (body.require_manifest, default False).
    return SimpleNamespace(
        archive=archive, name=name, credentials=None, require_manifest=False
    )


async def _deploy(body, user, create_fn=None):
    async def _fail_if_called(*a, **kw):  # pragma: no cover — asserted not reached
        raise AssertionError("create_agent_fn must not be reached")

    return await deploy_local_agent_logic(
        body, user, SimpleNamespace(), create_fn or _fail_if_called
    )


# ---------------------------------------------------------------------------
# The reported bug
# ---------------------------------------------------------------------------

class TestRejectedDeployLeavesNothing:

    @pytest.mark.asyncio
    async def test_rejected_mcp_json_leaves_no_deployed_template_dir(
        self, templates_dir, user
    ):
        """AC #1 — the exact reproduction from the issue."""
        body = _body(_archive("resid-probe", REJECTED_MCP), "resid-probe")

        with pytest.raises(HTTPException) as ei:
            await _deploy(body, user)

        assert ei.value.status_code == 400
        assert "Invalid .mcp.json in archive" in str(ei.value.detail)
        # The residue check, not the guard check — the guard was already right.
        assert not templates_dir.exists() or list(templates_dir.iterdir()) == []

    @pytest.mark.asyncio
    async def test_local_id_for_a_rejected_deploy_resolves_to_nothing(
        self, templates_dir, user
    ):
        """AC #2 — `local:<version_name>` must not resolve.

        Resolution is a directory lookup under the roots, so "no directory" is
        the whole of it; asserted against the id the deploy path itself would
        have used (`local:{version_name}`, `deploy.py`'s own create call).
        """
        body = _body(_archive("ghost-tpl", REJECTED_MCP), "ghost-tpl")

        with pytest.raises(HTTPException):
            await _deploy(body, user)

        assert not (templates_dir / "ghost-tpl").exists()
        assert not (templates_dir / "ghost-tpl-v2").exists()

    @pytest.mark.asyncio
    async def test_rejection_happens_before_any_external_side_effect(
        self, templates_dir, user, monkeypatch
    ):
        """The ordering property, stated directly.

        The quota lookup and the stop-previous-version step both sat between
        the copy and the old guard, so a doomed deploy could stop a running
        agent on its way to a 400. Anything reached after the guard is made to
        explode; a clean 400 proves the guard ran first.
        """
        def boom(*a, **kw):
            raise AssertionError("reached a post-guard step on a rejected deploy")

        for name in ("get_agents_by_prefix", "get_next_version_name",
                     "get_latest_version", "get_agent_container"):
            monkeypatch.setattr(deploy_mod, name, boom, raising=True)

        body = _body(_archive("early-gate", REJECTED_MCP), "early-gate")

        with pytest.raises(HTTPException) as ei:
            await _deploy(body, user)
        assert ei.value.status_code == 400


# ---------------------------------------------------------------------------
# The valid path is unchanged (AC #3)
# ---------------------------------------------------------------------------

class TestValidDeployUnaffected:

    @pytest.mark.asyncio
    async def test_valid_archive_still_reaches_creation_with_the_template_on_disk(
        self, templates_dir, user, monkeypatch
    ):
        """A valid `.mcp.json` must deploy exactly as before: the directory is
        persisted, and the handover to `create_agent_fn` happens with it in
        place — the fix must not turn the cleanup into a regression."""
        seen = {}

        monkeypatch.setattr(deploy_mod, "get_agents_by_prefix", lambda *a: [], raising=True)
        monkeypatch.setattr(deploy_mod, "get_latest_version", lambda *a: None, raising=True)
        monkeypatch.setattr(deploy_mod, "get_next_version_name", lambda n: n, raising=True)
        monkeypatch.setattr(deploy_mod, "get_agent_quota_for_role", lambda r: 0, raising=True)
        monkeypatch.setattr(deploy_mod.db, "get_agents_by_owner", lambda u: [], raising=True)
        monkeypatch.setattr(
            deploy_mod, "collect_mcp_credential_warnings", lambda p: [], raising=True
        )
        monkeypatch.setattr(
            deploy_mod, "_prepopulate_workspace_from_template",
            lambda v, d: None, raising=True,
        )

        async def create_fn(config, *a, **kw):
            seen["template"] = config.template
            seen["dir_present"] = (templates_dir / "good-tpl").is_dir()
            seen["mcp_present"] = (templates_dir / "good-tpl" / ".mcp.json").is_file()
            return _agent_status(config.name)

        body = _body(_archive("good-tpl", VALID_MCP), "good-tpl")
        result = await _deploy(body, user, create_fn)

        assert result.status == "success"
        assert seen == {
            "template": "local:good-tpl",
            "dir_present": True,
            "mcp_present": True,
        }
        # Still there after a successful deploy — cleanup is failure-only.
        assert (templates_dir / "good-tpl").is_dir()

    @pytest.mark.asyncio
    async def test_archive_without_an_mcp_json_deploys(
        self, templates_dir, user, monkeypatch
    ):
        monkeypatch.setattr(deploy_mod, "get_agents_by_prefix", lambda *a: [], raising=True)
        monkeypatch.setattr(deploy_mod, "get_latest_version", lambda *a: None, raising=True)
        monkeypatch.setattr(deploy_mod, "get_next_version_name", lambda n: n, raising=True)
        monkeypatch.setattr(deploy_mod, "get_agent_quota_for_role", lambda r: 0, raising=True)
        monkeypatch.setattr(deploy_mod.db, "get_agents_by_owner", lambda u: [], raising=True)
        monkeypatch.setattr(
            deploy_mod, "collect_mcp_credential_warnings", lambda p: [], raising=True
        )
        monkeypatch.setattr(
            deploy_mod, "_prepopulate_workspace_from_template",
            lambda v, d: None, raising=True,
        )

        async def create_fn(config, *a, **kw):
            return _agent_status(config.name)

        body = _body(_archive("no-mcp", None), "no-mcp")
        result = await _deploy(body, user, create_fn)
        assert result.status == "success"


# ---------------------------------------------------------------------------
# Second layer: a failure AFTER the copy also leaves nothing
# ---------------------------------------------------------------------------

class TestPartialDeployCleanup:

    @pytest.mark.asyncio
    async def test_a_failure_between_copy_and_creation_removes_the_directory(
        self, templates_dir, user, monkeypatch
    ):
        """Moving the `.mcp.json` gate fixes the reported route; it does not
        fix the class. `_prepopulate_workspace_from_template` raising (a docker
        outage) leaves the same addressable `local:<name>` residue."""
        monkeypatch.setattr(deploy_mod, "get_agents_by_prefix", lambda *a: [], raising=True)
        monkeypatch.setattr(deploy_mod, "get_latest_version", lambda *a: None, raising=True)
        monkeypatch.setattr(deploy_mod, "get_next_version_name", lambda n: n, raising=True)
        monkeypatch.setattr(deploy_mod, "get_agent_quota_for_role", lambda r: 0, raising=True)
        monkeypatch.setattr(deploy_mod.db, "get_agents_by_owner", lambda u: [], raising=True)
        monkeypatch.setattr(
            deploy_mod, "collect_mcp_credential_warnings", lambda p: [], raising=True
        )

        def explode(version_name, template_dir):
            raise HTTPException(status_code=500, detail="docker unavailable")

        monkeypatch.setattr(
            deploy_mod, "_prepopulate_workspace_from_template", explode, raising=True
        )

        body = _body(_archive("half-deploy", VALID_MCP), "half-deploy")

        with pytest.raises(HTTPException) as ei:
            await _deploy(body, user)

        assert ei.value.status_code == 500
        assert not (templates_dir / "half-deploy").exists()

    @pytest.mark.asyncio
    async def test_a_failure_during_creation_keeps_the_directory(
        self, templates_dir, user, monkeypatch
    ):
        """The deliberate limit of the cleanup. Once creation starts, the
        directory can be referenced by a container mount spec, so removing it
        on a late failure would break a half-created agent instead of tidying
        after one. Creation-path rollback is `crud.py`'s job."""
        monkeypatch.setattr(deploy_mod, "get_agents_by_prefix", lambda *a: [], raising=True)
        monkeypatch.setattr(deploy_mod, "get_latest_version", lambda *a: None, raising=True)
        monkeypatch.setattr(deploy_mod, "get_next_version_name", lambda n: n, raising=True)
        monkeypatch.setattr(deploy_mod, "get_agent_quota_for_role", lambda r: 0, raising=True)
        monkeypatch.setattr(deploy_mod.db, "get_agents_by_owner", lambda u: [], raising=True)
        monkeypatch.setattr(
            deploy_mod, "collect_mcp_credential_warnings", lambda p: [], raising=True
        )
        monkeypatch.setattr(
            deploy_mod, "_prepopulate_workspace_from_template",
            lambda v, d: None, raising=True,
        )

        async def create_fn(config, *a, **kw):
            raise HTTPException(status_code=500, detail="container create failed")

        body = _body(_archive("late-fail", VALID_MCP), "late-fail")

        with pytest.raises(HTTPException):
            await _deploy(body, user, create_fn)

        assert (templates_dir / "late-fail").is_dir()


class TestCleanupIsConfined:
    """`_remove_partial_deploy` re-checks containment at the sink.

    Raised by CodeQL (py/path-injection, high) against the `rmtree`: the path
    descends from a caller-supplied name, and the #950 guard that confines it
    lives in the CALLER. Fixed rather than dismissed — on a destructive sink,
    "the caller already validated it" holds only until there is a second call
    site.
    """

    def test_a_path_outside_the_templates_dir_is_refused(
        self, templates_dir, user, monkeypatch, tmp_path
    ):
        outside = tmp_path / "not-templates" / "precious"
        outside.mkdir(parents=True)
        (outside / "data.txt").write_text("do not delete me")

        deploy_mod._remove_partial_deploy(outside)

        assert (outside / "data.txt").exists()

    def test_the_templates_root_itself_is_refused(self, templates_dir, monkeypatch):
        templates_dir.mkdir(parents=True)
        (templates_dir / "other-agent").mkdir()

        deploy_mod._remove_partial_deploy(templates_dir)

        assert (templates_dir / "other-agent").exists()

    def test_a_traversal_escape_is_refused(self, templates_dir, tmp_path):
        templates_dir.mkdir(parents=True)
        sibling = tmp_path / "sibling"
        sibling.mkdir()

        deploy_mod._remove_partial_deploy(templates_dir / ".." / "sibling")

        assert sibling.exists()

    def test_a_legitimate_child_is_still_removed(self, templates_dir):
        child = templates_dir / "agent-v1"
        child.mkdir(parents=True)

        deploy_mod._remove_partial_deploy(child)

        assert not child.exists()

    def test_none_is_a_no_op(self):
        deploy_mod._remove_partial_deploy(None)  # must not raise


# ---------------------------------------------------------------------------
# Static: the gate must stay in front of the persist
# ---------------------------------------------------------------------------

def test_the_guard_is_called_before_the_copy_in_source_order():
    """Pins the ORDERING itself, so a refactor that moves the guard back below
    `copytree` fails here even if every behavioural test still passes on some
    other path (it was the position, not the guard, that was wrong)."""
    src = Path(deploy_mod.__file__).read_text()
    body = src.split("async def deploy_local_agent_logic", 1)[1]

    guard = body.index("_validate_archive_mcp_config(")
    copy = body.index("shutil.copytree(extract_root")

    assert guard < copy, (
        "the archive .mcp.json guard runs after the template is persisted — "
        "that is #2006"
    )
