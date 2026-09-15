"""Freshness + correctness guard for the selectable-model catalog (#2086).

`services/model_catalog.py` is the single source of truth for the selectable
Claude-model catalog; `src/frontend/src/constants/modelCatalog.js` is a GENERATED,
checked-in mirror. This guard rides ``backend-unit-test.yml`` (no ``paths:``
filter → every PR, including frontend-only ones), so a consumer edited without
re-running codegen introduces a NEW failing test id → red.

Two independent halves (Codex #1 / Strategy #1):
  * **byte freshness** — the committed JS is byte-identical to ``render_js()``
    (a consumer edited without re-running the generator fails here);
  * **structural validation** — the parsed JS records equal the records derived
    from the Python source (a wrong-but-fresh ``render_js()`` mapping that
    byte-matches its own output but is structurally wrong fails here).

WHAT THIS GUARD DOES NOT CATCH: source-vs-reality staleness. When Anthropic ships
a model and nobody edits ``model_catalog.py``, every list stays consistent and
green while the new model is unselectable everywhere — the exact bug that created
#2086. The control for that is the human PR-checklist / docs step ("edit
``model_catalog.py`` when a model ships"). Centralization cuts the edit surface
(5→1); the checklist cuts the notice burden.

Folds in the retired ``test_1660_model_list_drift.py`` intent, now sourced from
the Python catalog instead of brittle Vue regex:
  * the Claude-5 family (opus-5 / fable-5 / sonnet-5) reaches every list (the
    #1660 bug: presets added to the picker that neither the public-channel
    allow-list nor the admin dropdown followed);
  * **#1080 (KEEP, do not "fix"):** Haiku 4.5 is public-channel-selectable but
    deliberately NOT admin-default-selectable — adding it to the admin dropdown
    would let an admin default the whole fleet to the cheap tier, reversing #1080;
  * the two legacy ids are picker-only (neither public nor admin) — codified,
    not incidental drift;
  * the recommended/default model is always admin-selectable (a fresh install
    must render a non-blank ``<select>``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

_GENERATED_JS = _ROOT / "src" / "frontend" / "src" / "constants" / "modelCatalog.js"
_SOURCE_PY = _BACKEND / "services" / "model_catalog.py"
_SCRIPT = _ROOT / "scripts" / "gen_model_catalog.py"

_JS_PREFIX = "export const MODEL_CATALOG = "


def _catalog():
    """Import deferred so a heavy-import failure isolates to this test, not collection."""
    from services import model_catalog

    return model_catalog


def _expected_records(model_catalog) -> list[dict]:
    return [
        {
            "id": m.id,
            "label": m.label,
            "note": m.note,
            "publicChannel": m.public_channel,
            "adminDefaultSelectable": m.admin_default_selectable,
            "recommended": m.recommended,
            # ent#403 — appended last, like the dataclass fields and the
            # `_JS_KEY_MAP` rows. This dict IS the key-set assertion: the
            # structural compare below is an equality, so a new emitted key that
            # is not listed here turns this file red.
            "workspace": m.workspace,
            "workspaceTier": m.workspace_tier,
        }
        for m in model_catalog.MODEL_CATALOG
    ]


def _parse_committed_array() -> list[dict]:
    """Strip the ``export const MODEL_CATALOG = <json>;`` wrapper and JSON-load it."""
    text = _GENERATED_JS.read_text(encoding="utf-8")
    start = text.index(_JS_PREFIX) + len(_JS_PREFIX)
    end = text.rindex("]") + 1
    return json.loads(text[start:end])


# --- Guard the guard --------------------------------------------------------


def test_source_files_exist():
    assert _SOURCE_PY.is_file(), f"missing catalog source: {_SOURCE_PY}"
    assert _GENERATED_JS.is_file(), f"missing generated mirror: {_GENERATED_JS}"
    assert _SCRIPT.is_file(), f"missing codegen script: {_SCRIPT}"


def test_structural_check_catches_a_planted_mismatch():
    """Validate the anti-stub against the real corpus: a tampered record must
    fail the structural compare (else the compare is vacuous and never guards)."""
    model_catalog = _catalog()
    expected = _expected_records(model_catalog)
    tampered = json.loads(json.dumps(expected))
    tampered[0]["id"] = "claude-opus-999-not-a-model"
    assert tampered != expected


# --- Byte freshness + structural validation (the AC's CI guard) -------------


def test_generated_js_is_byte_fresh():
    model_catalog = _catalog()
    expected = model_catalog.render_js().encode("utf-8")
    actual = _GENERATED_JS.read_bytes()
    assert actual == expected, (
        "src/frontend/src/constants/modelCatalog.js is stale — "
        "run `python scripts/gen_model_catalog.py` and commit the result."
    )


def test_generated_js_matches_source_records_structurally():
    model_catalog = _catalog()
    assert _parse_committed_array() == _expected_records(model_catalog), (
        "the generated JS records do not equal the Python catalog records — "
        "render_js() mapping is wrong (a byte-match alone would not catch this)."
    )


def test_render_js_is_deterministic_and_well_formed():
    model_catalog = _catalog()
    a = model_catalog.render_js()
    assert a == model_catalog.render_js(), "render_js() is not idempotent"
    a.encode("utf-8")  # must be UTF-8 clean
    assert a.endswith("\n") and not a.endswith(
        "\n\n"
    ), "must end in exactly one newline"
    assert "\r" not in a, "must use LF line endings"


# --- opus-5 selectable end-to-end (the headline AC) -------------------------


def test_opus_5_is_present_and_selectable_end_to_end():
    model_catalog = _catalog()
    by_id = {m.id: m for m in model_catalog.MODEL_CATALOG}
    assert "claude-opus-5" in by_id, "claude-opus-5 missing from the catalog"
    entry = by_id["claude-opus-5"]
    assert entry.public_channel and entry.admin_default_selectable
    # 422 → 200: the #894 validation gate must now accept opus-5.
    from services.settings_service import is_valid_public_channel_model

    assert is_valid_public_channel_model(
        "claude-opus-5"
    ), "PUT /api/agents/{name}/public-channel-model would still 422 claude-opus-5"


# --- fable-5.1 selectable end-to-end (#2726, the headline AC) ---------------


def test_fable_5_1_is_present_and_selectable_end_to_end():
    """#2726: the current Fable tier must be selectable everywhere the catalog feeds.

    Undated id (AC 4) — never a date-suffixed variant. The 422->200 assertion is
    the one that actually bites: PUBLIC_CHANNEL_MODELS is a *validation* set, so a
    missing model is rejected by the API, not merely absent from a dropdown.
    """
    model_catalog = _catalog()
    by_id = {m.id: m for m in model_catalog.MODEL_CATALOG}
    assert "claude-fable-5-1" in by_id, "claude-fable-5-1 missing from the catalog"
    entry = by_id["claude-fable-5-1"]
    assert entry.public_channel and entry.admin_default_selectable
    assert not entry.recommended, "AC 7: the platform default does NOT move"
    from services.settings_service import is_valid_public_channel_model

    assert is_valid_public_channel_model(
        "claude-fable-5-1"
    ), "PUT /api/agents/{name}/public-channel-model would still 422 claude-fable-5-1"


def test_at_most_one_latest_marker_per_family_and_fable_is_5_1():
    """#2726 AC 2, as a GENERAL invariant rather than a one-off id check.

    The bug this ticket fixes is precisely "two entries in one tier both claim
    (latest)" — Fable 5 kept the marker after Fable 5.1 shipped. Asserting the
    *rule* catches the same mistake on the next refresh, in any family; the
    single pinned id below is the AC-specific half on top of it.
    """
    import re

    model_catalog = _catalog()
    latest_by_family: dict[str, str] = {}
    for m in model_catalog.MODEL_CATALOG:
        if "(latest)" not in m.note:
            continue
        fam = re.match(r"claude-([a-z]+)-", m.id)
        assert fam, f"unparseable model id: {m.id}"
        family = fam.group(1)
        assert family not in latest_by_family, (
            f"two '(latest)' markers in the {family} tier: "
            f"{latest_by_family[family]} and {m.id} — only the current "
            "generation carries it (#2726)"
        )
        latest_by_family[family] = m.id

    by_id = {m.id: m for m in model_catalog.MODEL_CATALOG}
    assert (
        "(latest)" in by_id["claude-fable-5-1"].note
    ), "AC 2: Claude Fable 5.1 must carry the '(latest)' marker in its tier"
    assert (
        "(latest)" not in by_id["claude-fable-5"].note
    ), "AC 2: Claude Fable 5 is no longer the latest in its tier (#2726)"
    assert latest_by_family.get("fable") == "claude-fable-5-1", (
        "the Fable tier's '(latest)' marker must sit on claude-fable-5-1. This "
        "pin is DELIBERATELY brittle: when the next Fable ships you MUST "
        "consciously edit this line — that is the point, not an inconvenience "
        "(the test_ent243 `_MINIMAL_PREFIXES` idiom)."
    )


# --- Per-flag assertions (independent flags, NOT a subset lattice) ----------


def test_per_model_flags():
    model_catalog = _catalog()
    by_id = {m.id: m for m in model_catalog.MODEL_CATALOG}

    # #1080 (KEEP): Haiku is public-channel-selectable but NOT admin-default.
    haiku = by_id["claude-haiku-4-5-20251001"]
    assert haiku.public_channel and not haiku.admin_default_selectable, (
        "Haiku must stay public-but-not-default (#1080) — do NOT add it to the "
        "admin dropdown; that would let an admin default the fleet to the cheap tier."
    )

    # Legacy picker-only: neither public-channel nor admin-default.
    legacy_ids = ("claude-opus-4-5-20251101", "claude-sonnet-4-5-20250929")
    for legacy in legacy_ids:
        assert not by_id[legacy].public_channel
        assert not by_id[legacy].admin_default_selectable

    # Claude-5 / 5.1 families + prior Opus generation: both flags True (#1660 lists).
    current = (
        "claude-opus-5",
        "claude-fable-5-1",
        "claude-fable-5",
        "claude-sonnet-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
    )
    for model_id in current:
        assert by_id[model_id].public_channel, f"{model_id} must be public-channel"
        assert by_id[
            model_id
        ].admin_default_selectable, f"{model_id} must be admin-default"

    # Guard the guard (#2726): every catalog id must fall in one of the groups
    # above, or a future entry is silently unchecked by this test. Before #2726
    # this named 9 of the catalog's 10 entries — `claude-sonnet-4-6` sat in no
    # group — so an 11th could be added and go entirely unasserted while green.
    from services.settings_service import PLATFORM_DEFAULT_MODEL_VALUE

    checked = {haiku.id, *legacy_ids, *current} | {PLATFORM_DEFAULT_MODEL_VALUE}
    assert checked == set(by_id), (
        f"models not covered by any flag group: {sorted(set(by_id) - checked)} — "
        "add each to the group that states its intended policy"
    )


def test_derived_sets_are_subsets_of_the_picker():
    """Folded from test_1660: every server-accepted / admin-offered model must be
    a picker entry. Holds by construction (the catalog IS the picker) — asserted
    to preserve the retired guard's intent."""
    model_catalog = _catalog()
    picker = {m.id for m in model_catalog.MODEL_CATALOG}
    assert {m.id for m in model_catalog.MODEL_CATALOG if m.public_channel} <= picker
    assert {
        m.id for m in model_catalog.MODEL_CATALOG if m.admin_default_selectable
    } <= picker


