"""Reaped process cmdlines must never carry a credential to a log sink (ent#292).

The agent-side orphan sweeper logged the full argv of every process it killed.
Trinity writes git remotes with the PAT embedded in the URL, so every reaped
`git remote-https` wrote a LIVE GitHub PAT in plaintext into the container log —
and from there into Vector's persisted archives and any snapshot of that volume.

Two properties are pinned here, because the incident showed both of the obvious
assumptions failing:

* **Shape-independence.** An audit that grepped only `oauth2:` declared an agent
  clean while it was in fact carrying credentials in the `x-access-token:` form,
  and both classic `ghp_` (~40 chars) and fine-grained `github_pat_` (~93) were
  live. Any fixed prefix or fixed length is the wrong rule; redacting URL
  *userinfo* is shape-independent and covers formats that do not exist yet.

* **Log level is not a mitigation.** `main.py` sets `basicConfig(level=INFO)`
  and Vector routes agent logs by container class with no level filter, so INFO
  and WARNING persist identically. The fix has to be redaction, not a downgrade.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_UTILS = (
    Path(__file__).resolve().parents[2]
    / "docker" / "base-image" / "agent_server" / "utils"
)


def _load(name: str):
    """Load a module from the agent-server utils package by path.

    Importing `agent_server` pulls in FastAPI, which these pure-text helpers do
    not need and CI need not install in order to check a redaction rule.

    Loaded WITHOUT registering anything in `sys.modules`: the sanitizer imports
    only stdlib, so it needs no package parent, and a test that mutates
    `sys.modules` leaks into whatever runs next under a shared interpreter.
    """
    path = _UTILS / f"{name}.py"
    if not path.exists():  # pragma: no cover
        pytest.skip("agent base image sources not present")
    spec = importlib.util.spec_from_file_location(f"_ent292_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cs():
    return _load("credential_sanitizer")


# The exact line shape from the incident report.
_REAPED = (
    "/usr/lib/git-core/git remote-https origin "
    "https://{userinfo}@github.com/ExampleOrg/example-repo.git"
)


@pytest.mark.parametrize("userinfo, secret", [
    ("oauth2:ghp_" + "A" * 36,                "ghp_" + "A" * 36),
    ("x-access-token:ghp_" + "B" * 36,        "ghp_" + "B" * 36),
    ("oauth2:github_pat_" + "C" * 80,         "github_pat_" + "C" * 80),
    ("x-access-token:github_pat_" + "D" * 80, "github_pat_" + "D" * 80),
    # The case a shape-based rule misses entirely, and the reason the rule is
    # anchored on URL structure rather than on token prefixes.
    ("oauth2:some-future-token-format-2031",  "some-future-token-format-2031"),
])
def test_no_credential_survives_a_reaped_cmdline(cs, userinfo, secret):
    out = cs.sanitize_cmdline(_REAPED.format(userinfo=userinfo))
    assert secret not in out
    assert "***@github.com" in out
    # Still useful for the diagnosis the log line exists to serve.
    assert "git remote-https" in out


def test_sanitize_text_itself_gained_the_rule(cs):
    """Folded into `sanitize_text`, not bolted onto the sweeper — so every
    existing caller is covered, not only the one sink that leaked."""
    out = cs.sanitize_text(
        "cloning https://oauth2:ghp_" + "E" * 36 + "@github.com/o/r.git"
    )
    assert "ghp_" not in out
    assert "***@" in out


@pytest.mark.parametrize("benign", [
    "git log --format=%an user@example.com",
    "/usr/bin/python3 /app/agent_server/main.py",
    "curl https://github.com/Org/Repo.git",
])
def test_the_url_rule_does_not_eat_ordinary_text(cs, benign):
    """The rule THIS change adds must not over-redact. An over-broad redactor
    gets switched off, and then nothing is redacted at all.

    Asserted against `redact_url_userinfo` rather than the full
    `sanitize_cmdline`, deliberately: `sanitize_text` also substitutes the VALUES
    of environment variables whose names look sensitive, and `GITHUB_.*` is one
    of those patterns. On a GitHub Actions runner `GITHUB_SERVER_URL` is
    literally `https://github.com`, so the last case above is legitimately
    rewritten to `curl ***REDACTED***/Org/Repo.git` there and byte-equality is
    environment-dependent by construction. That is correct behaviour of a
    pre-existing rule; pinning it here would make the suite pass locally and
    fail in CI, which is exactly what happened.
    """
    assert cs.redact_url_userinfo(benign) == benign


@pytest.mark.parametrize("benign", [
    "git log --format=%an user@example.com",
    "/usr/bin/python3 /app/agent_server/main.py",
])
def test_full_sanitize_keeps_a_cmdline_diagnosable(cs, benign):
    """End to end, a line with no credential must still be readable — the log
    exists to answer "which process keeps dying?"."""
    assert cs.sanitize_cmdline(benign) == benign


def test_sanitize_runs_before_the_length_cap():
    """Ordering matters: truncating first can slice a token mid-value and leave
    a partial secret that no pattern then matches. Sanitize, then cap."""
    src = (_UTILS / "orphan_sweep.py").read_text()
    line = next(
        l for l in src.splitlines()
        if "_read_cmdline(pid)" in l and "cmd =" in l
    )
    assert "_safe_cmdline(" in line
    assert line.index("_safe_cmdline(") < line.index("_CMDLINE_LOG_CAP")


def test_the_sweeper_has_no_unsanitized_cmdline_sink():
    """Structural guard for the CLASS, not the line. A future sink that logs
    argv without going through the helper reintroduces exactly this bug."""
    src = (_UTILS / "orphan_sweep.py").read_text()
    for ln, line in enumerate(src.splitlines(), 1):
        if "_read_cmdline(" in line and "def " not in line:
            assert "_safe_cmdline(" in line, (
                f"orphan_sweep.py:{ln} reads a cmdline without sanitizing it: "
                f"{line.strip()}"
            )


def test_url_redactor_is_shared_not_duplicated():
    """#1595 fixed this same credential in git stderr with a local regex, and
    ent#292 then found the argv sink uncovered. One rule in one place — a second
    copy is how the next sink gets missed."""
    git_router = _UTILS.parent / "routers" / "git.py"
    if not git_router.exists():  # pragma: no cover
        pytest.skip("agent base image sources not present")
    body = git_router.read_text()
    assert "redact_url_userinfo" in body
    assert 're.sub(r"(://)[^/@\\s]+@"' not in body, (
        "routers/git.py re-derived the regex instead of using the shared helper"
    )


def test_the_sweeper_redacts_when_the_helper_is_reachable(monkeypatch):
    """End to end through the sweeper's own entry point."""
    monkeypatch.syspath_prepend(str(_UTILS))
    sweep = _load("orphan_sweep")
    out = sweep._safe_cmdline(
        "git remote-https origin https://oauth2:ghp_" + "F" * 36 + "@github.com/o/r.git"
    )
    assert "ghp_" not in out
    assert "***@github.com" in out


