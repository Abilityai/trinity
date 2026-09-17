"""The one rule for "is this a decision the agent actually offered?" (#2376).

`OperatorResponse.response` is a bare `str`, and every layer passed it through
verbatim — router, DB write, and the write-back into the agent's own queue file.
Nothing checked that an **approval** item's recorded decision was one of the
options the agent offered, so #2370 shipped `response: "approved"` against
`options: ["Approve", "Deny"]` for five months without a single 4xx. The agent
reads back a decision string it never offered and has to guess.

This is the approval channel for irreversible actions (#1402 poison-park,
ent#329 respond -> resume, the TARGET_ARCHITECTURE v2 human gate), and it has
four producers today — the desktop store, `/m`, the Workspace asks panel and the
MCP tool. The next one can re-ship the class unless the SINK refuses, which is
why the check lives here rather than in any one router.

A leaf on purpose: `operator_queue_service` instantiates its sync-service
singleton and imports `AgentClient` at module scope, and the Workspace asks path
must not drag either in to answer a question about a list of strings.
"""

from typing import Optional, Sequence

# The placeholder `operator_queue_service` substitutes when an agent's own
# options blob blows the ingestion size cap (#1632). It is NOT an offered
# choice — it is the record that the choices were dropped — so an item wearing
# it has no usable options and is exempt below.
OPTIONS_DROPPED_MARKER = "(options omitted: exceeded size cap)"


class ResponseNotOfferedError(ValueError):
    """An approval decision that is not one of the item's own options.

    Carries the offered list so the caller can name it: a 422 that says only
    "invalid" leaves the operator guessing at strings the AGENT authored.
    """

    code = "response_not_an_offered_option"

    def __init__(self, response: str, options: Sequence[str]):
        self.response = response
        self.options = list(options)
        super().__init__(
            f"{response!r} is not one of the options this approval offered: "
            f"{self.options}"
        )


def usable_options(item: dict) -> Optional[list]:
    """The options an approval item genuinely offered, or None.

    None means "this item does not constrain the answer" — a question or an
    alert, an approval that offered nothing, a malformed blob, or one whose
    options were dropped at ingestion. Every one of those must stay answerable,
    so they are all spelled as the same absence rather than as special cases at
    the call site.
    """
    if (item or {}).get("type") != "approval":
        return None
    options = item.get("options")
    if not isinstance(options, list) or not options:
        return None
    # An entry that is not a string cannot be matched against, and a list that
    # is only the dropped-marker offered nothing.
    choices = [o for o in options if isinstance(o, str) and o != OPTIONS_DROPPED_MARKER]
    return choices or None


def validate_response_choice(item: dict, response: Optional[str]) -> None:
    """Raise `ResponseNotOfferedError` when an approval's decision was not offered.

    Exact string match, deliberately. The options are AGENT-authored, so the
    agent is the only party that knows whether `"approve"` and `"Approve"` mean
    the same thing to it — normalising here would silently answer that question
    on its behalf, and the whole point is that the recorded decision is one the
    agent can compare against its own list.

    An empty or absent `response` is NOT rejected here — emptiness is each
    entry point's own contract, not this validator's: the operator route
    requires `response` at the model (`OperatorResponse.response: str`) and the
    Workspace asks path refuses a missing/blank decision with its named
    `empty_answer` 422 (#2375 — it previously accepted note-only bodies and
    coerced the decision to "", which is how agents received empty answers).
    This function answers ONE question: when a decision IS present on an
    approval, was it one the agent offered?
    """
    if not response:
        return
    choices = usable_options(item)
    if choices is None:
        return
    if response not in choices:
        raise ResponseNotOfferedError(response, choices)