# --- recommended pinned to the platform default (#831) ----------------------


def test_recommended_is_exactly_one_and_is_the_platform_default():
    model_catalog = _catalog()
    recommended = [m for m in model_catalog.MODEL_CATALOG if m.recommended]
    assert len(recommended) == 1, "exactly one model must be recommended"
    from services.settings_service import PLATFORM_DEFAULT_MODEL_VALUE

    assert (
        recommended[0].id == PLATFORM_DEFAULT_MODEL_VALUE
    ), "the '(recommended)' hint must stay pinned to PLATFORM_DEFAULT_MODEL_VALUE (#831)"
    # Fresh-install non-blank <select>: the default must itself be selectable.
    assert recommended[0].admin_default_selectable


# --- backend derives its allow-list from the catalog ------------------------


def test_backend_public_channel_set_derives_from_catalog():
    model_catalog = _catalog()
    from services.settings_service import PUBLIC_CHANNEL_MODELS as backend_set

    assert set(backend_set) == {
        m.id for m in model_catalog.MODEL_CATALOG if m.public_channel
    }, "settings_service.PUBLIC_CHANNEL_MODELS drifted from the catalog re-export"


# --- the Workspace subset (ent#403) -----------------------------------------


def test_workspace_models_are_a_subset_of_the_public_channel_allow_list():
    """The Workspace composer must never offer a model the #894 route would 422.

    The Workspace validates its own field against `WORKSPACE_MODELS`, but the
    value it accepts is resolved through the SAME ladder the operator route
    writes (`is_valid_public_channel_model`). A workspace-only id would be taken
    at the composer and refused wherever the two meet — the "two sources
    silently disagree" ent#403's AC 5 exists to kill.

    `model_catalog.py` asserts this at import; asserting it here too is what
    makes the failure legible in CI rather than a collection error somewhere
    unrelated (every module that imports `settings_service` imports this).
    """
    model_catalog = _catalog()

    assert model_catalog.WORKSPACE_MODELS, "the curated Workspace set must not be empty"
    assert model_catalog.WORKSPACE_MODELS <= model_catalog.PUBLIC_CHANNEL_MODELS
    assert model_catalog.WORKSPACE_MODELS == {
        m.id for m in model_catalog.MODEL_CATALOG if m.workspace
    }, "WORKSPACE_MODELS drifted from the `workspace` flag it is derived from"


def test_every_workspace_model_carries_a_plain_language_tier():
    """The option's PRIMARY text is the tier, not the label — a workspace entry
    with an empty tier renders a blank option. Import-time-asserted; pinned here
    so the reason survives."""
    model_catalog = _catalog()

    tiers = [m.workspace_tier for m in model_catalog.MODEL_CATALOG if m.workspace]
    assert all(t.strip() for t in tiers), "a workspace model with no tier renders blank"
    assert len(set(tiers)) == len(tiers), (
        "two options leading with the same words are not a choice — the reason "
        "`workspace_tier` exists instead of reusing `note`"
    )
