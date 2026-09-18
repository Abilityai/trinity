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


def _prose(src: str) -> str:
    """Comment-free source with template line-wrapping collapsed.

    Two traps, one helper. `_code_only` handles the first — these files'
    comments quote the very strings some assertions forbid — and the
    short-selection assertion below matched its own JSDoc line on its first
    run, which is that trap landing a third time. Collapsing whitespace is the
    second half: where a sentence breaks is chosen by the editor's margin, so
    an assertion anchored on it fails the next time someone rewraps a
    paragraph whose WORDS did not change.
    """
    return re.sub(r"\s+", " ", _code_only(src))


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


def test_an_unconfirmable_member_is_not_pre_ticked():
    """The arriving selection is the store's, and the store excludes `prefix`.

    The panel used to build the default itself — `preview.members.map(m =>
    m.name)` — which ticked every member regardless of evidence. The 503
    refusal does not cover that: a `prefix` member also appears in the HEALTHY
    state (tags read fine, one agent simply has no tag row), where
    `membership_verified` is true and Remove is ENABLED. Opting OUT of a delete
    is the wrong direction for the default, and it is the exact ranking #2373
    gets right for `restart` and wrong for a destructive verb.

    The rule itself is executed by `systemsTeardown.spec.js`; what only a source
    assertion can see is that the PANEL still defers to it instead of growing a
    second copy.
    """
    src = _src(_PANEL)
    match = re.search(r"watch\(\s*\(\) => store\.teardownPreview,([\s\S]*?)\n\)", src)
    assert match, "the preview watcher is no longer a single reviewable block"
    body = match.group(1)
    assert "store.teardownDefaultSelection" in body, (
        "the panel must take its default selection from the store, where the "
        "evidence rule is testable"
    )
    assert not re.search(r"members\s*\|\|\s*\[\]\s*\)\.map", body), (
        "the panel is building its own all-members default again"
    )


def test_the_default_is_applied_on_MOUNT_too_not_only_on_a_fresh_preview():
    """The store outlives the component, so the watcher must be `immediate`.

    `Library.vue` mounts the systems section lazily (`v-if="… visited.has(
    'systems')"`) and nothing resets teardown state on unmount, so leaving the
    page and coming back remounts this panel onto a preview that is STILL
    current. A non-immediate watcher never fires for it: the removal set
    rendered with every box empty and "Select at least one agent", making this
    component's own stated default untrue on the second visit.

    Fail-safe in both directions — which is exactly why it survived: nothing
    breaks, the panel is just wrong and slightly insulting to re-tick by hand.
    Caught by driving the real panel in a browser, not by any assertion.
    """
    src = _src(_PANEL)
    match = re.search(
        r"watch\(\s*\(\) => store\.teardownPreview,([\s\S]*?)\n\)", src
    )
    assert match, "the preview watcher is no longer a single reviewable block"
    assert "immediate: true" in match.group(1), (
        "the preview watcher must be immediate, or a remount shows a current "
        "preview with an empty selection"
    )


def test_the_short_selection_is_explained_where_the_list_is():
    """An unticked row with no explanation reads as a miscount, and an operator
    who re-ticks it to "fix" the count has undone the safeguard by hand."""
    body = _code_only(_src(_PREVIEW))
    assert 'data-testid="teardown-unconfirmed-note"' in body
    assert "unticked. Tick one only if you know it belongs to this system." in _prose(
        _src(_PREVIEW)
    ), "the note must say the rows start unticked AND what to do about it"


def test_the_prefix_copy_asks_for_an_opt_in_not_an_opt_out():
    """The per-member line has to agree with the default it sits next to.
    "uncheck it if so" describes a box that arrives ticked; it now does not."""
    body = _prose(_src(_PREVIEW))
    assert "uncheck it" not in body.lower(), (
        "the prefix member arrives UNTICKED — copy telling the user to uncheck "
        "it describes the old default"
    )
    assert "tick it only if it belongs to this one." in body, (
        "the prefix member needs an explicit opt-in instruction"
    )


def test_ephemeral_members_are_marked_as_unrecoverable_before_confirming():
    """The recovery promise must not be made where it does not hold."""
    src = _src(_PREVIEW)
    assert "m.is_ephemeral" in src
    assert "no recovery" in src.lower()


