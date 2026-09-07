"""Chat titles — the one validator for a person-set conversation title (ent#473).

A Workspace thread and a room both carry a title a person can now edit, from
two different routers over two different tables. The boundary rules are the
same on both — one line, non-empty, length-capped, no control characters — so
they live in ONE dependency-free leaf rather than in two routers that merely
happen to agree. `client_portal/` and `shared_sessions/` both import from here;
the frontend mirrors the same rules in `portalUtils.normalizeChatTitle` so a
person is told before the request, and the server is the authority.

The cap is deliberately WIDER than the generated-title cap
(`client_portal.service._TITLE_MAX_CHARS`, 60): a model is asked for something
sidebar-shaped, a person is allowed to be precise. The sidebar truncates with
CSS either way.

Also here: the greeting shape. ent#186 titles a thread from its opening exchange
exactly once, so a thread that opens with "hi" keeps a greeting-era title
forever; ent#473 tries once more after the next real exchange when the opener
was greeting-shaped. The predicate is a leaf too, so the decision table in the
service can be tested without a database.
"""
from __future__ import annotations

import re

CHAT_TITLE_MAX_CHARS = 100

# The reason tokens a refusal carries. Tokens, not sentences: the copy is
# authored once in `chat_title_problem`, and a test can pin the branch without
# pinning the prose.
TITLE_EMPTY = "empty"
TITLE_MULTILINE = "multiline"
TITLE_TOO_LONG = "too_long"

_CONTROL_RE = re.compile(r"[\x00-\x09\x0b\x0c\x0e-\x1f\x7f]")


def normalize_chat_title(raw) -> tuple[str | None, str | None]:
    """``(title, None)`` for an acceptable title, ``(None, reason)`` otherwise.

    Normalisation is what a person would expect a text field to do — outer
    whitespace dropped, runs of inner whitespace collapsed — and nothing that
    changes meaning: no case folding, no punctuation stripping (a person who
    types "Q3 invoices?" meant the question mark; the generated-title sanitiser
    strips trailing punctuation because a MODEL adds it by habit).

    A line break anywhere INSIDE the trimmed text is a refusal rather than a
    silent join: a pasted two-line note is not a title, and joining it would
    render a sentence the person never wrote.
    """
    if not isinstance(raw, str):
        return None, TITLE_EMPTY
    s = raw.strip()
    if "\n" in s or "\r" in s:
        return None, TITLE_MULTILINE
    s = " ".join(_CONTROL_RE.sub(" ", s).split())
    if not s:
        return None, TITLE_EMPTY
    if len(s) > CHAT_TITLE_MAX_CHARS:
        return None, TITLE_TOO_LONG
    return s, None


def chat_title_problem(reason: str, raw=None) -> str:
    """The sentence a refusal carries — what happened, what to do, an example
    (design-system principle 17). ``raw`` lets the too-long case say by how much."""
    if reason == TITLE_MULTILINE:
        return "A title is one line — remove the line breaks. Example: Q3 invoice discrepancy"
    if reason == TITLE_TOO_LONG:
        have = len(" ".join(str(raw or "").split()))
        return (
            f"Keep the title to {CHAT_TITLE_MAX_CHARS} characters or fewer "
            f"(this one is {have}). Example: Q3 invoice discrepancy"
        )
    return "A title can't be empty. Example: Q3 invoice discrepancy"


# --- Greeting shape ----------------------------------------------------------

_GREETING_RE = re.compile(
    r"^(?:hi|hello|hey|heya|hiya|hello there|hi there|howdy|yo|greetings|"
    r"good (?:morning|afternoon|evening|day)|sup|what'?s up|"
    r"are you there|anyone there|is anyone there|"
    r"test|testing|ping|thanks|thank you|ok|okay)\b"
)
_GREETING_MAX_WORDS = 8


def is_greeting(text) -> bool:
    """True when ``text`` is greeting-shaped: a short message that opens with a
    salutation, a check-in or a test word — the shape ent#186's prompt was told
    to look past and could not, because there was no topic to find.

    Short is part of the shape. "Hi, can you pull the Q3 invoices for Acme"
    starts with a greeting and names a topic; a model titles that fine, and a
    second pass would only spend a call to arrive at the same title.
    """
    if not isinstance(text, str):
        return False
    s = " ".join(text.strip().lower().split())
    if not s:
        return False
    if len(s.split()) > _GREETING_MAX_WORDS:
        return False
    return bool(_GREETING_RE.match(s))
