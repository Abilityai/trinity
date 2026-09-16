"""The journey tier's model gate is decided by the instance, not the harness (#2812).

J03's first turn and J10's two model-asserting tests used to gate on
`os.getenv("ANTHROPIC_API_KEY")` in the **pytest process**. That is the harness
host, not the instance under test: a stack whose agents authenticate by
subscription (SUB-003) has no such variable on the host yet answers normally, so
those journeys skipped PERMANENTLY — and invisibly, because the reason is
allowlisted in `tests/harness/audit_skips.py`.

A gate that cannot fail is worse than no gate: #2336's per-PR journey-smoke and
#2350's merge-enforced Journey Impact declaration were both green while
asserting nothing about a real model answer.

These tests run in the unit tier — no live stack — by driving the helper with a
fake client, so the gate's own logic is covered even where the journeys cannot
run.
"""

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_JOURNEY_CONFTEST = _ROOT / "tests" / "journeys" / "conftest.py"


def _load_journey_conftest():
    """Import `tests/journeys/conftest.py` by path.

    By path rather than as a package: `tests/unit/pytest.ini` sets
    `norecursedirs = ..`, so the unit island deliberately cannot see its sibling
    directories, and a plain import would depend on sys.path shape.
    """
    spec = importlib.util.spec_from_file_location("_journey_conftest", _JOURNEY_CONFTEST)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_UNSET_SENTINEL = object()


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    """Serves both halves of the composite gate, and records what was asked.

    `auth_mode` answers the per-agent read; `claude_auth_configured` answers the
    instance read. They are separate knobs precisely because the journey-smoke
    failure was the combination "agent routed to api_key, instance holds no key".
    """

    def __init__(
        self,
        auth_mode="subscription",
        claude_auth_configured=True,
        status_code=200,
        payload=_UNSET_SENTINEL,
        raises=False,
        flags_status_code=200,
    ):
        self.auth_mode = auth_mode
        self.claude_auth_configured = claude_auth_configured
        self.status_code = status_code
        self.payload = payload
        self.raises = raises
        self.flags_status_code = flags_status_code
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        if self.raises:
            raise RuntimeError("transport blew up")
        if path == "/api/settings/feature-flags":
            return _FakeResponse(
                self.flags_status_code,
                {"claude_auth_configured": self.claude_auth_configured},
            )
        payload = self.payload
        if payload is _UNSET_SENTINEL:
            payload = {"agent_name": "a", "auth_mode": self.auth_mode}
        return _FakeResponse(self.status_code, payload)


@pytest.fixture()
def jc():
    module = _load_journey_conftest()
    # The helper memoises per agent name for the session; a shared dict across
    # tests would let one case decide another's verdict.
    module._AUTH_MODE_CACHE.clear()
    return module


@pytest.mark.parametrize("mode", ["subscription", "api_key"])
def test_agent_with_a_credential_runs(jc, mode):
    """A callee reporting either working auth mode does not skip."""
    client = _FakeClient(auth_mode=mode)
    assert jc.skip_unless_agent_can_answer(client, "a") == mode


def test_not_configured_skips_with_the_allowlisted_reason(jc):
    """The skip must stay matchable by `tests/harness/audit_skips.py`."""
    client = _FakeClient(auth_mode="not_configured")
    with pytest.raises(pytest.skip.Exception) as exc:
        jc.skip_unless_agent_can_answer(client, "a")
    reason = str(getattr(exc.value, "msg", exc.value))
    assert jc.MODEL_SKIP_REASON in reason
    # The agent and the mode go into the message, so a permanent skip is
    # diagnosable from the run output alone — the property #2812 was missing.
    assert "a" in reason and "not_configured" in reason


def test_skip_reason_is_still_allowlisted_by_the_audit(jc):
    """Belt and braces: run the real matcher over the real message.

    `MODEL_SKIP_REASON` is a verbatim substring of an `audit_skips` entry.
    Reword it and every skip in the tier becomes unallowlisted — which turns
    the skip audit red rather than failing quietly, but only if something
    checks. This is that something.
    """
    audit = importlib.util.spec_from_file_location(
        "_audit_skips", _ROOT / "tests" / "harness" / "audit_skips.py"
    )
    module = importlib.util.module_from_spec(audit)
    audit.loader.exec_module(module)

    client = _FakeClient(auth_mode="not_configured")
    with pytest.raises(pytest.skip.Exception) as exc:
        jc.skip_unless_agent_can_answer(client, "pytest-ephemeral-journey-ab12cd34")
    assert module._allowed(str(getattr(exc.value, "msg", exc.value)))


@pytest.mark.parametrize(
    "client",
    [
        _FakeClient(status_code=404, payload=None),
        _FakeClient(status_code=200, payload={}),
        _FakeClient(raises=True),
    ],
    ids=["404", "empty-body", "transport-error"],
)
def test_unreadable_auth_status_degrades_to_a_visible_skip(jc, client):
    """An unreadable gate skips rather than running.

    Same direction as the old missing-key gate: a harness that loses access
    degrades to a visible, allowlisted skip instead of a confusing assertion
    failure deep inside a chat turn.
    """
    with pytest.raises(pytest.skip.Exception) as exc:
        jc.skip_unless_agent_can_answer(client, "a")
    assert jc.MODEL_SKIP_REASON in str(getattr(exc.value, "msg", exc.value))


