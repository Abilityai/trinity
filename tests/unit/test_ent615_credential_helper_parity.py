"""Parity guard for the git credential helper (ent#615, Invariant #5).

The helper is a shell script the base image installs at
``/usr/local/bin/git-credential-trinity``. The backend carries a byte-identical
copy as ``services.git_credential_helper.HELPER_SCRIPT`` so it can push the
helper into a container running an OLDER base image — the remediation path for
the existing fleet. The agent image ships as its own image and structurally
cannot import ``src/backend``, so the policy is copied rather than imported;
that copy is what this file pins.

**A guard that walks only one of the two trees is not a guard** (the ent#314
lesson): that issue's AST scan had an empty allowlist over the whole backend and
still missed six bare ``yaml.safe_load`` calls in the agent server on the same
author-controlled documents, because it never looked there. So this file also
asserts that no THIRD copy of the helper exists in either tree — a second home
that nothing pins is how the next divergence ships.

To keep it green: edit ``docker/base-image/git-credential-trinity.sh`` (the
canonical file) and re-inject it into the backend constant.
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_CANON = _ROOT / "docker/base-image/git-credential-trinity.sh"
_BACKEND_MODULE = _ROOT / "src/backend/services/git_credential_helper.py"

# The two trees a policy leaf may legitimately live in.
_TREES = (_ROOT / "src/backend", _ROOT / "docker/base-image")

# A line only the helper script carries, used to find stray copies.
_FINGERPRINT = "TRINITY_GIT_NO_CREDENTIAL host="


def _helper_constant() -> str:
    from services.git_credential_helper import HELPER_SCRIPT

    return HELPER_SCRIPT


def test_the_two_copies_are_byte_identical():
    assert _CANON.exists(), f"{_CANON} is missing"
    assert _helper_constant() == _CANON.read_text(), (
        "git-credential-trinity.sh drifted between the base image and the "
        "backend's HELPER_SCRIPT constant — re-inject the canonical file."
    )


def test_no_third_copy_in_either_tree():
    """The fingerprint may appear in exactly two files, one per tree."""
    homes = []
    for tree in _TREES:
        for path in tree.rglob("*"):
            if not path.is_file() or path.suffix not in (".sh", ".py"):
                continue
            try:
                text = path.read_text(errors="ignore")
            except OSError:  # pragma: no cover - unreadable file
                continue
            if _FINGERPRINT in text:
                homes.append(path.relative_to(_ROOT).as_posix())
    assert sorted(homes) == [
        "docker/base-image/git-credential-trinity.sh",
        "src/backend/services/git_credential_helper.py",
    ], f"unexpected copies of the credential helper: {sorted(homes)}"


def test_the_registered_name_is_not_the_filename():
    """CRIT-1: git prepends ``git-credential-`` to a non-absolute helper value.

    Registering the filename resolves to
    ``git-credential-git-credential-trinity``, a command that does not exist —
    so the helper never runs, and with token-free URLs that is a silent
    fleet-wide fetch/push outage no source-only CI can see. Proven by execution
    in ``test_ent615_token_free_remotes.py``; pinned as a string here because
    both the Dockerfile and the backend installer must spell it the same way.
    """
    from services.git_credential_helper import HELPER_NAME, install_command

    assert HELPER_NAME == "trinity"
    assert "credential.helper trinity" in install_command()
    assert "credential.helper git-credential-trinity" not in install_command()

    dockerfile = (_ROOT / "docker/base-image/Dockerfile").read_text()
    registrations = re.findall(r"credential\.helper\s+(\S+)", dockerfile)
    assert registrations == ["trinity"], (
        f"the base image registers the helper as {registrations}, not 'trinity'"
    )


def test_the_installer_and_the_dockerfile_agree_on_the_path():
    from services.git_credential_helper import HELPER_PATH

    assert HELPER_PATH == "/usr/local/bin/git-credential-trinity"
    dockerfile = (_ROOT / "docker/base-image/Dockerfile").read_text()
    assert f"./git-credential-trinity.sh {HELPER_PATH}" in dockerfile
