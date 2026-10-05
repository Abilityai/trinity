"""Which gated skills does a request invoke? (trinity-enterprise#751)

The gate reads an agent's whole set of gated skill names once, then asks this
function which of them the requester's own text invokes. Matching the known
names against the text — rather than tokenising the text and looking each token
up — keeps the check to one read per dispatch and leaves no tokeniser to
disagree with the runtime about where a name ends: ``/pay-invoice.`` at the end
of a sentence is ``pay-invoice``, while ``/pay-invoice.v2`` is a different skill.

A pure leaf (``re`` + ``unicodedata`` only), so the gate service, the entry
points and their tests can import it without pulling in the dispatch stack.

Every ambiguous case errs toward matching: a false positive raises an approval
nobody needed, a false negative runs a gated skill without one. This is NOT a
boundary against a requester who names a skill only in prose ("please pay the
invoice") — that lane is the in-container hook (trinity-enterprise#752).
"""
import re
import unicodedata
from typing import Iterable, List, Optional

# Zero-width and bidirectional-control characters. A model ignores them; a
# regex does not. Stripped before matching so a zero-width space (U+200B) after
# the slash or a bidi override in front of it cannot hide an invocation.
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")

# Before the slash: anything except a name character, a dot or another slash —
# so `run:/x`, a backtick, a quote or a bracket still precede an invocation,
# while `docs/x`, `https://host/x` and `//x` are paths. After the name: no
# further name character, and no dot that continues into one.
_BEFORE = r"(?<![a-z0-9._/-])/"
_AFTER = r"(?![a-z0-9_-])(?!\.[a-z0-9])"


def _normalise(text: str) -> str:
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", text)).casefold()


def find_gated_invocations(text: Optional[str], gated_names: Iterable[str]) -> List[str]:
    """The gated names that ``text`` invokes, each once, in order of first use.

    Names are returned as ``gated_names`` spells them. Comparison is
    case-insensitive. An empty gate set costs nothing.
    """
    names = list(gated_names or ())
    if not text or not names:
        return []
    haystack = _normalise(text)
    found = []
    for name in names:
        match = re.search(_BEFORE + re.escape(_normalise(name)) + _AFTER, haystack)
        if match:
            found.append((match.start(), name))
    return [name for _, name in sorted(found)]
