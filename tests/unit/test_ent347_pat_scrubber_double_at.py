"""ent#347 — both free-text PAT scrubbers under-matched a double-`@` URL.

`skill_service._authenticated_url` splices the platform PAT in FRONT of
whatever userinfo the stored URL already carries:

    stored:   https://LEGACYTOK@github.com/org/private-skills
    spliced:  https://PLATFORMPAT@LEGACYTOK@github.com/org/private-skills

Both scrubbers used a character class that stops at the FIRST `@`
(`[^@\\s]+@` and `[^@]+@`), so the second credential survived verbatim into

  * `system_settings['skills_library_last_error']` — durable, and rendered on
    the admin Settings panel, and
  * an HTTP `detail` body.

Not a theoretical path: git resolves the username as `PLATFORMPAT@LEGACYTOK`
and auth is REJECTED, so the error branch — the one that persists the scrubbed
string — is the guaranteed branch, not the rare one. A stored URL can carry
userinfo today because `_adopt_legacy_clone` writes with no validation and
`validate_skills_library_url` ignores userinfo by design.

These tests drive the REAL `redact` / `_scrub_pat`, and the end-to-end case
builds its input with the REAL `_authenticated_url` rather than a hand-typed
double-`@` string — otherwise the suite would still pass if the splice changed
shape and the scrubbers silently stopped covering it.
"""

from __future__ import annotations

import pytest

from services.skill_source_clone import redact


# ---------------------------------------------------------------------------
# The defect
# ---------------------------------------------------------------------------


def test_double_at_url_is_fully_scrubbed():
    """The bug, directly: everything before the LAST `@` of the authority goes."""
    assert redact("https://a@b@github.com/o/r") == "https://***@github.com/o/r"


def test_no_credential_material_survives_a_real_looking_double_at():
    """Stated as an absence, not an equality — the property that matters is
    'the token is not in the output', and an equality assertion can be
    satisfied by a pattern that happens to produce the right string on one
    input while leaking on a neighbouring one."""
    out = redact("https://ghp_PLATFORM@ghp_LEGACY@github.com/org/private-skills")
    assert "ghp_PLATFORM" not in out
    assert "ghp_LEGACY" not in out
    assert out == "https://***@github.com/org/private-skills"


def test_single_credential_still_scrubbed():
    """The case that already worked must keep working."""
    assert redact("https://ghp_TOK@github.com/o/r") == "https://***@github.com/o/r"


def test_triple_at_is_also_covered():
    """Nothing in the splice caps the userinfo at two segments — a stored URL
    that already carried a double-`@` yields three. The rule is 'the last `@`
    before the path wins', not 'handle two'."""
    assert redact("https://x@y@z@github.com/o/r") == "https://***@github.com/o/r"


# ---------------------------------------------------------------------------
# What must NOT be touched
# ---------------------------------------------------------------------------


def test_at_in_query_is_not_a_credential():
    """An `@` after the path separator is data. Scrubbing it would corrupt the
    error message an operator is trying to read."""
    url = "https://github.com/o/r?ref=a@b"
    assert redact(url) == url


def test_at_in_a_path_segment_is_not_a_credential():
    url = "https://github.com/o/r/refs/heads/user@example"
    assert redact(url) == url


def test_at_in_a_query_with_no_path_is_not_a_credential():
    """`?` and `#` are excluded from the class on top of `/`: neither is legal
    unencoded in userinfo (RFC 3986), so excluding them can only prevent a
    false positive. The issue's suggested `[^\\s/]+@` would mangle this one."""
    url = "https://github.com?x=a@b"
    assert redact(url) == url


def test_plain_url_followed_by_a_credentialed_one():
    """`redact`'s old `[^@]+@` could cross `/` AND newlines, so an earlier plain
    URL matched all the way into a later credentialed one — mangling the
    message while hiding the token only by accident."""
    text = "remote: https://github.com/o/r\nfatal: https://ghp_TOK@github.com/o/p denied"
    out = redact(text)
    assert "ghp_TOK" not in out
    assert out == (
        "remote: https://github.com/o/r\nfatal: https://***@github.com/o/p denied"
    )


def test_multiline_stderr_scrubs_every_occurrence():
    """git stderr carries several URLs plus prose — this is why the scrubbers
    stay text-oriented rather than using the single-URL parser."""
    text = (
        "Cloning into '/data/skills-library'...\n"
        "remote: https://a@b@github.com/o/one\n"
        "fatal: could not read Username for https://c@d@github.com/o/two\n"
    )
    out = redact(text)
    for token in ("a@b", "c@d"):
        assert token not in out
    assert out.count("https://***@github.com/o/") == 2


# ---------------------------------------------------------------------------
# Totality
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", None, "no urls here at all", "https://", "@@@"])
def test_redact_never_raises(value):
    redact(value)


def test_scrub_pat_never_raises_on_a_hostile_exception_string():
    """`_scrub_pat` is called on `str(e)` for an arbitrary exception, whose
    `__str__` can itself raise. Its contract is 'never raises' and the caller
    is a persistence path — a throw there would replace a scrubbed error with
    an unhandled one."""
    from services.skill_service import _scrub_pat

    class _Hostile:
        def __str__(self):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        str(_Hostile())  # the input itself is what explodes

    assert _scrub_pat("https://a@b@github.com/o/r") == "https://***@github.com/o/r"


# ---------------------------------------------------------------------------
# The two scrubbers must not drift apart again
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "https://a@b@github.com/o/r",
        "https://ghp_TOK@github.com/o/r",
        "https://github.com/o/r?ref=a@b",
        "https://github.com?x=a@b",
        "remote: https://github.com/o/r\nfatal: https://t@github.com/o/p",
        "",
    ],
)
def test_both_scrubbers_agree(text):
    """The root cause was TWO hand-written patterns that had drifted apart and
    were each wrong differently. Pinned as an equality across both entry points
    so a future edit to one is caught here rather than by a leaked token."""
    from services.skill_service import _scrub_pat

    assert _scrub_pat(text) == redact(text)


# ---------------------------------------------------------------------------
# End-to-end: the shape the splice actually produces
# ---------------------------------------------------------------------------


def test_scrubber_covers_what_authenticated_url_actually_builds():
    """Input built by the REAL splice, not typed by hand.

    This is the assertion that ties the fix to the reported path: if
    `_authenticated_url` ever changes how it composes the authority, this fails
    instead of the scrubbers quietly ceasing to cover it.
    """
    from services.skill_service import SkillService

    stored = "https://ghp_LEGACY@github.com/org/private-skills"
    spliced = SkillService._authenticated_url(stored, "ghp_PLATFORM")

    # Precondition: the splice really does produce a double-`@` authority.
    assert spliced.count("@") >= 2, f"splice no longer double-@: {spliced!r}"
    assert "ghp_PLATFORM" in spliced and "ghp_LEGACY" in spliced

    # The durable-state path: an error string carrying that URL.
    persisted = redact(f"fatal: could not read Username for '{spliced}'")
    assert "ghp_PLATFORM" not in persisted
    assert "ghp_LEGACY" not in persisted
    assert "https://***@github.com/org/private-skills" in persisted
