"""Pydantic models for Workspace asks (ent#364). OSS core since ent#428."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class WorkspaceAsk(BaseModel):
    """One ask, as the addressee sees it.

    Deliberately NOT the whole queue row. An operator-queue item carries fields a
    client has no business reading — `context` is agent-authored and may hold
    execution ids, internal paths, whatever the agent put there — so this is an
    explicit projection rather than a dump with a blocklist. A field reaches the
    client because it is named here.
    """
    id: str
    agent_name: str
    kind: str                       # question | approval | alert
    priority: str
    title: str
    question: str
    options: Optional[List[Any]] = None
    # trinity-enterprise#611: the exact action an approval asks this person to
    # approve. Agent-authored like `context`, but named here on purpose: it is the
    # thing being decided, and the prompt tells agents to put it here "so the
    # operator can verify what they are approving". `context` stays off.
    proposal: Optional[Dict[str, Any]] = None
    created_at: str
    expires_at: Optional[str] = None
    # pending | answered | dismissed | cancelled | expired (`dismissed`:
    # trinity-enterprise#748, the addressee chose not to answer). A listing carries pending asks,
    # plus — with `include_ended` — the ones that ended in the last 7 days
    # (trinity-enterprise#611); the ANSWER response projects the row it recorded.
    status: str
    # How the ask ended, COARSE on purpose (trinity-enterprise#611): `ended_by`
    # is `you` | `operator` | `platform` (#3246) | `agent` | `timeout`, never an
    # email (`agent`: the agent replaced it with a newer ask, #3247), and the operator's
    # cancel reason never crosses. `ended_at` is when it ended — None when the
    # platform does not know (a row that ended before the ledger), never the
    # time the ask was filed.
    ended_at: Optional[str] = None
    ended_by: Optional[str] = None
    chat_id: Optional[str] = None   # the thread it was attached to, when known
    # ent#734: raised BY the turn serving `chat_id` (draw it as a tile there),
    # as opposed to a background ask (schedule / loop / gate) whose `chat_id` is
    # Main only as the reply target and which renders in no chat.
    raised_in_turn: bool = False
    # trinity-enterprise#747: the chat this ask's addressee opened to discuss it,
    # when they did. Platform-written; Discuss on an ask that has one continues
    # it instead of opening a second.
    discussion_chat_id: Optional[str] = None
    # #2915: what the platform last established about the agent's own copy of
    # this ask, COARSE on purpose — `confirmed | changed | closed | unconfirmed`.
    # A client never sees the reason (it names the operator's infrastructure)
    # nor a poller timestamp. `aging` is the operator's configured bound.
    sync: str = "unconfirmed"
    aging: bool = False
    # #3242: an approval decided only by its options (platform-minted — a skill
    # gate). The surface hides the "Something else" chip, which the sink would
    # refuse (`not_off_menu`). Says nothing else about the gate.
    decided_by_options: bool = False
    # ent#430 AC #5: whether answering this ask sets work in motion, so a
    # surface can say "answered" without implying the agent started working.
    # Populated only on the ANSWER response — a pending ask has not been
    # answered, so the question does not arise, and defaulting it to False on a
    # listing would read as "answering this does nothing".
    resume_requested: Optional[bool] = None
    # #3247: the two ends of an agent's replace, each the OTHER ask's
    # `request_id` and nothing else about it — and only when this ask's
    # addressee could already see that ask (`service._linked_request_id`).
    replaces: Optional[str] = None
    replaced_by: Optional[str] = None


class WorkspaceAskAnswer(BaseModel):
    """An answer from the addressee.

    `response` is the DECISION — the chosen option or the typed answer — and it
    is what the agent reads: the sync write-back copies it to the queue file
    verbatim and the ent#329 resume framing presents it as "the answer".
    `response_text` is an optional free-text NOTE riding alongside a decision;
    it cannot stand alone (#2375: the Workspace panel used to post a typed
    answer as `response_text`, the service coerced the missing `response` to
    "", and the agent read an empty answer). Both stay Optional at the model so
    the service can refuse with its own named 422 (`empty_answer`) instead of a
    bare validation shape; the service is the gate.
    """
    response: Optional[str] = Field(default=None, max_length=500)
    response_text: Optional[str] = Field(default=None, max_length=4000)
    # #2915: see `OperatorResponse.acknowledge_divergence`.
    acknowledge_divergence: bool = False


# --- trinity-enterprise#610 PR A2, §3g L7 (E1): an ask's context ---------------
#
# Platform data only, and deliberately STRICTER than the Work tab's projection
# (`work/models.py::WorkItem` carries the run id): no `cost` and no
# `execution_id` anywhere below (§6.9). `execution_id` is AGENT-written on the
# queue row, so it is a lookup key the service validates, never a fact it
# forwards.


class WorkspaceAskOriginMessage(BaseModel):
    """One message of the chat the ask came from — an excerpt, never the body
    (`chat_previews._arrival_excerpt`: credentials redacted, markdown stripped,
    at most 280 chars). No `cost`."""
    id: str
    role: str
    at: str
    excerpt: str


class WorkspaceAskOrigin(BaseModel):
    """Where the ask came from. `verified` is True only when the run's portal
    session is the viewer's own thread; only then are `messages` filled (the
    three before the ask). Otherwise it is the ask's own chat — Main, for every
    ingested ask — with no excerpt, because what precedes an ask in Main is
    usually unrelated to it."""
    chat_id: str
    title: Optional[str] = None
    is_main: bool = False
    verified: bool = False
    messages: List[WorkspaceAskOriginMessage] = []


class WorkspaceAskRun(BaseModel):
    """The run that raised the ask — shown only after the agent, live-window and
    audience checks pass. `label` names the schedule for a platform principal
    only ("Asked by the nightly-billing run"); a client reads "Asked during a
    scheduled run" (T12)."""
    kind: str               # schedule | manual | turn | delegated | loop | room | other
    label: str
    started_at: Optional[str] = None


class WorkspaceAskAnswered(BaseModel):
    """One of the viewer's own recent answers to this agent — how they answered
    a similar ask. The answer is an excerpt of the decision, never the note."""
    id: str
    title: str
    answer: Optional[str] = None
    ended_at: Optional[str] = None


class WorkspaceAskContext(BaseModel):
    origin: Optional[WorkspaceAskOrigin] = None
    run: Optional[WorkspaceAskRun] = None
    recent_answers: List[WorkspaceAskAnswered] = []


class WorkspaceAskDiscussion(BaseModel):
    """The chat an ask is discussed in (trinity-enterprise#747). `created` is
    False when Discuss continued the chat an earlier click opened."""
    chat_id: str
    agent_name: str
    title: Optional[str] = None
    created: bool
    ask: WorkspaceAsk
