"""Regression guard: a PRE-RELEASE tag must never publish `latest` to GHCR.

Bug: `publish-images.yml` gated its `type=raw,value=latest` line on
`startsWith(github.ref, 'refs/tags/v')` — which answers "is this a version tag",
not "is this a version anyone should be running". `refs/tags/v0.9.5-rc1`
satisfies that prefix, so pushing a release candidate would move `latest` and
every unpinned hosted install would pull the RC on its next
`start.sh --hosted`. An RC exists precisely to be built from BEFORE the payload
is blessed (the marketplace snapshot needs GHCR images, #2281/#2471), so it is
the one tag that must not become the default pull.

metadata-action cannot be relied on here. Its documented prerelease handling —
"pre-release will only extend {{version}}" — applies to `type=semver` entries,
which is why `{{major}}.{{minor}}` needs no guard of its own, and has no effect
at all on a `type=raw` value. The exclusion has to be written into the `enable:`
expression, so it is pinned here rather than trusted.

Same family as test_canary_env_prod_parity / test_1871_log_config_parity: a
silent, single-line CI regression whose blast radius is every deployed install.

Lives under tests/unit/ so the CI unit job (`cd tests && pytest unit/`) collects
it — a guard must run where the bug would regress.
"""
from __future__ import annotations

import re
from pathlib import Path

# tests/unit/<this file> → parent=tests/unit, .parent=tests, .parent=repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "publish-images.yml"


def _latest_line() -> str:
    lines = [
        ln.strip()
        for ln in _WORKFLOW.read_text().splitlines()
        if "type=raw,value=latest" in ln and not ln.strip().startswith("#")
    ]
    assert len(lines) == 1, f"expected exactly one `latest` tag rule, found {len(lines)}"
    return lines[0]


def test_latest_excludes_prerelease_tags() -> None:
    """The `latest` rule must exclude a hyphenated (pre-release) tag name.

    The `-` test IS the semver prerelease rule (spec item 9), so this asserts the
    shape rather than a list of rc/beta/alpha spellings that would go stale.
    """
    line = _latest_line()
    assert "!contains(github.ref_name, '-')" in line, (
        "publish-images.yml publishes `latest` for pre-release tags. "
        f"A `v*-rc1` push would walk `latest` backwards for every unpinned "
        f"hosted install. Line was: {line}"
    )


def test_latest_still_gated_on_a_real_tag_push() -> None:
    """The pre-existing guards must survive — the prerelease rule is additive.

    Dropping either of these re-opens #2280's own hazard (a workflow_dispatch
    smoke build becoming what every hosted install pulls).
    """
    line = _latest_line()
    assert "github.event_name == 'push'" in line
    assert "startsWith(github.ref, 'refs/tags/v')" in line


def test_semver_patterns_are_untouched() -> None:
    """`{{version}}` / `v{{version}}` must still publish for a pre-release.

    An RC has to be pullable by its own exact tag — that is the whole point of
    cutting one. Only the mutable `latest` pointer is withheld.
    """
    body = _WORKFLOW.read_text()
    for pattern in ("type=semver,pattern={{version}}", "type=semver,pattern=v{{version}}"):
        assert pattern in body, f"missing {pattern}"
        rule = next(ln for ln in body.splitlines() if pattern in ln)
        assert "ref_name" not in rule, (
            f"the prerelease exclusion must NOT be applied to {pattern} — an RC "
            f"must remain pullable by its exact tag. Line was: {rule.strip()}"
        )


def test_major_minor_is_left_to_metadata_action() -> None:
    """`{{major}}.{{minor}}` carries no hyphen guard, deliberately.

    metadata-action already withholds it for a pre-release. Adding a redundant
    guard here would imply the semver entries need one, which is the confusion
    that produced the bug.
    """
    body = _WORKFLOW.read_text()
    rule = next(ln for ln in body.splitlines() if "{{major}}.{{minor}}" in ln)
    assert "ref_name" not in rule, f"unexpected hyphen guard on major.minor: {rule.strip()}"
