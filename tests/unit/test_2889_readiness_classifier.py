"""#2889 — a 503 from an exhausted credit balance must FAIL a model-turn test, not skip it.

Three things are pinned here:

1. The classifier itself (`tests/testkit/readiness.py`): header first, breaker
   header second, transport vocabulary third, and — the defect — an
   unattributed 503 is a FAILURE, never a skip.
2. The transport vocabulary against the backend's ACTUAL source, so a wording
   change in `_parse_agent_http_error` / `routers/chat.py` /
   `_circuit_breaker_error` goes red here instead of silently turning a
   readiness race into a spurious failure (the 2am-Friday flake).
3. The call sites: no model-turn test may skip on a bare 503 again. An AST
   guard walks every file that imports the helper and fails on a
   `if <resp>.status_code == 503: pytest.skip(...)` whose `<resp>` came from a
   POST to `/chat`, `/task` or `/fan-out`.

Runs in the unit island; the backend module import for (2) is by source text,
not by importing the service (keeps this file free of the backend's import-time
side effects).
"""

from __future__ import annotations

import ast
import contextlib
import pathlib
import re
from types import SimpleNamespace

import httpx
import pytest

from testkit import readiness as R

TESTS_DIR = pathlib.Path(__file__).resolve().parent.parent
REPO = TESTS_DIR.parent
BACKEND = REPO / "src" / "backend"


def _resp(status=503, body="", headers=None):
    return SimpleNamespace(status_code=status, text=body, headers=dict(headers or {}))


def _detail(text: str) -> str:
    import json

    return json.dumps({"detail": text})


@contextlib.contextmanager
def _must_fail():
    """`pytest.raises(pytest.fail.Exception)` that also refuses a SKIP.

    `Skipped` is not a subclass of `Failed`, so under plain `pytest.raises` a
    helper that wrongly *skips* propagates the skip and the test that exists
    to forbid laundering is itself reported as skipped — green. Mutating the
    helper to "skip every 429" left this file at 0 failed / 6 skipped until
    every fail-expecting case went through here (#2919 review).
    """
    try:
        with pytest.raises(pytest.fail.Exception) as exc:
            yield exc
    except pytest.skip.Exception as e:
        raise AssertionError(f"SKIPPED where it must FAIL (the laundering this file guards): {e}") from None


# --------------------------------------------------------------------------- #
# 1. classification
# --------------------------------------------------------------------------- #

CREDIT_BALANCE_BODY = _detail(
    "Execution failed with no output (exit code 1): Claude Code execution failed "
    "(exit code 1): Credit balance is too low. To resolve: (1) add credits to your "
    "account, or (2) assign a subscription token with available usage in "
    "Settings -> Subscriptions."
)


@pytest.mark.parametrize(
    "body,headers,readiness,code",
    [
        # The issue's exact case, with the #2889 header the backend now sends.
        (CREDIT_BALANCE_BODY, {R.ERROR_CODE_HEADER: "auth"}, False, "auth"),
        # ...and WITHOUT the header (a stack predating it): still a failure —
        # nothing in the body says the agent server was unreachable.
        (CREDIT_BALANCE_BODY, {}, False, "unknown"),
        # Header is authoritative over body: a "network" code is a race even if
        # the prose is odd; a non-network code is a failure even if the prose
        # happens to contain a transport word.
        (_detail("something weird"), {R.ERROR_CODE_HEADER: "network"}, True, "network"),
        (_detail("ConnectError mentioned in an auth message"), {R.ERROR_CODE_HEADER: "auth"}, False, "auth"),
        (_detail("usage limit"), {R.ERROR_CODE_HEADER: "billing"}, False, "billing"),
        # Header value is normalised (case / whitespace).
        (_detail("x"), {R.ERROR_CODE_HEADER: " NETWORK "}, True, "network"),
        # Dispatch breaker (#526): auth-dead, not a race.
        (_detail('{"error": "circuit_open"}'), {"X-Circuit-Open": "true"}, False, "circuit_open"),
        # Transport-shaped bodies, no header: readiness race.
        (_detail("Failed to communicate with agent: HTTP error: ConnectError"), {}, True, "transport"),
        (_detail("Failed to communicate with agent: HTTP error: ReadTimeout"), {}, True, "transport"),
        (_detail("Failed to communicate with agent: HTTP error: PoolTimeout"), {}, True, "transport"),
        (_detail("Agent is not running"), {}, True, "transport"),
        (_detail("Agent unreachable — transport circuit breaker open (no TCP response from the agent)"), {}, True, "transport"),
        (_detail("All connection attempts failed"), {}, True, "transport"),
        # Plain-text (non-JSON) body still classifies.
        ("connection refused", {}, True, "transport"),
        # The agent answered and the turn failed for a non-credential reason:
        # still lost coverage, still a failure.
        (_detail("Execution failed with no output (exit code 137): killed"), {}, False, "unknown"),
        (_detail("Authentication failure: Not logged in. Check subscription token."), {}, False, "unknown"),
        ("", {}, False, "unknown"),
    ],
)
def test_classify_unavailable(body, headers, readiness, code):
    v = R.classify_unavailable(_resp(body=body, headers=headers))
    assert (v.readiness, v.code) == (readiness, code), v


