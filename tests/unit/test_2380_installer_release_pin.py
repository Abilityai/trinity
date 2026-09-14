"""The installer's default release tag must track the VERSION file (#2380).

`scripts/deploy/trinity-do-create.sh` hardcodes the release it installs:

    TRINITY_IMAGE_TAG="${TRINITY_IMAGE_TAG:-v0.9.5-rc2}"

That default is what the operator gets, because the published guide tells them to
run the script straight off a tag with no environment set. So on the day `v0.9.5`
is cut, a script fetched FROM the v0.9.5 tag would still `git clone --branch
v0.9.5-rc2` and `docker pull ...:v0.9.5-rc2` unless someone remembers to bump this
one line.

Nothing else catches it. It is not a syntax error, the images for the old tag
still exist and still pull, and the install SUCCEEDS — it simply installs the
previous release candidate. The operator has no way to notice, and neither does
CI: the release checklist's "VERSION match" step compares the VERSION file to the
tag, not this script to either.

The guard ties the two together. It passes today (VERSION `0.9.5-rc2`, default
`v0.9.5-rc2`) and fails the moment VERSION is bumped for the cut, which is exactly
when someone needs to be told. The failure direction is deliberate: the release
stops until the installer is pointed at the release being made.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "deploy" / "trinity-do-create.sh"
_VERSION = _REPO_ROOT / "VERSION"

_DEFAULT_RE = re.compile(
    r'^TRINITY_IMAGE_TAG="\$\{TRINITY_IMAGE_TAG:-(?P<tag>[^}]+)\}"', re.M
)


def test_installer_default_tag_matches_the_version_file() -> None:
    match = _DEFAULT_RE.search(_SCRIPT.read_text())
    assert match, (
        "could not find the TRINITY_IMAGE_TAG default in trinity-do-create.sh — "
        "if the line was reshaped, reshape this guard with it rather than "
        "deleting it"
    )
    default_tag = match.group("tag")
    expected = "v" + _VERSION.read_text().strip()

    assert default_tag == expected, (
        f"trinity-do-create.sh installs {default_tag} but VERSION says "
        f"{_VERSION.read_text().strip()}.\n"
        f"Set the default to {expected}. An operator running the guide's command "
        f"off the {expected} tag would otherwise silently install {default_tag} — "
        f"a successful install of the wrong release, invisible to them and to CI."
    )


def test_the_default_is_a_v_prefixed_tag() -> None:
    """One string feeds both `git clone --branch` and `docker pull`.

    The git tag is `v`-prefixed and the image tag is published under both
    spellings, so only the `v` form works for both. `v0.9.5-rc1` shipped without
    its `v`-prefixed image alias for exactly this reason (#2471) and left no
    value of the tag able to build the marketplace snapshot.
    """
    match = _DEFAULT_RE.search(_SCRIPT.read_text())
    assert match
    assert match.group("tag").startswith("v"), (
        "the default must be the v-prefixed git tag; the bare semver form does "
        "not exist as a git ref and `git clone --branch` would fail"
    )
