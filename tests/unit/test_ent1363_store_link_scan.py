"""Links already in the deployed-templates store (trinity-enterprise#1363).

A deploy refuses an archive whose links resolve outside it. A template stored
before that check existed is still on disk and still read, so the same rule is
applied to the store at boot: a link that leaves its own template directory is
removed, and nothing else is.

"Outside" means outside `<store>/<name>/`, not outside the store. A link from
one template into a sibling is the case that matters most.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from services.agent_service import deploy as deploy_mod  # noqa: E402

scan = deploy_mod.remove_escaping_links_from_store


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "deployed-templates"
    root.mkdir()
    for name in ("mine", "theirs"):
        (root / name).mkdir()
        (root / name / "template.yaml").write_text(f"name: {name}\n")
    (root / "theirs" / ".mcp.json").write_text('{"owner": "theirs"}')
    return root


def _tree(root: Path) -> dict:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames + filenames:
            p = Path(dirpath) / n
            rel = p.relative_to(root).as_posix()
            out[rel] = ("link", os.readlink(p)) if p.is_symlink() else (
                "dir" if p.is_dir() else ("file", p.read_bytes()))
    return out


class TestWhatIsRemoved:
    def test_a_link_into_a_sibling_template(self, store):
        (store / "mine" / "x").symlink_to("../theirs/.mcp.json")
        assert scan(store) == [("mine", "x", "../theirs/.mcp.json")]
        assert not os.path.lexists(store / "mine" / "x")

    def test_a_link_out_of_the_store(self, store, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("s")
        (store / "mine" / "deep").mkdir()
        (store / "mine" / "deep" / "x").symlink_to(secret)
        assert scan(store) == [("mine", "deep/x", str(secret))]

    def test_a_link_to_a_directory_outside(self, store):
        (store / "mine" / "d").symlink_to("../theirs", target_is_directory=True)
        assert scan(store) == [("mine", "d", "../theirs")]
        assert (store / "theirs" / ".mcp.json").exists()

    def test_a_dangling_link_that_points_outside(self, store):
        (store / "mine" / "x").symlink_to("/nonexistent/elsewhere")
        assert scan(store) == [("mine", "x", "/nonexistent/elsewhere")]

    def test_a_chain_that_only_escapes_once_resolved(self, store):
        """The shape #1328 reported: each link passes a lexical check."""
        (store / "mine" / "a").symlink_to(".")
        (store / "mine" / "x").symlink_to("a/a/../../../theirs/.mcp.json")
        removed = scan(store)
        assert ("mine", "x", "a/a/../../../theirs/.mcp.json") in removed
        assert os.path.lexists(store / "mine" / "a"), "an in-root link stays"

    def test_a_top_level_entry_that_is_itself_a_link(self, store):
        (store / "alias").symlink_to("theirs", target_is_directory=True)
        assert scan(store) == [("alias", ".", "theirs")]
        assert (store / "theirs").is_dir()

    def test_every_hit_in_every_template(self, store):
        (store / "mine" / "x").symlink_to("../theirs/.mcp.json")
        (store / "mine" / "y").symlink_to("/etc/hostname")
        (store / "theirs" / "z").symlink_to("../mine/template.yaml")
        assert scan(store) == [
            ("mine", "x", "../theirs/.mcp.json"),
            ("mine", "y", "/etc/hostname"),
            ("theirs", "z", "../mine/template.yaml"),
        ]