def test_classification_reads_the_full_body_and_truncates_only_the_evidence():
    """A transport marker past the evidence cut must still count — classify on
    the whole body; bound only what goes into the reason text."""
    padding = "x" * (R.EVIDENCE_CHARS + 50)
    v = R.classify_unavailable(_resp(body=_detail(padding + " HTTP error: ConnectError")))
    assert v.readiness and v.code == "transport"
    assert len(v.evidence) <= R.EVIDENCE_CHARS


def test_evidence_is_the_detail_field_when_present():
    v = R.classify_unavailable(_resp(body=CREDIT_BALANCE_BODY))
    assert v.evidence.startswith("Execution failed with no output")
    assert "Credit balance is too low" in v.evidence


def test_classify_never_raises_on_odd_response_objects():
    v = R.classify_unavailable(SimpleNamespace(status_code=503))
    assert v.code == "unknown" and v.evidence == "<empty body>"


# --------------------------------------------------------------------------- #
# require_agent_answer — the call-site helper
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clean_session_state():
    R.reset_session_provider_failure()
    yield
    R.reset_session_provider_failure()


def test_non_turn_statuses_are_a_no_op():
    """Only the statuses a model turn can launder (503 — #2889; 429 — #2919)
    are classified; everything else is the caller's own assertion."""
    for status in (200, 202, 404, 500, 502, 504):
        R.require_agent_answer(_resp(status=status, body="whatever"), what="POST /task")
    assert R.session_provider_failure() is None


# --------------------------------------------------------------------------- #
# #2919 — the 429 twin: billing fails with attribution, capacity skips with
# evidence, and an unattributed 429 is a FAILURE (the same doctrine one status
# code over).
# --------------------------------------------------------------------------- #

USAGE_LIMIT_BODY = _detail(
    "Subscription usage limit: You've hit your usage limit for this subscription; "
    "resets at 2026-09-23T03:00:00Z"
)

# The three live header-less capacity shapes (dossier §4b), verbatim.
CAPACITY_ADMISSION_DICT_BODY = (
    '{"detail": {"error": "Agent queue is full", "agent": "a", "queue_length": 0, '
    '"retry_after": 30, "message": "Agent \'a\' is busy. Please try again later."}}'
)
CAPACITY_BACKLOG_FULL_BODY = _detail(
    "Agent 'a' is at capacity (2 parallel tasks) and its backlog is full. Try again later."
)
CAPACITY_MAP_TASK_BODY = _detail("Agent 'a' is at capacity. Try again later.")


def test_billing_429_fails_with_attribution_and_is_remembered():
    """The issue's case: an exhausted subscription surfaces as 429 (#2638) and
    the backend labels it `billing`; the test must FAIL, not skip."""
    with _must_fail() as exc:
        R.require_agent_answer(
            _resp(status=429, body=USAGE_LIMIT_BODY, headers={R.ERROR_CODE_HEADER: "billing"}),
            what="POST /chat",
        )
    msg = str(exc.value)
    assert "answered 429" in msg and "RAN and failed" in msg and "code=billing" in msg
    assert "usage limit" in msg
    seen = R.session_provider_failure()
    assert seen and seen["what"] == "POST /chat" and seen["verdict"].code == "billing"


@pytest.mark.parametrize(
    "body,fragment",
    [
        (CAPACITY_ADMISSION_DICT_BODY, "Agent queue is full"),
        (CAPACITY_BACKLOG_FULL_BODY, "backlog is full"),
        (CAPACITY_MAP_TASK_BODY, "is at capacity"),
    ],
)
def test_capacity_429_skips_with_evidence_and_is_not_remembered(body, fragment):
    """A genuine admission refusal (the model never ran) still SKIPS — with the
    backend's own wording as evidence — and is never a credential verdict."""
    with pytest.raises(pytest.skip.Exception) as exc:
        R.require_agent_answer(_resp(status=429, body=body), what="POST /task")
    msg = str(exc.value)
    assert "agent at capacity" in msg and "answered 429" in msg
    assert "POST /task" in msg and "code=capacity" in msg and fragment in msg
    assert R.session_provider_failure() is None