def test_an_unverified_read_is_its_own_panel_saying_nothing_was_removed():
    # Via `_prose`: the phrase sits inside a `<strong>` that a re-wrap split
    # across two lines, which failed this assertion without changing a word of
    # what the operator reads. Copy assertions anchor on words, not on where
    # the editor's margin happened to fall.
    assert "membership_verified === false" in _src(_PREVIEW)
    assert "nothing has been removed" in _prose(_src(_PREVIEW)).lower()


def test_the_refusal_does_not_offer_transient_advice_for_a_standing_fault():
    """"Try again in a moment" is advice for a blip.

    The banner renders on a failed TAG READ — a schema or permission fault
    that does not clear on its own. Re-previewing produces the identical
    screen forever, which teaches the operator to read the refusal as
    flakiness rather than as the real stop it is. It must say what happened,
    what it means and what to do instead (principle 25).
    """
    body = _prose(_src(_PREVIEW))
    assert "again in a moment" not in body, (
        "the refusal is offering a retry for a fault that does not clear"
    )
    assert "This does not clear on its own" in body, (
        "the refusal must say the fault is standing, not transient"
    )
    # And it still offers the one thing that does work, which is the per-agent
    # path the server's own refusal detail names.
    assert "remove agents one at a time from the agent list." in body


def test_the_refusal_names_no_fault_the_flag_cannot_identify():
    """`membership_verified` is ONE boolean over TWO independent faults.

    `verified = tags_readable and roster_complete` (service.py). The banner
    used to say "the system's membership tags could not be read" on both — a
    false statement on the roster arm, where the tags read perfectly and it is
    the agent LIST that is incomplete. A boolean cannot say which fired, so
    the banner states the consequence (identical either way) and quotes the
    server for the cause.
    """
    body = _prose(_src(_PREVIEW))
    banner = re.search(
        r"membership_verified === false([\s\S]*?)</section>", _src(_PREVIEW)
    )
    assert banner, "the refusal banner is no longer a single reviewable section"
    assert "membership tags could not be read" not in _prose(banner.group(1)), (
        "the banner names the tag read, which is only one of the two faults "
        "behind the flag it renders on"
    )
    assert 'data-testid="teardown-membership-fault"' in body, (
        "the banner must carry the server's own statement of the cause"
    )
    assert "membershipWarnings" in body


def test_every_server_warning_lands_in_exactly_one_place():
    """The banner claims the membership lines; Notes takes the rest; the two
    with a dedicated block above are suppressed. The roster line used to fall
    through to Notes, putting the real fault in a footnote beneath a banner
    naming the other one — and a filter that merely subtracted it from Notes
    would have dropped it off the screen.

    `otherWarnings` is therefore defined as "not suppressed and not already
    taken by the banner", never as a second independent regex: two regexes
    that must stay complementary are two chances to drop a line silently.
    """
    body = _code_only(_src(_PREVIEW))
    assert "roster could not be completed" in body, (
        "the roster fault must be routed, not left to fall through to Notes"
    )
    assert "!membershipWarnings.value.includes(line)" in body, (
        "Notes must subtract exactly what the banner took, so a line the "
        "banner declines still reaches the screen"
    )
    # And the banner only claims them while it is on screen.
    assert re.search(
        r"membership_verified === false[\s\S]{0,200}?MEMBERSHIP_FAULT\.test", body
    ), "the banner's claim must be conditional on the banner rendering"



def test_protected_agents_are_listed_rather_than_silently_dropped():
    """`excluded` carries the platform agents a `trinity-` prefix captured. The
    server never removes them; the UI must not leave the operator wondering
    where they went."""
    src = _src(_PREVIEW)
    assert "preview.excluded" in src


def test_the_ack_reuses_the_ent126_contract_but_not_its_address():
    """One PATTERN for "restate the consequence and affirm it" — and two
    distinct addresses.

    The prop/emit shape is shared on purpose: a second convention for the same
    job is how the two drift. The `data-testid` is deliberately NOT, and that
    distinction is the point of this test. Install and remove can both be
    previewed on the same page, so a shared id puts two elements at one
    address — strict-mode ambiguous for any Playwright `getByTestId(...)`, and
    `e2e/system-install.spec.js` addresses the install ack exactly that way. It
    would not have failed CI (the teardown panel is unentitled there and renders
    nothing), which is precisely why it needs a test rather than a CI run.
    """
    preview = _code_only(_src(_PREVIEW))
    manifest = _code_only(_src(_MANIFEST_PREVIEW))

    # The shared contract.
    for src in (preview, manifest):
        assert "update:acknowledged" in src
        assert ":checked=\"acknowledged\"" in src

    # The separate addresses.
    assert 'data-testid="ack-checkbox"' in manifest
    assert 'data-testid="teardown-ack-checkbox"' in preview
    assert 'data-testid="ack-checkbox"' not in preview, (
        "two acknowledgement checkboxes can be on the page at once; they must "
        "not share one test id"
    )


