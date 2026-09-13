"""ent#454 — the teardown UI's non-negotiables, asserted at the source level.

`vitest.config.js` pins `environment: 'node'` with no component mounting, so
`tests/unit/systemsTeardown.spec.js` can cover the STORE but nothing in a
`.vue` template. These are the template-level facts that would be a silent
regression, and a source assertion is the only runnable guard for them here (a
full render is a `ui`-labeled e2e follow-up — the `test_1577_proactive_toggle_
guard.py` shape and the same reasoning).

Each assertion below is here because losing it produces a WORKING-LOOKING UI
that does the wrong thing:

  * a Remove button not gated on all four conditions is a button that deletes a
    fleet on a stale preview, or on a removal set the user never acknowledged;
  * a preview without the `prefix` badge hides the one signal that says "this
    agent may belong to a different system" — the over-capture escape hatch the
    whole design leans on;
  * an ungated panel offers a verb whose backend 404s on an OSS build;
  * tone switched on an HTTP code instead of `status` mis-reports `partial`
    (200) and `failed` (500 with the report as the body).
"""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_SYSTEMS = _ROOT / "src" / "frontend" / "src" / "components" / "systems"
_PANEL = _SYSTEMS / "SystemTeardownPanel.vue"
_PREVIEW = _SYSTEMS / "TeardownPreview.vue"
_RESULT = _SYSTEMS / "TeardownResult.vue"
_MANIFEST_PREVIEW = _SYSTEMS / "ManifestPreview.vue"
_DEPLOY_RESULT = _SYSTEMS / "DeployResult.vue"
_STORE = _ROOT / "src" / "frontend" / "src" / "stores" / "systems.js"
_LIBRARY = _ROOT / "src" / "frontend" / "src" / "views" / "Library.vue"

_NEW_FILES = (_PANEL, _PREVIEW, _RESULT)


def _src(path: Path) -> str:
    return path.read_text()


def _code_only(src: str) -> str:
    """A `.vue` source with HTML and JS comments stripped.

    Necessary, not fastidious: the comments in these files quote the very
    things some assertions forbid — an issue reference like `#454` looks like a
    hex colour, and the H-005 note literally says "never v-html". A naive
    substring check matches its own explanation and fails on a correct file,
    which is the trap the OSS `_code_only` in `test_2373_system_endpoints.py`
    exists for and the one the ent#155 spec hit with `voiceConvLive`. Both of
    these fired here on the first run.
    """
    src = re.sub(r"<!--[\s\S]*?-->", "", src)
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    src = re.sub(r"(?m)^\s*//.*$", "", src)
    src = re.sub(r"(?m)^\s*\*.*$", "", src)
    return src


# ---------------------------------------------------------------------------
# The destructive button's gates
# ---------------------------------------------------------------------------

def test_remove_is_gated_on_all_four_conditions():
    """Preview-is-current AND verified membership AND acknowledged AND a
    non-empty selection. Four, because each blocks a different bad outcome and
    none of the others covers it."""
    src = _src(_PANEL)
    match = re.search(r"const canRemove = computed\(([\s\S]*?)\n\)", src)
    assert match, "canRemove is no longer a single reviewable expression"
    gate = match.group(1)
    for condition in (
        "teardownPreviewIsCurrent",   # not a stale removal set
        "teardownMembershipUnverified",  # not an unverifiable one
        "acknowledged",               # the consequence was restated and affirmed
        "checked.value.length",       # the server 400s an empty confirmed set
    ):
        assert condition in gate, f"canRemove lost its {condition} gate"


def test_the_remove_button_is_the_danger_variant_and_a_primitive():
    """Destructive verbs are named and styled as such (principle 19), through
    the primitive — identical pixels from a hand-rolled class string is still a
    defect (the contract's primitives rule)."""
    src = _src(_PANEL)
    assert re.search(
        r'<BaseButton\s+variant="danger"[\s\S]{0,400}?data-testid="teardown-execute"', src
    ), "the execute control must be a BaseButton with variant=danger"
    # And it says what it will do, with the count, not just "Remove".
    assert "Remove {{ checked.length }} agent" in src


def test_a_disabled_remove_says_why():
    """A dead button with no reason is the same defect as a dead button."""
    src = _src(_PANEL)
    assert "blockedReason" in src
    assert 'data-testid="teardown-blocked"' in src


# ---------------------------------------------------------------------------
# The preview is a checklist, and the badge is the escape hatch
# ---------------------------------------------------------------------------

def test_the_preview_renders_a_per_member_checkbox():
    """AC: a per-agent opt-out. A read-only list would make `evidence: prefix`
    informational rather than actionable."""
    src = _src(_PREVIEW)
    assert 'type="checkbox"' in src
    assert 'data-testid="`member-${m.name}`"'.replace("'", '"') in src or (
        "data-testid" in src and "member-" in src
    ), "each member row needs an addressable checkbox"
    assert "update:checked" in src, "the confirmed set must reach the parent"