def test_capacity_header_is_authoritative_over_a_billing_looking_body():
    """Header beats body in both directions: a producer that says `capacity`
    is believed even when the prose mentions a limit — and nothing is
    recorded against the credential."""
    with pytest.raises(pytest.skip.Exception) as exc:
        R.require_agent_answer(
            _resp(status=429, body=_detail("usage limit"), headers={R.ERROR_CODE_HEADER: "capacity"}),
            what="POST /chat",
        )
    assert "code=capacity" in str(exc.value)
    assert R.session_provider_failure() is None


def test_billing_header_is_authoritative_over_a_capacity_looking_body():
    with _must_fail() as exc:
        R.require_agent_answer(
            _resp(status=429, body=_detail("Agent 'a' is at capacity"), headers={R.ERROR_CODE_HEADER: "billing"}),
            what="POST /chat",
        )
    assert "code=billing" in str(exc.value)
    seen = R.session_provider_failure()
    assert seen and seen["verdict"].code == "billing"


def test_unattributed_429_fails_rather_than_skips():
    """A stack that predates the header answering the agent's usage-limit
    prose: FAIL as `unknown` (not recorded — nothing attributed it to the
    credential). The mirror of #2889's unknown-503 doctrine."""
    with _must_fail() as exc:
        R.require_agent_answer(
            _resp(status=429, body=_detail("Claude Code execution failed: Subscription usage limit reached")),
            what="POST /chat",
        )
    assert "code=unknown" in str(exc.value) and "usage limit" in str(exc.value)
    assert R.session_provider_failure() is None


def test_switched_billing_429_dict_body_fails_and_records():
    """The SUB-003 auto-switched shape: the platform already moved the agent
    to a working subscription, yet the response is still 429+`billing`.

    Documented choice, not an accident: this mirrors #2894's treatment of the
    503 auto-switched `auth` shape (which records too). Treating the
    structural `auto_switch` key as "fail this test, do not arm the cascade"
    is the Q5 follow-up named in the PR body, not this PR."""
    body = (
        '{"detail": {"error": "usage limit", "auto_switch": {"new_subscription": "sub-b"}, '
        '"message": "Rate limit hit. Subscription auto-switched to \'sub-b\'. Please retry.", '
        '"retry_after": 15}}'
    )
    with _must_fail() as exc:
        R.require_agent_answer(
            _resp(status=429, body=body, headers={R.ERROR_CODE_HEADER: "billing"}),
            what="POST /chat",
        )
    assert "auto-switched" in str(exc.value)
    seen = R.session_provider_failure()
    assert seen and seen["verdict"].code == "billing"


def test_capacity_never_indicts_the_credential():
    assert R.CAPACITY_CODE not in R.CREDENTIAL_CODES
    assert R.Verdict(False, "capacity", "x", 429).indicts_credential is False


@pytest.mark.parametrize(
    "body,headers,readiness,code",
    [
        # The three live header-less capacity shapes.
        (CAPACITY_ADMISSION_DICT_BODY, {}, False, "capacity"),
        (CAPACITY_BACKLOG_FULL_BODY, {}, False, "capacity"),
        (CAPACITY_MAP_TASK_BODY, {}, False, "capacity"),
        # Header-less, no capacity wording: unknown, never a skip.
        (_detail("usage limit"), {}, False, "unknown"),
        ("", {}, False, "unknown"),
        # The transport vocabulary is NOT consulted on a 429 — a fall-through
        # to the 503 arm would launder this as a readiness race.
        (_detail("Failed to communicate with agent: HTTP error: ConnectError"), {}, False, "unknown"),
        # The breaker header still wins on a 429.
        (_detail("x"), {"X-Circuit-Open": "true"}, False, "circuit_open"),
        # Header is authoritative, normalised.
        (_detail("x"), {R.ERROR_CODE_HEADER: "network"}, True, "network"),
        (_detail("x"), {R.ERROR_CODE_HEADER: " CAPACITY "}, False, "capacity"),
        (_detail("Agent 'a' is at capacity"), {R.ERROR_CODE_HEADER: "billing"}, False, "billing"),
    ],
)
def test_classify_429(body, headers, readiness, code):
    v = R.classify_unavailable(_resp(status=429, body=body, headers=headers))
    assert (v.readiness, v.code, v.status) == (readiness, code, 429), v


