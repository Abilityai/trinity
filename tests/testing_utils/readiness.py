"""Classify a 503 or 429 from a model turn: a race / admission refusal, or a turn that ran and failed (#2889, #2919).

Roughly forty live-tier tests POST ``/api/agents/{name}/chat`` or ``/task`` and
used to ``pytest.skip("Agent server not ready")`` on ANY 503. The backend
answers 503 for two unrelated things:

* the agent server could not be reached at all (``HTTP error: ConnectError``,
  ``Agent is not running``, the transport breaker) — a readiness race the
  ``created_agent`` fixture's 45 s wait did not fully cover; and
* the agent WAS reached, the turn ran, and it failed — an exhausted credit
  balance, a rejected token, an OOM'd process, a usage limit.

The second is lost coverage reported as a benign skip, which contradicts the
doctrine in ``docs/testing/STRATEGY.md`` (a skip is not a pass; an infra-caused
skip is a finding). This module holds the ONE classification policy, so the two
causes can never again share a reason string.

The same gap exists one status code over (#2919): a 429 is either an admission
refusal — the agent's slots and backlog are full, the model never ran, a
genuine ``capacity`` skip — or an exhausted subscription that the agent
surfaces as 429 (#2638) and the backend labels ``billing``. Eighteen sites
used to ``pytest.skip("Agent queue full")`` on either.

Signal precedence, strongest first:

1. ``X-Trinity-Error-Code`` — the backend's own ``TaskExecutionErrorCode`` for
   the failure (#2889 emits it on every sync ``/chat``/``/task`` 4xx/5xx it
   classifies). ``network`` is the only value that means "never reached";
   ``capacity`` (emitted by the three admission-refusal sites since #2919) is
   the only value that means "refused before any model turn".
2. ``X-Circuit-Open`` — the dispatch breaker (#526) fed by prior AUTH failures.
3. On a 429, a capacity-shaped body, for a stack that predates the header:
   the router's "Agent queue is full", the service's "is at capacity". The
   transport vocabulary is deliberately NOT consulted on a 429 — no producer
   emits transport words on one, and a fall-through would be a laundering path.
4. On a 503, a transport-shaped body, for a stack that predates the header: the
   httpx exception class names ``_parse_agent_http_error`` embeds, the router's
   "Agent is not running", the transport breaker's own wording.
5. Otherwise: the agent answered, the turn failed → the test fails and names
   the body. Unknown is deliberately NOT a skip — the whole defect was an
   unattributed status hiding behind a readiness (or queue-full) excuse. On a
   stack that predates the ``billing`` header a usage-limit 429 therefore fails
   each test individually instead of arming the session cascade — the correct
   direction; do not "fix" it with a credential substring table.

Only transport vocabulary is matched here. There is deliberately no
"credit balance"/"authentication" substring table: the repo already carries two
intentionally separate auth classifiers (``services/failure_classifier.py`` and
the agent's ``error_classifier.py``) with a documented drift history (#904), and
a third copy in the test tree would be the next one to drift.

Stdlib-only, so both the live tiers (``tests/``) and the unit island
(``tests/unit/``) import it; ``tests/unit/test_2889_readiness_classifier.py``
pins the transport vocabulary to the backend's actual source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional

import pytest

ERROR_CODE_HEADER = "X-Trinity-Error-Code"
CIRCUIT_OPEN_HEADER = "X-Circuit-Open"

# The header value that means "the agent server was never reached".
NETWORK_CODE = "network"

# The header value that means "admission refused before any model turn"
# (#2919 makes the three CapacityFull producers emit it; the body vocabulary
# below is the fallback for a stack that predates that).
CAPACITY_CODE = "capacity"

# The statuses a model turn can answer with a cause the status alone cannot
# name: 503 (#2889 — unreachable vs ran-and-failed) and 429 (#2919 —
# admission refused vs an exhausted subscription, #2638).
LAUNDERED_STATUSES: frozenset[int] = frozenset({503, 429})

# Transport-shaped fragments, matched case-insensitively against the FULL body.
# Each entry names the backend site that emits it; the unit test pins them.
TRANSPORT_BODY_MARKERS: tuple[str, ...] = (
    # chat_execution_service._parse_agent_http_error: "HTTP error: <httpx class>"
    "connecterror",
    "connecttimeout",
    "readtimeout",
    "pooltimeout",
    "writetimeout",
    "remoteprotocolerror",
    # httpx's own text for a refused connect, when the raw body leaks through
    "connection refused",
    "all connection attempts failed",
    # routers/chat.py — the container is not running at all
    "agent is not running",
    # task_execution_service._circuit_breaker_error (transport breaker)
    "transport circuit breaker open",
    # chat_execution_service._dispatch_sync_backlog / task_execution_service
    "failed to connect to agent",
)

# Capacity-shaped fragments, matched case-insensitively against the FULL body
# of a 429 that carries no header. Each names its backend site; the unit test
# pins them to source.
CAPACITY_BODY_MARKERS: tuple[str, ...] = (
    # routers/chat.py — /chat admission CapacityFull (dict-shaped detail)
    "agent queue is full",
    # chat_execution_service._dispatch_async (CapacityFull, backlog full too)
    # and _map_task_failure's 429 branch — both say "is at capacity"
    "is at capacity",
)

# Evidence in a skip/fail reason is bounded; classification is NOT (it runs on
# the full body — a marker past the cut must still count).
EVIDENCE_CHARS = 300


# Codes that indict the CREDENTIAL rather than one turn: once seen, every later
# model turn in the session would spend its call learning the same thing.
CREDENTIAL_CODES: frozenset[str] = frozenset({"auth", "billing", "circuit_open"})


@dataclass(frozen=True)
class Verdict:
    """One 503's or 429's classification.

    ``readiness`` — the agent server was not reachable: a race, skip with evidence.
    ``code`` — the backend's error code when it sent one, else a derived label
    (``circuit_open``, ``transport``, ``capacity``, ``unknown``).
    ``evidence`` — the bounded, human-readable reason text.
    ``status`` — the HTTP status that was classified (trailing default so the
    positional ``Verdict(readiness, code, evidence)`` constructions still work).
    """

    readiness: bool
    code: str
    evidence: str
    status: int = 503

    @property
    def indicts_credential(self) -> bool:
        """True when the failure is the credential's, not this one turn's —
        the session then fails later model turns fast. An ``unknown`` or
        ``agent_error`` 503 fails only the test that saw it: a one-off OOM
        must not take 50 unrelated tests down with a cascade nobody ran."""
        return self.code in CREDENTIAL_CODES


def _body_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    content = getattr(response, "content", b"")
    if isinstance(content, (bytes, bytearray)):
        return content.decode("utf-8", errors="replace")
    return str(content or "")


def _headers(response: Any) -> Mapping[str, str]:
    headers = getattr(response, "headers", None)
    return headers if headers is not None else {}


def _detail_text(body: str) -> str:
    """The ``detail`` field when the body is FastAPI JSON, else the raw body.
    Cheap and regex-based on purpose — a body that is not JSON must still
    classify, and ``json`` on an arbitrary 503 page is the wrong tool."""
    m = re.search(r'"detail"\s*:\s*"((?:[^"\\]|\\.)*)"', body)
    if m:
        return m.group(1)
    return body


def classify_unavailable(response: Any) -> Verdict:
    """Classify a response the caller has already established is a 503 or 429.

    Never raises. Reads the header first, then the breaker header, then the
    status-specific body vocabulary on the full body (capacity on a 429,
    transport on a 503); anything else is "the turn ran and failed" with the
    body as evidence. The status is read here (not passed) so the signature
    stays the one its three callers, incl. ``tests/conftest.py``, use.
    """
    headers = _headers(response)
    body = _body_text(response)
    status = getattr(response, "status_code", 503)
    evidence = " ".join(_detail_text(body).split())[:EVIDENCE_CHARS] or "<empty body>"

    code = headers.get(ERROR_CODE_HEADER) if hasattr(headers, "get") else None
    if isinstance(code, str) and code:
        code = code.strip().lower()
        return Verdict(readiness=(code == NETWORK_CODE), code=code, evidence=evidence, status=status)

    circuit = headers.get(CIRCUIT_OPEN_HEADER) if hasattr(headers, "get") else None
    if isinstance(circuit, str) and circuit.lower() == "true":
        return Verdict(readiness=False, code="circuit_open", evidence=evidence, status=status)

    low = body.lower()
    if status == 429:
        # Capacity wording or nothing: the transport vocabulary is NOT
        # consulted on a 429 (no producer emits it there; falling through
        # would add a laundering path).
        if any(marker in low for marker in CAPACITY_BODY_MARKERS):
            return Verdict(readiness=False, code=CAPACITY_CODE, evidence=evidence, status=status)
        return Verdict(readiness=False, code="unknown", evidence=evidence, status=status)

    if any(marker in low for marker in TRANSPORT_BODY_MARKERS):
        return Verdict(readiness=True, code="transport", evidence=evidence, status=status)

    return Verdict(readiness=False, code="unknown", evidence=evidence, status=status)


# Session-level memory of the first "the credential cannot execute" verdict, so
# later model-turn tests fail fast instead of each spending a 120 s call to
# learn the same thing (read by tests/conftest.py::pytest_runtest_setup).
_SESSION_PROVIDER_FAILURE: dict = {}


def record_provider_failure(what: str, verdict: Verdict) -> None:
    _SESSION_PROVIDER_FAILURE.setdefault("what", what)
    _SESSION_PROVIDER_FAILURE.setdefault("verdict", verdict)


def session_provider_failure() -> Optional[dict]:
    return dict(_SESSION_PROVIDER_FAILURE) if _SESSION_PROVIDER_FAILURE else None


def reset_session_provider_failure() -> None:
    _SESSION_PROVIDER_FAILURE.clear()


def readiness_skip_reason(what: str, verdict: Verdict) -> str:
    return (
        f"agent server still starting — {what} answered {verdict.status} with no agent response "
        f"(code={verdict.code}); evidence: {verdict.evidence}"
    )


def capacity_skip_reason(what: str, verdict: Verdict) -> str:
    return (
        f"agent at capacity — {what} answered {verdict.status} before any model turn "
        f"(code={verdict.code}); evidence: {verdict.evidence}"
    )


def execution_failure_reason(what: str, verdict: Verdict) -> str:
    return (
        f"{what} answered {verdict.status} from a turn that RAN and failed — provider credential "
        f"unusable or execution error, not a readiness race (code={verdict.code}); "
        f"evidence: {verdict.evidence}"
    )


def require_agent_answer(response: Any, *, what: str = "the model turn") -> None:
    """No-op unless ``response`` is a 503 or 429. Then: a readiness race or an
    admission refusal skips with the evidence in the reason; anything the agent
    answered FAILS the test (a credential-class code also arms the session
    cascade).

    ``what`` names the call for the reason text (``"POST /chat"``).
    """
    if getattr(response, "status_code", None) not in LAUNDERED_STATUSES:
        return
    verdict = classify_unavailable(response)
    if verdict.readiness:
        pytest.skip(readiness_skip_reason(what, verdict))
    if verdict.code == CAPACITY_CODE:
        pytest.skip(capacity_skip_reason(what, verdict))
    if verdict.indicts_credential:
        record_provider_failure(what, verdict)
    pytest.fail(execution_failure_reason(what, verdict), pytrace=False)
