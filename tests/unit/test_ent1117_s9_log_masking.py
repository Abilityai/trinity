"""`utils.log_masking` — the held-value mask for text bound for the backend log
(abilityai/trinity-enterprise#1117).

A held credential must not reach a log record in any form an exception is
known to render it in: raw, stripped, inside a str repr, inside a bytes repr
(h11/httpx ``Illegal header value b'...'``), inside a JSON string, base64, or
as a fragment long enough to be useful. Every test value below is one that
``sanitize_text`` does NOT recognise (pinned by the first test), so a missing
mask cannot hide behind the shape-based pass.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket

import httpx
import pytest

from utils.credential_sanitizer import sanitize_text
from utils.log_masking import MASK, MIN_HELD_LENGTH, mask_held_values, masked_traceback

pytestmark = pytest.mark.unit

HEAD = "s9held-" + "H" * 24
TAIL = "T" * 24 + "tailpartZ"
# A credential pasted with a line break: the form the httpx header error echoes.
SPLIT = HEAD + "\n" + TAIL
PLAIN = "s9-held-value-not-a-known-shape-0042"
# A tail shorter than the 8-character piece floor, behind a character every
# escaping scheme renders differently: only the form that matches the whole
# rendering keeps the tail out, so each form is pinned on its own.
SHORT = "tail7c"
NON_ASCII = HEAD + "\u00e9\n" + SHORT
# Pasted with padding; the handler sends it stripped.
PADDED = "  " + HEAD + ":" + SHORT + "  "


def _real_header_error(name: str, value: str) -> Exception:
    """The exception httpx itself raises for a header value with a control
    character. A listening socket completes the TCP handshake from its backlog,
    and h11 refuses the header before anything is sent."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    async def go():
        async with httpx.AsyncClient() as client:
            await client.get(f"http://127.0.0.1:{port}/", headers={name: value}, timeout=2)

    try:
        asyncio.run(go())
    except Exception as e:  # noqa: BLE001 — the error under test
        return e
    finally:
        srv.close()
    raise AssertionError("httpx accepted the header")


def test_the_test_values_are_not_recognised_by_sanitize_text():
    """If `sanitize_text` already masked these, every test below would pass
    without the held-value mask."""
    for value in (HEAD, TAIL, PLAIN):
        assert value in sanitize_text(f"x {value} y")


@pytest.mark.parametrize("render", [
    pytest.param(lambda v: v, id="raw"),
    pytest.param(lambda v: f"  {v}\n", id="padded"),
    pytest.param(lambda v: v.strip(), id="stripped"),
    pytest.param(lambda v: repr(v), id="str-repr"),
    pytest.param(lambda v: repr(v.encode()), id="bytes-repr"),
    pytest.param(lambda v: json.dumps(v), id="json"),
    pytest.param(lambda v: base64.b64encode(v.encode()).decode(), id="base64"),
    pytest.param(lambda v: base64.b64encode(f"x-access-token:{v}".encode()).decode(), id="git-basic-auth"),
])
@pytest.mark.parametrize("value", [SPLIT, PLAIN, f"  {PLAIN}  ", NON_ASCII, PADDED],
                         ids=["split", "plain", "padded", "non-ascii-short-tail", "padded-short-tail"])
def test_every_rendering_of_a_held_value_is_masked(render, value):
    out = mask_held_values(f"before {render(value)} after", value)
    assert HEAD not in out and TAIL not in out and PLAIN not in out, out
    assert SHORT not in out, out
    assert MASK in out
    assert out.startswith("before ") and out.endswith(" after"), out


def test_the_real_httpx_header_error_is_masked_whole():
    exc = _real_header_error("x-api-key", SPLIT)
    raw = str(exc)
    assert "Illegal header value" in raw and TAIL in raw  # the echo is real
    out = mask_held_values(raw, SPLIT)
    assert HEAD not in out and TAIL not in out, out
    # Longest first: the escaped form is replaced as one piece.
    assert out == "Illegal header value b'***'", out


@pytest.mark.parametrize("value", [
    "s9held-" + "A" * 12 + "'" + "B" * 12,       # a quote changes the repr
    "s9held-" + "A" * 12 + "​" + "B" * 12,  # a non-ASCII character
    "s9held-" + "A" * 12 + "\\" + "B" * 12,      # a backslash doubles in repr
])
def test_a_value_is_masked_piece_by_piece_in_a_rendering_not_named(value):
    # A rendering the form list does not name: percent-encoding.
    from urllib.parse import quote

    out = mask_held_values(f"url /bot{quote(value, safe='')}/getMe", value)
    assert "A" * 12 not in out and "B" * 12 not in out, out