def test_prefix_evidence_is_badged_and_explained():
    """The over-capture signal. Without it the operator cannot tell a tagged
    member from one matched by name alone, which is the only distinction that
    stops a teardown of `acme` removing `acme-extra`'s agents."""
    src = _src(_PREVIEW)
    assert "m.evidence === 'prefix'" in src
    assert "matched by name only" in src
    # Shape/word, not hue alone (principle 24) — a BaseBadge carries text.
    assert "<BaseBadge" in src


def test_ephemeral_members_are_marked_as_unrecoverable_before_confirming():
    """The recovery promise must not be made where it does not hold."""
    src = _src(_PREVIEW)
    assert "m.is_ephemeral" in src
    assert "no recovery" in src.lower()


def test_an_unverified_read_is_its_own_panel_saying_nothing_was_removed():
    src = _src(_PREVIEW)
    assert "membership_verified === false" in src
    assert "nothing has been removed" in src.lower()


def test_protected_agents_are_listed_rather_than_silently_dropped():
    """`excluded` carries the platform agents a `trinity-` prefix captured. The
    server never removes them; the UI must not leave the operator wondering
    where they went."""
    src = _src(_PREVIEW)
    assert "preview.excluded" in src


def test_the_ack_reuses_the_ent126_contract():
    """One pattern for "restate the consequence and affirm it", not two."""
    preview = _src(_PREVIEW)
    manifest = _src(_MANIFEST_PREVIEW)
    for src in (preview, manifest):
        assert 'data-testid="ack-checkbox"' in src
        assert "update:acknowledged" in src


def test_the_member_list_is_bounded():
    """A fleet is unbounded data: internal scroll + a stated total, never a page
    that grows without limit (principle 28)."""
    src = _src(_PREVIEW)
    assert "overflow-y-auto" in src
    assert re.search(r"max-h-\d+", src), "the member list needs a bounded viewport"
    assert "of {{ members.length }} selected" in src


# ---------------------------------------------------------------------------
# Honest reporting
# ---------------------------------------------------------------------------

def test_the_result_switches_on_status_not_the_http_code():
    """`partial` is 200 and `failed` is 500-with-the-report. Keyed on the code,
    the UI reports the two backwards."""
    src = _src(_RESULT)
    assert "TONES[props.result.status]" in src
    assert "UNKNOWN_TONE" in src, "an unrecognised status must render something honest"
    for status in ("torn_down", "partial", "failed"):
        assert f"  {status}: {{" in src, f"no tone for status={status}"
    assert "status_code" not in re.search(
        r"const tone = computed[\s\S]*?\n", src
    ).group(0)


def test_skipped_failed_and_aborted_render_as_three_different_things():
    """An operator acts on them differently: a refusal is not a breakage, and
    an un-attempted member is neither."""
    src = _src(_RESULT)
    assert "'skipped'" in src and "'failed'" in src and "'aborted'" in src
    assert "Left in place" in src, "skipped must not be rendered under Failed"
    assert "Not attempted" in src, "aborted must say it was never tried"
    # Every skip reason the server can send has copy — an operator reading a
    # bare enum learns nothing.
    for reason in (
        "system_agent",
        "not_authorized",
        "not_found",
        "not_confirmed",
        "not_a_member",
    ):
        assert reason in src, f"no operator copy for skip reason {reason}"


def test_a_discarded_ghost_is_not_reported_as_recoverable():
    src = _src(_RESULT)
    assert "'discarded'" in src
    assert "not recoverable" in src


def test_the_recovery_text_is_rendered_verbatim_from_the_server():
    """AC 8. The server owns the statement; the UI must not paraphrase a
    retention window it does not read."""
    src = _src(_RESULT)
    assert "result.recovery" in src


def test_a_timeout_offers_no_retry():
    """Teardown is synchronous and serial: cancelling the request does not
    cancel the server, so "try again" would be an offer to delete more."""
    src = _src(_PANEL)
    assert "teardownOutcomeUnknown" in src
    assert "Do not simply try again" in src


# ---------------------------------------------------------------------------
# The entitlement gate
# ---------------------------------------------------------------------------

def test_the_panel_renders_nothing_without_the_entitlement():
    """An OSS build has no teardown route: a visible control would be a dead
    one, and its own 404 would be the first the user heard of it."""
    src = _src(_PANEL)
    assert "TEARDOWN_FEATURE_ID" in src
    assert "enterpriseFeatures.includes(TEARDOWN_FEATURE_ID)" in src
    assert re.search(r'<BaseCard v-if="entitled"', src), (
        "the gate must wrap the whole panel, not an inner element"
    )


