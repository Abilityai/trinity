"""Edge-case + property analysis of PAT propagation (`/edge-cases`, 2026-08-05).

Target: `services/github_pat_propagation_service._patch_env_github_pat` /
`_format_pat_line` / `_env_has_github_pat`, as merged on `dev` by #1979
(issue #1967).

#1967's suite covers eligibility, the remote rewrite, and per-agent failure
isolation. What it does not cover is the *text transform itself*: this is a
regex substitution that writes a credential into a file another process parses,
so the honest question is a round-trip one — **after patching, does the agent
read back exactly the token we rotated to?**

That is stated once as a Hypothesis property and pinned as explicit cases,
because "what the agent sees" is the only definition of success that matters for
a credential rotation.

The oracle is the agent's own `.env` decoder — the REAL
`execution_env.unquote_env_value`, loaded from the base image by path (#2243).
It used to be a hand-written replica that only stripped quotes, and that replica
had drifted out from under two of the assertions below: #2023 made the encoding
reversible (escape the escape character, then the quote, and unescape both on
read), so a replica that never unescapes reports corruption the real agent never
sees. A second copy of an encoding is exactly what #2023 removed from product
code; keeping one in the test that judges it put the verdict back in the copy.

Cases marked `xfail(strict=True)` are real defects; product code is unchanged
per the skill's protocol. When one is fixed the marker flips the test RED
(XPASS(strict)) rather than going quietly green — which is the point, and is how
the three retired below announced themselves.
"""

from __future__ import annotations

import importlib.util
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


_READER_PATH = (
    _REPO / "docker" / "base-image" / "agent_server" / "services" / "execution_env.py"
)


def _real_reader():
    """The agent's own decoder, loaded by path (the `test_2023_…` idiom).

    Imported rather than reimplemented: the backend cannot import the agent-server
    package (different image), but it CAN load the one module by path, and that is
    the difference between judging the writer against the real inverse and judging
    it against a replica that has to be kept in sync by hand (#2243).
    """
    spec = importlib.util.spec_from_file_location("_env_reader_pat_props", _READER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ENV_READER = _real_reader()


def agent_reads(env_content: str) -> dict:
    """What the agent's `.env` reader sees: last-wins keys, real value decoding.

    The line scan is here because the property under test is about DUPLICATE
    lines (#2016) and last-wins is the semantics that makes a duplicate
    dangerous. The value decoding — the part that has an encoding contract and
    can therefore drift — is delegated to `execution_env.unquote_env_value`
    rather than re-implemented as `.strip('"')`.
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
        out[key] = _ENV_READER.unquote_env_value(value)
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

    def test_a_duplicated_pat_line_still_rotates(self, svc):
        """Finding 2 — FIXED by #2016 (PR #2025), marker retired.

        This carried `xfail(strict=True)` while the bug was live: `count=1`
        replaced only the FIRST `GITHUB_PAT` line, and the agent's `.env` parser
        is last-wins, so a duplicated line left the agent authenticating with the
        REVOKED token while the rotation reported `updated`.

        `31ba8d98` levels every occurrence, so the marker did its job and flipped
        to XPASS(strict) — which is a FAILURE, and is why CI went red here rather
        than quietly reporting a bug that no longer exists. Retired to a plain
        regression test: it now guards the fix instead of the defect.
        """
        env = ('GITHUB_PAT="ghp_old"\n'
               'SOMETHING=1\n'
               'GITHUB_PAT="ghp_old"\n')
        patched = svc._patch_env_github_pat(env, "ghp_new")
        assert agent_reads(patched)["GITHUB_PAT"] == "ghp_new", (
            "the agent still reads the old token"
        )

    @pytest.mark.parametrize("pat", [r"tok\1", r"tok\g<1>", r"tok\slash"])
    def test_a_backslash_in_the_token_round_trips(self, svc, pat):
        """Finding 3 — FIXED by #2017 (PR #2024), marker retired.

        The bug: the new line was passed to `re.sub` as a replacement STRING, so
        a backslash in the token was parsed as a group reference — `\\1` and
        `\\g<1>` raised `re.error: invalid group reference` and aborted that
        agent's rotation with a message about regex syntax. `_patch_env_github_pat`
        now substitutes with a callable (`lambda _match: new_line`), which is
        inserted verbatim.

        The marker did its job: once the fix landed these three reported
        XPASS(strict) — a FAILURE — instead of quietly documenting a bug that no
        longer existed (#2243). Retired to a plain regression test, and it asserts
        the ROUND TRIP rather than merely "does not raise": not raising was the
        crash symptom, while writing a backslash the agent decodes back to the
        same token is the actual contract. The written line carries the doubled
        `\\\\` of the #2023 encoding, and the real reader reverses it.
        """
        patched = svc._patch_env_github_pat('GITHUB_PAT="old"\n', pat)
        assert agent_reads(patched)["GITHUB_PAT"] == pat

    def test_a_quote_in_the_token_round_trips(self, svc):
        """Finding 4 — FIXED by #2023, marker retired (#2243).

        This carried `xfail(strict=True)` for "the writer escapes `\"` but the
        agent's .env parser never unescapes it". That was true of the writer/reader
        pair as it stood, and #2023 fixed BOTH halves: the encoding escapes the
        escape character first and `unquote_env_value` reverses it in one scan.

        The marker nevertheless kept reporting XFAIL — green — because the oracle
        in this file was a replica that only stripped quotes. So the test went on
        asserting a defect that no longer existed, and only the replica made the
        claim look true. That is the same class as the three above, reached from
        the other side: a stale marker hidden by a stale copy rather than an
        XPASS. Fixing the oracle is what exposed it (#2243).
        """
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
