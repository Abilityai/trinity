"""/edge-cases — boundary + property analysis of the Codex credential resolvers (#1971).

Targets `_parse_env_value`, `_load_api_key_with_source` and
`_has_subscription_auth`. All three are parsing/auth boundaries whose output now
decides two things it did not before #1971: whether `CODEX_API_KEY` is exported
into the CLI's environment (and therefore whether a subscription `auth.json`
survives), and whether dispatch is refused outright.

That promotion is what makes them worth this pass. `_parse_env_value` predates
#1971 and was only ever a convenience for hand-edited `.env` files; its result
now selects an auth *mode*.

Method: /edge-cases (BVA + equivalence partitioning + Hypothesis).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings, strategies as st  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_BASE_IMAGE = _REPO / "docker" / "base-image"
if str(_BASE_IMAGE) not in sys.path:
    sys.path.insert(0, str(_BASE_IMAGE))

try:
    from agent_server.services import codex_runtime as cr
except ImportError:  # pragma: no cover - agent-server deps required
    cr = None

pytestmark = pytest.mark.skipif(cr is None, reason="agent-server deps unavailable")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """No ambient key vars, and `.env` resolution pointed at a temp home."""
    for var in ("OPENAI_API_KEY", "CODEX_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cr, "_AGENT_HOME", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# _parse_env_value — the `.env` right-hand side.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("row", "raw", "expected"),
    [
        pytest.param(1, "sk-plain", "sk-plain", id="r1-plain"),
        pytest.param(2, "  sk-pad  ", "sk-pad", id="r2-outer-whitespace"),
        pytest.param(3, '"sk-q"', "sk-q", id="r3-double-quoted"),
        pytest.param(4, "'sk-s'", "sk-s", id="r4-single-quoted"),
        pytest.param(5, '"sk-a#b"', "sk-a#b", id="r5-hash-inside-quotes-kept"),
        pytest.param(6, "sk-x # note", "sk-x", id="r6-inline-comment-dropped"),
        pytest.param(7, "sk-x#y", "sk-x#y", id="r7-hash-without-space-is-value"),
        pytest.param(8, "", "", id="r8-empty"),
        pytest.param(9, "   ", "", id="r9-whitespace-only"),
        pytest.param(10, '"', "", id="r10-lone-double-quote"),
        pytest.param(11, '"unterminated', "unterminated", id="r11-unterminated-quote"),
        pytest.param(12, '""', "", id="r12-empty-quoted"),
    ],
)
def test_parse_env_value_boundaries(row, raw, expected):
    """Rows 1-12. Row 5 vs 7 is the subtle pair: a `#` is only a comment when
    preceded by whitespace, and quoting protects it either way — an API key
    containing `#` must not be silently truncated into a different key."""
    assert cr._parse_env_value(raw) == expected


# ---------------------------------------------------------------------------
# _load_api_key_with_source — precedence.
# ---------------------------------------------------------------------------


def test_process_env_prefers_openai_over_codex(_isolate, monkeypatch):
    """Row 13. In the process env the order is `_API_KEY_VARS`, so the
    ecosystem-standard name wins and `CODEX_API_KEY` is NOT exported."""
    monkeypatch.setenv("CODEX_API_KEY", "ck")
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    assert cr._load_api_key_with_source() == ("sk", "OPENAI_API_KEY")


def test_dotenv_resolution_is_by_line_order_not_var_priority(_isolate):
    """Row 14 — UNSPECIFIED, documented rather than asserted as correct.

    The `.env` scan returns the FIRST matching line, so a file listing
    `CODEX_API_KEY` above `OPENAI_API_KEY` resolves to the former — the opposite
    of the process-env branch above. Pre-existing, and harmless while the source
    only chose a *value*; after #1971 it also decides whether `CODEX_API_KEY` is
    exported, i.e. whether a subscription `auth.json` is discarded.

    Defensible either way (the operator did write that name), so this test
    pins current behaviour rather than claiming a bug. If it should change, the
    fix is to scan by `_API_KEY_VARS` priority in both branches.
    """
    (_isolate / ".env").write_text("CODEX_API_KEY=ck-first\nOPENAI_API_KEY=sk-second\n")
    assert cr._load_api_key_with_source() == ("ck-first", "CODEX_API_KEY")

    (_isolate / ".env").write_text("OPENAI_API_KEY=sk-first\nCODEX_API_KEY=ck-second\n")
    assert cr._load_api_key_with_source() == ("sk-first", "OPENAI_API_KEY")


@pytest.mark.parametrize(
    ("row", "content"),
    [
        pytest.param(15, "", id="r15-empty-file"),
        pytest.param(16, "\n\n\n", id="r16-blank-lines"),
        pytest.param(17, "# OPENAI_API_KEY=sk-commented\n", id="r17-commented-out"),
        pytest.param(18, "OPENAI_API_KEY\n", id="r18-no-equals"),
        pytest.param(19, "OPENAI_API_KEY=\n", id="r19-empty-value"),
        pytest.param(20, "OPENAI_API_KEY=   \n", id="r20-whitespace-value"),
        pytest.param(21, "SOMETHING_ELSE=sk-x\n", id="r21-unrelated-key"),
        pytest.param(22, "OPENAI_API_KEY_EXTRA=sk-x\n", id="r22-prefix-not-exact"),
    ],
)
def test_no_key_resolves_from_these(row, content, _isolate):
    """Rows 15-22. Row 19/20 matter most: an empty assignment must NOT resolve
    to `""` — a blank key would satisfy the dispatch gate and then fail at the
    CLI with a far less obvious error."""
    (_isolate / ".env").write_text(content)
    assert cr._load_api_key_with_source() == (None, None)


def test_an_empty_line_does_not_stop_the_scan(_isolate):
    """Row 23: a blank `OPENAI_API_KEY=` before a real `CODEX_API_KEY=` must not
    shadow it — the loop continues on a falsy value rather than returning."""
    (_isolate / ".env").write_text("OPENAI_API_KEY=\nCODEX_API_KEY=ck-real\n")
    assert cr._load_api_key_with_source() == ("ck-real", "CODEX_API_KEY")


def test_export_prefix_is_tolerated(_isolate):
    """Row 24: a hand-edited `.env` often carries `export`."""
    (_isolate / ".env").write_text("export CODEX_API_KEY=ck-e\n")
    assert cr._load_api_key_with_source() == ("ck-e", "CODEX_API_KEY")


def test_missing_env_file_is_not_an_error(_isolate):
    """Row 25: the subscription-only container has no `.env` at all."""
    assert cr._load_api_key_with_source() == (None, None)


def test_unreadable_env_file_is_not_an_error(_isolate):
    """Row 26: a directory where `.env` should be — must degrade, not raise,
    since this runs on the dispatch path."""
    (_isolate / ".env").mkdir()
    assert cr._load_api_key_with_source() == (None, None)


def test_a_non_utf8_env_file_does_not_crash_the_dispatch(_isolate):
    """Row 26b — regression guard for a PRE-EXISTING bug this analysis found.

    `.env` is hand-edited and credentials get pasted, so a single non-UTF-8 byte
    (a Latin-1 value, a smart quote from the wrong encoding) is entirely
    plausible. `read_text()` raised `UnicodeDecodeError` on it — which is a
    **ValueError, not an OSError**, so the `except (IOError, OSError)` around
    this loop never caught it. The error escaped `_load_openai_api_key`, escaped
    `_execute_codex`, and failed the dispatch outright instead of either
    resolving the key or returning the honest 503.

    Predates #1971, but that change made this resolver decide the auth *gate*
    too, so its failure mode got louder.
    """
    (_isolate / ".env").write_bytes(b"OPENAI_API_KEY=sk-caf\xe9\n")
    value, source = cr._load_api_key_with_source()  # must not raise
    assert source == "OPENAI_API_KEY"
    assert value.startswith("sk-caf")


def test_a_valid_key_survives_a_mangled_line_elsewhere(_isolate):
    """The reason for `errors="replace"` over swallowing the whole file: one bad
    byte on an unrelated line must not hide a perfectly good key."""
    (_isolate / ".env").write_bytes(b"JUNK=\xff\xfe\nCODEX_API_KEY=ck-good\n")
    assert cr._load_api_key_with_source() == ("ck-good", "CODEX_API_KEY")


# ---------------------------------------------------------------------------
# _has_subscription_auth.
# ---------------------------------------------------------------------------


def test_whitespace_only_auth_json_counts_as_present(_isolate):
    """Row 27 — deliberate, and the boundary of "non-empty".

    A 1-byte whitespace file passes, because the check is byte-length not
    content. That is consistent with the stated contract (validating the token
    is the CLI's job) but it does mean "non-empty" is weaker than "usable". The
    alternative — parsing it — is the content validation this function
    explicitly refuses to do, so the CLI's own error is the right reporter.
    """
    (_isolate / "auth.json").write_text(" ")
    assert cr._has_subscription_auth(str(_isolate)) is True


@pytest.mark.parametrize(
    ("row", "setup"),
    [
        pytest.param(28, "absent", id="r28-absent"),
        pytest.param(29, "empty", id="r29-zero-bytes"),
        pytest.param(30, "dir", id="r30-directory"),
        pytest.param(31, "broken-symlink", id="r31-broken-symlink"),
        pytest.param(32, "missing-home", id="r32-missing-codex-home"),
    ],
)
def test_not_subscription(row, setup, _isolate):
    """Rows 28-32. Each is a shape that must NOT be mistaken for a credential —
    a false positive here converts a crisp 503 into an opaque CLI failure."""
    home = _isolate
    if setup == "empty":
        (home / "auth.json").write_text("")
    elif setup == "dir":
        (home / "auth.json").mkdir()
    elif setup == "broken-symlink":
        (home / "auth.json").symlink_to(home / "does-not-exist")
    elif setup == "missing-home":
        home = home / "no-such-dir"
    assert cr._has_subscription_auth(str(home)) is False


# ---------------------------------------------------------------------------
# Properties.
# ---------------------------------------------------------------------------


@given(raw=st.text(max_size=200))
@settings(max_examples=400, deadline=None)
def test_parse_env_value_is_total_and_returns_str(raw):
    """Runs on the dispatch path against a hand-editable file; a raise here
    fails the turn over a formatting quirk."""
    assert isinstance(cr._parse_env_value(raw), str)


@given(raw=st.text(max_size=200))
@settings(max_examples=400, deadline=None)
def test_parse_env_value_never_grows_its_input(raw):
    """A parser that lengthens its input is doing something other than
    parsing — cheap oracle against accidental quoting/escaping."""
    assert len(cr._parse_env_value(raw)) <= len(raw)


# The two properties below build their own temp dir PER EXAMPLE rather than
# taking the `_isolate` fixture: pytest fixtures are function-scoped, so under
# `@given` they are set up once and state leaks between examples — Hypothesis
# rejects that outright (FailedHealthCheck), and it is right to. Suppressing the
# health check would have kept the leak; this removes it.


@given(
    lines=st.lists(
        st.tuples(
            st.sampled_from(["OPENAI_API_KEY", "CODEX_API_KEY", "OTHER", "# comment"]),
            # Surrogates excluded: they cannot be written to a real file at all
            # (`write_text` raises), so a `.env` can never contain one. Hypothesis
            # found that shape and it is an unrealistic input — a wrong TEST, not
            # a bug. The realistic sibling (a file holding non-UTF-8 BYTES) is a
            # real bug and gets its own test above.
            st.text(
                alphabet=st.characters(
                    blacklist_characters="\n\r", blacklist_categories=("Cs",)
                ),
                max_size=30,
            ),
        ),
        max_size=10,
    )
)
@settings(max_examples=300, deadline=None)
def test_resolution_is_total_and_self_consistent(lines):
    """Whatever a `.env` contains, resolution must not raise, and a returned
    source must be one of the two recognised names paired with a non-empty
    value — a `(value, None)` or `(None, source)` pair would make the export
    decision incoherent.

    `monkeypatch` is function-scoped too, so the module global is saved and
    restored by hand rather than fixture-patched.
    """
    import tempfile

    original_home = cr._AGENT_HOME
    saved = {v: os.environ.pop(v, None) for v in ("OPENAI_API_KEY", "CODEX_API_KEY")}
    try:
        with tempfile.TemporaryDirectory() as home:
            cr._AGENT_HOME = home
            Path(home, ".env").write_text("\n".join(f"{k}={v}" for k, v in lines))
            value, source = cr._load_api_key_with_source()
    finally:
        cr._AGENT_HOME = original_home
        for var, was in saved.items():
            if was is not None:
                os.environ[var] = was

    assert (value is None) == (source is None)
    if source is not None:
        assert source in cr._API_KEY_VARS
        assert value  # never empty-string


@given(content=st.text(max_size=100))
@settings(max_examples=200, deadline=None)
def test_subscription_detection_is_total(content):
    """Arbitrary file content must yield a bool, never an exception — this
    gates dispatch."""
    import tempfile

    with tempfile.TemporaryDirectory() as home:
        Path(home, "auth.json").write_text(content)
        assert isinstance(cr._has_subscription_auth(home), bool)
