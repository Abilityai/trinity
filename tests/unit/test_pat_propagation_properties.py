"""Edge-case + property analysis of PAT propagation (`/edge-cases`, 2026-08-05).

Target: `services/github_pat_propagation_service._patch_env_github_pat` /
`_format_pat_line` / `_env_has_github_pat`, as merged on `dev` by #1979
(issue #1967).

#1967's suite covers eligibility, the remote rewrite, and per-agent failure
isolation. What it does not cover is the *text transform itself*: this is a
regex substitution that writes a credential into a file another process parses,
so the honest question is a round-trip one — **after patching, does the agent
read back exactly the token we rotated to?**

That is stated once as a Hypothesis property and pinned as explicit cases for
the two inputs where it does not hold. The oracle is the agent's own `.env`
reader, copied here from
`docker/base-image/agent_server/services/execution_env.parse_env_file` — the
same last-wins, one-quote-pair semantics, because "what the agent sees" is the
only definition of success that matters for a credential rotation.

Cases marked `xfail(strict=True)` are real defects; product code is unchanged
per the skill's protocol.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

_REPO = Path(__file__).resolve().parents[2]
_BACKEND_STR = str(_REPO / "src" / "backend")
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def svc():
    try:
        import services.github_pat_propagation_service as m
    except ImportError:  # pragma: no cover — backend venv required
        pytest.skip("backend venv required")
    return m


# ---------------------------------------------------------------------------
# The oracle: the agent's own .env reader
# ---------------------------------------------------------------------------

_ENV_LINE_KEYS = ("GITHUB_PAT", "GH_TOKEN", "GITHUB_TOKEN")


def agent_reads(env_content: str) -> dict:
    """Byte-faithful copy of the agent-server `.env` parser (last-wins).

    Deliberately a copy and not an import: the agent server ships in its own
    image and the backend cannot import from it. Kept small enough to audit
    against the original by eye.
    """
    out = {}
    for line in env_content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        out[key] = value.strip().strip('"').strip("'")
    return out


# A GitHub PAT's real alphabet (`ghp_…`, `github_pat_…`) — the tokens this code
# will actually be handed.
REAL_PATS = st.from_regex(r"\A(ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{20,60})\Z")

# Plausible surrounding .env content, excluding the keys under test so the
# fixture cannot pre-seed the very lines the property is about.
ENV_NOISE = st.lists(
    st.tuples(
        st.from_regex(r"\A[A-Z][A-Z0-9_]{0,12}\Z").filter(
            lambda k: k not in _ENV_LINE_KEYS
        ),
        st.text(alphabet=st.characters(blacklist_categories=("Cs", "Cc")), max_size=20),
    ),
    max_size=6,
).map(lambda pairs: "".join(f'{k}="{v}"\n' for k, v in pairs))


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------

class TestRoundTrip:

    @settings(max_examples=200, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(pat=REAL_PATS, noise=ENV_NOISE, has_existing=st.booleans())
    def test_the_agent_reads_back_exactly_the_rotated_token(
        self, svc, pat, noise, has_existing
    ):
        """The whole point of the feature, as one property: whatever the file
        looked like, after a rotation the agent must read the NEW token under
        all three key names."""
        env = noise + ('GITHUB_PAT="ghp_' + "o" * 36 + '"\n' if has_existing else "")
        patched = svc._patch_env_github_pat(env, pat)
        seen = agent_reads(patched)
        for key in _ENV_LINE_KEYS:
            assert seen.get(key) == pat, f"{key} reads back as {seen.get(key)!r}"

    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(pat=REAL_PATS, noise=ENV_NOISE)
    def test_patching_is_idempotent(self, svc, pat, noise):
        once = svc._patch_env_github_pat(noise, pat)
        assert svc._patch_env_github_pat(once, pat) == once

    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(pat=REAL_PATS, noise=ENV_NOISE)
    def test_unrelated_keys_survive(self, svc, pat, noise):
        before = agent_reads(noise)
        after = agent_reads(svc._patch_env_github_pat(noise, pat))
        for k, v in before.items():
            assert after.get(k) == v, f"rotation clobbered unrelated key {k}"

    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(pat=REAL_PATS, noise=ENV_NOISE)
    def test_no_token_is_left_behind_anywhere_in_the_file(self, svc, pat, noise):
        """Stronger than the read-back: the OLD token must not survive as text
        either, or a `.env` sourced by a shell (or grepped by a human) still
        yields the revoked credential."""
        old = "ghp_" + "o" * 36
        patched = svc._patch_env_github_pat(f'GITHUB_PAT="{old}"\n' + noise, pat)
        assert old not in patched


# ---------------------------------------------------------------------------
# Where the round-trip does not hold
# ---------------------------------------------------------------------------

class TestKnownGaps:

    @pytest.mark.xfail(
        strict=True,
        reason="BUG: `count=1` replaces only the FIRST GITHUB_PAT line, and the "
               "agent's .env parser is last-wins — so a duplicated line leaves "
               "the agent authenticating with the REVOKED token while the "
               "rotation reports `updated`. See /edge-cases report 2026-08-05, "
               "finding 2.",
    )
    def test_a_duplicated_pat_line_still_rotates(self, svc):
        env = ('GITHUB_PAT="ghp_old"\n'
               'SOMETHING=1\n'
               'GITHUB_PAT="ghp_old"\n')
        patched = svc._patch_env_github_pat(env, "ghp_new")
        assert agent_reads(patched)["GITHUB_PAT"] == "ghp_new", (
            "the agent still reads the old token"
        )

    @pytest.mark.parametrize("pat", [r"tok\1", r"tok\g<1>", r"tok\slash"])
    @pytest.mark.xfail(
        strict=True,
        reason="BUG: the new line is used as an `re.sub` REPLACEMENT, so a "
               "backslash in the token is parsed as a group reference and "
               "raises re.error mid-rotation. See /edge-cases report "
               "2026-08-05, finding 3.",
    )
    def test_a_backslash_in_the_token_does_not_raise(self, svc, pat):
        svc._patch_env_github_pat('GITHUB_PAT="old"\n', pat)

    @pytest.mark.xfail(
        strict=True,
        reason="Writer/reader asymmetry: `_format_pat_line` escapes `\"` but "
               "the agent's .env parser never unescapes it. Belongs to the "
               ".env parse contract, not to #1979. See /edge-cases report "
               "2026-08-05, finding 4.",
    )
    def test_a_quote_in_the_token_round_trips(self, svc):
        pat = 'ghp_a"b'
        patched = svc._patch_env_github_pat('GITHUB_PAT="old"\n', pat)
        assert agent_reads(patched)["GITHUB_PAT"] == pat


# ---------------------------------------------------------------------------
# Deterministic boundary cases that DO hold (regression value)
# ---------------------------------------------------------------------------

class TestBoundaries:

    @pytest.mark.parametrize("env,label", [
        ("", "empty file"),
        ("\n", "just a newline"),
        ("NO_TRAILING_NEWLINE=1", "no trailing newline"),
        ("  GITHUB_PAT=\"indented\"\n", "leading whitespace"),
        ("\tGITHUB_PAT=\"tabbed\"\n", "leading tab"),
        ("# GITHUB_PAT=\"commented\"\n", "commented-out line"),
        ("GITHUB_PAT=\n", "present but empty"),
        ("GITHUB_PATX=\"lookalike\"\n", "lookalike key"),
        ("MY_GITHUB_PAT=\"suffix\"\n", "key with a prefix"),
        ("A=1\r\nGITHUB_PAT=\"crlf\"\r\n", "CRLF line endings"),
    ])
    def test_the_agent_reads_the_new_token(self, svc, env, label):
        patched = svc._patch_env_github_pat(env, "ghp_new")
        assert agent_reads(patched).get("GITHUB_PAT") == "ghp_new", label

    def test_a_lookalike_key_is_not_clobbered(self, svc):
        patched = svc._patch_env_github_pat('GITHUB_PATX="keep"\n', "ghp_new")
        assert agent_reads(patched)["GITHUB_PATX"] == "keep"

    def test_a_commented_line_is_not_treated_as_a_pat(self, svc):
        assert svc._env_has_github_pat('# GITHUB_PAT="x"\n') is False

    def test_an_exported_line_is_not_recognised(self, svc):
        """`export GITHUB_PAT=…` reads as "no PAT", so on the global path such
        an agent is skipped as `skipped_no_pat`. Documented, not asserted as a
        bug: the eligibility rule is deliberately `.env`-line-shaped."""
        assert svc._env_has_github_pat('export GITHUB_PAT="x"\n') is False

    def test_appending_does_not_glue_onto_a_previous_line(self, svc):
        patched = svc._patch_env_github_pat("FOO=1", "ghp_new")
        assert "FOO=1GITHUB_PAT" not in patched
        assert agent_reads(patched)["FOO"] == "1"
