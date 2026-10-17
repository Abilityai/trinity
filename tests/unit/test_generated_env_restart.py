"""Regression for #3710. Generated template .env is a seed, not a restart-time credential authority.

Run the actual startup credential-import shell block with only its fixed paths
redirected into tmp_path; no Docker, provider credentials or startup services.
"""
from pathlib import Path
import subprocess
import sys

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _restart(home, generated):
    source = (_ROOT / "docker/base-image/startup.sh").read_text()
    block = source.split("# Copy generated credential files (", 1)[1]
    block = "# Copy generated credential files (" + block.split("# === Codex runtime setup", 1)[0]
    block = block.replace("/home/developer", str(home)).replace("/generated-creds", str(generated))
    subprocess.run(["bash", "-c", block], check=True, capture_output=True, timeout=5)


@pytest.fixture
def workspace(tmp_path):
    home, generated = tmp_path / "home", tmp_path / "generated"
    home.mkdir()
    generated.mkdir()
    (generated / ".env").write_text("EXAMPLE_SERVICE_TOKEN=\n")
    return home, generated


def test_generated_env_seeds_new_workspace(workspace):
    home, generated = workspace
    _restart(home, generated)
    assert (home / ".env").read_bytes() == (generated / ".env").read_bytes()


@pytest.mark.parametrize("content", ["EXAMPLE_SERVICE_TOKEN=synthetic-rotated-value\n", ""])
def test_restart_preserves_injected_or_deliberately_cleared_env(workspace, content):
    home, generated = workspace
    _restart(home, generated)
    # The supported inject endpoint writes this workspace file and chmods 0600;
    # it does not update the creation-time generated credential mount.
    target = home / ".env"
    target.write_text(content)
    target.chmod(0o600)
    for _ in range(2):
        _restart(home, generated)
        assert target.read_text() == content
        assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("exists", [
    True,
    # BSD cp (macOS) stats through a dangling symlink and creates the target;
    # GNU coreutils in the production base image hits EEXIST and skips. The
    # production behaviour is what matters; skip the non-portable case locally.
    pytest.param(False, marks=pytest.mark.skipif(
        sys.platform == "darwin", reason="BSD cp -n follows a dangling symlink; GNU cp does not")),
])
def test_restart_does_not_follow_existing_env_symlink(workspace, exists):
    home, generated = workspace
    outside = home.parent / "outside"
    if exists:
        outside.write_text("synthetic-owner-content\n")
    (home / ".env").symlink_to(outside)
    _restart(home, generated)
    assert (home / ".env").is_symlink()
    if exists:
        assert outside.read_text() == "synthetic-owner-content\n"
    else:
        assert not outside.exists()