def test_the_sweeper_drops_the_cmdline_when_the_helper_is_unreachable(monkeypatch):
    """The security guarantee must not depend on an import succeeding.

    Rather than keep a second copy of the redaction regex here — which is
    exactly the drift that caused this bug, since #1595 fixed the same
    credential in git stderr with a local regex and the argv sink was missed —
    the sweeper DROPS the cmdline when the sanitizer cannot be reached.
    Diagnostics degrade; a credential still cannot leak.
    """
    monkeypatch.syspath_prepend(str(_UTILS))
    sweep = _load("orphan_sweep")

    def _no_helper(*a, **k):
        raise ImportError("simulated: sanitizer unreachable")

    monkeypatch.setattr(sweep, "__import__", _no_helper, raising=False)
    import builtins
    monkeypatch.setattr(builtins, "__import__", _no_helper)

    out = sweep._safe_cmdline(
        "git remote-https origin https://oauth2:ghp_" + "G" * 36 + "@github.com/o/r.git"
    )
    assert "ghp_" not in out
    assert "github.com" not in out
    assert out == sweep._CMD_UNAVAILABLE


def test_the_redaction_rule_has_exactly_one_definition():
    """The issue's explicit instruction: promote a SHARED helper rather than add
    a sweeper-local regex, because "the exposure exists anywhere argv or env is
    logged". Two copies of a security pattern drift, and the drift is what this
    whole issue is."""
    sweep_src = (_UTILS / "orphan_sweep.py").read_text()
    git_src = (_UTILS.parent / "routers" / "git.py").read_text()
    for name, body in (("orphan_sweep.py", sweep_src), ("routers/git.py", git_src)):
        assert "(://)[^/@" not in body, (
            f"{name} re-derived the URL-userinfo regex instead of using the "
            "shared helper in credential_sanitizer"
        )
