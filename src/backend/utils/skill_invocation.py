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

# Unicode's Default_Ignorable_Code_Point set (soft hyphen, joiners, bidi and
# Mongolian controls, variation selectors, tag characters, fillers …). A model
# ignores them; a regex does not. The text is matched twice — with them removed
# (a run before a slash read as a space), and with each replaced by a space —
# so one cannot hide an invocation either way: removed, `/\u00adx` is `/x` and
# `/pay\u200d-x` is `/pay-x`; spaced, `/x\u00adyz` still ends `x` (trinity#3274).
_INVISIBLE_CHAR = (
    "(?:[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f"
    "\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufff8]"
    # Each range beyond the Basic Multilingual Plane in a class of its own:
    # CodeQL reads such a range as `\ufffd-\ufffd`, so several in one class look
    # like overlapping ranges (py/overly-large-range). Same characters either way.
    "|[\U0001bca0-\U0001bca3]|[\U0001d173-\U0001d17a]|[\U000e0000-\U000e0fff])")
_INVISIBLE = re.compile(_INVISIBLE_CHAR)
# A run of them right before a slash: removed, it could join a letter to the
# slash and turn `run\u00ad/x` into the path `run/x`, so it reads as a space.
# Matched only from the start of a run (the lookbehind): otherwise every offset
# inside a long run with no slash after it rescans the rest — quadratic time.
_INVISIBLE_BEFORE_SLASH = re.compile("(?<!" + _INVISIBLE_CHAR + ")" + _INVISIBLE_CHAR + "+(?=/)")

# Before the slash: anything except a letter, a dot or another slash — so
# `run:/x`, a backtick, a quote, a bracket, `-/x`, `_/x_` and `1/x` precede an
# invocation, while `docs/x`, `https://host/x`, `a.b/x` and `//x` are paths
# (a digit or `-`/`_` before the slash reads as an invocation since #3274:
# `api/v1/x` style paths now match when their last segment is a gated name).
# After the name: no further letter or digit, no run of `_`/`-` that continues
# into one, and no dot directly before one — so `/x.`, `/x_`, `/x-`, `/x...y`
# and `/x._y` end the name while `/x.v2`, `/x_v2` and `/x--v2` are other skills.
# Every change since #751 only widens what matches (a property test holds it).
_BEFORE = r"(?<![a-z./])/"
_AFTER = r"(?![a-z0-9])(?![_-]+[a-z0-9])(?!\.[a-z0-9])"


def _normalise(text: str, invisible: str = "") -> str:
    text = unicodedata.normalize("NFKC", text)
    if not invisible:
        text = _INVISIBLE_BEFORE_SLASH.sub(" ", text)
    return _INVISIBLE.sub(invisible, text).casefold()


def find_gated_invocations(text: Optional[str], gated_names: Iterable[str]) -> List[str]:
    """The gated names that ``text`` invokes, each once, in order of first use.

    Names are returned as ``gated_names`` spells them. Comparison is
    case-insensitive. An empty gate set costs nothing.
    """
    names = list(gated_names or ())
    if not text or not names:
        return []
    haystacks = [_normalise(text, ""), _normalise(text, " ")]
    found = []
    for name in names:
        pattern = _BEFORE + re.escape(_normalise(name)) + _AFTER
        starts = [m.start() for m in (re.search(pattern, h) for h in haystacks) if m]
        if starts:
            found.append((min(starts), name))
    return [name for _, name in sorted(found)]