def test_no_test_id_is_shared_with_the_install_panel_it_sits_beside():
    """The general rule, not the one instance.

    Fixing the acknowledgement id by hand found a SECOND collision the same way
    (`goto-fleet`, owned by DeployResult.vue), which is the signal that the
    per-id fix was the wrong shape. Install and remove are two panels on one
    page, and on an entitled build both can show a preview and a result at the
    same time — so any id they share is strict-mode ambiguous for a Playwright
    `getByTestId(...)`, which is how `e2e/system-install.spec.js` addresses both
    `ack-checkbox` and `goto-fleet`.

    None of this fails CI today (the teardown panel is unentitled there and
    renders nothing), which is the whole reason it needs a test.
    """
    ids = re.compile(r'data-testid="([a-z0-9-]+)"')
    install = set()
    for path in (_MANIFEST_PREVIEW, _DEPLOY_RESULT, _SYSTEMS / "SystemInstallPanel.vue"):
        install |= set(ids.findall(_code_only(_src(path))))
    teardown = set()
    for path in _NEW_FILES:
        teardown |= set(ids.findall(_code_only(_src(path))))

    assert install, "premise check: the install panel has test ids"
    assert teardown, "premise check: the teardown panel has test ids"
    shared = install & teardown
    assert not shared, (
        f"shared test id(s) {sorted(shared)} — both panels can render on the "
        "Library page at once, so each id must address exactly one element"
    )


def test_the_servers_opt_out_sentence_is_not_rendered_beside_the_opt_in_one():
    """The prefix warning must be suppressed now that it has a block above it.

    The server's line ends *"Uncheck anything that does not belong before
    confirming"* — the OPT-OUT instruction this change reversed. Rendered in
    "Notes" it sat six lines under the panel's own *"tick it only if it
    belongs"*, so one screen gave opposite instructions about the same row,
    and the stale half read as authoritative because it came from the server.

    This is the rule the component's docstring already states — a warning with
    a dedicated block above is not repeated as prose — applied to the warning
    that only just gained one.
    """
    body = _code_only(_src(_PREVIEW))
    assert "matched by NAME ONLY" in body, (
        "the prefix warning must be routed out of Notes; the panel's own note "
        "and per-member badge cover it"
    )
    # Suppressed, not claimed by the banner: it is raised in the HEALTHY state
    # too, where no banner renders at all.
    m = re.search(r"const SUPPRESSED = /([^/]+)/i", body)
    assert m and "matched by NAME ONLY" in m.group(1), (
        "it belongs in SUPPRESSED (dedicated block above), not MEMBERSHIP_FAULT"
    )


def test_the_tag_line_claims_no_count_it_cannot_support():
    """`SystemTeardownTag.member_count` is `len(members)` — every candidate,
    tagged or matched by name. Rendering it as "(N tagged)" overstates the tag
    whenever any member is `prefix`, and in the refusal state it quotes a
    figure from the very read the banner above says failed: two statements
    about one read, opposite in confidence, on one screen.

    The total the operator needs is already stated on the list itself.
    """
    body = _prose(_src(_PREVIEW))
    assert "tag.member_count" not in body, (
        "member_count counts every candidate, not the tagged ones — it cannot "
        "be rendered as a tag count"
    )
    assert "tagged)" not in body
    # The sentence keeps its actual job.
    assert "not a separate record to delete" in body


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


def test_the_result_drops_the_preview_only_instruction():
    """A result screen must not print an instruction for the screen before it.

    The server's prefix warning ends *"Uncheck anything that does not belong
    before confirming"*. On the RESULT that is advice for a decision already
    taken, under a report of what was already removed — and it is the opt-OUT
    wording the preview stopped using. Seen only by tearing a system down for
    real; the preview's own filter did not cover this component.
    """
    body = _code_only(_src(_RESULT))
    assert "PREVIEW_ONLY" in body and "matched by NAME ONLY" in body, (
        "the result must filter the preview-time instruction out of Notes"
    )
    assert re.search(r"warnings = computed\([\s\S]{0,200}?filter", body), (
        "result warnings are rendered unfiltered"
    )


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
