"""Mask the credentials a handler holds out of text bound for the backend log.

abilityai/trinity-enterprise#1117. A route that answers a fixed text plus the
exception class name (the #1917 pattern) moves the exception detail into the
Vector-captured backend log. When the handler holds a credential (a key under
test, a PAT being saved, a bot token in the request URL), the exception can
echo that credential, and the log must not carry it.

The pattern pass in ``utils.credential_sanitizer.sanitize_text`` only knows
credential SHAPES. A held value is known by IDENTITY, so it is masked here in
every form an exception message is known to render it in:

- raw, and stripped of surrounding whitespace;
- the inside of its ``str`` repr (``'a\\nb'``), the inside of its ``bytes``
  repr (h11 / httpx: ``Illegal header value b'...'``) and the inside of its
  JSON string;
- base64 of its UTF-8 bytes (which also covers a ``Basic`` header for
  ``x-access-token:<value>``: that prefix is 15 bytes, so the encodings align);
- every run of characters that no escaping scheme rewrites
  (``[A-Za-z0-9_.~-]``): a value pasted with a line break, or carrying a
  quote or a non-ASCII character, is still masked piece by piece in a form
  this list does not name.

Every form shorter than ``MIN_HELD_LENGTH`` is skipped, and so is a value whose
stripped length is shorter: masking a one-character value would garble every
word that contains it, and no credential these routes accept is that short.
Forms are replaced longest first, so a whole value becomes one ``***`` rather
than a run of masked pieces.

Names here are chosen to stay clear of CodeQL's sensitive-data name heuristics
(``SensitiveDataHeuristics.qll``): a function or parameter whose name reads as
sensitive makes its value a source, and logging it would raise
``py/clear-text-logging-sensitive-data``. The held values only ever reach the
FIRST argument of ``str.replace``, which is not a taint step, so the masked
text that is logged carries no taint from them.
"""

from __future__ import annotations

import base64
import json
import re
import traceback
from typing import Optional

from utils.credential_sanitizer import redact_url_userinfo, sanitize_text

MASK = "***"
MIN_HELD_LENGTH = 8

# Characters that str/bytes repr, JSON escaping and percent-encoding all leave
# as they are, so a run of them appears verbatim in any of those renderings.
_VERBATIM_RUN = re.compile(r"[A-Za-z0-9_.~-]+")


def _forms(value: str) -> set[str]:
    """Every rendering of ``value`` that ``mask_held_values`` replaces."""
    raw = value.encode("utf-8", "backslashreplace")
    forms = {
        value,
        value.strip(),
        repr(value)[1:-1],
        repr(raw)[2:-1],
        json.dumps(value)[1:-1],
        base64.b64encode(raw).decode("ascii"),
    }
    forms.update(_VERBATIM_RUN.findall(value))
    return forms


def mask_held_values(text: str, *held: Optional[str], min_length: int = MIN_HELD_LENGTH) -> str:
    """Return ``text`` with every form of every ``held`` value replaced by ``MASK``.

    ``None`` and empty values are ignored, so a caller can pass an optional
    field as it is. A value that is not a ``str`` raises ``TypeError``: a
    caller that passes the wrong object would otherwise mask nothing and log
    the credential it meant to protect. Only held values are masked; the
    shape-based passes are ``masked_traceback``'s job.
    """
    if not text:
        return text
    forms: set[str] = set()
    for value in held:
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise TypeError(f"held values must be str, not {type(value).__name__}")
        if len(value.strip()) < min_length:
            continue
        forms.update(form for form in _forms(value) if len(form) >= min_length)
    for form in sorted(forms, key=lambda f: (len(f), f), reverse=True):
        text = text.replace(form, MASK)
    return text


def masked_traceback(exc: BaseException, *held: Optional[str]) -> str:
    """``exc`` with its traceback and cause chain, ready to log.

    Three passes, because they cover different credentials: the ``held``
    values by identity (``mask_held_values``), then any URL userinfo the text
    echoes (a stale credential baked into a remote URL is not the held one),
    then the known credential shapes (``sanitize_text``). Log the result
    instead of passing ``exc_info``, which renders the raw message.
    """
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return sanitize_text(redact_url_userinfo(mask_held_values(text, *held)))
