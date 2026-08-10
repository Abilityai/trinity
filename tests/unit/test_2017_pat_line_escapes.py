"""#2017 — a token's own characters must not be read as regex syntax.

`_patch_env_github_pat` passed the formatted `.env` line to `re.sub` as the
*replacement string*, and `re.sub` parses that string for escapes. So a token
carrying a backslash was interpreted as a group reference:

    ghp_a\\g<1>b     -> re.error: invalid group reference 1 at position 20
    ghp_back\\slash  -> re.error: bad escape \\s at position 20

The blast radius was bounded — `_propagate_to_agent` catches only `httpx`
errors, so this escaped to the `return_exceptions=True` gather and was recorded
as that agent's `failed` with the message attached. The rotation continued.
But since the trigger is the *token*, not the agent, it failed for every agent
in the fleet, and told the operator `bad escape \\s` rather than anything about
the token they had just pasted.

Fixed with a callable replacement, which `re` inserts verbatim.

Found by the `/edge-cases` round-trip property in
`test_pat_propagation_properties.py`; this file pins the specific inputs so the
string-replacement form cannot come back.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

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


def agent_reads(env_content: str) -> dict:
    """The agent-server `.env` reader, copied (it ships in its own image).

    Byte-faithful to `routers/credentials.py`: last-wins, one `.strip()` per
    quote character. Deliberately NOT "fixed" here — see the module note in
    `test_pat_propagation_properties.py`; this copy exists to answer "what does
    the agent actually see", which is the only thing that matters for a
    rotation.
    """
    out = {}
    for line in env_content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            out[key] = value.strip().strip('"').strip("'")
    return out


# The three shapes from the issue, plus the ones `re` treats specially in a
# replacement string. `\g<name>`, `\1` and `\\` are group/escape syntax; the
# rest are ordinary characters that a paste can still pick up.
HOSTILE_TOKENS = [
    pytest.param("ghp_a\\g<1>b", id="group-reference-by-name"),
    pytest.param("ghp_with\\1group", id="group-reference-by-number"),
    pytest.param("ghp_back\\slash", id="unknown-escape"),
    pytest.param("ghp_trailing\\", id="trailing-backslash"),
    pytest.param("ghp_double\\\\slash", id="double-backslash"),
    pytest.param("ghp_dollar$sign", id="dollar"),
    pytest.param("ghp_amp&ersand", id="ampersand"),
]


class TestTheTokenIsNeverParsedAsRegex:

    @pytest.mark.parametrize("pat", HOSTILE_TOKENS)
    def test_patching_does_not_raise(self, svc, pat):
        svc._patch_env_github_pat('GITHUB_PAT="old"\n', pat)

    @pytest.mark.parametrize("pat", HOSTILE_TOKENS)
    def test_the_agent_reads_back_exactly_the_token(self, svc, pat):
        """Not raising is half of it — the value has to survive verbatim.

        A replacement string would have mangled `\\g<1>` into a captured group
        even where it did not raise, silently writing a *different* token.
        """
        patched = svc._patch_env_github_pat('GITHUB_PAT="old"\n', pat)
        assert agent_reads(patched)["GITHUB_PAT"] == pat

    @pytest.mark.parametrize("pat", HOSTILE_TOKENS)
    def test_the_append_path_agrees_with_the_replace_path(self, svc, pat):
        """The two branches format the line differently (one goes through
        `re.sub`, one through an f-string). They must not diverge on a value
        that only the substitution path could corrupt."""
        replaced = svc._patch_env_github_pat('GITHUB_PAT="old"\n', pat)
        appended = svc._patch_env_github_pat("", pat)
        assert agent_reads(replaced)["GITHUB_PAT"] == agent_reads(appended)["GITHUB_PAT"]

    def test_all_three_key_mirrors_survive(self, svc):
        """#1574 mirrors the token into GH_TOKEN/GITHUB_TOKEN. The first key
        takes the `re.sub` path and the other two are appended, so a
        replacement-string bug shows up as the keys disagreeing."""
        pat = "ghp_a\\g<1>b"
        seen = agent_reads(svc._patch_env_github_pat('GITHUB_PAT="old"\n', pat))
        assert seen["GITHUB_PAT"] == seen["GH_TOKEN"] == seen["GITHUB_TOKEN"] == pat


class TestOrdinaryTokensAreUnaffected:
    """The fix must not change the normal path in any way."""

    @pytest.mark.parametrize("pat", [
        "ghp_" + "A" * 36,
        "github_pat_" + "b" * 82,
        "gho_" + "c" * 36,
        "0123456789abcdef0123456789abcdef01234567",   # legacy 40-hex
    ])
    def test_real_token_shapes_round_trip(self, svc, pat):
        patched = svc._patch_env_github_pat('GITHUB_PAT="old"\n', pat)
        assert agent_reads(patched)["GITHUB_PAT"] == pat

    def test_the_old_token_is_gone(self, svc):
        patched = svc._patch_env_github_pat('GITHUB_PAT="ghp_old"\n', "ghp_new")
        assert "ghp_old" not in patched

    def test_surrounding_lines_are_untouched(self, svc):
        env = 'BEFORE=1\nGITHUB_PAT="old"\nAFTER=2\n'
        patched = svc._patch_env_github_pat(env, "ghp_a\\1b")
        seen = agent_reads(patched)
        assert seen["BEFORE"] == "1" and seen["AFTER"] == "2"


def test_the_replacement_is_a_callable_not_a_string():
    """Pins the mechanism, via the AST rather than the text.

    A future edit could re-introduce `line_re.sub(new_line, ...)` and every
    behavioural test above would still pass for tokens that happen to contain
    no backslash — which is every real GitHub PAT. The property being protected
    is 'the token is never parsed', and only the call shape expresses it.

    It has to read the CALL, not the source text. The first draft asserted
    `"lambda" in inspect.getsource(...)`, and this function's own comment block
    opens with ``# `lambda _: new_line`, NOT the string itself (#2017)`` — so
    the scan was satisfied by the prose and stayed green with the fix reverted,
    which is the one moment it exists for (the #2025 conflict resolution
    rewrites these same three lines). Same trap as the sibling guard in
    `test_2016_duplicate_pat_line.py::test_the_substitution_is_not_capped_at_one`,
    and the fourth instance of the class in `docs/memory/learnings.md`.
    """
    import ast
    import inspect
    import textwrap

    import services.github_pat_propagation_service as mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(mod._patch_env_github_pat)))
    subs = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "sub"
    ]
    assert subs, "no `.sub(...)` call found — has the patcher been rewritten?"
    for call in subs:
        assert call.args, "`.sub()` called with no replacement argument"
        repl = call.args[0]
        # A string replacement — literal, f-string, concatenation, or %-format
        # — is parsed by `re.sub` for escapes. A credential must never be.
        # `ast.Lambda` only — deliberately not "anything that isn't a literal".
        # The exact shape of the bug is `line_re.sub(new_line, out)`, whose
        # replacement is an `ast.Name`, and a Name is statically ambiguous: the
        # str variable that caused #2017 and a named callable are the same node.
        # An allow-list containing Name therefore passes the reverted bug —
        # my first draft did, and the mutation below caught it. If this is ever
        # rewritten to use a named callable, widen this assertion deliberately
        # and prove callability, rather than loosening it to Name.
        assert isinstance(repl, ast.Lambda), (
            "the .env line is being passed to re.sub as a replacement STRING "
            "again — a backslash in the token is then read as regex syntax "
            f"(#2017); expected a lambda, got {type(repl).__name__}"
        )
