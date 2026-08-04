"""Bundled templates must DECLARE their catalog intent (#1931).

The issue's last acceptance criterion: *a newly-added bundled directory must
declare its catalog intent rather than defaulting to visible.*

**The RUNTIME default is deliberately unchanged.** `_build_local_template`
coerces through `bool(data.get("hidden", False))`, so an absent key still
lists. Flipping that default would literally satisfy the sentence while
converting "author forgets a key" from a loud, visible wrong-template into a
*silent absence* — the worse failure mode, and one that would also contradict
the #1759/ent#128 posture that a template's own defects cost it its metadata,
never its place. So the declaration is required **here**, at CI time, where it
fails loudly and names every offender.

This file is deliberately dependency-free (no `template_service` import shim):

1. The check reads `template.yaml` off disk and asks whether the `hidden` KEY
   is *present*. `_build_local_template`'s `bool(...)` coercion destroys
   exactly that information — after it, present-and-false is indistinguishable
   from absent — so the service cannot answer this question at all.
2. It keeps `import yaml` out of `test_local_templates_listing.py`, whose
   import block is a live merge-conflict surface this run.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG = REPO_ROOT / "config" / "agent-templates"

# The shipped user-facing catalog. Kept here, next to the intent gate, because
# `"hidden" in data` is satisfied by `hidden: false` and the sibling `dd-`/
# `test-`/`demo-` prefix ban only catches directories that happen to be named
# that way — neither stops a new `acme-crm/` from shipping visible.
EXPECTED_VISIBLE = {"sage", "scout", "scribe"}


def _template_yamls():
    """Every bundled directory that the catalog scan would consider.

    Mirrors `template_service._build_local_template`'s own tolerance, and it is
    not politeness — two of these skips are crashes. `yaml.safe_load("")`
    returns `None` and `"hidden" in None` raises `TypeError`; a list/scalar
    document does the same. `config/agent-templates/` also holds a `README.md`,
    so the walk must be `is_dir()`-filtered.
    """
    for child in sorted(CATALOG.iterdir()):
        if not child.is_dir():
            continue                                   # README.md et al.
        path = child / "template.yaml"
        if not path.exists():
            continue                                   # not a template; the service skips it too
        try:
            data = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue                                   # malformed → already out of the catalog (ent#128)
        if not isinstance(data, dict):
            continue                                   # None / list / scalar → same
        yield child.name, data


def test_every_bundled_template_declares_catalog_intent():
    """`hidden:` is mandatory — `true` for a fixture/demo/platform agent,
    `false` for a starter we stand behind as someone's first agent.

    Guards the *undeclared-but-valid* case only; the malformed cases belong to
    ent#128 / #1759 and are already covered.
    """
    if not CATALOG.exists():
        pytest.skip("config/agent-templates not present in this checkout")

    undeclared = [name for name, data in _template_yamls() if "hidden" not in data]

    assert not undeclared, (
        "bundled template(s) do not declare catalog intent — add `hidden: true` "
        "(fixture / demo fleet / platform agent) or `hidden: false` (a starter "
        f"we stand behind) to template.yaml: {undeclared}"
    )


def test_declared_intent_is_a_real_boolean():
    """`hidden: "true"` is a string and coerces to True by accident; `hidden:`
    with no value is `None` and coerces to False. Either would make the
    declaration above meaningless, so require the author actually wrote a bool.
    """
    if not CATALOG.exists():
        pytest.skip("config/agent-templates not present in this checkout")

    bad = {
        name: data["hidden"]
        for name, data in _template_yamls()
        if "hidden" in data and not isinstance(data["hidden"], bool)
    }
    assert not bad, f"`hidden:` must be a YAML boolean (true/false), got: {bad}"


def test_shipped_visible_catalog_is_pinned():
    """The user-facing catalog is exactly sage/scout/scribe (#1931).

    Deliberately adding a starter? Add its name to `EXPECTED_VISIBLE` —
    **this line IS the review gate.** A template reaching a fresh operator's
    Library as one of their first options is a product decision, not a
    side-effect of adding a directory.
    """
    if not CATALOG.exists():
        pytest.skip("config/agent-templates not present in this checkout")

    visible = {name for name, data in _template_yamls() if not data.get("hidden", False)}

    assert visible == EXPECTED_VISIBLE, (
        "the shipped visible catalog changed. Deliberately adding a starter? "
        "Add its name to EXPECTED_VISIBLE here — this line IS the review gate "
        f"(#1931). Now visible: {sorted(visible)}"
    )
