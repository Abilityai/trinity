"""Refuse a silently randomized run; announce an explicitly randomized one (#2891).

`pytest-randomly` is deliberately absent from `tests/requirements-test.txt` and
installed in-workflow only, because pytest auto-discovers installed plugins and
the plugin would otherwise shuffle every local run with no signal. Nothing
enforced that: the moment the plugin lands in a local virtualenv, every
`run-full.sh` / `run-core.sh` invocation is shuffled, and latent order
dependencies surface as failures that do not reproduce — each triaged as a
suspected product regression before isolation showed otherwise.

Two outcomes, decided once here and called from BOTH conftests (`tests/` and
the sealed `tests/unit/` island, which never loads the parent conftest):

* The plugin is active and the run did NOT opt in → `pytest.UsageError`. Fail
  fast, before collection, with the two ways out spelled out.
* The run opted in — `--randomly-seed=<n>` on the command line (what CI's three
  seed matrix passes), `-p randomly` explicitly, or `TRINITY_RANDOM_ORDER=1` —
  → an unmissable banner naming the resolved seed and how to reproduce or
  disable it.

Opt-in is read from the INVOCATION, not from `config.getoption`: pytest-randomly
resolves `--randomly-seed`'s `"default"` to a fresh random int inside its own
`pytest_configure`, so by the time a conftest asks, an implicit run and an
explicit one look identical. `-p no:randomly` never reaches this module — the
plugin is not loaded, so `hasplugin("randomly")` is False.

Stdlib + pytest only; imported by conftests at collection.
"""

from __future__ import annotations

import os
from typing import Iterable

import pytest

OPT_IN_ENV = "TRINITY_RANDOM_ORDER"

_REFUSAL = (
    "pytest-randomly is installed in this virtualenv and would shuffle this run "
    "with no signal (#2891). Either\n"
    "  * disable it for a deterministic run:   pytest -p no:randomly ...\n"
    "    (tests/run-full.sh, run-core.sh, run-integration.sh and run-smoke.sh already do), or\n"
    "  * opt in explicitly, so the shuffle is announced and reproducible:\n"
    "      pytest -p randomly --randomly-seed=<n> ...   (what CI's seed matrix passes)\n"
    f"      {OPT_IN_ENV}=1 pytest ...\n"
    "A run that failed under a shuffle you did not ask for is not a regression "
    "until it fails with `-p no:randomly` too."
)


def _plugin_active(config) -> bool:
    return bool(config.pluginmanager.hasplugin("randomly"))


def opted_in(args: Iterable[str], environ=os.environ) -> bool:
    """Did the invocation ask for a shuffle? Pure over the argv + env."""
    if environ.get(OPT_IN_ENV, "").strip() == "1":
        return True
    args = list(args)
    for i, a in enumerate(args):
        if a.startswith("--randomly-seed"):
            return True
        # `-p randomly` (two tokens) or `-prandomly` (one)
        if a == "-p" and i + 1 < len(args) and args[i + 1] == "randomly":
            return True
        if a == "-prandomly":
            return True
    # PYTEST_ADDOPTS is part of the invocation too.
    extra = environ.get("PYTEST_ADDOPTS", "")
    return "--randomly-seed" in extra or "-p randomly" in extra


def enforce(config) -> None:
    """Call from a `pytest_configure` hookimpl (tryfirst, so it runs BEFORE
    pytest-randomly resolves the seed and before any test is collected)."""
    if not _plugin_active(config):
        return
    params = getattr(config, "invocation_params", None)
    if opted_in(params.args if params is not None else ()):
        return
    raise pytest.UsageError(_REFUSAL)


def announce(session) -> None:
    """Call from `pytest_sessionstart`: by then pytest-randomly has resolved
    the seed, and the terminal reporter exists to carry the banner."""
    text = banner(session.config)
    if not text:
        return
    tr = session.config.pluginmanager.getplugin("terminalreporter")
    if tr is not None:
        tr.write_line(text, yellow=True, bold=True)
    else:  # pragma: no cover - no terminal (e.g. --collect-only under a runner)
        print(text)


def banner(config) -> str | None:
    """The line to print for an opted-in shuffle. Call from a `pytest_configure`
    hookimpl marked `trylast`, so pytest-randomly has already turned
    `"default"` into the int it will use."""
    if not _plugin_active(config):
        return None
    try:
        seed = config.getoption("randomly_seed")
    except (ValueError, AttributeError):  # option not registered — plugin blocked late
        return None
    return (
        f"\n{'=' * 78}\n"
        f"  pytest-randomly ACTIVE — test order is shuffled (seed={seed}).\n"
        f"  Reproduce:  -p randomly --randomly-seed={seed}      Disable:  -p no:randomly\n"
        f"  A failure here is order-dependent until it also fails with -p no:randomly (#2891).\n"
        f"{'=' * 78}"
    )
