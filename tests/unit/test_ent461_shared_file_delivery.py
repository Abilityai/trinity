"""ent#461 — a shared file link has to actually open on a phone.

The bytes were never wrong. The RESPONSE SHAPE was, in four ways at once, and
each one alone is enough to break playback in an iOS in-app browser: no Range
support, a forced download, a legacy content type under `nosniff`, and a
same-origin CORP that stops Telegram embedding it at all.

The tests that matter most here are NOT the ones proving Range works. They are
the ones proving the inline allowlist did not quietly widen — because relaxing
`attachment` is relaxing a documented XSS defence, and the tempting mistake
("images are safe, allow `image/*`") lets `image/svg+xml` through, which is a
script host wearing an image's name.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from routers.files import (  # noqa: E402
    _INLINE_SAFE_TYPES,
    _format_disposition,
    _iter_file,
    is_inline_safe,
    normalize_mime,
    parse_range_header,
)


# --------------------------------------------------------------------------- #
# The inline allowlist — the security half
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("dangerous", [
    "text/html",
    "application/xhtml+xml",
    "image/svg+xml",          # an image by name, a script host in fact
    "text/xml",
    "application/xml",
    "text/javascript",
    "application/javascript",
    "application/octet-stream",
    "text/plain",
    "application/zip",
])
def test_dangerous_and_unknown_types_are_never_inline(dangerous):
    """This route serves agent-authored bytes from the same origin as public
    chat, so an inline `text/html` is stored XSS. SVG is in this list because
    it is the one a reviewer waves through."""
    assert not is_inline_safe(dangerous)
    assert _format_disposition("x", inline=is_inline_safe(dangerous)).startswith("attachment;")


@pytest.mark.parametrize("safe", ["audio/wav", "audio/mpeg", "image/png", "video/mp4", "application/pdf"])
def test_the_media_types_the_bug_was_about_are_inline(safe):
    assert is_inline_safe(safe)
    assert _format_disposition("x", inline=True).startswith("inline;")


def test_the_allowlist_contains_no_scriptable_type():
    """A guard against future widening rather than against today's contents:
    anything xml/html/script-shaped must never appear in this set."""
    for t in _INLINE_SAFE_TYPES:
        assert "html" not in t and "xml" not in t and "script" not in t, t


def test_inline_is_decided_by_the_server_not_the_request():
    """`_format_disposition` takes `inline` as a keyword the caller computes from
    `is_inline_safe`. Pinned so nobody later wires it to a `?inline=1` query
    param, which would hand the decision to the attacker."""
    import inspect
    sig = inspect.signature(_format_disposition)
    assert sig.parameters["inline"].kind is inspect.Parameter.KEYWORD_ONLY


# --------------------------------------------------------------------------- #
# MIME normalization — the `nosniff` interaction
# --------------------------------------------------------------------------- #

def test_the_reported_wav_type_is_normalized():
    """`audio/x-wav` is unregistered; under `nosniff` a strict player declines it
    and the browser is forbidden from guessing better. This is the reported file."""
    assert normalize_mime("audio/x-wav") == "audio/wav"
    assert is_inline_safe("audio/x-wav")


@pytest.mark.parametrize("raw,expected", [
    ("audio/wave", "audio/wav"),
    ("audio/mp3", "audio/mpeg"),
    ("audio/x-mpeg", "audio/mpeg"),
    ("AUDIO/X-WAV", "audio/wav"),                 # case
    ("audio/x-wav; charset=binary", "audio/wav"), # magic emits parameters
    ("image/x-png", "image/png"),
    (None, "application/octet-stream"),
    ("", "application/octet-stream"),
])
def test_normalization_cases(raw, expected):
    assert normalize_mime(raw) == expected


def test_normalization_cannot_promote_a_dangerous_type():
    """Aliasing must never be a route into the allowlist."""
    for t in ["text/html", "image/svg+xml", "application/xhtml+xml"]:
        assert not is_inline_safe(normalize_mime(t))


# --------------------------------------------------------------------------- #
# Range parsing — wrong bytes with a 206 is undetectable by the client
# --------------------------------------------------------------------------- #

def test_a_normal_range():
    assert parse_range_header("bytes=0-1023", 2000) == (0, 1023)


def test_an_open_ended_range_runs_to_eof():
    assert parse_range_header("bytes=500-", 2000) == (500, 1999)


def test_a_suffix_range_means_the_LAST_n_bytes():
    """`bytes=-500` is the last 500 bytes, not the first. Reading it as
    `start=0` serves the wrong bytes under a 206, which the client has no way
    to detect — it is the silent-corruption case in this parser."""
    assert parse_range_header("bytes=-500", 2000) == (1500, 1999)


def test_a_range_past_the_end_is_clamped_not_extended():
    assert parse_range_header("bytes=1500-99999", 2000) == (1500, 1999)


@pytest.mark.parametrize("bad", ["bytes=2000-", "bytes=5000-6000", "bytes=-0"])
def test_unsatisfiable_ranges_are_reported_as_such(bad):
    """These get a 416 carrying the real length, so the client can re-ask."""
    assert parse_range_header(bad, 2000) == "unsatisfiable"


@pytest.mark.parametrize("ignored", [
    None, "", "items=0-10",        # only the `bytes` unit is defined
    "bytes=abc-def", "bytes=0-10, 20-30",   # multi-range → full 200 (RFC 7233 §3.1)
    "bytes=", "garbage",
])
def test_unusable_range_headers_fall_back_to_a_full_200(ignored):
    assert parse_range_header(ignored, 2000) is None


def test_no_range_math_on_an_empty_file():
    assert parse_range_header("bytes=0-10", 0) is None


# --------------------------------------------------------------------------- #
# Ranged streaming — the body must match its own Content-Length
# --------------------------------------------------------------------------- #

def test_a_range_stream_returns_exactly_the_requested_bytes(tmp_path):
    """Over-reading past the end produces a body longer than the declared
    `Content-Length`, which every client reads as a corrupt download."""
    f = tmp_path / "blob.bin"
    payload = bytes(range(256)) * 40      # 10240 bytes
    f.write_bytes(payload)

    got = b"".join(_iter_file(str(f), 100, 500))
    assert got == payload[100:600]
    assert len(got) == 500


def test_a_range_ending_at_eof_stops_cleanly(tmp_path):
    f = tmp_path / "blob.bin"
    f.write_bytes(b"x" * 100)
    assert len(b"".join(_iter_file(str(f), 90, 10))) == 10


def test_the_unranged_stream_is_the_whole_file(tmp_path):
    f = tmp_path / "blob.bin"
    f.write_bytes(b"y" * 5000)
    assert len(b"".join(_iter_file(str(f)))) == 5000


def test_a_length_longer_than_the_file_does_not_hang(tmp_path):
    """Defensive: the caller clamps, but a stream that spins on EOF waiting for
    bytes that will never arrive is the worst failure mode here."""
    f = tmp_path / "blob.bin"
    f.write_bytes(b"z" * 10)
    assert b"".join(_iter_file(str(f), 0, 99999)) == b"z" * 10


# --------------------------------------------------------------------------- #
# The middleware interaction — found only by testing the REAL app
# --------------------------------------------------------------------------- #

def test_the_security_middleware_does_not_clobber_a_routes_own_corp():
    """`main.add_security_headers` runs after every route.

    It set `Cross-Origin-Resource-Policy` with a plain assignment, which
    silently overwrote the header this route deliberately sets — making the
    `cross-origin` half of the ent#461 fix completely INERT in the real
    application while every unit test passed, because a bare
    `FastAPI()` + router harness has no middleware.

    Caught by running the real server and reading the response. Pinned here as a
    source assertion because asserting it end-to-end needs a live stack, and the
    thing that must not regress is the `setdefault` — a future edit back to `=`
    would re-break it invisibly.
    """
    import ast
    import inspect
    from pathlib import Path

    main_src = (Path(__file__).resolve().parents[2] / "src" / "backend" / "main.py").read_text()
    tree = ast.parse(main_src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "add_security_headers"
    )
    body = ast.unparse(fn)

    assert "setdefault('Cross-Origin-Resource-Policy', 'same-origin')" in body, (
        "the security middleware must SET A DEFAULT for CORP, not assign it — a "
        "plain assignment overwrites the file-download route's `cross-origin` "
        "policy and the link stops being embeddable from Telegram/Slack/WhatsApp, "
        "with nothing in the unit suite able to see it (ent#461)."
    )
    assert 'headers["Cross-Origin-Resource-Policy"] =' not in body, (
        "a plain CORP assignment is back in the security middleware — see above."
    )
