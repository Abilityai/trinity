"""#1028 — `routers/settings` is a package, and the split changed no behaviour.

`routers/settings.py` was 3,529 lines: the largest file in the backend and
more than four times the 800-line critical threshold. It is now ten domain
modules composed onto one router.

The risk a decomposition like this carries is not "a handler broke" — each
handler still works when called directly, and its own unit test still passes.
The risk is **route registration order**. FastAPI matches in registration
order, so `GET /{key}` placed before `GET /ops/config` answers "setting not
found" for a route that plainly exists, and nothing that tests handlers in
isolation can see it.

So this file pins the two properties that survive a regrouping:

  * the mounted route SET is exactly what the single module mounted, and
  * no route can be shadowed by an earlier one.

It deliberately does NOT pin the literal order. Regrouping by domain reorders
specific routes relative to each other, and that is inert — two specific routes
only interfere when one can match the other's URL, which is what the shadowing
check below actually tests. Asserting the old order verbatim would fail on a
change that cannot affect a caller, and would have to be edited by hand every
time a route moves — a guard nobody trusts is a guard nobody keeps.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_PKG = _REPO / "src" / "backend" / "routers" / "settings"


# The 60 routes `routers/settings.py` mounted at `dd910564`, the commit this
# split forked from — frozen here rather than read back out of git.
#
# The first version of this guard did `git show dd910564:…` at test time. That
# reads better and never runs: `.github/workflows/backend-unit-test.yml` pins
# `fetch-depth: 1` on every checkout, so the blob is unreachable on the runner
# and the comparison skipped on every CI run it has ever had — leaving the one
# property a 3,529 → 10 module split actually risks (a route silently lost or
# invented) proven nowhere. A guard that only runs on the author's machine is
# not a guard.
#
# A literal is the right shape for a *fork point*: it is a historical fact that
# cannot change, so freezing it costs nothing in maintenance. It is not a
# baseline that gets re-cut — a route added since the fork goes in
# `_ADDED_SINCE_SPLIT` below, one reviewed line at a time.
#
# `test_the_frozen_pre_split_set_matches_git` re-derives this from the real blob
# whenever history is deep enough (locally, and in any full-clone job), so the
# literal cannot quietly drift away from what it claims to transcribe.
_PRE_SPLIT_ROUTES = frozenset({
    ('/api/settings', ('GET',), 'get_all_settings'),
    ('/api/settings/a2a-endpoints', ('GET',), 'list_a2a_outbound_endpoints'),
    ('/api/settings/a2a-endpoints', ('PUT',), 'upsert_a2a_outbound_endpoint'),
    ('/api/settings/a2a-endpoints/{ref}', ('DELETE',), 'remove_a2a_outbound_endpoint'),
    ('/api/settings/agent-defaults/access-policy', ('GET',), 'get_agent_default_access_policy'),
    ('/api/settings/agent-defaults/access-policy', ('PUT',), 'update_agent_default_access_policy'),
    ('/api/settings/agent-defaults/resources', ('GET',), 'get_agent_default_resources'),
    ('/api/settings/agent-defaults/resources', ('PUT',), 'update_agent_default_resources'),
    ('/api/settings/agent-quotas', ('GET',), 'get_agent_quotas'),
    ('/api/settings/agent-quotas', ('PUT',), 'update_agent_quotas'),
    ('/api/settings/api-keys', ('GET',), 'get_api_keys_status'),
    ('/api/settings/api-keys/anthropic', ('DELETE',), 'delete_anthropic_key'),
    ('/api/settings/api-keys/anthropic', ('PUT',), 'update_anthropic_key'),
    ('/api/settings/api-keys/anthropic/test', ('POST',), 'test_anthropic_key'),
    ('/api/settings/api-keys/github', ('DELETE',), 'delete_github_pat'),
    ('/api/settings/api-keys/github', ('PUT',), 'update_github_pat'),
    ('/api/settings/api-keys/github/test', ('POST',), 'test_github_pat'),
    ('/api/settings/brain-orb', ('GET',), 'get_brain_orb_settings'),
    ('/api/settings/brain-orb', ('PUT',), 'update_brain_orb_settings'),
    ('/api/settings/elevenlabs', ('GET',), 'get_elevenlabs_settings'),
    ('/api/settings/elevenlabs', ('PUT',), 'update_elevenlabs_settings'),
    ('/api/settings/email-whitelist', ('GET',), 'list_email_whitelist'),
    ('/api/settings/email-whitelist', ('POST',), 'add_email_to_whitelist'),
    ('/api/settings/email-whitelist/{email}', ('DELETE',), 'remove_email_from_whitelist'),
    ('/api/settings/feature-flags', ('GET',), 'get_public_feature_flags'),
    ('/api/settings/github-templates', ('DELETE',), 'delete_github_templates'),
    ('/api/settings/github-templates', ('GET',), 'get_github_templates'),
    ('/api/settings/github-templates', ('PUT',), 'update_github_templates'),
    ('/api/settings/max-parallel-tasks-ceiling', ('GET',), 'get_max_parallel_tasks_ceiling_setting'),
    ('/api/settings/max-parallel-tasks-ceiling', ('PUT',), 'update_max_parallel_tasks_ceiling_setting'),
    ('/api/settings/mcp-url', ('DELETE',), 'delete_mcp_url'),
    ('/api/settings/mcp-url', ('GET',), 'get_mcp_url'),
    ('/api/settings/mcp-url', ('PUT',), 'update_mcp_url'),
    ('/api/settings/operator-intake', ('GET',), 'get_operator_intake'),
    ('/api/settings/operator-intake', ('PUT',), 'set_operator_intake'),
    ('/api/settings/ops/config', ('GET',), 'get_ops_settings'),
    ('/api/settings/ops/config', ('PUT',), 'update_ops_settings'),
    ('/api/settings/ops/reset', ('POST',), 'reset_ops_settings'),
    ('/api/settings/portal-session-policy', ('GET',), 'get_portal_session_policy_status'),
    ('/api/settings/proactive-rate-limits', ('GET',), 'get_proactive_rate_limits_setting'),
    ('/api/settings/proactive-rate-limits', ('PUT',), 'update_proactive_rate_limits_setting'),
    ('/api/settings/retention', ('GET',), 'get_retention_status'),
    ('/api/settings/retention/acknowledge', ('POST',), 'acknowledge_retention_prune'),
    ('/api/settings/skills-library', ('GET',), 'get_skills_library_automation_setting'),
    ('/api/settings/skills-library', ('PUT',), 'update_skills_library_automation_setting'),
    ('/api/settings/slack', ('DELETE',), 'delete_slack_settings'),
    ('/api/settings/slack', ('GET',), 'get_slack_settings_status'),
    ('/api/settings/slack', ('PUT',), 'update_slack_settings'),
    ('/api/settings/slack/connect', ('POST',), 'connect_slack_transport'),
    ('/api/settings/slack/disconnect', ('POST',), 'disconnect_slack_transport'),
    ('/api/settings/slack/install', ('POST',), 'install_slack_workspace'),
    ('/api/settings/slack/status', ('GET',), 'get_slack_transport_status'),
    ('/api/settings/telemetry-sharing', ('GET',), 'get_telemetry_sharing'),
    ('/api/settings/telemetry-sharing', ('PUT',), 'set_telemetry_sharing'),
    ('/api/settings/template-registry', ('DELETE',), 'delete_template_registry'),
    ('/api/settings/template-registry', ('GET',), 'get_template_registry'),
    ('/api/settings/template-registry', ('PUT',), 'update_template_registry'),
    ('/api/settings/{key}', ('DELETE',), 'delete_setting'),
    ('/api/settings/{key}', ('GET',), 'get_setting'),
    ('/api/settings/{key}', ('PUT',), 'update_setting'),
})


def _pre_split_router_from_git(monkeypatch, tmp_path):
    """The single-module `routers/settings.py` as it was before the split.

    Only reachable on a full clone. Written under `tmp_path`, NOT into
    `src/backend/` — an interrupted run there leaves a top-level module behind
    that `Dockerfile:131` would bake into the image.
    """
    blob = subprocess.run(
        ["git", "show", "dd910564:src/backend/routers/settings.py"],
        cwd=_REPO, capture_output=True, text=True,
    )
    if blob.returncode != 0:
        pytest.skip("pre-split blob unavailable (shallow clone)")
    tmp = tmp_path / "_pre_split_settings.py"
    tmp.write_text(blob.stdout)
    spec = importlib.util.spec_from_file_location("_pre_split_settings", tmp)
    mod = importlib.util.module_from_spec(spec)
    # monkeypatch-scoped so the registration is reverted at test teardown
    # (the sys.modules lint forbids bare assignment/pop here).
    monkeypatch.setitem(sys.modules, "_pre_split_settings", mod)
    spec.loader.exec_module(mod)
    return mod.router


def _sig(route):
    return (route.path, tuple(sorted(route.methods)), route.name)


# Routes that landed on `dev` AFTER the fork point this guard compares against,
# and are therefore legitimately absent from the pre-split blob. Each entry is
# an explicit, reviewed statement that a route is NEW — not that the comparison
# is noisy. An unlisted addition still fails, which is the property that makes
# the guard worth keeping; the alternative (moving the pinned blob forward on
# every merge) silently re-baselines whatever drifted in with it.
_ADDED_SINCE_SPLIT = {
    # ent#437 — "don't ask again" marker for the Finish-setup consent card.
    ("/api/settings/telemetry-sharing/ask/dismiss", ("POST",),
     "dismiss_telemetry_ask"),
}


def test_the_mounted_route_set_is_unchanged():
    """The API a caller sees is identical — no route lost, none invented.

    Runs against the frozen fork-point set, so it runs in CI — which the
    git-blob version it replaced never did.
    """
    import routers.settings as new

    before = _PRE_SPLIT_ROUTES
    after = {_sig(r) for r in new.router.routes}
    invented = after - before - _ADDED_SINCE_SPLIT
    assert invented == set(), f"routes invented by the split: {sorted(invented)}"
    assert before - after == set(), f"routes lost by the split: {sorted(before - after)}"


def test_the_frozen_pre_split_set_matches_git(monkeypatch, tmp_path):
    """The literal above is a transcription; this is what keeps it honest.

    Skips on a shallow clone — which is every CI checkout — and that is fine:
    the property CI has to prove is the one above, and this only guards the
    fixture against a hand-edit that would weaken it.
    """
    router = _pre_split_router_from_git(monkeypatch, tmp_path)
    assert {_sig(r) for r in router.routes} == set(_PRE_SPLIT_ROUTES)


def test_the_post_split_allowlist_is_not_stale():
    """An allowlist entry that no longer names a mounted route is a lie the next
    reader inherits — it would silently excuse a *different* route with the same
    signature later. Fail while the fix is one line."""
    import routers.settings as new

    mounted = {_sig(r) for r in new.router.routes}
    stale = _ADDED_SINCE_SPLIT - mounted
    assert stale == set(), (
        "these _ADDED_SINCE_SPLIT entries no longer name a mounted route — "
        f"drop them: {sorted(stale)}"
    )


def _concrete(path: str) -> str:
    """A path with its parameters filled in, so it can be matched as a URL."""
    return re.sub(r"\{[^}]+\}", "x", path)


def test_no_route_is_shadowed_by_an_earlier_one():
    """The property that actually matters, stated directly.

    `/{key}` matches any single segment, so if it is registered before
    `/ops/config` every request for the latter is answered by the former. This
    walks every pair in registration order and fails on the first route that an
    earlier one would swallow — which catches the catch-all case and any future
    parameterised route added above its siblings.
    """
    import routers.settings as new

    routes = list(new.router.routes)
    shadowed = []
    for i, later in enumerate(routes):
        url = _concrete(later.path)
        for earlier in routes[:i]:
            if not (earlier.methods & later.methods):
                continue
            if earlier.path == later.path:
                continue
            if earlier.path_regex.match(url):
                shadowed.append(f"{sorted(later.methods)[0]} {later.path} "
                                f"shadowed by {earlier.path}")
                break
    assert shadowed == [], (
        "these routes are unreachable — an earlier registration matches their "
        "URL first (Invariant #4): " + "; ".join(shadowed)
    )


def test_the_catch_all_is_included_last():
    """Stated separately from the shadowing check because it is the ordering
    rule a human edits `__init__.py` against — the shadowing test says *a*
    route is unreachable, this one says *which include line* is wrong."""
    import routers.settings as new

    paths = [r.path for r in new.router.routes]
    catch_all = [i for i, p in enumerate(paths) if "{key}" in p]
    specific = [i for i, p in enumerate(paths) if "{key}" not in p]
    assert catch_all, "the /{key} catch-all vanished"
    assert min(catch_all) > max(specific), (
        "a specific settings route is registered after /{key} and is therefore "
        "dead — move its include_router() call above generic's in "
        "routers/settings/__init__.py"
    )


def _non_blank_lines(path: Path) -> int:
    """Lines that carry text — everything except blank separators.

    NOT called `_logical_lines`, and the rename is the point. A "logical line"
    in Python excludes comments, so that name over this body says the opposite
    of what it does — and reading the phrase "logical lines" in the docstring
    below is exactly what talked a previous pass into excluding comments and
    re-baselining this guard. A name that has to be read against its own
    implementation is the trap, not the fix for it.

    Blank lines are excluded and comments are NOT, and the asymmetry is
    deliberate.

    Restoring a PEP-8 blank line between two defs is not a module growing, so a
    metric that counts blanks makes formatting look like size. But excluding
    comments as well would move the calibration: this package's files carry
    110-208 comment lines each, so a comment-blind count would hand
    `credentials.py` ~160 lines of headroom the 800 ceiling never gave it —
    re-baselining the guard under cover of a fix, which is the thing this file
    exists to make hard.

    Measured, not assumed: `credentials.py` is 687 non-blank lines both at
    `6d8c93a4` (raw 778) and after the blank-line restoration (raw 806). This
    metric is invariant under exactly the change that prompted it and under
    nothing else.
    """
    return sum(1 for line in path.read_text().splitlines() if line.strip())


def test_every_module_is_under_the_critical_threshold():
    """The size AC. 800 non-blank lines is the repo's critical class; the point
    of the split was to leave nothing above it.

    "Non-blank", stated in the AC's own words rather than left to the helper:
    the ceiling was calibrated against files counted this way, and the phrase
    that describes it must not be one a reader can take as licence to measure
    something narrower."""
    oversized = {
        p.name: n
        for p in _PKG.glob("*.py")
        if (n := _non_blank_lines(p)) > 800
    }
    assert oversized == {}, f"still over the 800-line threshold: {oversized}"


def test_the_import_surface_callers_depend_on_still_resolves():
    """`routers/connector.py` imports `resolve_mcp_url`; tests import the key
    sets and the repo pattern. A split that renames the import surface is not a
    pure refactor, so the package re-exports them."""
    import routers.settings as s

    for name in ("router", "resolve_mcp_url", "MCP_URL_SETTING_KEY",
                 "LEGACY_SKILLS_LIBRARY_KEYS", "SKILLS_AUTOMATION_KEYS",
                 "_REPO_PATTERN", "mask_api_key"):
        assert hasattr(s, name), f"routers.settings.{name} no longer resolves"
