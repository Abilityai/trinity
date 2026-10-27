"""#2891 — an installed pytest-randomly may not shuffle a run silently.

Pins the policy in `tests/testkit/randomly_guard.py` and its wiring:

1. `opted_in` recognises exactly the explicit forms (CLI seed, `-p randomly`,
   `PYTEST_ADDOPTS`, the env opt-in) and nothing else.
2. `enforce` refuses (UsageError) when the plugin is active and nobody opted
   in; is a no-op when the plugin is absent; passes when opted in.
3. `banner` names the resolved seed and both the reproduce and disable forms.
4. Both conftests (`tests/` and the sealed `tests/unit/` island) call the guard
   from a `tryfirst` `pytest_configure`, and all four `run-*.sh` scripts pin
   `-p no:randomly` on every pytest invocation — the ordering is explicit, not
   dependent on the plugin's absence.
"""

from __future__ import annotations

import pathlib
import re
from types import SimpleNamespace

import pytest

from testkit import randomly_guard as G

TESTS = pathlib.Path(__file__).resolve().parent.parent


def _config(active: bool, args=(), seed="default"):
    pm = SimpleNamespace(hasplugin=lambda name: active and name == "randomly")
    return SimpleNamespace(
        pluginmanager=pm,
        invocation_params=SimpleNamespace(args=tuple(args)),
        getoption=lambda name: seed if name == "randomly_seed" else None,
    )


# --------------------------------------------------------------------------- #
# 1. opt-in recognition
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "args,env,expected",
    [
        (["unit/"], {}, False),
        (["unit/", "-p", "no:randomly"], {}, False),  # blocked plugin never reaches enforce anyway
        (["unit/", "--randomly-seed=12345"], {}, True),
        (["unit/", "--randomly-seed", "12345"], {}, True),
        (["-p", "randomly", "--randomly-seed=1", "unit/"], {}, True),
        (["-p", "randomly", "unit/"], {}, True),
        (["-prandomly", "unit/"], {}, True),
        (["unit/"], {"TRINITY_RANDOM_ORDER": "1"}, True),
        (["unit/"], {"TRINITY_RANDOM_ORDER": "0"}, False),
        (["unit/"], {"PYTEST_ADDOPTS": "--randomly-seed=99"}, True),
        (["unit/"], {"PYTEST_ADDOPTS": "-p randomly"}, True),
        # a path that merely mentions the word is not an opt-in
        (["unit/test_randomly_thing.py"], {}, False),
    ],
)
def test_opted_in(args, env, expected):
    assert G.opted_in(args, environ=env) is expected


# --------------------------------------------------------------------------- #
# 2. enforce
# --------------------------------------------------------------------------- #


def test_enforce_refuses_a_silent_shuffle(monkeypatch):
    # The outer run may itself be opted in (TRINITY_RANDOM_ORDER=1, or
    # PYTEST_ADDOPTS carrying a seed); the case under test has neither.
    monkeypatch.delenv(G.OPT_IN_ENV, raising=False)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    with pytest.raises(pytest.UsageError) as exc:
        G.enforce(_config(active=True, args=["unit/"]))
    msg = str(exc.value)
    assert "-p no:randomly" in msg
    assert "--randomly-seed" in msg
    assert G.OPT_IN_ENV in msg


def test_enforce_is_a_no_op_when_the_plugin_is_absent():
    G.enforce(_config(active=False, args=["unit/"]))  # no raise


def test_enforce_passes_an_explicit_seed(monkeypatch):
    monkeypatch.delenv(G.OPT_IN_ENV, raising=False)
    G.enforce(_config(active=True, args=["-p", "randomly", "--randomly-seed=12345", "unit/"]))


def test_enforce_passes_the_env_opt_in(monkeypatch):
    monkeypatch.setenv(G.OPT_IN_ENV, "1")
    G.enforce(_config(active=True, args=["unit/"]))


# --------------------------------------------------------------------------- #
# 3. banner
# --------------------------------------------------------------------------- #


def test_banner_names_seed_reproduce_and_disable():
    text = G.banner(_config(active=True, seed=4242))
    assert "seed=4242" in text
    assert "--randomly-seed=4242" in text
    assert "-p no:randomly" in text


def test_banner_is_silent_without_the_plugin():
    assert G.banner(_config(active=False, seed=4242)) is None


# --------------------------------------------------------------------------- #
# 4. wiring
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("conftest", ["conftest.py", "unit/conftest.py"])
def test_both_conftests_enforce_from_a_tryfirst_configure(conftest):
    src = (TESTS / conftest).read_text()
    assert "randomly_guard" in src, f"{conftest} does not import the guard"
    # The hookimpl must run BEFORE pytest-randomly's own pytest_configure
    # resolves an implicit seed, or an opted-in run and a silent one look alike.
    m = re.search(r"@pytest\.hookimpl\(tryfirst=True\)\s*\ndef pytest_configure\(config\):(.*?)(?=\n(?:def |@|class ))",
                  src, re.S)
    assert m, f"{conftest}: pytest_configure is not a tryfirst hookimpl"
    assert ".enforce(config)" in m.group(1), f"{conftest}: pytest_configure does not call enforce"
    assert re.search(r"def pytest_sessionstart\(session\):\s*\n(?:.*\n)*?.*\.announce\(session\)", src), (
        f"{conftest}: no sessionstart banner"
    )


@pytest.mark.parametrize("script", ["run-full.sh", "run-core.sh", "run-integration.sh", "run-smoke.sh"])
def test_every_runner_pytest_invocation_pins_deterministic_order(script):
    src = (TESTS / script).read_text()
    invocations = [l for l in src.splitlines() if re.search(r"python -m pytest\b", l) and not l.lstrip().startswith("#")]
    assert invocations, f"{script}: no pytest invocation found"
    unpinned = [l for l in invocations if "-p no:randomly" not in l]
    assert not unpinned, f"{script}: pytest invoked without -p no:randomly:\n  " + "\n  ".join(unpinned)
