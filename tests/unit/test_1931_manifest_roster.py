"""Bundled manifests validate, and their resolved agent names satisfy the
rosters their own templates hardcode (#1931).

Two assertions nothing else covers. What *is* already covered, so this file
does not duplicate it:

| already covered | where |
|---|---|
| `parse_manifest` over every `config/manifests/*.yaml` | `test_1884_manifest_yaml_hardening.py::test_every_shipped_manifest_still_parses` |
| `local:` → `template.yaml` + `CLAUDE.md` exist; declared `commands:` ship files | `test_ent124_default_system_seed.py` (widened to the glob by #1931) |

So the genuinely new coverage here is `validate_manifest` (parse != validate)
and the roster consistency check below.

Enumeration is by **glob**, never a hard-coded list — otherwise the fifth
bundled manifest ships unguarded.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_DIR = REPO_ROOT / "config" / "manifests"
CATALOG = REPO_ROOT / "config" / "agent-templates"

# Same import shim as test_1884_manifest_yaml_hardening.py — the established
# precedent for reaching services.system_service from a unit test.
_BACKEND_STR = str(REPO_ROOT / "src" / "backend")
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


def _svc():
    try:
        from services import system_service
    except ImportError:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")
    return system_service


def _manifest_paths():
    paths = sorted(MANIFEST_DIR.glob("*.yaml"))
    assert paths, "expected bundled manifests to exist"
    return paths


def _manifest_ids():
    return [p.name for p in _manifest_paths()]


def _prompt_files(template_dir: Path):
    """Every file a template ships that the runtime feeds the model as
    instructions — the surfaces where a hardcoded collaborator name can hide.

    Deduped and sorted so a failure message is stable. A missing file is simply
    absent from the walk; `test_ent124_default_system_seed.py` owns the
    "CLAUDE.md must exist" half.
    """
    candidates = [
        template_dir / "CLAUDE.md",
        *template_dir.glob(".claude/commands/*.md"),
        *template_dir.glob(".claude/skills/**/*.md"),
    ]
    return sorted({p for p in candidates if p.is_file()})


@pytest.mark.parametrize("path", _manifest_paths(), ids=_manifest_ids())
def test_every_bundled_manifest_validates(path: Path):
    """learnings 2026-07-06: *validate a config with the SAME parser the
    executor uses, or bad configs pass then fail silently.*

    `parse_manifest` (already globbed elsewhere) only proves the YAML shape.
    `validate_manifest` is what a deploy actually runs: it raises on a bad
    system/agent name, a template missing its `github:`/`local:` prefix, and —
    the one that matters for #1931 — an unknown agent on either side of a
    `permissions.explicit` edge. A typo in the nine dd-lead edges is a
    `ValueError` here instead of a 500 in front of an audience.
    """
    svc = _svc()
    manifest = svc.parse_manifest(path.read_text(encoding="utf-8"))
    warnings = svc.validate_manifest(manifest)   # raises on invalid
    assert warnings == [], f"{path.name}: {warnings}"


def test_manifest_resolved_names_satisfy_hardcoded_rosters():
    """A template's prompt text may name its collaborators literally — in
    `CLAUDE.md`, in a slash command, in a skill. If the manifest that deploys it
    resolves to different names, the fleet deploys eleven healthy containers and
    cannot talk to itself — green tests, broken demo (#1931).

    Deployed name == f"{manifest.name}-{short_name}"
    (`system_service.resolve_agent_names`).

    **Detection shape, and why it is this one.** The naive check — "find tokens
    starting with this manifest's own name" — goes *vacuously green* on exactly
    the regression it exists to catch: rename the system to `vc-demo` and no
    token starts with `vc-demo-`, so nothing is scanned and nothing fails. So
    the scan is anchored on the **short name** instead: any `<prefix>-<short>`
    token in a deployed template's prompt text must equal that manifest's own
    resolved name for `<short>`. Renaming the system leaves the old literal in
    the prompt, the token still ends in a live short name, and the assertion
    fires.

    The token pattern requires a hyphen-joined prefix and refuses a trailing
    hyphen or a `<placeholder>` tail, so prose like "the `vc-due-diligence-dd-`
    prefix" or "`vc-due-diligence-dd-<x>`" is ignored rather than reported as a
    phantom agent.

    **Scanned surface = every file the runtime feeds the model as instructions**,
    not just `CLAUDE.md`: `.claude/commands/*.md` and `.claude/skills/**/*.md`
    too. A literal roster is just as load-bearing — and just as silently
    breakable — in a slash command as in `CLAUDE.md`, and the sibling ent#239
    check in `test_ent124_default_system_seed.py` already treats
    `.claude/commands/` as a first-class shipped surface. Restricting the walk
    to `CLAUDE.md` left four real literals unguarded in the bundled corpus
    (`sage/.claude/commands/request-research.md` -> `acme-scout`;
    `demo-analyst/.claude/commands/{briefing,request-research}.md` ->
    `research-network-researcher`). Verified over the whole bundled corpus: 28
    matching tokens across 4 manifests, zero offenders, zero phantoms.
    """
    svc = _svc()
    offenders: list[str] = []
    scanned = 0

    for path in _manifest_paths():
        manifest = svc.parse_manifest(path.read_text(encoding="utf-8"))
        shorts = list(manifest.agents)
        patterns = {
            short: re.compile(
                r"(?<![A-Za-z0-9_-])[a-z0-9][a-z0-9-]*-" + re.escape(short) + r"(?![A-Za-z0-9_-])"
            )
            for short in shorts
        }

        for cfg in manifest.agents.values():
            if not cfg.template.startswith("local:"):
                continue
            template_dir = CATALOG / cfg.template.split(":", 1)[1]
            if not template_dir.is_dir():
                continue                     # ent124's test owns the "must exist" half

            for prompt_file in _prompt_files(template_dir):
                text = prompt_file.read_text(encoding="utf-8")

                for short, pattern in patterns.items():
                    expected = f"{manifest.name}-{short}"
                    for match in pattern.finditer(text):
                        scanned += 1
                        token = match.group(0)
                        if token != expected:
                            offenders.append(
                                f"{path.name}: {prompt_file.relative_to(REPO_ROOT)} names "
                                f"'{token}', but this manifest deploys short name "
                                f"'{short}' as '{expected}' — that agent will not exist"
                            )

    assert not offenders, (
        "hardcoded agent roster does not match the manifest that deploys it:\n  "
        + "\n  ".join(offenders)
    )
    # The corpus is not empty — if this ever hits zero the test has gone
    # vacuous (a renamed prompt-file convention, a moved catalog) and is no
    # longer guarding anything.
    assert scanned > 0, "no cross-agent name references found — the scan went vacuous"
