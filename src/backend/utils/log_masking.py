"""Mask the credentials a handler holds out of text bound for the backend log.

abilityai/trinity-enterprise#1117. A route that answers a fixed text plus the
exception class name (the #1917 pattern) moves the exception detail into the
Vector-captured backend log. When the handler holds a credential (a key under
test, a PAT being saved, a bot token in the request URL), the exception can
echo that credential, and the log must not carry it.

The pattern pass in ``utils.credential_sanitizer.sanitize_text`` only knows
credential SHAPES. A held value is known by IDENTITY, and is masked here.

What is masked (``mask_held_values``; steps run 1, 3, 2, see below):

1. Whole renderings of each held value: raw; stripped; the inside of its
   ``str`` repr (``'a\\nb'``); the inside of its ``bytes`` repr (h11 / httpx:
   ``Illegal header value b'...'``); the inside of its JSON string; base64 of
   its UTF-8 bytes. Base64 also covers a ``Basic`` header for
   ``x-access-token:<value>``, because that prefix is 15 bytes and the
   encodings align.
2. Pieces: every run of the value's characters that no escaping scheme
   rewrites (``[A-Za-z0-9_.~-]``, 8+ characters), wherever it appears. A value
   pasted with a line break, or carrying a quote or a non-ASCII character, is
   still masked piece by piece in a rendering step 1 does not name
   (percent-encoding, ``ascii()``, a dict or tuple ``%r``).
3. Cut renderings: any run of 16+ such characters in the text that shares a
   16-character stretch with a run of a held value is masked whole. This
   catches renderings that show only PART of a run: pydantic's truncated
   ``input_value='head...tail'``, ``reprlib``, a prefix-only echo, line
   wrapping and header folding, a value split across two strings.

Step 1 runs first, then step 3, then step 2. Step 2 runs last because its
pieces are a value's SHORTEST runs (see the bounds below): replaced before
step 3, a piece that is part of a longer run would cut that run into parts
under 16 characters, which step 3 then skips. Steps 1 and 2 each replace
longest first, so a whole value becomes one ``***``.

What is NOT masked, by design:

- A value whose stripped length is under ``MIN_HELD_LENGTH`` (8), and any
  single rendering or piece under 8: masking a one-character value would
  garble every word that contains it. No credential these routes accept is
  that short. In a rendering that is not whole, the value's runs under 8
  therefore stay visible, with the ``+``, ``/`` or other characters between
  them: a PEM key with its line breaks dropped shows such stretches (up to 33
  characters, measured on 60 RSA keys of 2048-8192 bits), and one re-wrapped
  at another width can show a whole line.
- A visible stretch of a cut run that is shorter than 16 characters (for
  example the 10-character tail a wrapped line leaves on its own), unless it
  is a whole 8+ piece.
- A piece under 8 characters inside a DOUBLY escaped rendering (``repr`` of a
  repr, JSON inside JSON, ``repr(exc)``): ``b'***\\\\ntail7c'`` keeps
  ``tail7c``. A value of 8+ characters whose runs are ALL under 8 (a
  password-like value) is therefore recoverable from such renderings; no
  credential these routes accept has that shape.
- Overlap of two held values in the text: every held value's whole renderings
  are replaced before any value's cut runs and pieces, so a held value that
  appears inside another held value's run cuts that run, and its parts can
  stay visible (under 16 characters, or a piece of 8-15).
- Past the cost caps below, in any rendering that does not show the value
  whole (step 1): a run of 8-15 characters that is not among the value's
  ``MAX_PIECES`` shortest runs (step 3 catches one of 12-15 characters only
  inside a longer run of the text, and only when a sample lands in it), and
  a stretch of a run past the first ``MAX_WINDOW_SOURCE`` characters of the
  value's runs, counted longest run first, unless that run
  is a kept piece shown whole. The first exists only for a value with more
  than ``MAX_PIECES`` distinct runs of 8-15 characters, the second only for
  one with more than ``MAX_WINDOW_SOURCE`` characters in its distinct runs of
  12+. A PEM private key up to 8192-bit RSA, alone or inside a
  service-account JSON, is within both (measured on 60 RSA keys of
  2048-8192 bits: at most about 90 runs of 8-15 characters and about 6,000
  characters of runs of 12+, against 128 and 65,536).

Bounds, so a crafted value cannot cost seconds on the event loop. Per held
value, step 2 keeps at most ``MAX_PIECES`` pieces, the value's SHORTEST runs:
step 3 already masks, wherever it appears, every run of 16+ characters inside
its budget, so the piece slots go to the runs it cannot guarantee.
Step 3 takes its 12-character stretches from the value's runs, longest run
first, from at most ``MAX_WINDOW_SOURCE`` characters in all. Step 2 costs one
scan of the text per kept piece; step 3 samples the text every ``_STRIDE``
characters, so its cost is linear in the text and independent of the held
values.

Rules for callers (S12/S13 and any later adopter):

1. Never run a held value through pydantic validation or another truncating
   formatter inside a masked ``try`` unless step 3 covers what it shows; a
   visible stretch under 16 characters is not caught.
2. For a ``Basic`` header whose user part is not 15 bytes (``oauth2:``,
   ``user:``, ``git:``), pass ``f"{user}:{pat}"`` as an extra held value:
   base64 of the PAT alone does not align with it.
3. Pass the credential, not the URL that carries it: the URL's pieces
   (``github.com``, path segments) would be masked everywhere and garble the
   line. ``masked_traceback`` redacts URL userinfo on its own.
4. Never log with ``logger.exception`` or ``exc_info`` next to masked text:
   both render the raw message again.
5. Bound a held value the service has not validated (length or shape) before
   passing it. The caps above bound the cost; past them the masking is weaker
   (the last "NOT masked" item), for example for a bundle of several large
   private keys passed as one value.
6. Keep wrapper, variable and parameter names off CodeQL's sensitive-name
   heuristics (``SensitiveDataHeuristics.qll``: secret, password, passcode,
   passphrase, api key/token, auth key, oauth, cert, account, ...). A function
   or parameter whose name reads as sensitive makes its value a source, and
   logging it raises ``py/clear-text-logging-sensitive-data``;
   ``scrub_secret(...)`` is such a call. Held values here only ever reach the
   FIRST argument of ``str.replace`` and a set-membership test, neither of
   which is a taint step, so the masked text that is logged carries no taint
   from them.
"""

