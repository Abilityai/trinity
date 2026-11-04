"""Regression for #3710. Generated credential files are seeds, not restart-time credential authorities.

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
    block = block.replace("/home/developer", str(home))
    block = block.replace("/config/generated-creds", str(home.parent / "volume-seeds"))
    block = block.replace("/generated-creds", str(home.parent / "legacy-seeds"))
    subprocess.run(["bash", "-c", block], check=True, capture_output=True, timeout=5)


@pytest.fixture(params=[("legacy", ".env"), ("volume", ".env"),
                        ("legacy", ".mcp.json"), ("volume", ".mcp.json")])
def workspace(tmp_path, request):
    transport, filename = request.param
    home = tmp_path / "home"
    generated = tmp_path / ("volume-seeds" if transport == "volume" else "legacy-seeds")
    home.mkdir()
    generated.mkdir()
    (generated / filename).write_text(
        "EXAMPLE_SERVICE_TOKEN=\n" if filename == ".env" else '{"mcpServers": {}}\n')
    if transport == "volume":
        legacy = tmp_path / "legacy-seeds"
        legacy.mkdir()
        (legacy / filename).write_text(
            "EXAMPLE_SERVICE_TOKEN=unused-legacy-seed\n" if filename == ".env"
            else '{"mcpServers": {"legacy": {"command": "unused"}}}\n')
    return home, generated, filename


def test_generated_credentials_seed_new_workspace(workspace):
    home, generated, filename = workspace
    _restart(home, generated)
    assert (home / filename).read_bytes() == (generated / filename).read_bytes()


@pytest.mark.parametrize("cleared", [False, True])
def test_restart_preserves_injected_or_deliberately_cleared_credentials(workspace, cleared):
    home, generated, filename = workspace
    content = "" if cleared else (
        "EXAMPLE_SERVICE_TOKEN=synthetic-rotated-value\n" if filename == ".env"
        else '{"mcpServers": {"injected": {"command": "synthetic"}}}\n')
    _restart(home, generated)
    # The supported inject endpoint writes this workspace file and chmods 0600;
    # it does not update the creation-time generated credential mount.
    target = home / filename
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
def test_restart_does_not_follow_existing_credential_symlink(workspace, exists):
    home, generated, filename = workspace
    outside = home.parent / "outside"
    if exists:
        outside.write_text("synthetic-owner-content\n")
    (home / filename).symlink_to(outside)
    _restart(home, generated)
    assert (home / filename).is_symlink()
    if exists:
        assert outside.read_text() == "synthetic-owner-content\n"
    else:
        assert not outside.exists()


def test_mounted_agent_config_does_not_abort_startup(tmp_path):
    config = tmp_path / "agent-config.yaml"
    config.write_text("agent:\n  name: synthetic\n")
    source = (_ROOT / "docker/base-image/startup.sh").read_text()
    block = source.split("# Start SSH if enabled", 1)[1].split("# Configure MCP servers", 1)[0]
    block = block.replace("/config/agent-config.yaml", str(config))
    # Execute the actual startup block under the script's fail-fast behavior;
    # the image does not ship /app/configure_agent.py.
    subprocess.run(["bash", "-ec", 'ENABLE_SSH=false\n' + block],
                   check=True, capture_output=True, timeout=5)


@pytest.mark.parametrize("transport", ["legacy", "volume"])
@pytest.mark.parametrize("relative", ["service.pem", ".config/gcloud/credentials.db", "credential-files/service.pem"])
@pytest.mark.parametrize("existing", [None, "rotated", "empty", "symlink", "dangling"])
def test_3722_recovery_preserves_mutable_file_credentials(tmp_path, transport, relative, existing):
    home = tmp_path / "home"
    home.mkdir()
    generated = tmp_path / ("volume-seeds" if transport == "volume" else "legacy-seeds")
    seed = generated / relative
    seed.parent.mkdir(parents=True)
    seed.write_text("synthetic-creation-seed")
    seed.chmod(0o600)
    target = home / relative.removeprefix("credential-files/")
    target.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    if existing in ("symlink", "dangling"):
        if existing == "symlink":
            outside.write_text("synthetic-owner-content")
            outside.chmod(0o640)
        target.symlink_to(outside)
    elif existing is not None:
        target.write_text("" if existing == "empty" else "synthetic-rotated-key")
        target.chmod(0o600)
    _restart(home, generated)
    if existing in ("symlink", "dangling"):
        assert target.is_symlink()
        if existing == "symlink":
            assert outside.read_text() == "synthetic-owner-content"
            assert outside.stat().st_mode & 0o777 == 0o640
        else:
            assert not outside.exists()
    else:
        assert target.read_text() == ("synthetic-creation-seed" if existing is None else
                                      "" if existing == "empty" else "synthetic-rotated-key")
        assert target.stat().st_mode & 0o777 == 0o600
