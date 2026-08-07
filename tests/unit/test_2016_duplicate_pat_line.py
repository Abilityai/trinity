"""#2016 — a duplicated `GITHUB_PAT` line must not survive a rotation.

`_patch_env_github_pat` replaced the FIRST matching line (`count=1`) while the
agent's own `.env` reader is **last-wins**. So on a file carrying two
`GITHUB_PAT=` lines the rotation wrote the new token to line 1, the revoked
token survived below it, and the agent went on authenticating with the revoked
one — while `propagate_pat_to_all_agents` reported that agent as `updated`.

Same silent-success failure #1967 exists to close, reached by a different
route: there, the rotation never touched the agent; here, it touches it and
the agent ignores the result.

Every assertion below goes through **what the agent reads**, not through what
the file contains. That distinction is the whole bug — the file did contain the
new token, on a line nothing read.
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

OLD = "ghp_" + "o" * 36
NEW = "ghp_" + "n" * 36
KEYS = ("GITHUB_PAT", "GH_TOKEN", "GITHUB_TOKEN")


@pytest.fixture(scope="module")
def svc():
    try:
        import services.github_pat_propagation_service as m
    except ImportError:  # pragma: no cover — backend venv required
        pytest.skip("backend venv required")
    return m


def agent_reads(env_content: str) -> dict:
    """The agent-server `.env` reader, copied — last-wins is the point.

    Byte-faithful to `docker/base-image/agent_server/routers/credentials.py`.
    A copy rather than an import because the agent server ships in its own
    image; small enough to audit against the original by eye.
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


class TestTheReportedBug:

    def test_the_agent_reads_the_new_token_despite_a_duplicate(self, svc):
        """The reproduction from the issue."""
        env = f'GITHUB_PAT="{OLD}"\nSOMETHING=1\nGITHUB_PAT="{OLD}"\n'
        patched = svc._patch_env_github_pat(env, NEW)
        assert agent_reads(patched)["GITHUB_PAT"] == NEW

    def test_the_revoked_token_survives_nowhere(self, svc):
        """AC #2, and the stronger claim: even a line nothing parses must not
        keep a revoked credential, because `.env` is also read by shells,
        greps and humans."""
        env = f'GITHUB_PAT="{OLD}"\nX=1\nGITHUB_PAT="{OLD}"\nGH_TOKEN="{OLD}"\n'
        patched = svc._patch_env_github_pat(env, NEW)
        assert OLD not in patched

    @pytest.mark.parametrize("copies", [2, 3, 5])
    def test_any_number_of_duplicates_is_levelled(self, svc, copies):
        env = "".join(f'GITHUB_PAT="{OLD}"\n' for _ in range(copies))
        patched = svc._patch_env_github_pat(env, NEW)
        assert agent_reads(patched)["GITHUB_PAT"] == NEW
        assert patched.count(NEW) >= copies

    @pytest.mark.parametrize("key", KEYS)
    def test_each_mirrored_key_is_levelled_independently(self, svc, key):
        """#1574 mirrors the token into GH_TOKEN/GITHUB_TOKEN, and each key runs
        its own substitution — a `count=1` left on any one of them reopens the
        bug for that key alone."""
        env = f'{key}="{OLD}"\nFILLER=1\n{key}="{OLD}"\n'
        patched = svc._patch_env_github_pat(env, NEW)
        assert agent_reads(patched)[key] == NEW

    def test_duplicates_of_different_keys_do_not_interfere(self, svc):
        env = (f'GITHUB_PAT="{OLD}"\nGH_TOKEN="{OLD}"\n'
               f'GITHUB_PAT="{OLD}"\nGITHUB_TOKEN="{OLD}"\nGH_TOKEN="{OLD}"\n')
        seen = agent_reads(svc._patch_env_github_pat(env, NEW))
        assert all(seen[k] == NEW for k in KEYS)


