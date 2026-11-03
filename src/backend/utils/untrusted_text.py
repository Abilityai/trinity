"""Untrusted text inside a prompt: defanging the block markers (trinity-enterprise#419).

A block of untrusted text in a prompt is delimited by lines the platform
writes, and those lines mean something only if the text inside cannot
reproduce them. Deleting the markers does not guarantee that: removing an inner
occurrence splices the characters around it into a fresh one
(``[End of [End of X]X]`` → ``[End of X]``), so a delete has to loop to a fixed
point, and each loop costs a full pass — quadratic on hostile input.

``defang_bracket_markers`` instead turns the leading ``[`` of every occurrence,
ignoring case, into ``(``. Nothing is deleted, and every marker holds exactly
one ``[`` — its first character — so no new occurrence can form: one linear
pass is already the fixed point.

Shared by the Telegram group-history block (ent#600) and the per-person memory
envelope (``services/user_memory_guard.py``).
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable, Tuple


@lru_cache(maxsize=32)
def _pattern(markers: Tuple[str, ...]) -> "re.Pattern[str]":
    for marker in markers:
        if not marker.startswith("[") or "[" in marker[1:]:
            raise ValueError(f"a marker must start with its only '[': {marker!r}")
    return re.compile("|".join(re.escape(m) for m in markers), re.IGNORECASE)


def defang_bracket_markers(text: str, markers: Iterable[str]) -> str:
    """Every occurrence of a marker (ignoring case) with its leading ``[``
    turned into ``(``, in one pass. Markers must start with their only ``[``."""
    markers = tuple(m for m in markers if m)
    if not text or not markers:
        return text or ""
    return _pattern(markers).sub(lambda m: "(" + m.group()[1:], text)