def test_capacity_skip_reason_is_not_on_the_skip_audit_allowlist():
    """The twin of the readiness case: an admission refusal is still a test
    that did not run; `tests/run-full.sh`'s audit must keep flagging it."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("audit_skips", TESTS_DIR / "harness" / "audit_skips.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    reason = R.capacity_skip_reason("POST /task", R.Verdict(False, "capacity", "Agent 'a' is at capacity", 429))
    assert not mod._allowed(reason)


def test_reason_builders_name_the_status():
    v429 = R.Verdict(False, "unknown", "x", 429)
    v503 = R.Verdict(True, "transport", "x")
    assert "429" in R.readiness_skip_reason("POST /task", v429)
    assert "429" in R.execution_failure_reason("POST /task", v429)
    assert "503" in R.readiness_skip_reason("POST /task", v503)
    assert "503" in R.execution_failure_reason("POST /task", v503)


def test_readiness_race_skips_and_names_the_evidence():
    with pytest.raises(pytest.skip.Exception) as exc:
        R.require_agent_answer(
            _resp(body=_detail("Failed to communicate with agent: HTTP error: ConnectError")),
            what="POST /chat",
        )
    msg = str(exc.value)
    assert "agent server still starting" in msg
    assert "POST /chat" in msg and "ConnectError" in msg
    # A race is NOT recorded as a provider failure — the next test must try.
    assert R.session_provider_failure() is None


def test_readiness_skip_reason_is_not_on_the_skip_audit_allowlist():
    """A readiness race that survives the fixture's wait is still lost
    coverage; `tests/run-full.sh`'s audit must keep flagging it."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("audit_skips", TESTS_DIR / "harness" / "audit_skips.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    reason = R.readiness_skip_reason("POST /chat", R.Verdict(True, "transport", "HTTP error: ConnectError"))
    assert not mod._allowed(reason)


def test_credit_balance_503_fails_and_is_remembered_for_the_session():
    with _must_fail() as exc:
        R.require_agent_answer(
            _resp(body=CREDIT_BALANCE_BODY, headers={R.ERROR_CODE_HEADER: "auth"}),
            what="POST /task",
        )
    msg = str(exc.value)
    assert "RAN and failed" in msg and "code=auth" in msg
    assert "Credit balance is too low" in msg
    seen = R.session_provider_failure()
    assert seen and seen["what"] == "POST /task" and seen["verdict"].code == "auth"


def test_first_provider_failure_wins_the_session_slot():
    R.record_provider_failure("first", R.Verdict(False, "auth", "a"))
    R.record_provider_failure("second", R.Verdict(False, "billing", "b"))
    assert R.session_provider_failure()["what"] == "first"


def test_unknown_503_fails_rather_than_skips():
    """The defect: an unattributed 503 hid behind a readiness excuse."""
    with _must_fail():
        R.require_agent_answer(_resp(body=_detail("Failed to execute task. The agent may be unavailable.")), what="POST /task")


@pytest.mark.parametrize(
    "code,status,cascades",
    [
        ("auth", 503, True),
        ("billing", 503, True),
        ("circuit_open", 503, True),
        ("unknown", 503, False),
        ("agent_error", 503, False),
        ("timeout", 503, False),
        # #2919: the same rule one status over.
        ("billing", 429, True),
        ("unknown", 429, False),
    ],
)
def test_only_credential_codes_fail_the_rest_of_the_session_fast(code, status, cascades):
    """A one-off OOM (`agent_error`/`unknown`) fails ITS test; it must not take
    every later model turn down with it unrun. A credential verdict does."""
    headers = {} if code == "unknown" else {R.ERROR_CODE_HEADER: code}
    with _must_fail():
        R.require_agent_answer(_resp(status=status, body=_detail("boom"), headers=headers), what="POST /task")
    assert (R.session_provider_failure() is not None) is cascades


# --------------------------------------------------------------------------- #
# 2. transport vocabulary ↔ backend source contract
# --------------------------------------------------------------------------- #


def _src(rel: str) -> str:
    return (BACKEND / rel).read_text()


