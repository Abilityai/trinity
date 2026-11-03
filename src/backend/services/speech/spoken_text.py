"""What part of an agent's reply is read aloud. The one home of this rule.

The written reply is always whole. This only decides what the speaker says: no code
beside prose (a reply that is nothing but a code block is read from inside it), no
`<screen>` blocks, no markdown punctuation or bare URLs, and a bounded length with a
fixed closing line. It removes text and never rewrites the agent's sentences.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

REST_IN_CHAT = "The rest is in the chat."
# About a minute and a quarter of speech.
SPOKEN_CHAR_LIMIT = 1200

_SCREEN = re.compile(r"<screen>.*?</screen>", re.I | re.S)
_OPEN_SCREEN = re.compile(r"<screen>.*\Z", re.I | re.S)
_FENCE = re.compile(r"```.*?(```|\Z)", re.S)
# A fence's body without its markers and language tag.
_FENCE_BODY = re.compile(r"```[^\n]*\n?(.*?)(```|\Z)", re.S)
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"https?://\S+")
_INLINE_CODE = re.compile(r"`([^`]*)`")
_MARKS = re.compile(r"(^\s{0,3}#{1,6}\s+)|(^\s*[-*+]\s+)|(^\s*>\s?)|(\*\*|__|~~)", re.M)
_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$", re.M)
_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"')\]]*\s+|\n+")
_ABBREV = re.compile(r"\b(e\.g|i\.e|etc|vs|mr|mrs|ms|dr|no|inc)\.$", re.I)
_NORMAL = re.compile(r"[^\w]+", re.U)


def speakable(text: str) -> str:
    """The reply with everything that should not be read aloud removed. Code beside
    prose is dropped; when code blocks are all there is, their text is the answer."""
    t = text or ""
    t = _SCREEN.sub(" ", t)
    t = _OPEN_SCREEN.sub(" ", t)
    without_code = _FENCE.sub(" ", t)
    t = without_code if _NORMAL.sub("", without_code) else _FENCE_BODY.sub(r"\1", t)
    t = _TABLE_LINE.sub(" ", t)
    t = _LINK.sub(r"\1", t)
    t = _URL.sub(" ", t)
    t = _INLINE_CODE.sub(r"\1", t)
    t = _MARKS.sub("", t)
    return t.strip()


def sentences(text: str) -> list[str]:
    """Speakable sentences, in order. An abbreviation's full stop does not split."""
    out: list[str] = []
    pending = ""
    position = 0
    source = speakable(text)
    for match in _SENTENCE_END.finditer(source):
        piece = pending + source[position:match.start()]
        trailing = source[match.start():match.end()].rstrip()
        position = match.end()
        if _ABBREV.search(piece.strip()) and "\n" not in match.group(0):
            pending = piece + trailing + " "
            continue
        pending = ""
        if piece.strip():
            out.append((piece + trailing).strip())
    rest = (pending + source[position:]).strip()
    if rest:
        out.append(rest)
    return [s for s in out if _NORMAL.sub("", s)]


def _key(sentence: str) -> str:
    return _NORMAL.sub(" ", sentence).strip().lower()


def unspoken_tail(reply: str, already_spoken: Iterable[str]) -> list[str]:
    """The sentences of the persisted reply not yet said, for reconciling a stream
    with the finished turn. A sentence said once is only consumed once, so an agent
    that really repeats itself is still heard."""
    said = Counter(_key(s) for s in already_spoken)
    tail: list[str] = []
    for sentence in sentences(reply):
        key = _key(sentence)
        if said[key] > 0:
            said[key] -= 1
        else:
            tail.append(sentence)
    return tail


class SpokenBudget:
    """Bounds one turn's speech. Once over, `admit` returns the closing line exactly
    once and nothing after."""

    def __init__(self, limit: int = SPOKEN_CHAR_LIMIT):
        self._left = limit
        self._closed = False

    def admit(self, sentence: str) -> str | None:
        if self._closed:
            return None
        if len(sentence) > self._left:
            self._closed = True
            return REST_IN_CHAT
        self._left -= len(sentence)
        return sentence
