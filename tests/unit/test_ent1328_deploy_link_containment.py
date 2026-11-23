"""deploy-local: a symlink is judged by where it resolves once it exists
(abilityai/trinity-enterprise#1328).

The pre-extraction check reads each link name on its own, before anything is
on disk. Two links can pass it separately and escape together: `a -> .` turns
every `a/` hop into a no-op once extracted, so `x -> a/a/../../outside` is an
in-root path on paper and leaves the root on disk.

These tests drive the real `deploy_local_agent_logic` through the
`test_2060_deploy_integrity` harness. The property asserted is the one that
matters: a file OUTSIDE the archive is never readable through anything the
deploy stages. The #2060 contract (in-root links kept, dangling in-root links
kept with a warning) is asserted beside it, unchanged.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent))

from services.agent_service import deploy as deploy_mod  # noqa: E402
from test_2060_deploy_integrity import (  # noqa: E402,F401  (fixtures)
    _body, _build_archive, _deploy, flow_stubs, templates_dir, user,
)

pytestmark = pytest.mark.unit

SECRET = "LABFAKE-not-a-real-key-ent1328"
VICTIM_CONFIG = {"mcpServers": {"billing": {"command": "npx", "args": ["-y", "billing-mcp"],
                                             "env": {"BILLING_KEY": SECRET}}}}


def _hops(n: int, tail, hop: str = "a") -> str:
    """`a/` n times, then `..` n times, then `tail`.

    On paper the hops and the `..` run cancel, so this is `<root>/<tail>`: an
    in-root path, which is why the per-member check passes it. On disk, with
    `a -> .` extracted, the hops go nowhere and the `..` run climbs n levels
    out of the root (stopping at `/`), so `tail` is read from up there.
    """
    return "/".join([hop] * n + [".."] * n) + "/" + str(tail).lstrip("/")


@pytest.fixture
def victim(templates_dir):
    """Another user's deployed template, holding a literal credential."""
    d = templates_dir / "victim-agent"
    d.mkdir(parents=True)
    (d / ".mcp.json").write_text(json.dumps(VICTIM_CONFIG))
    return d


def _nothing_staged_reads_the_victim(root: Path) -> None:
    """No path under a deployed template gives the victim's secret."""
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            try:
                if path.is_file():
                    assert SECRET not in path.read_text(errors="ignore"), f"{path} reads the victim's file"
            except OSError:
                pass


async def _refused(archive_name, links, user, files=None, manifest="auto"):
    archive = _build_archive(archive_name, files=files, links=links, manifest=manifest)
    with pytest.raises(HTTPException) as refused:
        await _deploy(_body(archive, archive_name), user)
    assert refused.value.status_code == 400
    return refused.value.detail


