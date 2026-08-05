"""#2023 — a credential must survive the `.env` round trip unchanged.

The writer escaped an embedded double quote; the reader never unescaped it. So
any credential containing `"` was injected as one string and read back as
another — `a"b` written as `KEY="a\\"b"` and read as `a\\"b` — and the agent
authenticated with a value the operator never supplied. The failure presents as
a bad credential, which is the worst possible disguise for a parsing bug.

Split out of #2017 (which fixed the unrelated `re.error`) because it is not a
PAT problem: GitHub PATs are `[A-Za-z0-9_]`, so a quote cannot appear in one.
It affects every credential written through this path.

The fix has to move four things at once, which is why it waited for #2010:

  * the writer (`routers/credentials.py`) — now escapes the escape character
    first, so `\\\\`/`\\"` is an unambiguous encoding;
  * the reader (`execution_env.unquote_env_value`) — strips ONE matched pair
    and reverses that encoding in a single scan;
  * `credential_requirements_service._env_pairs` — the ent#127 predicate is
    *defined* as agreement with the reader, and its SOURCE is spliced into an
    in-container probe, so it carries an inlined mirror rather than an import;
  * `test_ent127_predicate.py` — whose parity fixtures now run against the real
    reader instead of a hand-written replica.

This file is the round trip itself: writer output in, agent-visible value out.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BACKEND_STR = str(_REPO / "src" / "backend")
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

_READER = (
    _REPO / "docker" / "base-image" / "agent_server" / "services" / "execution_env.py"
)

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def reader():
    spec = importlib.util.spec_from_file_location("_env_reader_2023", _READER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def write_line(reader, key: str, value: str) -> str:
    """The REAL writer, not a copy of it.

    An earlier version of this helper reimplemented the escaping here. Mutation
    testing then showed the hole: deleting the backslash-escaping from the
    actual writer broke none of these tests, because they were exercising the
    copy. That is the same "second copy is unverified" failure this PR's own
    parity work is about, so the encode half was extracted into
    `execution_env.format_env_line` — beside its inverse — and is called here.
    """
    return reader.format_env_line(key, value) + "\n"


# Values a real credential can contain. The quote and backslash cases are the
# defect; the rest guard against a fix that breaks ordinary secrets.
VALUES = [
    pytest.param('a"b', id="embedded-double-quote"),
    pytest.param('"leading-and-trailing"', id="wrapped-in-quotes"),
    pytest.param('say "hello" twice', id="quoted-phrase"),
    pytest.param("back\\slash", id="backslash"),
    pytest.param("trailing\\", id="trailing-backslash"),
    pytest.param("double\\\\slash", id="double-backslash"),
    pytest.param('mixed\\"both', id="backslash-then-quote"),
    pytest.param("it's", id="apostrophe"),
    pytest.param("'single'", id="wrapped-in-single-quotes"),
    pytest.param("plain-token-12345", id="ordinary"),
    pytest.param("", id="empty"),
    pytest.param("  padded  ", id="internal-padding"),
    pytest.param("sk-proj-AbCd/1234+xyz=", id="base64-ish"),
    pytest.param("pa$$w0rd!#%^&*()", id="shell-metachars"),
    pytest.param("café ✓ 日本語", id="non-ascii"),
]


class TestRoundTrip:

    @pytest.mark.parametrize("value", VALUES)
    def test_the_agent_reads_back_what_was_written(self, reader, tmp_path, value):
        env_file = tmp_path / ".env"
        env_file.write_text(write_line(reader, "SECRET", value))

        assert reader.parse_env_file(env_file)["SECRET"] == value

    @pytest.mark.parametrize("value", VALUES)
    def test_the_ent127_predicate_agrees_with_the_agent(self, reader, tmp_path, value):
        """The predicate is *defined* as agreement with the reader, so it has to
        round-trip identically — including for the values that used to corrupt."""
        from services import credential_requirements_service as crs

        line = write_line(reader, "SECRET", value)
        env_file = tmp_path / ".env"
        env_file.write_text(line)

        assert crs._env_pairs(line.splitlines()) == reader.parse_env_file(env_file)

    def test_several_values_in_one_file(self, reader, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text(
            write_line(reader, "A", 'has"quote')
            + write_line(reader, "B", "has\\slash")
            + write_line(reader, "C", "ordinary")
        )

        got = reader.parse_env_file(env_file)

        assert got == {"A": 'has"quote', "B": "has\\slash", "C": "ordinary"}

    def test_a_quote_no_longer_terminates_the_line_early(self, reader, tmp_path):
        """The concrete corruption: the value's own quote used to end the line,
        so everything after it was lost or misread."""
        env_file = tmp_path / ".env"
        env_file.write_text(write_line(reader, "SECRET", 'abc"def') + "NEXT=survives\n")

        got = reader.parse_env_file(env_file)

        assert got["SECRET"] == 'abc"def'
        assert got["NEXT"] == "survives"


class TestTheEncodingIsUnambiguous:

    def test_a_trailing_backslash_does_not_swallow_the_closing_quote(self, reader):
        """Escaping only `\"` left `KEY="a\\"` — the closing quote reads as
        escaped and the line never terminates. Escaping the escape first is
        what makes the encoding decodable at all."""
        assert write_line(reader, "K", "a\\") == 'K="a\\\\"\n'

    @pytest.mark.parametrize("value,expected", [
        ('a"b', 'K="a\\"b"'),
        ("a\\b", 'K="a\\\\b"'),
        ('a\\"b', 'K="a\\\\\\"b"'),
        ("plain", 'K="plain"'),
    ])
    def test_the_encoding_itself_is_pinned(self, reader, value, expected):
        """Assert the LINE, not just that it round-trips.

        Found by mutation testing: deleting the quote-escaping from the writer
        left every round-trip test passing, because `unquote_env_value` strips
        the outermost pair positionally and does not scan for a closing quote —
        `K="a"b"` decodes back to `a"b` regardless.

        The escaping still matters, and this is what says so: `.env` is not read
        only by this parser. A shell sourcing the file, or anything that scans
        for the terminating quote, sees `K="a"b"` as `ab`. Round-trip fidelity
        through our own reader is necessary but not sufficient.
        """
        assert reader.format_env_line("K", value) == expected

    def test_the_escape_pair_is_reversed_in_one_pass(self, reader, tmp_path):
        """Two sequential replaces would mis-handle an escaped backslash that
        precedes an escaped quote."""
        env_file = tmp_path / ".env"
        env_file.write_text(write_line(reader, "K", '\\"'))
        assert reader.parse_env_file(env_file)["K"] == '\\"'


class TestUnquoteDirectly:

    @pytest.mark.parametrize("raw,expected", [
        ('"plain"', "plain"),
        ("'single'", "single"),
        ("bare", "bare"),
        ('""', ""),
        ('"a\\"b"', 'a"b'),
        ('"a\\\\b"', "a\\b"),
        ('"', '"'),
        ("'", "'"),
        ('"unterminated', '"unterminated'),
        ("'\"v\"'", '"v"'),          # single-quoted: taken literally
        ('"\\n not a newline"', "\\n not a newline"),   # only \" and \\ are escapes
    ])
    def test_shapes(self, reader, raw, expected):
        assert reader.unquote_env_value(raw) == expected


def test_the_probe_script_still_compiles_with_the_inlined_mirror():
    """`_env_pairs`'s SOURCE is spliced into an in-container script, so it must
    stay self-contained — an import or a module-global reference would
    NameError inside the probe, at a point where the failure surfaces as an
    unreadable credential report rather than as an ImportError here."""
    from services import credential_requirements_service as crs

    compile(crs._build_collector_script("/tmp/probe-root"), "<probe>", "exec")