def test_short_held_values_do_not_garble_the_text():
    text = 'raise RuntimeError("database is locked at host db-primary")'
    assert mask_held_values(text, "a", "at", "locked", " primary ") == text
    assert len("locked") < MIN_HELD_LENGTH


def test_a_short_value_padded_past_the_floor_does_not_garble_indentation():
    # Raw, this is 8 characters; stripped it is 1, so it is skipped whole.
    text = "  File \"x.py\"\n        assert ready\n"
    assert mask_held_values(text, "       a") == text


def test_a_short_piece_of_a_long_value_does_not_garble_the_text():
    text = "database is locked at host db-primary"
    assert mask_held_values(text, HEAD + " at") == text


def test_none_and_empty_held_values_are_ignored():
    assert mask_held_values("boom-value", None, "", "   ") == "boom-value"
    assert mask_held_values("", PLAIN) == ""


def test_a_held_value_that_is_not_a_str_fails_loudly():
    with pytest.raises(TypeError):
        mask_held_values("boom", b"s9-bytes-value-0001")  # type: ignore[arg-type]


def _raise_chained(value: str):
    try:
        raise OSError(f"inner {value!r}")
    except OSError as inner:
        raise RuntimeError(f"outer {value} https://deploy:pw-s9@git.example.com/x.git") from inner


def test_masked_traceback_masks_held_values_userinfo_and_shapes_across_the_chain():
    try:
        _raise_chained(SPLIT)
    except RuntimeError as e:
        out = masked_traceback(e, SPLIT)
    assert "Traceback (most recent call last)" in out
    assert "OSError" in out and "RuntimeError" in out  # the cause chain is kept
    assert HEAD not in out and TAIL not in out, out
    assert "pw-s9" not in out and "://***@git.example.com" in out, out


def test_masked_traceback_still_runs_the_shape_pass():
    unheld = "ghp_" + "Z" * 36  # a credential nobody passed as held
    try:
        raise RuntimeError(f"push rejected for {unheld}")
    except RuntimeError as e:
        out = masked_traceback(e)
    assert unheld not in out and "RuntimeError" in out


def test_masked_traceback_without_held_values_still_redacts_userinfo():
    try:
        raise RuntimeError("clone failed https://deploy:pw-s9@git.example.com/x.git")
    except RuntimeError as e:
        out = masked_traceback(e)
    assert "pw-s9" not in out and "RuntimeError" in out


# ---------------------------------------------------------------------------
# Cut renderings: a formatter that shows only PART of a run of the held value.
# ---------------------------------------------------------------------------

# One unshaped run (no sanitize_text pattern matches it): 78 characters.
CUT = "s9held-" + "".join(chr(65 + (i * 7) % 26) for i in range(60)) + "-endpartXYZ"


def _stretches(value: str, n: int = 16) -> list[str]:
    """Every n-character stretch of ``value``: none may survive the mask."""
    return [value[i:i + n] for i in range(len(value) - n + 1)]


def _assert_no_stretch(out: str, shown: str, value: str = CUT) -> None:
    """No 16-character stretch of the value survives, and no 12-character
    stretch that the rendering actually showed does either."""
    leaked = [s for s in _stretches(value) if s in out]
    leaked += [s for s in _stretches(value, 12) if s in shown and s in out]
    assert not leaked, (leaked[:3], out)


def test_the_cut_value_is_not_recognised_by_sanitize_text():
    assert CUT in sanitize_text(f"x {CUT} y")


def test_pydantic_truncated_input_value_is_masked():
    """pydantic 2.12 renders a long input as `input_value='<25>...<24>'`."""
    from pydantic import BaseModel, ValidationError

    class _Model(BaseModel):
        n: int

    try:
        _Model(n=CUT)
    except ValidationError as e:
        raw = str(e)
        out = masked_traceback(e, CUT)
    assert "..." in raw and CUT[:20] in raw and CUT[-20:] in raw  # the cut is real
    assert "input_value=" in out and "int_parsing" in out  # the detail stays
    _assert_no_stretch(out, raw)


