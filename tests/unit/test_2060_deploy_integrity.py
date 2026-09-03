"""#2060 — deploy-local integrity contract: no silently-incomplete deploys.

The archive rides the calling model's own turn as a base64 tool argument, and
nothing anywhere in the round trip compared what landed against what was on
disk — a pruned-but-well-formed archive (extra tar ``--exclude``s, paste
truncation, macOS AppleDouble pollution, dereferenced symlinks) deployed
``status: "success"``. These tests drive the REAL ``deploy_local_agent_logic``
(the ``test_2006_deploy_residue.py`` harness: monkeypatched
``DEPLOYED_TEMPLATES_DIR_IN_BACKEND``, stubbed ``create_agent_fn``/db/docker)
and pin the new contract:

* embedded ``.trinity-manifest.json`` verified post-extract AND post-copy,
  fail-closed 400 ``MANIFEST_DRIFT`` naming the drifted paths;
* in-root symlinks preserved end to end (``copytree(symlinks=True)``), dangling
  in-root links preserved with a named warning, escapes still refused
  (regression-pinned — the refusals pre-date #2060);
* caps carry observed + limit; a decompressed-size cap closes the gzip-bomb
  hole; AppleDouble ``._*`` members are skipped with a warning;
* the response carries evidence (``verified``/``files_expected``/
  ``files_deployed``/``symlinks_deployed``/``compatibility_hard_count``);
* residue + compensation: ``dest_created`` assigned before the copy (#2006
  class), workspace-volume cleanup, attached-stale-volume 409, and the
  previous version restarted on ANY failed deploy that had stopped it;
* Idempotency-Key on the router (scope ``agent_deploy:{user_id}``) and a
  per-base-name deploy lock.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import stat
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from models import AgentStatus
from services.agent_service import deploy as deploy_mod
from services.agent_service.deploy import deploy_local_agent_logic

pytestmark = pytest.mark.unit

MANIFEST_NAME = ".trinity-manifest.json"

DEFAULT_FILES = {
    "template.yaml": (
        "name: {name}\ntype: business-assistant\n"
        "resources:\n  cpu: '2'\n  memory: 4g\n"
    ),
    "CLAUDE.md": "# agent\n",
}


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _build_archive(
    name: str,
    files: dict[str, str] | None = None,
    links: dict[str, str] | None = None,
    manifest="auto",
    manifest_extra: list | None = None,
    manifest_drop: set[str] | None = None,
    manifest_raw: str | None = None,
    appledouble: list[str] | None = None,
    file_modes: dict[str, int] | None = None,
) -> str:
    """Base64 tar.gz as the API takes it.

    ``files``/``links`` are relative to the agent root; members are nested
    under ``{name}/`` (the nested-root form the unwrap handles). ``manifest``:
    ``"auto"`` computes entries from files+links, ``None`` omits the member,
    a list is embedded verbatim; ``manifest_raw`` embeds raw bytes.
    """
    all_files = {k: v.format(name=name) for k, v in DEFAULT_FILES.items()}
    if files:
        all_files.update(files)
    links = links or {}

    entries = None
    if manifest_raw is None:
        if manifest == "auto":
            entries = []
            for rel, content in sorted(all_files.items()):
                entries.append({"path": rel, "sha256": _sha(content.encode())})
            for rel, target in sorted(links.items()):
                entries.append({"path": rel, "link_target": target})
            if manifest_drop:
                entries = [e for e in entries if e["path"] not in manifest_drop]
            if manifest_extra:
                entries = entries + list(manifest_extra)
        elif manifest is not None:
            entries = manifest

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(arcname: str, content: bytes, mode: int | None = None):
            info = tarfile.TarInfo(f"{name}/{arcname}")
            info.size = len(content)
            if mode is not None:
                info.mode = mode
            tar.addfile(info, io.BytesIO(content))

        for rel, content in all_files.items():
            add(rel, content.encode(), (file_modes or {}).get(rel))
        for rel, target in links.items():
            info = tarfile.TarInfo(f"{name}/{rel}")
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)
        for rel in appledouble or []:
            add(rel, b"\x00\x05\x16\x07 apple resource fork")
        if manifest_raw is not None:
            add(MANIFEST_NAME, manifest_raw.encode())
        elif entries is not None:
            add(MANIFEST_NAME, json.dumps(entries).encode())
    return base64.b64encode(buf.getvalue()).decode()


@pytest.fixture
def templates_dir(tmp_path, monkeypatch):
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
    return AgentStatus(
        name=name, type="business-assistant", status="running", port=2222,
        created=datetime(2026, 8, 5, tzinfo=timezone.utc),
        resources={"cpu": "2", "memory": "4g"},
    )


def _body(archive: str, name: str, credentials=None, require_manifest=False):
    # Always carries `require_manifest`, mirroring DeployLocalRequest's field
    # (default False) — deploy.py reads it strictly (`body.require_manifest`),
    # so a body without the attribute fails loudly by design.
    return SimpleNamespace(
        archive=archive, name=name, credentials=credentials,
        require_manifest=bool(require_manifest),
    )


async def _deploy(body, user, create_fn=None):
    async def _default_create(config, *a, **kw):
        return _agent_status(config.name)

    return await deploy_local_agent_logic(
        body, user, SimpleNamespace(), create_fn or _default_create
    )


@pytest.fixture
def flow_stubs(templates_dir, monkeypatch):
    """Stub everything past the verification band so the real flow can run
    without db/docker/redis. New-code seams (compat gate, deploy-lock client,
    volume cleanup) are patched with ``raising=False`` so this file is also
    runnable against the pre-fix base for the RED proof."""
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

    async def _no_compat(name):
        return None

    monkeypatch.setattr(deploy_mod, "_compatibility_hard_count", _no_compat, raising=False)
    monkeypatch.setattr(deploy_mod, "_deploy_lock_client", lambda: None, raising=False)
    return monkeypatch


def _drift_detail(exc_info) -> dict:
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "MANIFEST_DRIFT"
    return detail


# ---------------------------------------------------------------------------
# CRUX — the reported bug: a pruned archive with a manifest claiming more
# ---------------------------------------------------------------------------

class TestManifestDrift:

    @pytest.mark.asyncio
    async def test_pruned_archive_with_manifest_refused_names_missing_paths(
        self, flow_stubs, templates_dir, user
    ):
        """A valid archive whose embedded manifest lists a file the tar lacks
        (the extra-``--exclude`` accident) must be REFUSED, naming the path.
        On the pre-fix base the manifest deploys as an inert file and the
        deploy succeeds silently."""
        archive = _build_archive(
            "pruned-agent",
            manifest_extra=[
                {"path": "skills/deploy/SKILL.md", "sha256": _sha(b"pruned away")}
            ],
        )
        with pytest.raises(HTTPException) as ei:
            await _deploy(_body(archive, "pruned-agent"), user)

        assert ei.value.status_code == 400
        detail = _drift_detail(ei)
        assert "skills/deploy/SKILL.md" in detail["missing"]
        # No residue: refused before any persist.
        assert not (templates_dir / "pruned-agent").exists()

    @pytest.mark.asyncio
    async def test_drift_refused_before_any_side_effect(
        self, flow_stubs, templates_dir, user, monkeypatch
    ):
        """The #2006 gate-ordering rule extends to the manifest gate: a doomed
        deploy must not compute versions or stop a running agent."""
        def boom(*a, **kw):
            raise AssertionError("reached a post-verification step on a drifted deploy")

        for fn in ("get_next_version_name", "get_latest_version", "get_agents_by_prefix"):
            monkeypatch.setattr(deploy_mod, fn, boom, raising=True)

        archive = _build_archive(
            "early-drift",
            manifest_extra=[{"path": "gone.md", "sha256": _sha(b"x")}],
        )
        with pytest.raises(HTTPException) as ei:
            await _deploy(_body(archive, "early-drift"), user)
        assert ei.value.status_code == 400

    @pytest.mark.asyncio
    async def test_manifest_extra_file_refused(self, flow_stubs, templates_dir, user):
        """A tar member the manifest does not list is drift too (a stale
        committed manifest must fail honestly, not silently under-verify)."""
        archive = _build_archive(
            "extra-agent",
            files={"surprise.md": "not in manifest\n"},
            manifest_drop={"surprise.md"},
        )
        with pytest.raises(HTTPException) as ei:
            await _deploy(_body(archive, "extra-agent"), user)
        detail = _drift_detail(ei)
        assert "surprise.md" in detail["extra"]

    @pytest.mark.asyncio
    async def test_manifest_altered_sha_refused(self, flow_stubs, templates_dir, user):
        archive = _build_archive(
            "altered-agent",
            files={"notes.md": "actual content\n"},
            manifest_drop={"notes.md"},
            manifest_extra=[{"path": "notes.md", "sha256": _sha(b"different content")}],
        )
        with pytest.raises(HTTPException) as ei:
            await _deploy(_body(archive, "altered-agent"), user)
        detail = _drift_detail(ei)
        assert "notes.md" in detail["altered"]

    @pytest.mark.asyncio
    async def test_manifest_link_target_mismatch_refused(
        self, flow_stubs, templates_dir, user
    ):
        archive = _build_archive(
            "linkdrift-agent",
            links={"alias.md": "CLAUDE.md"},
            manifest_drop={"alias.md"},
            manifest_extra=[{"path": "alias.md", "link_target": "template.yaml"}],
        )
        with pytest.raises(HTTPException) as ei:
            await _deploy(_body(archive, "linkdrift-agent"), user)
        detail = _drift_detail(ei)
        assert "alias.md" in detail["link_mismatch"]

    @pytest.mark.asyncio
    async def test_drift_error_does_not_teach_pruning(
        self, flow_stubs, templates_dir, user
    ):
        """The recovery text must direct at rebuild-without-excludes / CLI —
        never at removing entries from the manifest (that would teach the
        consistent two-command forge)."""
        archive = _build_archive(
            "advice-agent",
            manifest_extra=[{"path": "gone.md", "sha256": _sha(b"x")}],
        )
        with pytest.raises(HTTPException) as ei:
            await _deploy(_body(archive, "advice-agent"), user)
        error_text = _drift_detail(ei)["error"].lower()
        assert "rebuild" in error_text
        assert "remove" not in error_text  # no "remove it from the manifest"


class TestManifestRequiredAndInvalid:

    @pytest.mark.asyncio
    async def test_manifest_required_when_flag_set(
        self, flow_stubs, templates_dir, user
    ):
        """The MCP tool sets ``require_manifest: true`` in tool code; the
        backend answers a manifest-less archive with a named 400 carrying the
        generation snippet."""
        archive = _build_archive("needs-manifest", manifest=None)
        with pytest.raises(HTTPException) as ei:
            await _deploy(_body(archive, "needs-manifest", require_manifest=True), user)
        assert ei.value.status_code == 400
        detail = ei.value.detail
        assert detail["code"] == "MANIFEST_REQUIRED"
        assert MANIFEST_NAME in str(detail)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "entries",
        [
            # duplicate path
            [{"path": "a.md", "sha256": "0" * 64}, {"path": "a.md", "sha256": "1" * 64}],
            # traversal
            [{"path": "../escape.md", "sha256": "0" * 64}],
            # absolute
            [{"path": "/etc/passwd", "sha256": "0" * 64}],
            # both kinds at once
            [{"path": "a.md", "sha256": "0" * 64, "link_target": "b"}],
            # neither kind
            [{"path": "a.md"}],
            # lists itself
            [{"path": MANIFEST_NAME, "sha256": "0" * 64}],
            # NUL byte — would raise ValueError inside os.path.lexists during
            # verification, escaping the named 400 into the catch-all 500
            [{"path": "a\x00b.md", "sha256": "0" * 64}],
        ],
        ids=["dup", "traversal", "absolute", "both-kinds", "no-kind",
             "self-listed", "nul-byte"],
    )
    async def test_manifest_invalid_shapes_refused(
        self, flow_stubs, templates_dir, user, entries
    ):
        archive = _build_archive("bad-manifest", manifest=entries)
        with pytest.raises(HTTPException) as ei:
            await _deploy(_body(archive, "bad-manifest"), user)
        assert ei.value.status_code == 400
        assert ei.value.detail["code"] == "MANIFEST_INVALID"

    @pytest.mark.asyncio
    async def test_manifest_not_json_refused(self, flow_stubs, templates_dir, user):
        archive = _build_archive("nonjson-manifest", manifest_raw="not json {")
        with pytest.raises(HTTPException) as ei:
            await _deploy(_body(archive, "nonjson-manifest"), user)
        assert ei.value.detail["code"] == "MANIFEST_INVALID"

    @pytest.mark.asyncio
    async def test_manifest_over_read_cap_refused(
        self, flow_stubs, templates_dir, user, monkeypatch
    ):
        monkeypatch.setattr(deploy_mod, "MAX_MANIFEST_BYTES", 64, raising=False)
        archive = _build_archive(
            "fat-manifest", manifest_raw=json.dumps([{"path": "x" * 200, "sha256": "0" * 64}])
        )
        with pytest.raises(HTTPException) as ei:
            await _deploy(_body(archive, "fat-manifest"), user)
        assert ei.value.detail["code"] == "MANIFEST_INVALID"


# ---------------------------------------------------------------------------
# Symlink contract
# ---------------------------------------------------------------------------

class TestSymlinkContract:

    @pytest.mark.asyncio
    async def test_inroot_symlink_preserved_in_deployed_template(
        self, flow_stubs, templates_dir, user
    ):
        """In-root links are preserved end to end — on the base they were
        silently dereferenced by ``copytree``'s default."""
        archive = _build_archive("linky-agent", links={"alias.md": "CLAUDE.md"})
        result = await _deploy(_body(archive, "linky-agent"), user)

        assert result.status == "success"
        deployed = templates_dir / "linky-agent" / "alias.md"
        assert deployed.is_symlink()
        assert os.readlink(deployed) == "CLAUDE.md"

    @pytest.mark.asyncio
    async def test_dangling_inroot_symlink_preserved_with_warning_no_residue(
        self, flow_stubs, templates_dir, user
    ):
        """A link to a runtime-created dir (``content/``, ``data/``) is
        legitimate: preserved + named warning. On the base this was an opaque
        ``shutil.Error`` 500 that also left the partial template dir behind."""
        archive = _build_archive(
            "dangling-agent", links={"notes": "content/notes.md"}
        )
        result = await _deploy(_body(archive, "dangling-agent"), user)

        assert result.status == "success"
        deployed = templates_dir / "dangling-agent" / "notes"
        assert deployed.is_symlink()
        assert any(
            "dangling symlink preserved" in w and "notes" in w
            and "content/notes.md" in w
            for w in result.warnings
        )

    @pytest.mark.asyncio
    async def test_symlink_escape_refused_names_path_and_target(
        self, flow_stubs, templates_dir, user
    ):
        """Regression pin — passes on the base too (dossier b1 correction):
        the refusal names both the member and the target."""
        archive = _build_archive("escape-agent", links={"evil": "../../secret"})
        with pytest.raises(HTTPException) as ei:
            await _deploy(_body(archive, "escape-agent"), user)
        assert ei.value.status_code == 400
        text = str(ei.value.detail)
        assert "evil" in text and "../../secret" in text

    @pytest.mark.asyncio
    async def test_symlink_chain_escape_refused(
        self, flow_stubs, templates_dir, user
    ):
        """a→b, b→../../outside: the exiting hop is itself a member and is
        refused individually, named. Regression pin (passes on base)."""
        archive = _build_archive(
            "chain-escape", links={"a": "b", "b": "../../outside"}
        )
        with pytest.raises(HTTPException) as ei:
            await _deploy(_body(archive, "chain-escape"), user)
        assert ei.value.status_code == 400
        assert "b" in str(ei.value.detail)

    @pytest.mark.asyncio
    async def test_symlink_chain_inroot_preserved(
        self, flow_stubs, templates_dir, user
    ):
        archive = _build_archive(
            "chain-agent", links={"a": "b", "b": "CLAUDE.md"}
        )
        result = await _deploy(_body(archive, "chain-agent"), user)
        assert result.status == "success"
        assert (templates_dir / "chain-agent" / "a").is_symlink()
        assert (templates_dir / "chain-agent" / "b").is_symlink()

    @pytest.mark.asyncio
    async def test_setuid_bit_stripped_on_extraction(
        self, flow_stubs, templates_dir, user
    ):
        """Behavioral pin of ``extractall(filter='tar')`` — the filter strips
        setuid/setgid/sticky; unpinned extraction preserved them."""
        archive = _build_archive(
            "setuid-agent",
            files={"tool.sh": "#!/bin/sh\n"},
            file_modes={"tool.sh": 0o4755},
        )
        result = await _deploy(_body(archive, "setuid-agent"), user)
        assert result.status == "success"
        mode = (templates_dir / "setuid-agent" / "tool.sh").stat().st_mode
        assert mode & stat.S_ISUID == 0


