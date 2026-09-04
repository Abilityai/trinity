"""Deploy command: package and deploy a local agent directory."""

import base64
import io
import json
import os
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml

from ..client import TrinityClient, TrinityAPIError
from ..config import get_instance_url, _resolve_profile_name

# Directories/files always excluded from the archive
ALWAYS_EXCLUDE = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".trinity-remote.yaml",
}

# File patterns excluded (matched by name)
EXCLUDE_PATTERNS = {".env", ".env.local", ".env.production"}


def _get_gitignore_patterns(root: Path) -> list[str]:
    """Read .gitignore and return list of patterns."""
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        return []
    patterns = []
    for line in gitignore.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def _is_git_repo(path: Path) -> bool:
    return (path / ".git").exists()


def _git_ls_files(root: Path) -> list[str]:
    """Use git ls-files to get tracked + untracked (non-ignored) files."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return [f for f in result.stdout.strip().splitlines() if f]
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    return []


def _should_exclude(rel_path: str) -> bool:
    """Check if a relative path should be excluded."""
    parts = Path(rel_path).parts
    for part in parts:
        if part in ALWAYS_EXCLUDE:
            return True
    if Path(rel_path).name in EXCLUDE_PATTERNS:
        return True
    return False


MANIFEST_NAME = ".trinity-manifest.json"


def _manifest_entry(full: Path, rel: str) -> dict | None:
    """One #2060 integrity-manifest entry: symlinks carry link_target (the
    default `tar.add` stores them as link members), files carry sha256."""
    import hashlib

    if full.is_symlink():
        return {"path": rel, "link_target": os.readlink(full)}
    if full.is_file():
        return {"path": rel, "sha256": hashlib.sha256(full.read_bytes()).hexdigest()}
    return None


def _create_archive(root: Path) -> bytes:
    """Create a tar.gz archive of the agent directory.

    #2060: an integrity manifest is computed over exactly the member set the
    archive carries and injected in-memory as `.trinity-manifest.json` (the
    user's source directory is never mutated). The backend verifies the
    extracted tree against it and refuses drift.
    """
    buf = io.BytesIO()
    manifest: list[dict] = []

    if _is_git_repo(root):
        # Use git to determine which files to include
        files = _git_ls_files(root)
        if not files:
            # Fallback to walking if git ls-files fails
            files = None
    else:
        files = None

    def add(tar: tarfile.TarFile, full: Path, rel: str) -> None:
        if rel == MANIFEST_NAME or Path(rel).name.startswith("._"):
            return  # backend skips AppleDouble members; a stale committed manifest is superseded
        entry = _manifest_entry(full, rel)
        if entry is None:
            return
        tar.add(str(full), arcname=rel)
        manifest.append(entry)

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if files is not None:
            for rel in files:
                if _should_exclude(rel):
                    continue
                full = root / rel
                if full.is_symlink() or full.is_file():
                    add(tar, full, rel)
        else:
            # Walk directory manually
            for dirpath, dirnames, filenames in os.walk(root):
                # Prune excluded directories in-place; surface symlinked dirs
                # as link members instead of descending into them.
                dirnames[:] = [
                    d for d in dirnames
                    if d not in ALWAYS_EXCLUDE and not d.startswith(".")
                ]
                for d in list(dirnames):
                    full = Path(dirpath) / d
                    if full.is_symlink():
                        rel = full.relative_to(root)
                        if not _should_exclude(str(rel)):
                            add(tar, full, str(rel))
                        dirnames.remove(d)
                for fname in filenames:
                    full = Path(dirpath) / fname
                    rel = full.relative_to(root)
                    if _should_exclude(str(rel)):
                        continue
                    add(tar, full, str(rel))

        # Inject the manifest in-memory — never written to the source dir.
        manifest_bytes = json.dumps(manifest).encode()
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(manifest_bytes)
        tar.addfile(info, io.BytesIO(manifest_bytes))

    return buf.getvalue()


def _load_tracking(root: Path) -> dict | None:
    """Load .trinity-remote.yaml if it exists."""
    tracking_file = root / ".trinity-remote.yaml"
    if not tracking_file.exists():
        return None
    try:
        return yaml.safe_load(tracking_file.read_text())
    except (yaml.YAMLError, OSError):
        return None


def _save_tracking(root: Path, instance_url: str, agent_name: str, profile_name: str):
    """Write .trinity-remote.yaml tracking file."""
    tracking_file = root / ".trinity-remote.yaml"
    data = {
        "instance": instance_url,
        "agent": agent_name,
        "profile": profile_name,
        "deployed_at": datetime.now(timezone.utc).isoformat(),
    }
    tracking_file.write_text(
        "# Auto-generated by trinity deploy — do not edit\n"
        + yaml.dump(data, default_flow_style=False)
    )

    # Add to .gitignore if in a git repo and not already listed
    if _is_git_repo(root):
        gitignore = root / ".gitignore"
        marker = ".trinity-remote.yaml"
        if gitignore.exists():
            content = gitignore.read_text()
            if marker not in content:
                with open(gitignore, "a") as f:
                    f.write(f"\n{marker}\n")
        else:
            gitignore.write_text(f"{marker}\n")


@click.command()
@click.argument("path", default=".", type=click.Path(exists=True, file_okay=False, resolve_path=True))
@click.option("--name", default=None, help="Override agent name (default: directory name or template.yaml name)")
@click.option("--repo", default=None, help="Deploy from a public GitHub repo instead of local files (e.g. user/repo)")
@click.pass_context
def deploy(ctx, path, name, repo):
    """Deploy a local agent directory to Trinity.

    Packages the directory, uploads it, and creates/updates the agent.
    On first deploy, writes .trinity-remote.yaml for tracking.
    Subsequent deploys update the same agent automatically.

    \b
    Examples:
        trinity deploy .              Deploy current directory
        trinity deploy ./my-agent     Deploy a specific directory
        trinity deploy . --name bot   Override agent name
        trinity deploy --repo user/repo  Deploy from GitHub
    """
    profile_name = None
    root = ctx.find_root()
    if root.obj:
        profile_name = root.obj.get("profile")

    resolved_profile = _resolve_profile_name(profile_name)
    instance_url = get_instance_url(profile_name)

    if repo:
        # GitHub-based deploy — use existing create agent flow
        client = TrinityClient(profile=profile_name)
        agent_name = name or repo.split("/")[-1]
        click.echo(f"Creating agent '{agent_name}' from github:{repo}...")
        try:
            result = client.post("/api/agents", json={
                "name": agent_name,
                "template": f"github:{repo}",
            })
            click.echo(f"Agent '{result['name']}' created (status: {result['status']})")
        except TrinityAPIError as e:
            click.echo(f"Deploy failed: {e.detail}", err=True)
            raise SystemExit(1)
        return

    # File-based deploy
    agent_dir = Path(path)

    # Check for tracking file (redeploy)
    tracking = _load_tracking(agent_dir)
    if tracking:
        tracked_instance = tracking.get("instance", "")
        if instance_url and tracked_instance and tracked_instance != instance_url:
            click.echo(
                f"Warning: .trinity-remote.yaml points to {tracked_instance} "
                f"but current profile targets {instance_url}",
                err=True,
            )
            if not click.confirm("Deploy to current profile instance anyway?"):
                raise SystemExit(0)
        # Use tracked agent name for redeploy
        if not name:
            name = tracking.get("agent")

    # Default name from directory
    if not name:
        template_yaml = agent_dir / "template.yaml"
        if template_yaml.exists():
            try:
                tdata = yaml.safe_load(template_yaml.read_text())
                name = tdata.get("name")
            except (yaml.YAMLError, OSError):
                pass
    if not name:
        name = agent_dir.name

    click.echo(f"Packaging '{agent_dir.name}'...")
    archive_bytes = _create_archive(agent_dir)
    size_mb = len(archive_bytes) / (1024 * 1024)
    click.echo(f"Archive: {size_mb:.1f} MB")

    if size_mb > 50:
        click.echo("Error: Archive exceeds 50 MB limit", err=True)
        raise SystemExit(1)

    archive_b64 = base64.b64encode(archive_bytes).decode()

    client = TrinityClient(profile=profile_name)

    action = "Redeploying" if tracking else "Deploying"
    click.echo(f"{action} '{name}'...")

    try:
        result = client.post("/api/agents/deploy-local", json={
            "archive": archive_b64,
            "name": name,
            # #2060: the CLI always embeds a manifest, so it always demands
            # verification — a backend that verified nothing must refuse.
            "require_manifest": True,
        })
    except TrinityAPIError as e:
        click.echo(f"Deploy failed: {e.detail}", err=True)
        raise SystemExit(1)

    if result.get("status") != "success":
        click.echo(f"Deploy failed: {result}", err=True)
        raise SystemExit(1)

    agent = result.get("agent", {})
    versioning = result.get("versioning", {})
    agent_name = agent.get("name", name)

    click.echo(f"Agent '{agent_name}' deployed (status: {agent.get('status', 'unknown')})")

    # #2060 evidence
    if result.get("verified"):
        evidence = (
            f"  Verified: {result.get('files_deployed')}/{result.get('files_expected')} files"
        )
        if result.get("symlinks_deployed"):
            evidence += f", {result['symlinks_deployed']} symlink(s)"
        click.echo(evidence)
    else:
        click.echo("  Warning: deploy content was NOT integrity-verified", err=True)
    if result.get("compatibility_hard_count") is not None:
        click.echo(f"  Compatibility: {result['compatibility_hard_count']} hard finding(s)")
    for w in result.get("warnings") or []:
        click.echo(f"  Warning: {w}", err=True)

    if versioning.get("previous_version"):
        click.echo(f"  Previous version: {versioning['previous_version']} (stopped: {versioning.get('previous_version_stopped', False)})")
    if versioning.get("new_version"):
        click.echo(f"  Version: {versioning['new_version']}")

    # Write tracking file
    _save_tracking(agent_dir, instance_url, versioning.get("base_name", name), resolved_profile)
    click.echo(f"Tracking file written: .trinity-remote.yaml")
