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

# The ONE platform-reserved decision every approval accepts besides its own
# options (#3242): "none of these — here is what to do instead". The person's
# instruction travels in `response_text`, never as `response` (the #2375 class).
# Markdown-inert and plain English on purpose: a human can read it raw in an
# export, and `__x__` would render bold wherever it leaks. Mirrored verbatim in
# `src/frontend/src/utils/operatorQueue.js` and `src/mcp-server/src/types.ts`
# (parity-tested). Never filtered out of `usable_options`: an approval whose
# only option is this literal must stay closed to every other string.
SOMETHING_ELSE = "(something else)"


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


class ReservedAnswerError(ValueError):
    """A refusal of the reserved `SOMETHING_ELSE` decision (#3242). Each
    subclass carries the named `code` both writers answer with as a 422."""

    code = "reserved_answer"


class InstructionRequiredError(ReservedAnswerError):
    """`SOMETHING_ELSE` with no instruction in `response_text`: an answer the
    agent cannot act on — "none of these" alone is Deny or a dismissal."""

    code = "instruction_required"

    def __init__(self):
        super().__init__(
            f"{SOMETHING_ELSE!r} means none of the offered options; the "
            "instruction for what to do instead must be in `response_text`."
        )


class ReservedValueError(ReservedAnswerError):
    """`SOMETHING_ELSE` on an item that is not an approval: there is no menu to
    step off, and the agent would read a refusal of options it never offered."""

    code = "reserved_value"

    def __init__(self):
        super().__init__(
            f"{SOMETHING_ELSE!r} is reserved for approvals; answer a question "
            "with the answer itself."
        )


class NotOffMenuError(ReservedAnswerError):
    """`SOMETHING_ELSE` on a platform-minted approval (a skill gate counts only
    its own options, and no agent reads the text): decided by its options."""

    code = "not_off_menu"

    def __init__(self):
        super().__init__("This approval is decided by its options; pick one of them.")


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


def validate_response_choice(
    item: dict, response: Optional[str], *, response_text: Optional[str]
) -> None:
    """Raise `ResponseNotOfferedError` when an approval's decision was not offered.

    `SOMETHING_ELSE` is handled FIRST (#3242): accepted on any approval whose
    `response_text` carries an instruction (`InstructionRequiredError` when it is
    blank), refused on every other item type (`ReservedValueError`). It is
    checked before membership, so an agent that offered the literal itself gets
    the reserved meaning, not a second one. `response_text` is keyword-only and
    required so a stale two-argument call fails loudly instead of skipping the
    instruction rule.

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
    if response == SOMETHING_ELSE:
        if (item or {}).get("type") != "approval":
            raise ReservedValueError()
        if not (response_text or "").strip():
            raise InstructionRequiredError()
        return
    choices = usable_options(item)
    if choices is None:
        return
    if response not in choices:
        raise ResponseNotOfferedError(response, choices)