from __future__ import annotations

import base64
import json
import re
import traceback
from typing import Iterable, Optional

from utils.credential_sanitizer import redact_url_userinfo, sanitize_text

MASK = "***"
MIN_HELD_LENGTH = 8
# A cut rendering is caught when it shows this many characters of a run.
MIN_CUT_LENGTH = 16
# Cost bounds per held value: pieces kept (its shortest runs), and characters
# of its runs (longest run first) that step 3's stretches are taken from.
MAX_PIECES = 128
MAX_WINDOW_SOURCE = 65536

# Characters that str/bytes repr, JSON escaping and percent-encoding all leave
# as they are, so a run of them appears verbatim in any of those renderings.
_VERBATIM_RUN = re.compile(r"[A-Za-z0-9_.~-]+")
# Step 3 stores every _WINDOW-character stretch of the held runs and samples
# the text every _STRIDE characters: any shared stretch of
# _WINDOW + _STRIDE - 1 (= MIN_CUT_LENGTH) characters contains a sample.
_WINDOW = 12
_STRIDE = MIN_CUT_LENGTH - _WINDOW + 1


def _renderings(value: str) -> set[str]:
    """Step 1: the whole renderings of ``value``."""
    raw = value.encode("utf-8", "backslashreplace")
    return {
        value,
        value.strip(),
        repr(value)[1:-1],
        repr(raw)[2:-1],
        json.dumps(value)[1:-1],
        base64.b64encode(raw).decode("ascii"),
    }


def _runs(value: str, min_length: int) -> list[str]:
    """The value's distinct verbatim runs of ``min_length``+, longest first."""
    runs = {run for run in _VERBATIM_RUN.findall(value) if len(run) >= min_length}
    return sorted(runs, key=lambda r: (len(r), r), reverse=True)


def _pieces(runs: list[str]) -> list[str]:
    """Step 2: the ``MAX_PIECES`` shortest of ``runs`` (given longest first)."""
    return runs[::-1][:MAX_PIECES]


def _windows(runs: Iterable[str]) -> set[str]:
    """Step 3: every _WINDOW-character stretch of ``runs`` (given longest
    first), from at most MAX_WINDOW_SOURCE characters in all."""
    found: set[str] = set()
    budget = MAX_WINDOW_SOURCE
    for run in runs:
        if budget <= 0:
            break
        run = run[:budget]
        budget -= len(run)
        found.update(run[i:i + _WINDOW] for i in range(len(run) - _WINDOW + 1))
    return found


def _mask_cut_runs(text: str, windows: set[str]) -> str:
    """Mask whole every text run of MIN_CUT_LENGTH+ that shares a stretch with a held run."""
    parts: list[str] = []
    last = 0
    for match in _VERBATIM_RUN.finditer(text):
        run = match.group()
        if len(run) < MIN_CUT_LENGTH:
            continue
        if any(run[i:i + _WINDOW] in windows for i in range(0, len(run) - _WINDOW + 1, _STRIDE)):
            parts.append(text[last:match.start()])
            parts.append(MASK)
            last = match.end()
    if not parts:
        return text
    parts.append(text[last:])
    return "".join(parts)


def mask_held_values(text: str, *held: Optional[str], min_length: int = MIN_HELD_LENGTH) -> str:
    """Return ``text`` with every held value masked as the module docstring describes.

    ``None`` and empty values are ignored, so a caller can pass an optional
    field as it is. A value that is not a ``str`` raises ``TypeError``: a
    caller that passes the wrong object would otherwise mask nothing and log
    the credential it meant to protect. Only held values are masked; the
    shape-based passes are ``masked_traceback``'s job.
    """
    if not text:
        return text
    forms: set[str] = set()
    pieces: set[str] = set()
    windows: set[str] = set()
    for value in held:
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise TypeError(f"held values must be str, not {type(value).__name__}")
        if len(value.strip()) < min_length:
            continue
        runs = _runs(value, min_length)
        forms.update(form for form in _renderings(value) if len(form) >= min_length)
        pieces.update(_pieces(runs))
        windows.update(_windows(runs))
    for form in sorted(forms, key=lambda f: (len(f), f), reverse=True):
        text = text.replace(form, MASK)
    if windows:
        text = _mask_cut_runs(text, windows)
    for piece in sorted(pieces, key=lambda f: (len(f), f), reverse=True):
        text = text.replace(piece, MASK)
    return text


def masked_traceback(exc: BaseException, *held: Optional[str]) -> str:
    """``exc`` with its traceback and cause chain, ready to log.

    Three passes, in this order, because they cover different credentials:
    the ``held`` values by identity (``mask_held_values``; first, so the
    shape pass cannot rewrite part of a held rendering before it is matched
    whole), then any URL userinfo the text echoes (a stale credential baked
    into a remote URL is not the held one), then the known credential shapes
    (``sanitize_text``). Log the result instead of passing ``exc_info``, which
    renders the raw message.
    """
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return sanitize_text(redact_url_userinfo(mask_held_values(text, *held)))