def test_backend_embeds_the_httpx_class_name_the_markers_match():
    """`_parse_agent_http_error` builds `"HTTP error: {type(e).__name__}"`, so
    the transport vocabulary is the httpx exception class names, lower-cased."""
    src = _src("services/chat_execution_service.py")
    assert 'error_msg = f"HTTP error: {type(e).__name__}"' in src
    for cls in (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout, httpx.WriteTimeout):
        assert cls.__name__.lower() in R.TRANSPORT_BODY_MARKERS, cls.__name__


def test_backend_not_running_and_breaker_wording_still_match():
    chat_router = _src("routers/chat.py")
    assert 'detail="Agent is not running"' in chat_router
    assert "agent is not running" in R.TRANSPORT_BODY_MARKERS

    tes = _src("services/task_execution_service.py")
    assert "transport circuit breaker open" in tes
    assert "transport circuit breaker open" in R.TRANSPORT_BODY_MARKERS


def test_backend_error_code_header_name_matches():
    src = _src("services/chat_execution_service.py")
    assert f'ERROR_CODE_HEADER = "{R.ERROR_CODE_HEADER}"' in src
    # The one value the classifier treats as "never reached" is the enum's.
    env = _src("services/execution_envelope.py")
    assert f'NETWORK = "{R.NETWORK_CODE}"' in env


def test_transport_markers_carry_no_credential_vocabulary():
    """No 'credit balance'/'authentication' table here by design (#904 drift
    class) — a provider word in the transport list would launder the defect
    back in as a skip."""
    for bad in ("credit", "billing", "auth", "token", "subscription", "unauthorized"):
        assert not any(bad in m for m in R.TRANSPORT_BODY_MARKERS), bad


def test_capacity_markers_carry_no_credential_vocabulary():
    """#2919: the same rule for the capacity tuple — a usage-limit word in it
    would launder an exhausted subscription back into a queue-full skip."""
    for bad in ("credit", "billing", "auth", "token", "subscription", "unauthorized", "limit", "rate"):
        assert not any(bad in m for m in R.CAPACITY_BODY_MARKERS), bad


def test_backend_capacity_wording_still_matches():
    """The header-less fallback is pinned to the three producers' actual
    wording (`routers/chat.py` admission dict; `_dispatch_async` and
    `_map_task_failure` both say "is at capacity")."""
    chat_router = _src("routers/chat.py")
    assert '"error": "Agent queue is full"' in chat_router
    ces = _src("services/chat_execution_service.py")
    assert ces.count("is at capacity") == 2, ces.count("is at capacity")
    source = (chat_router + ces).lower()
    for marker in R.CAPACITY_BODY_MARKERS:
        assert marker == marker.lower() and marker in source, marker


def test_backend_capacity_code_name_matches():
    env = _src("services/execution_envelope.py")
    assert f'CAPACITY = "{R.CAPACITY_CODE}"' in env


# --------------------------------------------------------------------------- #
# 3. call-site guard — no model-turn test skips on a bare 503 or 429
# --------------------------------------------------------------------------- #

TURN_URL = re.compile(r"/(task|chat|fan-out)['\"]?$")


def _files_importing_helper():
    out = []
    for path in list(TESTS_DIR.glob("test_*.py")) + list((TESTS_DIR / "agent_server").glob("test_*.py")):
        if "from testkit.readiness import require_agent_answer" in path.read_text():
            out.append(path)
    return out


def _turn_response_vars(fn: ast.AST) -> set[str]:
    """Names assigned from a `.post(<url ending in /task|/chat|/fan-out>)`."""
    names = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not (isinstance(call.func, ast.Attribute) and call.func.attr == "post"):
            continue
        if not call.args:
            continue
        url = call.args[0]
        text = None
        if isinstance(url, ast.Constant) and isinstance(url.value, str):
            text = url.value
        elif isinstance(url, ast.JoinedStr):
            text = "".join(v.value for v in url.values if isinstance(v, ast.Constant))
        if text and TURN_URL.search(text):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
    return names


