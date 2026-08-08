"""#2052 — the free-text PAT scrubbers only fired on a literal `https://`.

Follow-up to the ent#347 / ent#334 pair. ent#347 collapsed two drifted
hand-written scrubbers into one pattern; ent#334 added the parse-based
single-URL `strip_url_credentials`. The cross-check when ent#334 landed found
they agree on *where* the authority ends — over the double-`@`, triple-`@`,
port, IDN and userinfo-with-no-host shapes — but disagree on *when* to fire:

    _CREDENTIAL_URL_RE = re.compile(r"https://[^\\s/?#]+@")

is anchored on a literal `https://`, so three shapes the parser handles slipped
past it and were echoed VERBATIM:

    //tok@github.com/o/r         protocol-relative
    tok@github.com/o/r           scheme-less shorthand
    git+ssh://tok@github.com/o/r alternate scheme

Reachable, not theoretical: `_adopt_legacy_clone` writes source rows with no
validation at all and `validate_skills_library_url` accepts the scheme-less
shorthand, so a stored URL is legitimately allowed to be scheme-less or
protocol-relative; `_scrub_pat` then runs over `str(e)` of an ARBITRARY
exception, whose text is not constrained to what `_authenticated_url` produced.
The sink is `system_settings['skills_library_last_error']` — durable and
admin-rendered.

The fix shares the authority grammar between the two rather than adding a
fourth hand-written pattern, so the tests here are mostly PARITY assertions:
the property that must hold forever is "the regex and the parser resolve the
same authority", not any one output string.
"""

from __future__ import annotations

import pytest

from services.skill_source_clone import redact
from utils.url_validation import (
    scrub_url_credentials_in_text,
    strip_url_credentials,
)


# ---------------------------------------------------------------------------
# The defect: three shapes that leaked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("//tok@github.com/o/r", "//***@github.com/o/r"),
        ("tok@github.com/o/r", "***@github.com/o/r"),
        ("git+ssh://tok@github.com/o/r", "git+ssh://***@github.com/o/r"),
    ],
    ids=["protocol-relative", "scheme-less", "alternate-scheme"],
)
def test_non_https_authorities_are_scrubbed(url, expected):
    """Each of these returned the input UNCHANGED before the fix."""
    out = redact(url)
    assert "tok" not in out
    assert out == expected


def test_scheme_less_url_inside_an_arbitrary_exception_string():
    """The reported path, end to end: `_scrub_pat` over `str(e)` for an
    exception whose text carries a stored scheme-less URL. Asserted as an
    absence — the property is 'the token is not in the durable string'."""
    from services.skill_service import _scrub_pat

    persisted = _scrub_pat(str(RuntimeError("clone failed for ghp_TOK@github.com/o/r")))
    assert "ghp_TOK" not in persisted
    assert "***@github.com/o/r" in persisted


def test_protocol_relative_double_at_loses_every_segment():
    """The two fixes have to compose: the missing-scheme gap and ent#347's
    last-`@`-wins rule apply to the same URL at once."""
    out = redact("//ghp_PLATFORM@ghp_LEGACY@github.com/o/r")
    assert "ghp_PLATFORM" not in out
    assert "ghp_LEGACY" not in out
    assert out == "//***@github.com/o/r"


# ---------------------------------------------------------------------------
# Parity: the regex and the parser must resolve the SAME authority
# ---------------------------------------------------------------------------

# One corpus, driven through both entry points. It carries the shapes the
# ent#334 cross-check already covered PLUS the three that regressed, so a future
# edit to either side that re-opens the gap fails here rather than by leaking a
# token into `system_settings`.
_CORPUS = [
    # the three #2052 shapes
    "//tok@github.com/o/r",
    "tok@github.com/o/r",
    "git+ssh://tok@github.com/o/r",
    # ent#347 shapes
    "https://tok@github.com/o/r",
    "https://a@b@github.com/o/r",
    "https://x@y@z@github.com/o/r",
    # empty username — falsy to `parsed.username`, still an authority `@`
    "https://@github.com/o/r",
    # port, IDN, userinfo with no host
    "https://tok@github.com:8443/o/r",
    "https://tok@xn--e1afmkfd.xn--p1ai/o/r",
    "https://tok@/x",
    # scp-like git remote
    "git@github.com:o/r.git",
    # must NOT be touched — `@` that is data, not userinfo
    "https://github.com/o/r?ref=a@b",
    "https://github.com?x=a@b",
    "https://github.com/o/r/refs/heads/user@example",
    "https://github.com/o/r",
    # degenerate
    "",
    "https://",
    "no urls here at all",
]


@pytest.mark.parametrize("url", _CORPUS)
def test_regex_and_parser_agree_on_the_authority(url):
    """The load-bearing assertion of this issue.

    The two return DIFFERENT strings by design — the parser DROPS userinfo for
    display, the free-text scrub leaves a `***@` marker so an operator can see a
    credential was redacted. Removing that one marker must reproduce the
    parser's answer exactly; that is what "they agree on the boundary" means as
    an executable statement.
    """
    assert redact(url).replace("***@", "", 1) == strip_url_credentials(url)


