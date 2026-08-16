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
    for legacy in ("claude-opus-4-5-20251101", "claude-sonnet-4-5-20250929"):
        assert not by_id[legacy].public_channel
        assert not by_id[legacy].admin_default_selectable

    # Claude-5 family + prior Opus generation: both flags True (the #1660 lists).
    for current in (
        "claude-opus-5",
        "claude-fable-5",
        "claude-sonnet-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-4-6",
    ):
        assert by_id[current].public_channel, f"{current} must be public-channel"
        assert by_id[
            current
        ].admin_default_selectable, f"{current} must be admin-default"


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