class TestCancellationChain:
    """`a -> .` plus a link whose `a/` hops and `..` run cancel on paper."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("hops", [1, 2, 11, 12, 39])
    async def test_the_chain_is_refused_and_named(self, flow_stubs, templates_dir, user, victim, hops):
        # With enough hops the `..` run reaches `/` and the tail is the
        # victim's absolute path (11, 12, 39). With too few (1, 2) it lands on
        # a path that does not exist, still outside the root: refused the same.
        target = _hops(hops, victim / ".mcp.json")
        detail = await _refused("thief", {"a": ".", ".mcp.json": target}, user)

        assert detail["code"] == "INVALID_ARCHIVE"
        assert ".mcp.json" in detail["error"] and "escapes" in detail["error"]
        assert not (templates_dir / "thief").exists(), "nothing of a refused deploy is left behind"
        _nothing_staged_reads_the_victim(templates_dir / "thief")

    @pytest.mark.asyncio
    async def test_the_lexical_check_alone_would_have_passed_it(self, tmp_path):
        """The reason a post-extraction pass exists: on paper this link is in-root."""
        import tarfile

        base = tmp_path / "x"
        base.mkdir()
        member = tarfile.TarInfo("agent/note")
        member.type = tarfile.SYMTYPE
        member.linkname = _hops(12, "data/deployed-templates/someone/.mcp.json")
        ok, _ = deploy_mod._validate_tar_member(member, base)
        assert ok is True

    @pytest.mark.asyncio
    async def test_hops_nested_in_a_subdirectory(self, flow_stubs, templates_dir, user, victim):
        target = _hops(14, victim / ".mcp.json", hop="here")
        detail = await _refused(
            "nested", {"docs/here": ".", "docs/leak.json": target}, user,
            files={"docs/readme.md": "docs\n"})
        assert "docs/leak.json" in detail["error"]
        assert not (templates_dir / "nested").exists()

    @pytest.mark.asyncio
    async def test_any_file_name_not_only_the_mcp_config(self, flow_stubs, templates_dir, user, victim):
        target = _hops(12, victim / ".mcp.json")
        detail = await _refused("other-name", {"a": ".", "notes.txt": target}, user)
        assert "notes.txt" in detail["error"]

    @pytest.mark.asyncio
    async def test_without_a_manifest_too(self, flow_stubs, templates_dir, user, victim):
        target = _hops(12, victim / ".mcp.json")
        await _refused("no-manifest", {"a": ".", ".mcp.json": target}, user, manifest=None)
        assert not (templates_dir / "no-manifest").exists()


class TestSiblingInTheSharedStore:
    """A nested archive root is one directory below the extraction directory,
    and the pre-extraction check measures against the extraction directory. So
    `../<name>` is in bounds on paper, dangling in the temp tree, and once the
    tree is copied into the shared store it names a SIBLING template there."""

    @pytest.mark.asyncio
    async def test_a_link_to_a_sibling_templates_config_is_refused(self, flow_stubs, templates_dir, user, victim):
        detail = await _refused("neighbour", {".mcp.json": "../victim-agent/.mcp.json"}, user)
        assert ".mcp.json" in str(detail)
        assert not (templates_dir / "neighbour").exists()

    @pytest.mark.asyncio
    async def test_under_any_file_name(self, flow_stubs, templates_dir, user, victim):
        detail = await _refused("neighbour-2", {"peek.json": "../victim-agent/.mcp.json"}, user)
        assert detail["code"] == "INVALID_ARCHIVE" and "peek.json" in detail["error"]
        assert not (templates_dir / "neighbour-2").exists()

    @pytest.mark.asyncio
    async def test_a_link_to_the_whole_sibling_directory_is_refused(self, flow_stubs, templates_dir, user, victim):
        detail = await _refused("neighbour-3", {"theirs": "../victim-agent"}, user)
        assert "theirs" in detail["error"]


class TestBothTreesAreChecked:
    @pytest.mark.asyncio
    async def test_the_extracted_tree_then_the_deployed_copy(self, flow_stubs, templates_dir, user, monkeypatch):
        seen = []
        real = deploy_mod._refuse_escaping_symlinks

        def spy(root, stage):
            seen.append((stage, Path(root).name))
            return real(root, stage)

        monkeypatch.setattr(deploy_mod, "_refuse_escaping_symlinks", spy)
        result = await _deploy(_body(_build_archive("twice"), "twice"), user)
        assert result.status == "success"
        assert seen == [("post-extract", "twice"), ("post-copy", "twice")]

    @pytest.mark.asyncio
    async def test_a_refusal_on_the_deployed_copy_leaves_no_directory_behind(
            self, flow_stubs, templates_dir, user, monkeypatch):
        real = deploy_mod._refuse_escaping_symlinks

        def only_after_the_copy(root, stage):
            if stage == "post-copy":
                # What a link that only escapes from the store would look like.
                (Path(root) / "late").symlink_to("../elsewhere")
            return real(root, stage)

        monkeypatch.setattr(deploy_mod, "_refuse_escaping_symlinks", only_after_the_copy)
        with pytest.raises(HTTPException) as refused:
            await _deploy(_body(_build_archive("late-escape"), "late-escape"), user)
        assert refused.value.status_code == 400 and "late" in refused.value.detail["error"]
        assert not (templates_dir / "late-escape").exists()


class TestContainmentNotDanglingness:
    def test_an_existing_target_outside_the_root_is_reported(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("x")
        (root / "a").symlink_to(".")
        (root / "leak").symlink_to("a/a/../outside.txt")
        assert (root / "leak").exists(), "the target exists: this link is not dangling"
        assert deploy_mod._collect_dangling_symlinks(root) == []
        assert deploy_mod._escaping_symlinks(root) == [("leak", "a/a/../outside.txt")]

    def test_a_missing_target_outside_the_root_is_reported_too(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        (root / "a").symlink_to(".")
        (root / "later").symlink_to("a/../not-there-yet")
        assert [rel for rel, _ in deploy_mod._escaping_symlinks(root)] == ["later"]

    def test_links_that_stay_inside_are_left_alone(self, tmp_path):
        root = tmp_path / "root"
        (root / "sub").mkdir(parents=True)
        (root / "file.md").write_text("x")
        (root / "alias.md").symlink_to("file.md")
        (root / "self").symlink_to(".")
        (root / "sub" / "up").symlink_to("../file.md")
        (root / "dangling").symlink_to("content/notes.md")
        (root / "round").symlink_to("self/self/sub/../file.md")
        assert deploy_mod._escaping_symlinks(root) == []

    def test_a_loop_is_not_an_escape(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        (root / "x").symlink_to("y")
        (root / "y").symlink_to("x")
        assert deploy_mod._escaping_symlinks(root) == []

    def test_a_root_reached_through_a_link_is_still_its_own_root(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        (real / "file.md").write_text("x")
        (real / "alias.md").symlink_to("file.md")
        via = tmp_path / "via"
        via.symlink_to(real)
        assert deploy_mod._escaping_symlinks(via) == []


class TestTheContractFrom2060StillHolds:
    @pytest.mark.asyncio
    async def test_inroot_links_are_deployed_as_links(self, flow_stubs, templates_dir, user):
        archive = _build_archive("linky", links={"alias.md": "CLAUDE.md", "self": "."})
        result = await _deploy(_body(archive, "linky"), user)
        assert result.status == "success"
        assert os.readlink(templates_dir / "linky" / "alias.md") == "CLAUDE.md"
        assert result.symlinks_deployed == 2

    @pytest.mark.asyncio
    async def test_a_dangling_inroot_link_is_kept_with_its_warning(self, flow_stubs, templates_dir, user):
        archive = _build_archive("dangly", links={"notes": "content/notes.md"})
        result = await _deploy(_body(archive, "dangly"), user)
        assert result.status == "success"
        assert (templates_dir / "dangly" / "notes").is_symlink()
        assert any("dangling symlink preserved: notes" in w for w in result.warnings)


class TestTheMcpConfigIsNeverReadThroughALink:
    @pytest.mark.asyncio
    async def test_an_inroot_link_named_mcp_json_is_refused(self, flow_stubs, templates_dir, user):
        archive = _build_archive(
            "linked-config",
            files={"real.json": json.dumps({"mcpServers": {}})},
            links={".mcp.json": "real.json"})
        with pytest.raises(HTTPException) as refused:
            await _deploy(_body(archive, "linked-config"), user)
        assert refused.value.status_code == 400
        assert "symlink" in str(refused.value.detail)
        assert not (templates_dir / "linked-config").exists()

    @pytest.mark.asyncio
    async def test_a_dangling_link_named_mcp_json_is_refused_not_skipped(self, flow_stubs, templates_dir, user):
        archive = _build_archive("dangling-config", links={".mcp.json": "later/config.json"})
        with pytest.raises(HTTPException) as refused:
            await _deploy(_body(archive, "dangling-config"), user)
        assert refused.value.status_code == 400

    @pytest.mark.asyncio
    async def test_a_regular_mcp_json_still_deploys(self, flow_stubs, templates_dir, user):
        archive = _build_archive("plain-config", files={".mcp.json": json.dumps({"mcpServers": {}})})
        result = await _deploy(_body(archive, "plain-config"), user)
        assert result.status == "success"

    def test_staging_credentials_does_not_follow_a_linked_template_config(self, tmp_path, victim):
        """Defence in depth for anything else that writes into a template dir."""
        from services import template_service

        template = tmp_path / "attacker-template"
        template.mkdir()
        (template / ".mcp.json").symlink_to(victim / ".mcp.json")
        files = template_service.generate_credential_files(
            {"credentials": {"mcp_servers": {"billing": {"env": {"BILLING_KEY": "x"}}}}},
            {}, "attacker-agent", str(template))
        assert SECRET not in json.dumps(files)

    def test_staging_credentials_still_reads_a_regular_template_config(self, tmp_path):
        from services import template_service

        template = tmp_path / "own-template"
        template.mkdir()
        (template / ".mcp.json").write_text(json.dumps(
            {"mcpServers": {"billing": {"command": "npx", "env": {"BILLING_KEY": "${BILLING_KEY}"}}}}))
        files = template_service.generate_credential_files(
            {"credentials": {"mcp_servers": {"billing": {"env": {"BILLING_KEY": "x"}}}}},
            {"BILLING_KEY": "mine"}, "own-agent", str(template))
        assert "mine" in json.dumps(files)
