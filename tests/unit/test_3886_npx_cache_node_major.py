"""#3886 — a Node major bump does not strand native addons in the npx cache.

The base image moved Node 20 -> 24. The runtime is in the image; the npx cache
(``~/.npm/_npx``) is on the persistent home volume and survives the recreate.
npx reuses a cached tree as-is, so an MCP server with a native addon built
under Node 20 fails on Node 24 with ``NODE_MODULE_VERSION 115`` — verified
against real ``node:20``/``node:24`` images sharing one volume when this was
written. ``startup.sh`` drops the cache when the image's Node major differs
from the one stamped on the volume.

These tests run the real block out of ``startup.sh`` in bash, with
``/home/developer`` pointed at a temp dir and a stub ``node`` on PATH.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_STARTUP = Path(__file__).resolve().parents[2] / "docker" / "base-image" / "startup.sh"
_BLOCK = re.compile(r"# >>> node-major-npx-cache\n(.*?)# <<< node-major-npx-cache", re.S)

if shutil.which("bash") is None:  # pragma: no cover
    pytest.skip("bash not available", allow_module_level=True)


def _run(home: Path, major: str) -> str:
    block = _BLOCK.search(_STARTUP.read_text(encoding="utf-8"))
    assert block, "node-major-npx-cache markers missing from startup.sh"
    script = block.group(1).replace("/home/developer", str(home))

    bindir = home / "_bin"
    bindir.mkdir(exist_ok=True)
    node = bindir / "node"
    node.write_text(f"#!/bin/sh\necho {major}\n")
    node.chmod(0o755)

    env = {**os.environ, "PATH": f"{bindir}:{os.environ.get('PATH', '')}"}
    proc = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _cache(home: Path) -> Path:
    pkg = home / ".npm" / "_npx" / "229d424af6a48f66" / "node_modules" / "better-sqlite3"
    pkg.mkdir(parents=True)
    return pkg


def _stamp(home: Path) -> Path:
    return home / ".trinity" / "node-major"


def test_a_cache_from_before_the_stamp_is_cleared(tmp_path: Path) -> None:
    """The upgrade that matters: an existing agent has a cache and no stamp."""
    _cache(tmp_path)
    out = _run(tmp_path, "24")
    assert not (tmp_path / ".npm" / "_npx").exists()
    assert "unknown -> 24" in out
    assert _stamp(tmp_path).read_text().strip() == "24"


def test_a_major_change_clears_the_cache(tmp_path: Path) -> None:
    _cache(tmp_path)
    _stamp(tmp_path).parent.mkdir(parents=True)
    _stamp(tmp_path).write_text("20\n")
    out = _run(tmp_path, "24")
    assert not (tmp_path / ".npm" / "_npx").exists()
    assert "20 -> 24" in out
    assert _stamp(tmp_path).read_text().strip() == "24"


def test_the_same_major_keeps_the_cache(tmp_path: Path) -> None:
    """Every ordinary restart must not cost a re-fetch of every MCP server."""
    pkg = _cache(tmp_path)
    _stamp(tmp_path).parent.mkdir(parents=True)
    _stamp(tmp_path).write_text("24\n")
    out = _run(tmp_path, "24")
    assert pkg.is_dir()
    assert out == ""


def test_a_fresh_volume_only_writes_the_stamp(tmp_path: Path) -> None:
    out = _run(tmp_path, "24")
    assert out == ""
    assert _stamp(tmp_path).read_text().strip() == "24"


def test_project_node_modules_are_left_alone(tmp_path: Path) -> None:
    """The agent's own dependencies are its files, not a cache."""
    _cache(tmp_path)
    project = tmp_path / "app" / "node_modules" / "better-sqlite3"
    project.mkdir(parents=True)
    _run(tmp_path, "24")
    assert project.is_dir()


def test_no_node_on_path_changes_nothing(tmp_path: Path) -> None:
    pkg = _cache(tmp_path)
    block = _BLOCK.search(_STARTUP.read_text(encoding="utf-8"))
    script = block.group(1).replace("/home/developer", str(tmp_path))
    empty = tmp_path / "_empty_path"
    empty.mkdir()
    proc = subprocess.run(
        [shutil.which("bash"), "-c", script],
        env={**os.environ, "PATH": str(empty)},
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0
    assert pkg.is_dir()
    assert not _stamp(tmp_path).exists()
