"""
`services/agent_env_patch` — set / remove keys over an agent's whole `.env`
text (trinity-enterprise#966, plan T4).

A second writer of the file the agent's own reader parses, so the quoting rule
is pinned against the #2023 corpus through BOTH the backend's existing writer
(`github_pat_propagation_service._format_pat_line`, untouched here — D3 folds
the two) and the real agent-side reader.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = str(_REPO / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

NVM = {
    "NVM_DELEGATION_ID": "dlg-0123456789abcdef",
    "NVM_API_KEY": "sandbox:akey-" + "Z" * 24,
    "NVM_ENVIRONMENT": "sandbox",
}


@pytest.fixture(scope="module")
def patch():
    from services import agent_env_patch

    return agent_env_patch


@pytest.fixture(scope="module")
def reader():
    path = _REPO / "docker" / "base-image" / "agent_server" / "services" / "execution_env.py"
    spec = importlib.util.spec_from_file_location("_env_reader_ent966", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _parsed(reader, text: str, tmp_path) -> dict:
    env = tmp_path / ".env"
    env.write_text(text)
    return reader.parse_env_file(env)


class TestSetKeys:
    def test_set_over_an_empty_file(self, patch, reader, tmp_path):
        out = patch.set_keys("", NVM)
        assert out == (
            'NVM_DELEGATION_ID="dlg-0123456789abcdef"\n'
            'NVM_API_KEY="sandbox:akey-' + "Z" * 24 + '"\n'
            'NVM_ENVIRONMENT="sandbox"\n'
        )
        assert _parsed(reader, out, tmp_path) == NVM

    def test_set_replaces_in_place_and_keeps_everything_else_byte_for_byte(self, patch, reader, tmp_path):
        original = (
            "# agent credentials\n"
            'GITHUB_PAT="ghp_a\\\\b"\n'
            "\n"
            "NVM_DELEGATION_ID=dlg-old\n"
            "  ANTHROPIC_API_KEY = spaced\r\n"
            "OTHER=keep me\n"
        )
        out = patch.set_keys(original, {"NVM_DELEGATION_ID": "dlg-new-0123456789"})
        assert out == (
            "# agent credentials\n"
            'GITHUB_PAT="ghp_a\\\\b"\n'
            "\n"
            'NVM_DELEGATION_ID="dlg-new-0123456789"\n'
            "  ANTHROPIC_API_KEY = spaced\r\n"
            "OTHER=keep me\n"
        )
        assert out.index("NVM_DELEGATION_ID") == original.index("NVM_DELEGATION_ID")
        assert _parsed(reader, out, tmp_path)["NVM_DELEGATION_ID"] == "dlg-new-0123456789"

    def test_pat_line_untouched_by_a_nevermined_set(self, patch, reader, tmp_path):
        """The rider's pinned test: an unrelated credential survives unchanged."""
        pat_line = 'GITHUB_PAT="ghp_a\\"b\\\\c"\n'
        out = patch.set_keys(pat_line + "GH_TOKEN=plain\n", NVM)
        assert out.startswith(pat_line + "GH_TOKEN=plain\n")
        parsed = _parsed(reader, out, tmp_path)
        assert parsed["GITHUB_PAT"] == 'ghp_a"b\\c' and parsed["GH_TOKEN"] == "plain"
        assert {k: parsed[k] for k in NVM} == NVM

    def test_a_key_present_twice_is_collapsed_to_one(self, patch):
        """The agent's reader is last-wins (#2016): a duplicate left behind would
        shadow the value just written. Collapse to the first position."""
        text = "A=1\nNVM_API_KEY=old1\nB=2\nNVM_API_KEY=old2\nC=3\n"
        out = patch.set_keys(text, {"NVM_API_KEY": "fresh"})
        assert out == 'A=1\nNVM_API_KEY="fresh"\nB=2\nC=3\n'

    def test_append_adds_a_newline_separator_when_the_file_lacks_one(self, patch):
        out = patch.set_keys("A=1", {"NVM_ENVIRONMENT": "live"})
        assert out == 'A=1\nNVM_ENVIRONMENT="live"\n'

    def test_idempotent_round_trip(self, patch, reader, tmp_path):
        once = patch.set_keys("A=1\nNVM_API_KEY=old\n", NVM)
        twice = patch.set_keys(once, NVM)
        assert once == twice
        assert patch.remove_keys(twice, NVM) == "A=1\n"
        # remove → set lands the keys again (appended, so in mapping order)
        again = patch.set_keys(patch.remove_keys(twice, NVM), NVM)
        assert _parsed(reader, again, tmp_path) == {"A": "1", **NVM}

    @pytest.mark.parametrize("bad", ["a\nB=x", "a\r\nB=x", "trailing\n", "\rcr"])
    def test_newline_in_a_value_is_rejected(self, patch, bad):
        with pytest.raises(ValueError, match="newline"):
            patch.set_keys("", {"NVM_API_KEY": bad})

    @pytest.mark.parametrize("position", ["middle", "trailing"])
    @pytest.mark.parametrize("boundary", ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"])
    def test_every_splitlines_boundary_is_rejected(self, patch, reader, boundary, position, tmp_path):
        """The agent reader splits `.env` with `str.splitlines()`, so the writer
        refuses every boundary that splitter honours, not only `\\n`/`\\r`."""
        bad = "a" + boundary + "NVM_ENVIRONMENT=live" if position == "middle" else "abc" + boundary
        line = 'NVM_API_KEY="' + bad + '"\n'
        assert len(line.splitlines()) > 1  # what the reader would see if written
        with pytest.raises(ValueError, match="newline"):
            patch.set_keys("", {"NVM_API_KEY": bad})

    @pytest.mark.parametrize("bad", ["NVM\nKEY", "NVM KEY", "1NVM", "", "NVM=KEY"])
    def test_a_malformed_key_is_rejected(self, patch, bad):
        with pytest.raises(ValueError, match="key"):
            patch.set_keys("", {bad: "v"})

    def test_a_value_carrying_a_group_reference_is_inserted_verbatim(self, patch, reader, tmp_path):
        """#2017: `\\g<1>` in a credential must never be read as regex syntax."""
        out = patch.set_keys("NVM_API_KEY=old\n", {"NVM_API_KEY": r"k\g<1>\1\s"})
        assert _parsed(reader, out, tmp_path)["NVM_API_KEY"] == r"k\g<1>\1\s"


class TestRemoveKeys:
    def test_remove_only_named_keys(self, patch):
        text = (
            "A=1\n"
            'NVM_DELEGATION_ID="x"\n'
            "NVM_API_KEY=k\n"
            "NVM_API_KEY=k2\n"
            "NVM_ENVIRONMENT=sandbox\n"
            "NVM_API_KEY_BACKUP=keep\n"
            "B=2"
        )
        out = patch.remove_keys(text, ["NVM_DELEGATION_ID", "NVM_API_KEY"])
        assert out == "A=1\nNVM_ENVIRONMENT=sandbox\nNVM_API_KEY_BACKUP=keep\nB=2"

    def test_remove_absent_key_is_a_no_op(self, patch):
        assert patch.remove_keys("A=1\n", ["NVM_API_KEY"]) == "A=1\n"
        assert patch.remove_keys("", ["NVM_API_KEY"]) == ""


class TestQuotingParity:
    """One rule, two writers, one reader (#2023)."""

    CORPUS = [
        "plain",
        'a"b',
        r"a\b",
        r"a\\b",
        r"a\\\\b",
        r"trailing\\",
        r'\"already_escaped',
        "$(id)`whoami`",
        "with spaces and = signs",
    ]

    @pytest.mark.parametrize("value", CORPUS)
    def test_matches_the_pat_writer(self, patch, value):
        from services.github_pat_propagation_service import _format_pat_line

        assert patch.format_env_line("NVM_API_KEY", value) == _format_pat_line(value, "NVM_API_KEY")

    @pytest.mark.parametrize("value", CORPUS)
    def test_round_trips_through_the_real_reader(self, patch, reader, value, tmp_path):
        out = patch.set_keys("", {"NVM_API_KEY": value})
        assert _parsed(reader, out, tmp_path)["NVM_API_KEY"] == value
