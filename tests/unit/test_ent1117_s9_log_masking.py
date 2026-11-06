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
