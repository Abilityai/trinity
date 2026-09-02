"""Regression guard: every file the packer bundle installs must be tracked in git.

Bug (#2281 review): `packer/digitalocean/files/var/lib/cloud/scripts/per-instance/
001-trinity` — the cloud-init boot hook, i.e. the one file that makes a droplet do
anything at all — was written locally and silently never committed. `.gitignore`
carries the bare setuptools patterns `var/` and `lib/`, which git applies at every
depth, so `git add packer/` skipped it and `git status` stayed clean. The bundle
mirrors a target filesystem, so this collision is structural rather than unlucky:
a future `files/usr/lib/...` would go the same way.

Nothing that ran on the PR could see it. `packer validate` never reads the payload
tree, `shellcheck` only sees the files it is handed, and `packer build` — the one
step that would have died at `install: cannot stat` — is deliberately deferred to
the first release with GHCR images, i.e. to the vendor-submission moment.

So the guard is repo-side and runs in the existing unit job: for each
`install ... /tmp/trinity-files/<path>` in the provision script, assert the
corresponding `packer/digitalocean/files/<path>` is tracked. `git ls-files` rather
than `os.path.exists`, so the check fails on the developer's own machine where the
file is present-but-untracked — the state in which the bug was actually written.
Mode is asserted too: a boot hook installed 0755 from a 0644 source is a silently
inert droplet.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

# tests/unit/<this file> → parent=tests/unit, .parent=tests, .parent=repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_PROVISION = _REPO_ROOT / "packer" / "digitalocean" / "scripts" / "01-provision.sh"
_BUNDLE_ROOT = _REPO_ROOT / "packer" / "digitalocean" / "files"

# `install -D -m 0755 /tmp/trinity-files/<src> \` — the packer `file` provisioner
# uploads `files/` to `/tmp/trinity-files`, so <src> is a path inside the bundle.
_INSTALL_RE = re.compile(r"/tmp/trinity-files/(\S+)")


def _bundle_sources() -> list[str]:
    text = _PROVISION.read_text()
    return sorted({m.group(1) for m in _INSTALL_RE.finditer(text)})


def _tracked_bundle_files() -> dict[str, str]:
    """Map bundle-relative path → git mode, for what git actually has."""
    out = subprocess.run(
        ["git", "ls-files", "-s", "--", "packer/digitalocean/files"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    tracked: dict[str, str] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        meta, path = line.split("\t", 1)
        mode = meta.split()[0]
        tracked[path[len("packer/digitalocean/files/") :]] = mode
    return tracked


def test_provision_script_is_present():
    assert _PROVISION.is_file(), f"missing {_PROVISION}"


def test_every_installed_source_is_tracked():
    sources = _bundle_sources()
    assert sources, "no /tmp/trinity-files/... install sources found — did the script move?"

    tracked = _tracked_bundle_files()
    missing = [s for s in sources if s not in tracked]
    assert not missing, (
        "01-provision.sh installs bundle files that git does not track: "
        f"{missing}. They may exist locally and be swallowed by a bare .gitignore "
        "pattern (`var/`, `lib/`, `parts/`, …) — re-include the path in .gitignore "
        "and commit the file, or `packer build` dies at `install: cannot stat`."
    )


@pytest.mark.parametrize("source", _bundle_sources())
def test_installed_source_is_executable(source: str):
    # Every current install site uses `-m 0755`; a 0644 source would still install
    # 0755, but the intent should be visible in the tree the reviewer reads.
    mode = _tracked_bundle_files().get(source)
    assert mode == "100755", f"{source} is tracked as {mode}, expected 100755"
