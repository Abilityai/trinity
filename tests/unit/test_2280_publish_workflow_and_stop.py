"""Static guards for the publish workflow and stop.sh (#2280, review round 2).

Three defects, each of which is silent at authoring time and only visible on a
release cut or on a hosted server:

1. **The anonymous-pull verification cannot run at all.** The step built its
   reference from ``github.repository_owner``, which is literally ``Abilityai``.
   A GHCR repository name must be lowercase and docker rejects the mixed-case
   reference LOCALLY, before any network call (``repository name must be
   lowercase``) — so the step burns its five retries and fails on EVERY publish,
   reporting a perfectly public package as private. It destroys exactly the
   signal it exists to give ("red = flip visibility once") and trains everyone
   to ignore it. The push is unaffected: ``docker/metadata-action`` lowercases
   its ``images:`` input.

2. **A ``workflow_dispatch`` from a tag ref republishes version tags and walks
   ``latest`` backwards.** ``startsWith(github.ref, 'refs/tags/v')`` distinguishes
   tag from branch, not push from dispatch. And ``docker/metadata-action``'s
   default ``flavor: latest=auto`` applies ``latest`` on any semver tag ref by
   itself, so gating only the explicit ``type=raw`` line would be inert.

3. **``stop.sh`` is not hosted-aware and runs the verb start.sh forbids.** A bare
   ``docker compose`` in the checkout loads the DEV file, which does not define
   ``cloudflared`` — so a hosted stack's tunnel keeps serving publicly after the
   script prints "All services stopped".

Pure stdlib + PyYAML: no docker daemon, no backend import, no network.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / ".github" / "workflows" / "publish-images.yml"
_STOP_SH = _ROOT / "scripts" / "deploy" / "stop.sh"


def _workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _steps() -> list[dict]:
    return _workflow()["jobs"]["publish"]["steps"]


def _step_by(key: str, value: str) -> dict:
    for step in _steps():
        if step.get(key) == value:
            return step
    raise AssertionError(f"no step with {key}={value!r} in {_WORKFLOW}")


# --------------------------------------------------------------------------
# 1. The verify step must lowercase the registry reference.
# --------------------------------------------------------------------------


def test_verify_step_does_not_pass_raw_owner_as_an_image_reference() -> None:
    """`github.repository_owner` is `Abilityai`; docker refuses it verbatim."""
    step = _step_by("name", "Verify anonymous pull")
    env = step.get("env", {})

    for name, value in env.items():
        assert "ghcr.io/${{ github.repository_owner }}" not in str(value), (
            f"env {name} carries a mixed-case GHCR reference. docker rejects it "
            "before any network call, so the verification fails on every publish "
            "and reports a public package as private. Lowercase it in the shell "
            "body (`tr '[:upper:]' '[:lower:]'`) or hardcode ghcr.io/abilityai/."
        )


def test_verify_step_lowercases_the_reference_it_builds() -> None:
    body = _step_by("name", "Verify anonymous pull")["run"]
    assert "tr '[:upper:]' '[:lower:]'" in body, (
        "the verify step builds its own reference and must lowercase it — this "
        "org is `Abilityai` and GHCR repository names must be lowercase."
    )
    # And the reference it inspects is the one it built, not a raw env value.
    assert 'IMAGE="ghcr.io/$(' in body


# --------------------------------------------------------------------------
# 2. Only a real `push` of a v* tag may publish mutable/version tags.
# --------------------------------------------------------------------------


def _meta_with() -> dict:
    return _step_by("id", "meta")["with"]


def test_flavor_disables_the_implicit_latest() -> None:
    """`latest=auto` (the default) applies latest on any semver tag ref."""
    flavor = _meta_with().get("flavor", "")
    assert "latest=false" in str(flavor), (
        "without `flavor: latest=false` the action applies `latest` on any "
        "semver tag ref by itself, which makes the explicit enable= gate inert."
    )


def test_every_non_sha_tag_is_gated_on_a_push_event() -> None:
    tags = [t.strip() for t in _meta_with()["tags"].splitlines() if t.strip()]
    assert tags, "no tags configured"

    sha_tags = [t for t in tags if "value=sha-" in t]
    assert len(sha_tags) == 1, (
        "expected exactly one immutable sha tag — it is the only tag a "
        f"workflow_dispatch smoke build may publish. Got: {sha_tags}"
    )

    for tag in tags:
        if tag in sha_tags:
            assert "enable=" not in tag, (
                "the sha tag must publish on every run, including a dispatch — "
                "it is what a smoke build is for."
            )
            continue
        assert "github.event_name == 'push'" in tag, (
            f"tag line is not gated on a push event: {tag!r}. `github.ref` "
            "answers 'is this a tag ref', not 'is this a release' — a "
            "workflow_dispatch started from a tag carries refs/tags/v0.9.0 too, "
            "and dispatching an old tag rebuilds at a new digest, republishes an "
            "immutable version tag, and walks `latest` BACKWARDS."
        )


def test_latest_additionally_requires_a_v_tag_ref() -> None:
    tags = [t.strip() for t in _meta_with()["tags"].splitlines() if t.strip()]
    latest = [t for t in tags if "value=latest" in t]
    assert len(latest) == 1, f"expected exactly one latest tag line, got {latest}"
    assert "startsWith(github.ref, 'refs/tags/v')" in latest[0]


# --------------------------------------------------------------------------
# 3. stop.sh: hosted-aware, and `stop` rather than `down`.
# --------------------------------------------------------------------------


def _stop_sh() -> str:
    return _STOP_SH.read_text(encoding="utf-8")


def test_stop_sh_never_runs_compose_down() -> None:
    body = _stop_sh()
    offenders = [
        line.strip()
        for line in body.splitlines()
        if re.search(r"^\s*docker\s+compose\b.*\bdown\b", line)
    ]
    assert not offenders, (
        "stop.sh runs `docker compose down`, the exact command start.sh's own "
        f"closing summary tells operators NOT to run: {offenders}"
    )
    assert re.search(r"^docker compose .*\bstop\b", body, re.M), (
        "stop.sh must actually stop the stack"
    )


def test_stop_sh_selects_the_hosted_compose_file_from_the_running_stack() -> None:
    body = _stop_sh()
    assert "com.docker.compose.project.config_files" in body, (
        "stop.sh must read compose's own record of which files created the "
        "project — a marker file or a `trinity.db`-at-the-bind-path heuristic "
        "can drift from reality; the label cannot."
    )
    assert "-f docker-compose.hosted.yml" in body, (
        "a hosted stack stopped through the dev file leaves `trinity-cloudflared` "
        "— which the dev file does not define — running and publicly reachable "
        "after 'All services stopped'."
    )
    assert 'docker compose "${COMPOSE_FILES[@]}"' in body, (
        "the selected file list must actually be passed to compose"
    )
