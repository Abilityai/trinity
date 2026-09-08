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


def _pre_split_router(monkeypatch):
    """The single-module `routers/settings.py` as it was before the split.

    Read out of git rather than kept as a fixture copy: a checked-in duplicate
    of a 3,529-line module would rot immediately, and the point of comparison
    is the real thing this replaced.
    """
    blob = subprocess.run(
        ["git", "show", "dd910564:src/backend/routers/settings.py"],
        cwd=_REPO, capture_output=True, text=True,
    )
    if blob.returncode != 0:
        pytest.skip("pre-split blob unavailable (shallow clone)")
    tmp = _REPO / "src" / "backend" / "_pre_split_settings.py"
    tmp.write_text(blob.stdout)
    try:
        spec = importlib.util.spec_from_file_location("_pre_split_settings", tmp)
        mod = importlib.util.module_from_spec(spec)
        # monkeypatch-scoped so the registration is reverted at test teardown
        # (the sys.modules lint forbids bare assignment/pop here).
        monkeypatch.setitem(sys.modules, "_pre_split_settings", mod)
        spec.loader.exec_module(mod)
        return mod.router
    finally:
        tmp.unlink(missing_ok=True)


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


def test_the_mounted_route_set_is_unchanged(monkeypatch):
    """The API a caller sees is identical — no route lost, none invented."""
    import routers.settings as new

    before = {_sig(r) for r in _pre_split_router(monkeypatch).routes}
    after = {_sig(r) for r in new.router.routes}
    invented = after - before - _ADDED_SINCE_SPLIT
    assert invented == set(), f"routes invented by the split: {sorted(invented)}"
    assert before - after == set(), f"routes lost by the split: {sorted(before - after)}"


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


def _logical_lines(path: Path) -> int:
    """Lines that carry text — everything except blank separators.

    Blank lines are excluded and comments are NOT, and the asymmetry is the
    whole point rather than an oversight.

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
    """The size AC. 800 logical lines is the repo's critical class; the point of
    the split was to leave nothing above it."""
    oversized = {
        p.name: n
        for p in _PKG.glob("*.py")
        if (n := _logical_lines(p)) > 800
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