def test_gate_asks_about_the_named_agent(jc):
    """J10 passes the CALLEE, so the gate must ask about the name it is given."""
    client = _FakeClient(auth_mode="subscription")
    jc.skip_unless_agent_can_answer(client, "agent-b")
    assert client.paths == [
        "/api/subscriptions/agents/agent-b/auth",
        "/api/settings/feature-flags",
    ]


def test_auth_mode_is_read_once_per_agent(jc):
    """The mode is set at create and changed only by auto-switch, so re-reading
    it per test buys nothing — and the journey tier is wall-clock budgeted."""
    client = _FakeClient(auth_mode="api_key")
    for _ in range(4):
        jc.skip_unless_agent_can_answer(client, "same-agent")
    # One per-agent read + one instance read, however many times it is called.
    assert client.paths == [
        "/api/subscriptions/agents/same-agent/auth",
        "/api/settings/feature-flags",
    ]

    # A DIFFERENT agent is still asked about — the per-agent cache is per agent,
    # not a single global verdict. J10 has two agents and only the callee
    # matters. The instance read stays cached.
    jc.skip_unless_agent_can_answer(client, "other-agent")
    assert client.paths.count("/api/settings/feature-flags") == 1
    assert "/api/subscriptions/agents/other-agent/auth" in client.paths


def test_no_journey_file_reads_the_provider_key_from_the_host():
    """The defect itself: no journey may READ `ANTHROPIC_API_KEY` from the host.

    Matched over the AST, not the source text. The prose in these files
    necessarily names the variable to explain why it is no longer consulted —
    including inside a docstring, which no comment-prefix check can see — and a
    guard that greps would either fire on its own explanation or force the
    explanation out. It looks for the two ways the value can actually be read:
    `os.getenv("ANTHROPIC_API_KEY", ...)` and `os.environ[...]` /
    `os.environ.get(...)`.
    """
    import ast

    KEY = "ANTHROPIC_API_KEY"
    offenders = []

    for path in sorted((_ROOT / "tests" / "journeys").rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            # os.getenv(KEY) / os.environ.get(KEY)
            if isinstance(node, ast.Call):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and arg.value == KEY:
                        offenders.append(
                            f"{path.relative_to(_ROOT)}:{node.lineno}: reads {KEY} in a call"
                        )
            # os.environ["ANTHROPIC_API_KEY"]
            if isinstance(node, ast.Subscript):
                sl = node.slice
                if isinstance(sl, ast.Constant) and sl.value == KEY:
                    offenders.append(
                        f"{path.relative_to(_ROOT)}:{node.lineno}: subscripts {KEY}"
                    )

    assert not offenders, (
        "a journey decides a model gate from the pytest host's environment "
        "again (#2812) — ask the instance via "
        "`skip_unless_agent_can_answer` instead:\n  " + "\n  ".join(offenders)
    )


def test_routed_to_a_key_the_instance_does_not_have_skips(jc):
    """The journey-smoke regression, pinned.

    `get_agent_auth_mode` derives purely from DB state, and
    `use_platform_api_key` is a per-agent ROUTING FLAG — not evidence that a key
    exists. On the credential-free CI stack (`.env.example` ships
    `ANTHROPIC_API_KEY=` empty) a fresh agent still reports
    `auth_mode="api_key"`. Gating on that alone ran the keyed journeys against a
    keyless stack and FAILED them — the #2812 defect pointing the other way.
    """
    client = _FakeClient(auth_mode="api_key", claude_auth_configured=False)
    with pytest.raises(pytest.skip.Exception) as exc:
        jc.skip_unless_agent_can_answer(client, "a")
    reason = str(getattr(exc.value, "msg", exc.value))
    assert jc.MODEL_SKIP_REASON in reason
    assert "claude_auth_configured=False" in reason


def test_subscription_stack_with_no_platform_key_still_runs(jc):
    """The case #2812 exists to fix must not regress.

    A subscription-authenticated stack has no `ANTHROPIC_API_KEY` anywhere —
    not on the host, not as a platform key — yet answers normally.
    `claude_auth_configured` is True because a subscription is registered.
    """
    client = _FakeClient(auth_mode="subscription", claude_auth_configured=True)
    assert jc.skip_unless_agent_can_answer(client, "a") == "subscription"


def test_unreadable_feature_flags_skips(jc):
    """An unreadable instance read skips, like every other arm of this gate."""
    client = _FakeClient(auth_mode="api_key", flags_status_code=503)
    with pytest.raises(pytest.skip.Exception) as exc:
        jc.skip_unless_agent_can_answer(client, "a")
    assert jc.MODEL_SKIP_REASON in str(getattr(exc.value, "msg", exc.value))
