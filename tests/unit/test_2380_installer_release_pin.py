"""The installer must RESOLVE the release it installs, never hardcode one (#2380).

`scripts/deploy/trinity-do-create.sh` used to carry the release as a literal:

    TRINITY_IMAGE_TAG="${TRINITY_IMAGE_TAG:-v0.9.5-rc2}"

and the published guide tells operators to run the script straight off a tag
with no environment set, so that literal was what they got. On the day a release
was cut, a script fetched FROM the new tag still cloned and pulled the old one —
a successful install of the *previous* release, invisible to the operator and to
CI, because nothing is a syntax error and the old images still pull.

The first guard here tied that literal to the VERSION file, so a bump could not
land without someone editing the installer too. It worked exactly once per
release and cost a CI failure each time to deliver a reminder.

The installer now reads `releases/latest` at run time, so there is no literal to
keep in step and no reminder to deliver. These tests hold that property open.

**Why `releases/latest` and not the `latest` image tag.** One string feeds both
`git clone --branch` and `docker pull`. `latest` is a Docker tag and not a git
ref, so it cannot serve the clone — a resolver is the only shape that satisfies
both. And `releases/latest` *excludes* pre-releases, which is the behaviour that
matters: `/release` cuts every RC with `--prerelease` precisely so an RC can
publish images and seed a marketplace snapshot without becoming what operators
install. `publish-images.yml` withholds the `latest` image tag from hyphenated
versions under the same rule.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "deploy" / "trinity-do-create.sh"

# A literal default of any shape: `:-v0.9.5`, `:-0.9.5-rc2`, `:-latest`.
_LITERAL_DEFAULT_RE = re.compile(
    r'^TRINITY_IMAGE_TAG="\$\{TRINITY_IMAGE_TAG:-(?P<tag>[^}]*[^}\s])\}"', re.M
)


def _script() -> str:
    return _SCRIPT.read_text()


def test_the_installer_hardcodes_no_release_tag() -> None:
    """The regression this file exists for, asserted in its absence."""
    match = _LITERAL_DEFAULT_RE.search(_script())
    assert match is None, (
        f"trinity-do-create.sh hardcodes the release {match.group('tag')!r} again.\n"
        "An operator running the guide's command off a newer tag would silently "
        "install that one instead — a successful install of the wrong release.\n"
        "Resolve it from https://api.github.com/repos/abilityai/trinity/releases/latest "
        "instead, as `resolve_latest_release_tag` does."
        if match
        else ""
    )


def test_no_release_version_literal_survives_anywhere_in_the_installer() -> None:
    """A pin moved rather than removed is the same defect one line lower.

    **Indentation is the discriminator**, and deliberately so. Every executable
    assignment in this script sits at column 0; the only places a concrete
    version legitimately appears are indented — inside the `fail` message that
    shows an operator how to name a release, and in comments. A textual scan
    cannot parse shell, so it keys on the one property that actually separates
    the two, rather than pretending to understand quoting. Un-indent a real
    assignment and it is caught; the help text stays readable.
    """
    offenders = [
        line
        for line in _script().splitlines()
        if re.match(r"^(export\s+)?[A-Z_][A-Z0-9_]*=\s*[\"']?v?\d+\.\d+\.\d+", line)
    ]
    assert offenders == [], (
        "a concrete release version is assigned in trinity-do-create.sh:\n  "
        + "\n  ".join(offenders)
        + "\nThe installed release is resolved at run time; nothing here should name one."
    )


def test_the_resolver_is_present_and_reads_releases_latest() -> None:
    body = _script()
    assert "resolve_latest_release_tag()" in body, (
        "the resolver is gone — if it was renamed, rename it here too rather "
        "than deleting this guard"
    )
    assert "releases/latest" in body, (
        "the resolver must read `releases/latest`, which EXCLUDES pre-releases. "
        "`releases` (the plural list) would hand operators the newest RC, which "
        "is the one thing RCs are cut to avoid."
    )


def test_resolution_failure_is_fatal_rather_than_falling_back() -> None:
    """A stale fallback would reintroduce the original defect, quietly.

    Failing closed costs an operator one clear message naming the override.
    Failing open costs them an install of an unknown-age release.
    """
    body = _script()
    assert re.search(
        r'\[ -n "\$\{TRINITY_IMAGE_TAG:-\}" \] \|\| fail', body
    ), "an unresolved tag must abort the install, not fall through to a default"
    assert "TRINITY_IMAGE_TAG=v" in body, (
        "the failure message must show the operator how to name a release "
        "explicitly, or a rate-limited API leaves them with no way forward"
    )