def test_the_feature_id_has_exactly_one_definition():
    """The id is compared in four places (panel, deploy result, manifest
    preview, store). Four string literals is four chances to typo one into a
    permanently-hidden surface."""
    store = _src(_STORE)
    assert "export const TEARDOWN_FEATURE_ID = 'system_teardown'" in store
    for path in (_PANEL, _DEPLOY_RESULT, _MANIFEST_PREVIEW):
        src = _src(path)
        assert "TEARDOWN_FEATURE_ID" in src, f"{path.name} does not import the id"
        assert "'system_teardown'" not in src, (
            f"{path.name} restates the feature id instead of importing it"
        )


def test_the_install_warning_tracks_the_capability_not_the_build():
    """"There is no un-deploy" stays exactly true where the module is absent.
    Retiring it everywhere because one edition gained the verb would be the
    dishonest fix."""
    src = _src(_MANIFEST_PREVIEW)
    assert "teardownAvailable" in src
    assert "There is no un-deploy" in src, (
        "the OSS-truthful arm must survive — it is still true on that build"
    )
    assert re.search(r'v-if="teardownAvailable"[\s\S]{0,400}?v-else', src), (
        "the warning needs both arms, chosen by the entitlement"
    )


def test_the_deploy_result_offers_the_inverse_only_when_it_exists():
    """AC 7: the deploy result is an entry point into teardown."""
    src = _src(_DEPLOY_RESULT)
    assert 'data-testid="remove-system"' in src
    assert "remove-system" in src
    assert 'v-if="teardownAvailable"' in src
    assert "'remove-system', result.system_name" in src, (
        "the handoff must carry the system name, or the panel opens empty"
    )


def test_library_mounts_the_panel_and_wires_the_handoff():
    src = _src(_LIBRARY)
    assert "SystemTeardownPanel" in src
    assert 'initial-name="teardownTarget"' in src
    assert '@remove-system="removeSystem"' in src


# ---------------------------------------------------------------------------
# Design-system contract facts the ratchets cannot see
# ---------------------------------------------------------------------------

def test_no_hand_rolled_lookalikes_for_the_primitives_that_exist():
    """Buttons and inputs go through Base*. A raw `<button>` in a new file is
    the lookalike the contract forbids — with one carved-out exception in the
    existing DeployResult, which is pre-existing and not this change's to move.
    """
    for path in _NEW_FILES:
        src = _code_only(_src(path))
        assert "<button" not in src, (
            f"{path.name} hand-rolls a button; compose BaseButton"
        )
        assert "<input type=\"text\"" not in src, (
            f"{path.name} hand-rolls a text input; compose BaseInput"
        )


def test_both_themes_are_spelled_for_every_surface_color():
    """No per-theme hardcoded colour, and no light-only surface: every file that
    paints a surface paints it in both themes."""
    for path in _NEW_FILES:
        src = _code_only(_src(path))
        # Every file paints something; every file must paint it twice.
        assert "dark:" in src, f"{path.name} has no dark-theme styling at all"
        # No raw hex anywhere. The ratchet covers palette CLASSES; this covers
        # the other half of the token rule.
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", src), (
            f"{path.name} contains a hardcoded colour"
        )


def test_no_bare_loading_gate_on_a_data_surface():
    """Loading means "no data yet", never "fetch in flight" (#1927). The
    teardown panel gates on the PRESENCE of a preview/result, never on the
    in-flight flag."""
    for path in _NEW_FILES:
        src = _code_only(_src(path))
        assert not re.search(r'v-if="(store\.)?is(Previewing|TearingDown)\w*"', src), (
            f"{path.name} gates a surface on an in-flight flag"
        )
    panel = _src(_PANEL)
    # The in-flight flags are allowed — and expected — on the BUTTON, which is
    # the one sanctioned in-flight indicator.
    assert ':loading="store.isTearingDown"' in panel
    assert ':loading="store.isPreviewingTeardown"' in panel
    assert 'loading-label="Removing…"' in panel


def test_an_action_failure_has_a_user_visible_home():
    """No console.error-only catch, no alert() (principle 18)."""
    panel = _src(_PANEL)
    assert "<InlineError" in panel
    assert "store.teardownError" in panel
    for path in _NEW_FILES:
        src = _code_only(_src(path))
        assert "alert(" not in src, f"{path.name} uses alert()"
        assert "console.error" not in src, f"{path.name} logs a failure with no UI home"


def test_server_text_is_never_v_html():
    """Failure reasons are credential-sanitized server-side but NOT
    HTML-sanitized (H-005)."""
    for path in _NEW_FILES:
        src = _code_only(_src(path))
        assert "v-html" not in src, f"{path.name} renders server text as HTML"