# ---------------------------------------------------------------------------
# Caps + AppleDouble
# ---------------------------------------------------------------------------

class TestCaps:

    @pytest.mark.asyncio
    async def test_too_many_files_error_carries_observed_and_limit(
        self, flow_stubs, templates_dir, user, monkeypatch
    ):
        monkeypatch.setattr(deploy_mod, "MAX_FILES", 3, raising=True)
        archive = _build_archive(
            "many-files",
            files={"a.md": "a", "b.md": "b", "c.md": "c"},  # + 2 defaults = 5
        )
        with pytest.raises(HTTPException) as ei:
            await _deploy(_body(archive, "many-files"), user)
        detail = ei.value.detail
        assert detail["code"] == "TOO_MANY_FILES"
        assert detail["observed"] == 6  # 5 files + manifest member
        assert detail["limit"] == 3

    @pytest.mark.asyncio
    async def test_extracted_size_cap_refused_with_observed(
        self, flow_stubs, templates_dir, user, monkeypatch
    ):
        monkeypatch.setattr(deploy_mod, "MAX_EXTRACTED_SIZE", 100, raising=False)
        archive = _build_archive("bomb-agent", files={"big.bin": "x" * 5000})
        with pytest.raises(HTTPException) as ei:
            await _deploy(_body(archive, "bomb-agent"), user)
        detail = ei.value.detail
        assert detail["code"] == "ARCHIVE_EXTRACTED_TOO_LARGE"
        assert detail["observed"] > 5000
        assert detail["limit"] == 100

    @pytest.mark.asyncio
    async def test_appledouble_members_skipped_with_warning(
        self, flow_stubs, templates_dir, user
    ):
        """``._*`` members are macOS metadata pollution: skipped server-side
        (never extracted, never counted as manifest extras) with a warning."""
        archive = _build_archive(
            "mac-agent", appledouble=["._CLAUDE.md", "._template.yaml"]
        )
        result = await _deploy(_body(archive, "mac-agent"), user)
        assert result.status == "success"
        assert result.verified is True  # no false drift from skipped members
        assert not (templates_dir / "mac-agent" / "._CLAUDE.md").exists()
        assert any("AppleDouble" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Evidence-bearing response
# ---------------------------------------------------------------------------

class TestResponseEvidence:

    @pytest.mark.asyncio
    async def test_response_carries_verified_and_counts(
        self, flow_stubs, templates_dir, user
    ):
        archive = _build_archive("counted-agent", links={"alias.md": "CLAUDE.md"})
        result = await _deploy(_body(archive, "counted-agent"), user)

        assert result.verified is True
        assert result.files_expected == 2  # template.yaml + CLAUDE.md
        assert result.files_deployed == 2  # manifest member excluded
        assert result.symlinks_deployed == 1

    @pytest.mark.asyncio
    async def test_manifest_less_deploy_verified_false_with_warning(
        self, flow_stubs, templates_dir, user
    ):
        """Legacy path (shipped CLI, abilities plugin): still ``success`` —
        flipping status would make every legacy deploy report failure after
        succeeding — but explicitly, machine-readably unverified."""
        archive = _build_archive("legacy-agent", manifest=None)
        result = await _deploy(_body(archive, "legacy-agent"), user)

        assert result.status == "success"
        assert result.verified is False
        assert any(MANIFEST_NAME in w for w in result.warnings)

    @pytest.mark.asyncio
    async def test_credentials_merge_after_verification(
        self, flow_stubs, templates_dir, user
    ):
        """Ordering pin: the post-copy re-verify runs BEFORE the step-9b
        ``.env`` merge. The archive ships ``.env`` listed in the manifest; the
        request credentials append to it — run in the wrong order the sha
        check would refuse its own merge as drift."""
        archive = _build_archive("cred-agent", files={".env": "A=1\n"})
        result = await _deploy(
            _body(archive, "cred-agent", credentials={"FOO": "bar"}), user
        )
        assert result.status == "success"
        assert result.verified is True
        env_text = (templates_dir / "cred-agent" / ".env").read_text()
        assert "A=1" in env_text and "FOO=bar" in env_text

    @pytest.mark.asyncio
    async def test_compat_gate_populates_hard_count(
        self, flow_stubs, templates_dir, user, monkeypatch
    ):
        async def fake_compat(name):
            return 2

        monkeypatch.setattr(
            deploy_mod, "_compatibility_hard_count", fake_compat, raising=False
        )
        archive = _build_archive("compat-agent")
        result = await _deploy(_body(archive, "compat-agent"), user)
        assert result.compatibility_hard_count == 2

    @pytest.mark.asyncio
    async def test_compat_gate_fail_open(
        self, flow_stubs, templates_dir, user, monkeypatch
    ):
        """A raising compatibility report must not fail a completed deploy."""
        async def broken_compat(name):
            raise RuntimeError("collector down")

        monkeypatch.setattr(
            deploy_mod, "_compatibility_hard_count", broken_compat, raising=False
        )
        archive = _build_archive("compat-broken")
        result = await _deploy(_body(archive, "compat-broken"), user)
        assert result.status == "success"
        assert result.compatibility_hard_count is None
        assert any("compatibility" in w.lower() for w in result.warnings)


# ---------------------------------------------------------------------------
# Residue + compensation (S6)
# ---------------------------------------------------------------------------

def _previous_version_stubs(monkeypatch, stopped: dict, started: dict):
    """Wire a running previous version whose stop/restart are recorded."""
    prev = SimpleNamespace(name="agent-v1", status="running")
    monkeypatch.setattr(
        deploy_mod, "get_agents_by_prefix", lambda *a: [prev], raising=True
    )
    monkeypatch.setattr(deploy_mod, "get_latest_version", lambda *a: prev, raising=True)
    monkeypatch.setattr(
        deploy_mod, "get_next_version_name", lambda n: f"{n}-2", raising=True
    )
    monkeypatch.setattr(deploy_mod.db, "get_agents_by_owner", lambda u: ["agent-v1"], raising=True)
    monkeypatch.setattr(
        deploy_mod, "get_agent_container",
        lambda name: SimpleNamespace(name=name), raising=True,
    )

    async def fake_stop(container):
        stopped[container.name] = True

    async def fake_start(container):
        started[container.name] = True

    monkeypatch.setattr(deploy_mod, "container_stop", fake_stop, raising=True)
    monkeypatch.setattr(deploy_mod, "container_start", fake_start, raising=False)


class TestCompensation:

    @pytest.mark.asyncio
    async def test_copytree_failure_removes_partial_template_dir_and_restarts_previous(
        self, flow_stubs, templates_dir, user, monkeypatch
    ):
        """Crux #4 (#2006 class): ``dest_created`` must be assigned BEFORE the
        copy so a mid-copy failure is cleaned; the failure is a named 500; the
        stopped previous version is restarted."""
        stopped, started = {}, {}
        _previous_version_stubs(monkeypatch, stopped, started)

        real_copytree = deploy_mod.shutil.copytree

        def failing_copytree(src, dst, **kw):
            # Leave a partial copy behind, as a real mid-copy crash does.
            os.makedirs(dst, exist_ok=True)
            Path(dst, "partial.md").write_text("half-copied")
            raise deploy_mod.shutil.Error([(str(src), str(dst), "disk error")])

        monkeypatch.setattr(deploy_mod.shutil, "copytree", failing_copytree)

        archive = _build_archive("copyfail")
        with pytest.raises(HTTPException) as ei:
            await _deploy(_body(archive, "copyfail"), user)

        assert ei.value.status_code == 500
        assert ei.value.detail["code"] == "TEMPLATE_COPY_FAILED"
        assert not (templates_dir / "copyfail-2").exists()  # no residue
        assert stopped.get("agent-v1") is True
        assert started.get("agent-v1") is True
        monkeypatch.setattr(deploy_mod.shutil, "copytree", real_copytree)

    @pytest.mark.asyncio
    async def test_create_agent_failure_restarts_previous_version(
        self, flow_stubs, templates_dir, user, monkeypatch
    ):
        """Second-voice R3 fold: compensation covers the create raise too —
        crud rollback + ent#313 reclaim remove the failed container, so the
        restart cannot conflict. The workspace volume is cleaned as well."""
        stopped, started = {}, {}
        _previous_version_stubs(monkeypatch, stopped, started)
        cleaned = []
        monkeypatch.setattr(
            deploy_mod, "_cleanup_deploy_volume",
            lambda name: cleaned.append(name), raising=False,
        )

        async def failing_create(config, *a, **kw):
            raise HTTPException(status_code=500, detail="container create failed")

        archive = _build_archive("createfail")
        with pytest.raises(HTTPException):
            await _deploy(_body(archive, "createfail"), user, failing_create)

        assert started.get("agent-v1") is True
        assert cleaned == ["agent-createfail-2-workspace"]

    @pytest.mark.asyncio
    async def test_failed_prepop_removes_workspace_volume(
        self, flow_stubs, templates_dir, user, monkeypatch
    ):
        cleaned = []
        monkeypatch.setattr(
            deploy_mod, "_cleanup_deploy_volume",
            lambda name: cleaned.append(name), raising=False,
        )

        def exploding_prepop(version_name, template_dir):
            raise HTTPException(status_code=500, detail="docker unavailable")

        monkeypatch.setattr(
            deploy_mod, "_prepopulate_workspace_from_template",
            exploding_prepop, raising=True,
        )
        archive = _build_archive("prepfail")
        with pytest.raises(HTTPException):
            await _deploy(_body(archive, "prepfail"), user)

        assert cleaned == ["agent-prepfail-workspace"]

    @pytest.mark.asyncio
    async def test_late_failure_after_create_does_not_restart_previous(
        self, flow_stubs, templates_dir, user, monkeypatch
    ):
        """The compensation window closes when create_agent_fn RETURNS: a
        failure past that point (e.g. response construction) must not restart
        the previous version alongside the now-live new one — one base name
        running two live versions is the F5 double-run hazard."""
        stopped, started = {}, {}
        _previous_version_stubs(monkeypatch, stopped, started)

        async def create_then_late_failure(config, *a, **kw):
            return _agent_status(config.name)

        # Make the step AFTER a successful create raise: patch the response
        # model so construction fails once the agent is already live.
        class _Boom:
            def __init__(self, *a, **kw):
                raise RuntimeError("late response failure")

        monkeypatch.setattr(deploy_mod, "DeployLocalResponse", _Boom, raising=True)

        archive = _build_archive("lateboom")
        with pytest.raises(HTTPException) as ei:
            await _deploy(_body(archive, "lateboom"), user, create_then_late_failure)

        assert ei.value.status_code == 500
        assert stopped.get("agent-v1") is True
        assert started == {}  # new version is live — previous stays stopped

    @pytest.mark.asyncio
    async def test_success_does_not_restart_previous(
        self, flow_stubs, templates_dir, user, monkeypatch
    ):
        """The stop of the previous version on a SUCCESSFUL deploy is the
        feature, not a failure to compensate."""
        stopped, started = {}, {}
        _previous_version_stubs(monkeypatch, stopped, started)

        archive = _build_archive("goodswap")
        result = await _deploy(_body(archive, "goodswap"), user)
        assert result.status == "success"
        assert stopped.get("agent-v1") is True
        assert started == {}


# ---------------------------------------------------------------------------
# Workspace-volume hygiene at prepop (docker SDK stubbed)
# ---------------------------------------------------------------------------

class _FakeVolume:
    def __init__(self, name, labels=None):
        self.name = name
        self.attrs = {"Labels": labels or {
            "trinity.platform": "agent-workspace", "trinity.agent-name": name,
        }}
        self.removed = False

    def remove(self, force=False):
        self.removed = True


class _FakeVolumes:
    def __init__(self, existing=None):
        self.existing = existing
        self.created = []

    def get(self, name):
        import docker as _docker
        if self.existing is None:
            raise _docker.errors.NotFound("no volume")
        return self.existing

    def create(self, name, labels=None):
        self.created.append((name, labels))
        return _FakeVolume(name, labels)


class _FakeContainers:
    def __init__(self, mounted_volumes=()):
        self._mounted = mounted_volumes

    def list(self, all=False):
        return [
            SimpleNamespace(attrs={"Mounts": [
                {"Type": "volume", "Name": v} for v in self._mounted
            ]})
        ] if self._mounted else []


class _FakeDockerClient:
    def __init__(self, existing_volume=None, mounted=()):
        self.volumes = _FakeVolumes(existing_volume)
        self.containers = _FakeContainers(mounted)


class TestWorkspaceVolumeHygiene:

    def test_prepop_attached_stale_volume_409(self):
        """Never ``put_archive`` into a mounted volume — an attached volume
        under the new version name means a concurrent/zombie deploy."""
        vol = _FakeVolume("agent-v9-workspace")
        client = _FakeDockerClient(existing_volume=vol, mounted=("agent-v9-workspace",))
        with pytest.raises(HTTPException) as ei:
            deploy_mod._ensure_fresh_workspace_volume(client, "agent-v9-workspace", "v9")
        assert ei.value.status_code == 409
        assert ei.value.detail["code"] == "WORKSPACE_VOLUME_IN_USE"
        assert "agent-v9-workspace" in str(ei.value.detail)
        assert vol.removed is False

    def test_prepop_recreates_unattached_stale_volume(self):
        """A leftover volume from a failed attempt is removed and recreated —
        never overlaid (put_archive overlays, never prunes, so stale files
        from the failed attempt would survive into the retry)."""
        vol = _FakeVolume("agent-v9-workspace")
        client = _FakeDockerClient(existing_volume=vol, mounted=())
        deploy_mod._ensure_fresh_workspace_volume(client, "agent-v9-workspace", "v9")
        assert vol.removed is True
        assert client.volumes.created and client.volumes.created[0][0] == "agent-v9-workspace"

    def test_prepop_unknown_attachment_fails_closed(self):
        """If attachment cannot be established, refuse — removing on a blind
        guess is the #1664 lesson."""
        vol = _FakeVolume("agent-v9-workspace")
        client = _FakeDockerClient(existing_volume=vol, mounted=())

        def broken_list(all=False):
            raise RuntimeError("docker api down")

        client.containers.list = broken_list
        with pytest.raises(HTTPException) as ei:
            deploy_mod._ensure_fresh_workspace_volume(client, "agent-v9-workspace", "v9")
        assert ei.value.status_code == 409
        assert vol.removed is False

    def test_cleanup_refuses_foreign_label(self, monkeypatch):
        """The #1581 double-guard shape: a volume without the agent-workspace
        platform label is never removed by deploy cleanup."""
        vol = _FakeVolume("agent-x-workspace", labels={"trinity.platform": "other"})
        client = _FakeDockerClient(existing_volume=vol, mounted=())
        monkeypatch.setattr(
            deploy_mod.docker, "from_env", lambda: client, raising=True
        )
        deploy_mod._cleanup_deploy_volume("agent-x-workspace")
        assert vol.removed is False

    def test_cleanup_skips_attached_volume(self, monkeypatch):
        vol = _FakeVolume("agent-x-workspace")
        client = _FakeDockerClient(existing_volume=vol, mounted=("agent-x-workspace",))
        monkeypatch.setattr(
            deploy_mod.docker, "from_env", lambda: client, raising=True
        )
        deploy_mod._cleanup_deploy_volume("agent-x-workspace")
        assert vol.removed is False

    def test_cleanup_removes_unattached_deploy_volume(self, monkeypatch):
        vol = _FakeVolume("agent-x-workspace")
        client = _FakeDockerClient(existing_volume=vol, mounted=())
        monkeypatch.setattr(
            deploy_mod.docker, "from_env", lambda: client, raising=True
        )
        deploy_mod._cleanup_deploy_volume("agent-x-workspace")
        assert vol.removed is True


# ---------------------------------------------------------------------------
# Per-base-name deploy lock
# ---------------------------------------------------------------------------

class _FakeRedis:
    def __init__(self, set_result=True):
        self.set_result = set_result
        self.store = {}
        self.deleted = []

    def set(self, key, value, nx=False, ex=None):
        if not self.set_result:
            return False
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.deleted.append(key)
        self.store.pop(key, None)


class TestDeployLock:

    @pytest.mark.asyncio
    async def test_deploy_lock_contention_409(
        self, flow_stubs, templates_dir, user, monkeypatch
    ):
        fake = _FakeRedis(set_result=False)
        monkeypatch.setattr(deploy_mod, "_deploy_lock_client", lambda: fake, raising=False)
        archive = _build_archive("locked-agent")
        with pytest.raises(HTTPException) as ei:
            await _deploy(_body(archive, "locked-agent"), user)
        assert ei.value.status_code == 409
        assert ei.value.detail["code"] == "DEPLOY_IN_PROGRESS"

    @pytest.mark.asyncio
    async def test_deploy_lock_acquired_and_released(
        self, flow_stubs, templates_dir, user, monkeypatch
    ):
        fake = _FakeRedis(set_result=True)
        monkeypatch.setattr(deploy_mod, "_deploy_lock_client", lambda: fake, raising=False)
        archive = _build_archive("lockok-agent")
        result = await _deploy(_body(archive, "lockok-agent"), user)
        assert result.status == "success"
        assert fake.deleted == ["agent:deploy_op:lockok-agent"]


# ---------------------------------------------------------------------------
# Router-level idempotency (mirror of the create-endpoint pattern,
# tests/unit/test_ent15_import_intents.py)
# ---------------------------------------------------------------------------

import routers.agents as _agents_router  # noqa: E402
import services.idempotency_service as _idem  # noqa: E402
from models import DeployLocalResponse, VersioningInfo  # noqa: E402


def _deploy_response(version: str) -> DeployLocalResponse:
    return DeployLocalResponse(
        status="success",
        versioning=VersioningInfo(base_name="agent", new_version=version),
    )


@pytest.fixture
def deploy_endpoint_env(monkeypatch):
    env = {
        "begin_calls": [],
        "complete_calls": [],
        "fail_calls": [],
        "discard_calls": [],
        "agent_live": True,
        "begin_results": [
            _idem.IdempotencyDecision(enabled=True, replay=False, in_flight=False)
        ],
    }

    def fake_begin(scope, key):
        env["begin_calls"].append((scope, key))
        results = env["begin_results"]
        return results.pop(0) if len(results) > 1 else results[0]

    monkeypatch.setattr(_idem, "begin", fake_begin)
    monkeypatch.setattr(
        _idem, "complete",
        lambda decision, execution_id, snapshot=None: env["complete_calls"].append(
            (decision, execution_id, snapshot)
        ),
    )
    monkeypatch.setattr(_idem, "fail", lambda d: env["fail_calls"].append(d))
    monkeypatch.setattr(
        _idem, "discard_stale_replay",
        lambda scope, key: env["discard_calls"].append((scope, key)),
    )
    monkeypatch.setattr(
        _agents_router.db, "is_agent_live", lambda name: env["agent_live"]
    )
    logic_mock = AsyncMock(side_effect=lambda **kw: _deploy_response("agent-2"))
    monkeypatch.setattr(_agents_router, "deploy_local_agent_logic", logic_mock)
    env["logic_mock"] = logic_mock
    return env


def _endpoint_user():
    return SimpleNamespace(id=7, username="creator", role="creator")


class TestDeployIdempotency:

    @pytest.mark.asyncio
    async def test_deploy_idempotency_replay_single_version(self, deploy_endpoint_env):
        env = deploy_endpoint_env
        env["begin_results"] = [
            _idem.IdempotencyDecision(
                enabled=True, replay=True, in_flight=False,
                scope="agent_deploy:7", key="k1",
                snapshot={"status": "success",
                          "versioning": {"base_name": "agent", "new_version": "agent-2",
                                         "previous_version_stopped": False}},
            )
        ]
        body = SimpleNamespace(archive="x", name="agent")
        resp = await _agents_router.deploy_local_agent(
            body, MagicMock(), current_user=_endpoint_user(), idempotency_key="k1"
        )
        assert resp.headers["x-idempotent-replay"] == "true"
        assert env["logic_mock"].await_count == 0  # single version minted
        assert env["begin_calls"] == [("agent_deploy:7", "k1")]

    @pytest.mark.asyncio
    async def test_deploy_idempotency_inflight_409(self, deploy_endpoint_env):
        env = deploy_endpoint_env
        env["begin_results"] = [
            _idem.IdempotencyDecision(
                enabled=True, replay=True, in_flight=True,
                scope="agent_deploy:7", key="k1",
            )
        ]
        body = SimpleNamespace(archive="x", name="agent")
        with pytest.raises(HTTPException) as ei:
            await _agents_router.deploy_local_agent(
                body, MagicMock(), current_user=_endpoint_user(), idempotency_key="k1"
            )
        assert ei.value.status_code == 409
        assert ei.value.detail["code"] == "DEPLOY_IN_FLIGHT"
        assert env["logic_mock"].await_count == 0

    @pytest.mark.asyncio
    async def test_deploy_idempotency_stale_replay_falls_through(
        self, deploy_endpoint_env
    ):
        """#2040-F3: a completed replay is honored only while the recorded
        version is still live; stale → discard + a genuinely fresh deploy."""
        env = deploy_endpoint_env
        env["agent_live"] = False
        env["begin_results"] = [
            _idem.IdempotencyDecision(
                enabled=True, replay=True, in_flight=False,
                scope="agent_deploy:7", key="k1",
                snapshot={"versioning": {"new_version": "agent-2"}},
            ),
            _idem.IdempotencyDecision(enabled=True, replay=False, in_flight=False),
        ]
        body = SimpleNamespace(archive="x", name="agent")
        result = await _agents_router.deploy_local_agent(
            body, MagicMock(), current_user=_endpoint_user(), idempotency_key="k1"
        )
        assert env["discard_calls"] == [("agent_deploy:7", "k1")]
        assert env["logic_mock"].await_count == 1
        assert env["complete_calls"] and env["complete_calls"][0][1] == "agent-2"
        assert result.status == "success"

    @pytest.mark.asyncio
    async def test_deploy_failure_releases_the_claim(self, deploy_endpoint_env):
        env = deploy_endpoint_env
        env["logic_mock"].side_effect = HTTPException(status_code=400, detail="bad")
        body = SimpleNamespace(archive="x", name="agent")
        with pytest.raises(HTTPException):
            await _agents_router.deploy_local_agent(
                body, MagicMock(), current_user=_endpoint_user(), idempotency_key="k1"
            )
        assert len(env["fail_calls"]) == 1
        assert env["complete_calls"] == []


# ---------------------------------------------------------------------------
# Static ordering pins (the #2006 style: position, not just behavior)
# ---------------------------------------------------------------------------

def test_security_validation_stays_pre_extraction_in_source_order():
    """Layering rule (§2.1): `_validate_tar_member` runs over getmembers()
    BEFORE extractall — drift verification is the only post-extract layer."""
    src = Path(deploy_mod.__file__).read_text()
    body = src.split("def _safe_extract_tar", 1)[1]
    assert body.index("_validate_tar_member(") < body.index("tar.extractall(")


def test_post_extract_verification_precedes_version_side_effects():
    src = Path(deploy_mod.__file__).read_text()
    body = src.split("async def deploy_local_agent_logic", 1)[1]
    assert body.index("_verify_manifest(") < body.index("get_next_version_name(")


def test_post_copy_verification_precedes_credentials_merge():
    src = Path(deploy_mod.__file__).read_text()
    body = src.split("async def deploy_local_agent_logic", 1)[1]
    post_copy = body.index("_verify_manifest(", body.index("shutil.copytree("))
    # `env_content` is the step-9b merge itself (`body.credentials` alone
    # would anchor on the step-1b count cap, which legitimately runs first).
    assert post_copy < body.index("env_content")


def test_dest_created_assigned_before_copytree():
    """Crux #4: a mid-copy failure must be cleanable, so the handle exists
    before the copy starts."""
    src = Path(deploy_mod.__file__).read_text()
    body = src.split("async def deploy_local_agent_logic", 1)[1]
    assert body.index("dest_created = dest_path") < body.index("shutil.copytree(")