class TestTheDuplicateShapesThatActuallyOccur:
    """The duplicate is not created by this function — it arrives from an agent
    editing its own `.env` (#1999), an SSH/`docker exec` append, or a restored
    file. Those produce inconsistent formatting, not clean copies."""

    @pytest.mark.parametrize("second", [
        pytest.param('  GITHUB_PAT="{old}"', id="indented"),
        pytest.param('\tGITHUB_PAT="{old}"', id="tabbed"),
        pytest.param("GITHUB_PAT={old}", id="unquoted"),
        pytest.param("GITHUB_PAT='{old}'", id="single-quoted"),
        pytest.param("GITHUB_PAT=", id="empty-value"),
    ])
    def test_a_differently_formatted_duplicate_is_still_replaced(self, svc, second):
        env = f'GITHUB_PAT="{OLD}"\n' + second.format(old=OLD) + "\n"
        patched = svc._patch_env_github_pat(env, NEW)
        assert agent_reads(patched)["GITHUB_PAT"] == NEW
        assert OLD not in patched

    def test_a_commented_duplicate_is_left_alone(self, svc):
        """`# GITHUB_PAT=...` is not a binding — the reader skips it, so the
        regex must not either. Levelling it would rewrite an operator's note,
        and it cannot affect what the agent sees."""
        env = f'GITHUB_PAT="{OLD}"\n# GITHUB_PAT="a-note-about-{OLD}"\n'
        patched = svc._patch_env_github_pat(env, NEW)
        assert f"# GITHUB_PAT=\"a-note-about-{OLD}\"" in patched
        assert agent_reads(patched)["GITHUB_PAT"] == NEW

    def test_a_lookalike_key_is_never_touched(self, svc):
        env = f'GITHUB_PAT="{OLD}"\nMY_GITHUB_PAT="{OLD}"\nGITHUB_PATX="{OLD}"\n'
        seen = agent_reads(svc._patch_env_github_pat(env, NEW))
        assert seen["GITHUB_PAT"] == NEW
        assert seen["MY_GITHUB_PAT"] == OLD and seen["GITHUB_PATX"] == OLD


class TestTheSingleLineCaseIsUnchanged:
    """Levelling must not perturb the normal path."""

    def test_one_line_is_replaced_once(self, svc):
        patched = svc._patch_env_github_pat(f'GITHUB_PAT="{OLD}"\n', NEW)
        assert patched.count("GITHUB_PAT=") == 1
        assert agent_reads(patched)["GITHUB_PAT"] == NEW

    def test_absent_key_is_still_appended(self, svc):
        patched = svc._patch_env_github_pat("FOO=1\n", NEW)
        assert agent_reads(patched)["GITHUB_PAT"] == NEW
        assert agent_reads(patched)["FOO"] == "1"

    def test_rotation_is_idempotent(self, svc):
        once = svc._patch_env_github_pat(f'GITHUB_PAT="{OLD}"\nX=1\n', NEW)
        assert svc._patch_env_github_pat(once, NEW) == once

    def test_unrelated_lines_survive(self, svc):
        env = f'A=1\nGITHUB_PAT="{OLD}"\nB=2\nGITHUB_PAT="{OLD}"\nC=3\n'
        seen = agent_reads(svc._patch_env_github_pat(env, NEW))
        assert (seen["A"], seen["B"], seen["C"]) == ("1", "2", "3")


def test_the_substitution_is_not_capped_at_one():
    """Pins the mechanism, via the AST rather than the text.

    Every behavioural test above passes on a single-line `.env`, which is what
    almost every agent has — so a `count=1` reintroduced during a refactor
    would only show up on the duplicate case. Hence a structural assertion too.

    It has to read the CALL, not the source text: this function's own docstring
    explains the bug and therefore contains the string `count=1`, so a textual
    scan passes on the prose with the cap restored. My first draft did exactly
    that and failed here — the same trap `docs/memory/learnings.md` records for
    the #1871 `containers_run` guard and ent#314's loader sweep, and that
    ent#237's own auth guard hit last week when `ast.dump` rendered a docstring.
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
        caps = [kw for kw in call.keywords if kw.arg == "count"]
        assert not caps, (
            "the .env substitution is capped again — a duplicated line then "
            "keeps the revoked token under last-wins parsing (#2016)"
        )
        assert len(call.args) <= 2, (
            "a positional third argument to re.sub is `count` — same cap, "
            "written differently (#2016)"
        )