@pytest.mark.parametrize("render", [
    pytest.param(lambda v: f"{v[:30]}' '{v[30:]}", id="split-across-two-strings"),
    pytest.param(lambda v: "\n".join(v[i:i + 50] for i in range(0, len(v), 50)), id="wrapped-at-50"),
    pytest.param(lambda v: f"X-Key: {v[:40]}\r\n\t{v[40:]}", id="header-folding"),
    pytest.param(lambda v: __import__("reprlib").repr(v), id="reprlib"),
    pytest.param(lambda v: f"{v[:20]}…{v[-20:]}", id="middle-ellipsis"),
    pytest.param(lambda v: f"key starts {v[:16]}", id="prefix-only-16"),
])
def test_a_rendering_that_cuts_the_run_is_masked(render):
    shown = render(CUT)
    assert CUT not in shown  # it really is cut
    out = mask_held_values(f"before {shown} after", CUT)
    assert out.startswith("before ") and out.endswith(" after"), out
    _assert_no_stretch(out, shown)


@pytest.mark.parametrize("offset", range(8))
def test_a_16_character_stretch_is_masked_at_any_offset_in_a_longer_run(offset):
    # The text samples every few characters; a shared stretch of 16 must be
    # caught wherever it sits inside the surrounding run.
    text = f"see {'q' * offset}{CUT[10:26]}{'q' * (7 - offset)} here"
    out = mask_held_values(text, CUT)
    assert CUT[10:26] not in out and out == f"see {MASK} here", out


def test_a_visible_stretch_under_the_window_is_not_masked():
    """Documented limit: 11 characters of a run, alone, are not caught."""
    text = f"key starts {CUT[:11]} here"
    assert mask_held_values(text, CUT) == text


def test_an_unrelated_long_run_is_left_alone():
    text = "traceback /srv/app/services/subscription_auto_switch.py line 1802"
    assert mask_held_values(text, CUT) == text


# ---------------------------------------------------------------------------
# Pass order, run class and floors, pinned on their own.
# ---------------------------------------------------------------------------

def test_the_held_pass_runs_before_the_shape_pass():
    """On a REAL-shaped key the shape pass rewrites the head; run first, it
    would break the whole escaped rendering and leave a short tail visible."""
    key = "sk-ant-api03-" + "x" * 40 + "\nab12cd"
    try:
        raise RuntimeError(f"Illegal header value {key.encode()!r}")
    except RuntimeError as e:
        out = masked_traceback(e, key)
    assert "ab12cd" not in out, out


def test_the_run_class_includes_underscore_dot_tilde_and_hyphen():
    from urllib.parse import quote

    value = "ab_cd.ef~gh-ij_kl.mn'op_qr.st~uv-wx_yz.12"
    out = mask_held_values(quote(value, safe=""), value)
    assert "ab_cd.ef~gh-ij_kl.mn" not in out and "op_qr.st~uv-wx_yz.12" not in out, out


def test_a_piece_of_exactly_the_floor_is_masked():
    from urllib.parse import quote

    value = "ABCDEFGH'IJKLMNOP"
    out = mask_held_values(quote(value, safe=""), value)
    assert "ABCDEFGH" not in out and "IJKLMNOP" not in out, out


def test_a_value_of_exactly_the_floor_is_masked():
    assert mask_held_values("x ABCDEFGH y", "ABCDEFGH") == f"x {MASK} y"
    # A rendering of exactly 8 that no piece covers (both pieces are short).
    assert mask_held_values("x ABCD\nEFG y", "ABCD\nEFG") == f"x {MASK} y"
    assert mask_held_values("x ABCDEFG y", "ABCDEFG") == "x ABCDEFG y"


# ---------------------------------------------------------------------------
# Cost bound: a crafted value cannot cost seconds on the event loop.
# ---------------------------------------------------------------------------

def test_a_crafted_value_is_bounded_in_pieces_windows_and_time():
    import time
    from urllib.parse import quote

    from utils.log_masking import MAX_PIECES, MAX_WINDOW_SOURCE, _pieces, _windows

    crafted = "s9-" + "\n".join(f"R{i:07d}" for i in range(20_000))
    pieces = _pieces(crafted, MIN_HELD_LENGTH)
    assert len(pieces) == MAX_PIECES
    assert len(_windows(pieces)) <= MAX_WINDOW_SOURCE
    rng = __import__("random").Random(1117)
    one_run = "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(100_000))
    assert len(_windows([one_run])) <= MAX_WINDOW_SOURCE
    text = "url " + quote(crafted, safe="") + " " + "y" * 1_000_000
    start = time.perf_counter()
    mask_held_values(text, crafted)
    assert time.perf_counter() - start < 1.0  # ~0.06 s measured; 5.7 s unbounded