def _is_laundered_constant(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value in R.LAUNDERED_STATUSES


def _is_laundered_status_check(test: ast.AST):
    """`X.status_code == <s>`, `X.status_code in [<s>, ...]` / `(...)` / `{...}`,
    or an `or` of such checks → X, else None — for any `s` in
    `readiness.LAUNDERED_STATUSES` (503 #2889, 429 #2919), so the vocabulary
    has one home: the helper that classifies those statuses.

    Known escapes, accepted as #2894 accepted them (written down, not
    asserted): a bound that is not the status itself (`>= 400`), Yoda
    `429 == x`, a local
    `status = resp.status_code` alias, `from pytest import skip`, and
    `.post(url_var)` (the URL is not a literal, so `_turn_response_vars`
    never sees the response).
    """
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or):
        for value in test.values:
            var = _is_laundered_status_check(value)
            if var is not None:
                return var
        return None
    if not isinstance(test, ast.Compare) or len(test.comparators) != 1:
        return None
    left = test.left
    if not (isinstance(left, ast.Attribute) and left.attr == "status_code" and isinstance(left.value, ast.Name)):
        return None
    # Operator-agnostic, as #2894's `_is_503_check` was: `>= 429`, `!= 503`,
    # `not in [503]` all stay flagged (fail closed — a false positive is one
    # rewritten line, a false negative is a laundered failure).
    comp = test.comparators[0]
    if _is_laundered_constant(comp):
        return left.value.id
    if isinstance(comp, (ast.List, ast.Tuple, ast.Set)) and any(
        _is_laundered_constant(e) for e in comp.elts
    ):
        return left.value.id
    return None


@pytest.mark.parametrize("src,expected", [
    ("resp.status_code == 429", "resp"),
    ("resp.status_code == 503", "resp"),
    ("resp.status_code in [429, 503]", "resp"),
    ("resp.status_code in (429,)", "resp"),
    ("resp.status_code in {429}", "resp"),
    ("resp.status_code == 429 or resp.status_code == 503", "resp"),
    ("resp.status_code == 200 or resp.status_code == 429", "resp"),
    # Operator-agnostic like #2894's matcher — narrowing it would unguard 503.
    ("resp.status_code >= 429", "resp"),
    ("resp.status_code != 503", "resp"),
    ("resp.status_code not in [503]", "resp"),
    ("resp.status_code == 200", None),
    ("resp.status_code in [200, 202]", None),
    ("other.status == 429", None),
    ("resp.status_code == 429 and flaky", None),
])
def test_guard_matcher_recognises_the_laundered_shapes(src, expected):
    assert _is_laundered_status_check(ast.parse(src, mode="eval").body) == expected


def test_guard_walk_still_covers_the_2919_site_files():
    """The walk is pre-filtered on the import literal (learnings 2026-07-29):
    pin that the four files #2919 cleaned stay inside it, so dropping the
    import cannot silently take a file out of the guard."""
    walked = {p.relative_to(REPO).as_posix() for p in _files_importing_helper()}
    assert {
        "tests/test_agent_chat.py",
        "tests/test_dynamic_thinking_status.py",
        "tests/test_parallel_task.py",
        "tests/agent_server/test_agent_chat_direct.py",
    } <= walked, sorted(walked)


def test_no_model_turn_site_skips_on_a_bare_503_or_429():
    files = _files_importing_helper()
    assert len(files) >= 8, [p.name for p in files]
    offenders = []
    for path in files:
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            turn_vars = _turn_response_vars(fn)
            if not turn_vars:
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.If):
                    continue
                var = _is_laundered_status_check(node.test)
                if var not in turn_vars:
                    continue
                for stmt in ast.walk(node):
                    if (
                        isinstance(stmt, ast.Call)
                        and isinstance(stmt.func, ast.Attribute)
                        and stmt.func.attr == "skip"
                        and isinstance(stmt.func.value, ast.Name)
                        and stmt.func.value.id == "pytest"
                    ):
                        offenders.append(f"{path.relative_to(REPO)}:{stmt.lineno}")
    assert not offenders, (
        f"{len(offenders)} model-turn 503/429 skip(s) on a bare status check — "
        "route it through testkit.readiness.require_agent_answer instead "
        "(#2889, #2919):\n  " + "\n  ".join(offenders)
    )


def test_every_require_agent_answer_test_is_marked_requires_model():
    """The marker is what opts a test into the session preflight; a helper
    call without it fails per-site but never fails fast."""
    missing = []
    for path in _files_importing_helper():
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or not fn.name.startswith("test_"):
                continue
            uses = any(
                isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "require_agent_answer"
                for c in ast.walk(fn)
            )
            if not uses:
                continue
            marked = any(
                isinstance(d, ast.Attribute) and d.attr == "requires_model" for d in fn.decorator_list
            )
            if not marked:
                missing.append(f"{path.relative_to(REPO)}:{fn.lineno} {fn.name}")
    assert not missing, "\n".join(missing)


def test_requires_model_marker_is_registered():
    assert '"requires_model:' in (REPO / "pyproject.toml").read_text()