class TestWhatIsLeftAlone:
    def test_the_target_of_a_removed_link(self, store):
        (store / "mine" / "x").symlink_to("../theirs/.mcp.json")
        scan(store)
        assert (store / "theirs" / ".mcp.json").read_text() == '{"owner": "theirs"}'

    def test_links_that_stay_inside_their_template(self, store):
        mine = store / "mine"
        (mine / "docs").mkdir()
        (mine / "docs" / "readme").write_text("r")
        (mine / "alias").symlink_to("docs/readme")
        (mine / "docs" / "up").symlink_to("../template.yaml")
        (mine / "later").symlink_to("data/not-created-yet")  # the #2060 dangling case
        before = _tree(store)
        assert scan(store) == []
        assert _tree(store) == before

    def test_a_store_with_no_links_is_untouched(self, store):
        before = _tree(store)
        assert scan(store) == []
        assert _tree(store) == before

    def test_stray_files_at_the_top_level(self, store):
        (store / "notes.txt").write_text("n")
        assert scan(store) == []
        assert (store / "notes.txt").exists()

    def test_only_the_link_goes_from_a_template_with_a_hit(self, store):
        (store / "mine" / "x").symlink_to("../theirs/.mcp.json")
        before = _tree(store)
        scan(store)
        before.pop("mine/x")
        assert _tree(store) == before


class TestItNeverStopsABoot:
    def test_no_store(self, tmp_path):
        assert scan(tmp_path / "absent") == []

    def test_the_store_is_a_file(self, tmp_path):
        f = tmp_path / "store"
        f.write_text("")
        assert scan(f) == []

    def test_a_second_run_finds_nothing(self, store):
        (store / "mine" / "x").symlink_to("../theirs/.mcp.json")
        assert len(scan(store)) == 1
        assert scan(store) == []

    def test_a_link_another_worker_already_removed(self, store, monkeypatch):
        (store / "mine" / "x").symlink_to("../theirs/.mcp.json")
        real = os.unlink

        def gone(path, *a, **k):
            real(path)
            raise FileNotFoundError(path)

        monkeypatch.setattr(deploy_mod.os, "unlink", gone)
        assert scan(store) == [], "not counted twice across workers"

    def test_a_link_that_cannot_be_removed_is_logged_and_skipped(self, store, monkeypatch, caplog):
        (store / "mine" / "x").symlink_to("../theirs/.mcp.json")
        (store / "mine" / "y").symlink_to("/etc/hostname")
        real = os.unlink

        def refuse_x(path, *a, **k):
            if str(path).endswith("/x"):
                raise PermissionError(13, "denied")
            real(path)

        monkeypatch.setattr(deploy_mod.os, "unlink", refuse_x)
        with caplog.at_level(logging.ERROR):
            assert scan(store) == [("mine", "y", "/etc/hostname")]
        assert "could not remove link mine/x" in caplog.text


class TestTheRecord:
    def test_each_removal_names_template_path_and_target(self, store, caplog):
        (store / "mine" / "x").symlink_to("../theirs/.mcp.json")
        with caplog.at_level(logging.WARNING):
            scan(store)
        assert "removed link mine/x -> ../theirs/.mcp.json" in caplog.text

    def test_the_default_store_is_the_one_deploys_write_to(self, monkeypatch, store):
        monkeypatch.setattr(deploy_mod, "DEPLOYED_TEMPLATES_DIR_IN_BACKEND", str(store))
        (store / "mine" / "x").symlink_to("../theirs/.mcp.json")
        assert scan() == [("mine", "x", "../theirs/.mcp.json")]


class TestBootHook:
    def test_the_scheduled_scan_runs_off_the_loop_and_removes(self, monkeypatch, store):
        monkeypatch.setattr(deploy_mod, "DEPLOYED_TEMPLATES_DIR_IN_BACKEND", str(store))
        (store / "mine" / "x").symlink_to("../theirs/.mcp.json")

        async def go():
            deploy_mod.schedule_store_link_scan()
            await deploy_mod._store_link_scan_task

        asyncio.run(go())
        assert not os.path.lexists(store / "mine" / "x")

    def test_a_failing_scan_does_not_raise_into_the_loop(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("disk")

        monkeypatch.setattr(deploy_mod, "remove_escaping_links_from_store", boom)

        async def go():
            deploy_mod.schedule_store_link_scan()
            await deploy_mod._store_link_scan_task

        asyncio.run(go())

    def test_main_schedules_it_at_boot(self):
        main = (_BACKEND / "main.py").read_text(encoding="utf-8")
        assert "_deploy_boot.schedule_store_link_scan()" in main