@pytest.mark.parametrize("url", _CORPUS)
def test_they_agree_on_whether_a_credential_is_present_at_all(url):
    """The *when to fire* half — the actual #2052 defect. Stated separately
    because the string-equality test above could in principle be satisfied by
    both sides being wrong in the same direction."""
    assert (redact(url) != url) == (strip_url_credentials(url) != url)


@pytest.mark.parametrize("url", _CORPUS)
def test_no_userinfo_survives_a_scrub(url):
    """Whatever the output shape, nothing before the authority's last `@` may
    remain. Checked against the parser's own idea of the host rather than a
    hand-typed expectation."""
    out = redact(url)
    if strip_url_credentials(url) != url:
        assert "tok" not in out and "ghp_" not in out


def test_redact_delegates_rather_than_carrying_its_own_pattern():
    """The structural half of the fix. ent#347 removed one duplicate pattern;
    #2052 removed the second. A future contributor re-introducing a local
    pattern in `skill_source_clone` fails here."""
    import services.skill_source_clone as clone_mod

    assert not hasattr(clone_mod, "_CREDENTIAL_URL_RE")
    assert redact("//tok@h/x") == scrub_url_credentials_in_text("//tok@h/x")


# ---------------------------------------------------------------------------
# What must NOT regress
# ---------------------------------------------------------------------------


def test_query_at_is_still_not_a_credential_without_a_scheme_anchor():
    """The anchor that was removed was also what kept the pattern away from a
    query `@`. Dropping it naively makes `?ref=a@b` match as bare userinfo and
    mangles a legitimate URL, so this is the case the boundary lookbehind
    exists for — not an incidental regression check."""
    url = "https://github.com/o/r?ref=a@b"
    assert redact(url) == url


def test_multi_url_free_text_still_handled():
    """The reason the free-text side stays regex-oriented instead of 'just
    calling the parser': git stderr is prose plus several URLs, so there is no
    single URL to hand `urlparse`."""
    text = (
        "Cloning into '/data/skills-library'...\n"
        "remote: https://github.com/o/plain\n"
        "fatal: could not read Username for '//a@b@github.com/o/two'\n"
        "fatal: also tried ghp_THREE@github.com/o/three\n"
    )
    out = redact(text)
    for token in ("a@b", "ghp_THREE"):
        assert token not in out
    assert "https://github.com/o/plain" in out
    assert "//***@github.com/o/two" in out
    assert "***@github.com/o/three" in out


def test_scrub_is_idempotent():
    """`redact` output is re-scrubbed on some paths (logged, then persisted).
    A second pass must not eat the `***` marker or the host."""
    once = redact("//ghp_TOK@github.com/o/r")
    assert redact(once) == once


@pytest.mark.parametrize("value", ["", None, "@@@", "//", "https://", "a@"])
def test_never_raises(value):
    """Contract inherited from both sides: the caller is a persistence path, so
    a throw would replace a scrubbed error with an unhandled one."""
    redact(value)
    scrub_url_credentials_in_text(value)


def test_scan_is_linear_on_a_long_run_with_no_at():
    """Guards a real regression found while fixing #2052, not a hypothetical.

    Dropping the `https://` anchor is what closes the leak — but that anchor was
    also the literal `str.find` could jump to. The first fix expressed the new
    rule as a pattern starting with a character class, which has nothing to skip
    to, so a long run containing no `@` was rescanned from every offset:
    200 KB took 24 SECONDS and 500 KB took 151 on the machine this was written
    on. `redact` is called on RAW `git stderr`, which is unbounded and partly
    remote-controlled (a branch or path name the remote echoes back), so that is
    a denial-of-service reachable from a hostile repository.

    The bound is deliberately loose — three orders of magnitude above the ~1 ms
    the linear scan actually takes — so it fails only on a return to quadratic
    behaviour and never flakes on a loaded CI box.
    """
    import time

    blob = "a" * 500_000  # one run, no `@`: the worst case for a class-first scan
    started = time.perf_counter()
    out = scrub_url_credentials_in_text(blob)
    elapsed = time.perf_counter() - started

    assert out == blob, "a run with no `@` must be returned untouched"
    assert elapsed < 2.0, f"scrub went superlinear: {elapsed:.1f}s on 500 KB"


def test_long_run_does_not_hide_a_credential_after_it():
    """The linear rewrite must not gain speed by giving up coverage: a token
    following a large blob of prose is still scrubbed."""
    out = redact("x" * 200_000 + "\nfatal: //ghp_TOK@github.com/o/r")
    assert "ghp_TOK" not in out
    assert out.endswith("//***@github.com/o/r")
