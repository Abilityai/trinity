"""
The repo-binding ENDPOINT surface (trinity-enterprise#109).

`test_ent109_repo_binding.py` guards the service — classification, the CAS, the
ordering. This module guards the router: the layer that produced the behaviour
an operator actually sees, and where the auth gates, the locks, the idempotency
claim and the audit row live.

Six things are checked here because none of them is visible to a service-level
test:

1. **`reject_agent_principal` is really called.** `OwnedAgentByName` alone is
   not enough: an agent-scoped MCP key resolves to its OWNER *carrying the
   owner's role*, so on a default admin-owned install any agent's injected
   `TRINITY_MCP_API_KEY` passes an owner/role gate. This endpoint creates
   external GitHub state, persists a credential and replaces a container —
   operator-scale blast radius (trinity-ops-agent#232; #1644/#1816 precedent).

2. **The path param matches the handler parameter.** A `{agent_name}` route
   bound to a handler taking `name` 422s every single call and no service test
   can see it (#1069).

3. **The locks FAIL CLOSED.** A Redis outage must 503, not wave the operation
   through — two repo creates and two concurrent recreates of one container is
   a materially worse outcome than a refusal.

4. **The idempotency key is verb-folded**, so a client reusing one
   `Idempotency-Key` across different actions on the same agent cannot replay
   the wrong snapshot.

5. **Every exit path audits** (#905). A mutating, partially-irreversible
   operation whose failures are invisible to the audit trail is exactly the gap
   the sync/pull handlers were fixed for.

6. **`agent_name` resolves through the enumeration-safe dependency**
   (Invariant #8). The uniform-404 BEHAVIOUR is proven parametrically in
   `test_186_enumeration_uniformity.py`; what no dependency-level test can see
   is whether *this* endpoint is wired to it rather than to a hand-rolled
   lookup with a 404-then-403 split.

The handlers are plain `async def`s, so they are called directly rather than
through a TestClient: the assertions are about the handler's own branches, and
resolving the auth dependency is FastAPI's job, not this test's. Items 2 and 6
are the exception and are checked by route introspection.

Module: src/backend/routers/git.py
Issue:  abilityai/trinity-enterprise#109
"""

from __future__ import annotations

import inspect
import os
import re
import sys
import tempfile
import types
from pathlib import Path

import pytest

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
_TMP_DB = Path(tempfile.gettempdir()) / "trinity_test_ent109_endpoint.db"
os.environ.setdefault("TRINITY_DB_PATH", str(_TMP_DB))

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = str(_PROJECT_ROOT / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# Pinned at import time (sys.modules victim-side trap).
from fastapi import HTTPException  # noqa: E402

import dependencies  # noqa: E402
import routers.git as git_router  # noqa: E402
import models  # noqa: E402
from models import BindAgentRepoRequest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

pytestmark = pytest.mark.unit

PAT = "ghp_endpoint_secret"
DEST = "alice/my-agent-brain"


def _user(*, agent_name=None, role="admin", username="alice"):
    return types.SimpleNamespace(
        id=1,
        username=username,
        email=f"{username}@example.com",
        role=role,
        agent_name=agent_name,
    )


def _request():
    return types.SimpleNamespace(
        client=types.SimpleNamespace(host="127.0.0.1"),
        url=types.SimpleNamespace(path="/api/agents/bot/git/bind-to-own-repo"),
        state=types.SimpleNamespace(request_id="req-1"),
    )


def _body():
    return BindAgentRepoRequest(destination_repo=DEST, github_pat=PAT, private=True)


class _FakeRedis:
    """SETNX lock double. `ping_raises` / `set_raises` simulate an outage."""

    def __init__(self, *, ping_raises=False, set_raises=False, held=()):
        self.store = {k: "someone-else" for k in held}
        self.ping_raises = ping_raises
        self.set_raises = set_raises
        self.deleted = []

    def ping(self):
        if self.ping_raises:
            raise ConnectionError("redis down")
        return True

    def set(self, key, val, nx=False, ex=None):
        if self.set_raises:
            raise ConnectionError("redis down")
        if nx and key in self.store:
            return False
        self.store[key] = val
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.deleted.append(key)
        self.store.pop(key, None)


class _Harness:
    """Mutable shared state — a SimpleNamespace would copy the values, so
    `harness.bind_raises = X` in a test would never reach the closures below.
    """

    def __init__(self, redis, decision, bind_error_cls, outcome):
        self.redis = redis
        self.decision = decision
        self.BindError = bind_error_cls
        self.outcome = outcome
        self.bind_raises = None
        self.bind_calls = []
        self.audits = []
        self.idem = []

    def set_redis(self, r):
        self.redis = r


@pytest.fixture
def endpoint(monkeypatch):
    """The real handler with redis, audit, idempotency and the service stubbed."""
    from services.agent_service.repo_binding import BindError

    class _Decision:
        def __init__(self):
            self.replay = False
            self.in_flight = False
            self.snapshot = None

    outcome = types.SimpleNamespace(
        agent_name="bot",
        github_repo=DEST,
        previous_repo="Abilityai/cornelius",
        default_branch="main",
        private=True,
        created_repo=True,
        reused_existing=False,
        recreated=True,
        audit={"github_repo": DEST, "previous_repo": "Abilityai/cornelius"},
    )

    h = _Harness(_FakeRedis(), _Decision(), BindError, outcome)

    async def fake_audit(**kwargs):
        h.audits.append(kwargs)

    monkeypatch.setattr(git_router, "_audit_git", fake_audit)

    # `_bind_locks` does a function-local `from routers.auth import
    # get_redis_client`, so seeding sys.modules is what reaches it.
    monkeypatch.setitem(
        sys.modules,
        "routers.auth",
        types.SimpleNamespace(get_redis_client=lambda: h.redis),
    )

    # The handler does `from services import idempotency_service` — an
    # attribute lookup on the ALREADY-IMPORTED package, which a sys.modules
    # setitem does not touch. Patch the real module's functions instead.
    import services.idempotency_service as idem_mod

    def begin(scope, key):
        h.idem.append(("begin", scope, key))
        return h.decision

    monkeypatch.setattr(idem_mod, "begin", begin)
    monkeypatch.setattr(
        idem_mod, "complete", lambda d, e, s: h.idem.append(("complete", s))
    )
    monkeypatch.setattr(idem_mod, "fail", lambda d: h.idem.append(("fail",)))

    async def fake_bind(**kwargs):
        h.bind_calls.append(kwargs)
        if h.bind_raises is not None:
            raise h.bind_raises
        return h.outcome

    # This one IS reached via sys.modules: `from services.agent_service.
    # repo_binding import ...` resolves the submodule through sys.modules.
    monkeypatch.setitem(
        sys.modules,
        "services.agent_service.repo_binding",
        types.SimpleNamespace(BindError=BindError, bind_agent_to_own_repo=fake_bind),
    )

    return h


async def _call(endpoint, *, user=None, idempotency_key=None):
    return await git_router.bind_agent_to_own_repo(
        agent_name="bot",
        body=_body(),
        request=_request(),
        current_user=user or _user(),
        idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------------
# 1. Human-only
# ---------------------------------------------------------------------------


class TestHumanOnly:
    @pytest.mark.asyncio
    async def test_agent_scoped_key_is_403_even_when_it_resolves_to_an_admin(
        self, endpoint
    ):
        """The trinity-ops-agent#232 trap: `get_current_user` maps an
        agent-scoped MCP key to its owner WITH the owner's role, so on a default
        admin-owned install a role/owner gate alone is satisfied by any agent's
        injected key."""
        agent_principal = _user(agent_name="some-agent", role="admin")
        with pytest.raises(HTTPException) as exc:
            await _call(endpoint, user=agent_principal)
        assert exc.value.status_code == 403
        # Refused BEFORE anything is attempted.
        assert endpoint.bind_calls == []

    @pytest.mark.asyncio
    async def test_human_principal_passes(self, endpoint):
        result = await _call(endpoint)
        assert result.github_repo == DEST
        assert len(endpoint.bind_calls) == 1

    def test_the_guard_is_wired_in_the_handler_not_merely_imported(self):
        """An import with no call site would leave the endpoint open while
        looking protected in a diff."""
        src = inspect.getsource(git_router.bind_agent_to_own_repo)
        assert "reject_agent_principal(current_user)" in src


# ---------------------------------------------------------------------------
# 2. Route wiring (#1069 class)
# ---------------------------------------------------------------------------


class TestRouteWiring:
    def _routes(self):
        return {
            r.path: r
            for r in git_router.router.routes
            if "bind-to-own-repo" in getattr(r, "path", "")
        }

    def test_both_routes_are_registered(self):
        paths = set(self._routes())
        assert paths == {
            "/api/agents/{agent_name}/git/bind-to-own-repo",
            "/api/agents/{agent_name}/git/bind-to-own-repo/status",
        }

    @pytest.mark.parametrize(
        "path",
        [
            "/api/agents/{agent_name}/git/bind-to-own-repo",
            "/api/agents/{agent_name}/git/bind-to-own-repo/status",
        ],
    )
    def test_path_param_matches_the_handler_parameter(self, path):
        """A `{agent_name}` route bound to a handler taking `name` 422s every
        call, and no service-level test can see it (#1069)."""
        route = self._routes()[path]
        declared = set(re.findall(r"{(\w+)}", path))
        params = set(inspect.signature(route.endpoint).parameters)
        assert (
            declared <= params
        ), f"{path} declares {declared} but the handler takes {params}"

    def test_methods_are_as_documented(self):
        routes = self._routes()
        assert "POST" in routes["/api/agents/{agent_name}/git/bind-to-own-repo"].methods
        assert (
            "GET"
            in routes["/api/agents/{agent_name}/git/bind-to-own-repo/status"].methods
        )

    @pytest.mark.parametrize(
        "path,expected_dependency",
        [
            (
                "/api/agents/{agent_name}/git/bind-to-own-repo",
                "get_owned_agent_by_name",
            ),
            (
                "/api/agents/{agent_name}/git/bind-to-own-repo/status",
                "get_authorized_agent_by_name",
            ),
        ],
    )
    def test_agent_name_resolves_through_the_enumeration_safe_dependency(
        self, path, expected_dependency
    ):
        """Invariant #8: route through the dependency, never a 404-then-403
        split.

        Both helpers return a UNIFORM 404 for an agent that does not exist and
        one the caller cannot reach, evaluating existence and access before
        branching — behaviour proven parametrically in
        `test_186_enumeration_uniformity.py`. Re-asserting it here would only
        re-test the shared dependency. What that test cannot see is whether THIS
        endpoint is wired to it, so the assertion is the identity of the
        callable actually bound to `agent_name` — not a name that happens to
        match, and not an import with no call site.

        The scopes are not interchangeable: the mutating verb is owner-scoped
        (it creates external GitHub state, persists a credential and replaces a
        container) while the read-only status verb is read-scoped, because it
        discloses nothing the Git tab does not already show. Swapping them
        would either lock a shared reader out of a surface they can already see
        or let one rebind an agent they do not own.
        """
        route = self._routes()[path]
        bound = {d.name: d.call for d in route.dependant.dependencies}
        assert "agent_name" in bound, (
            f"{path} does not resolve `agent_name` through a dependency at all "
            "— a hand-rolled lookup is how the 404-then-403 enumeration oracle "
            "gets reintroduced (Invariant #8)"
        )
        assert bound["agent_name"] is getattr(dependencies, expected_dependency), (
            f"{path} must bind `agent_name` to "
            f"dependencies.{expected_dependency}, got "
            f"{getattr(bound['agent_name'], '__name__', bound['agent_name'])!r}"
        )


# ---------------------------------------------------------------------------
# 3. Locks fail CLOSED
# ---------------------------------------------------------------------------


class TestLocksFailClosed:
    @pytest.mark.asyncio
    async def test_redis_unreachable_is_503_not_an_unlocked_run(self, endpoint):
        """`_agent_data_op_lock` fails OPEN, calibrated for a tar round-trip.
        Here a lost lock means two repo creates, two CAS writes and two
        concurrent recreates of one container."""
        endpoint.set_redis(_FakeRedis(ping_raises=True))
        with pytest.raises(HTTPException) as exc:
            await _call(endpoint)
        assert exc.value.status_code == 503
        assert exc.value.detail["code"] == "BIND_OP_IN_PROGRESS"
        assert exc.value.headers.get("Retry-After")
        assert endpoint.bind_calls == [], "the operation must NOT have run"

    @pytest.mark.asyncio
    async def test_a_raising_set_also_fails_closed(self, endpoint):
        """Redis answering PING then dying mid-acquire is the same verdict."""
        endpoint.set_redis(_FakeRedis(set_raises=True))
        with pytest.raises(HTTPException) as exc:
            await _call(endpoint)
        assert exc.value.status_code == 503
        assert endpoint.bind_calls == []

    @pytest.mark.asyncio
    async def test_destination_lock_is_keyed_by_destination_not_agent(self, endpoint):
        """The real race is two DIFFERENT agents on one destination, which a
        per-agent lock never serializes."""
        import hashlib

        held = "agent:bind_dest:" + hashlib.sha256(DEST.lower().encode()).hexdigest()
        endpoint.set_redis(_FakeRedis(held=[held]))
        with pytest.raises(HTTPException) as exc:
            await _call(endpoint)
        assert exc.value.status_code == 503
        assert endpoint.bind_calls == []

    @pytest.mark.asyncio
    async def test_destination_key_is_case_folded(self, endpoint):
        """GitHub slugs are case-insensitive: `Alice/Brain` must take the same
        lock as `alice/brain`, or the lock is trivially bypassed."""
        import hashlib

        held = "agent:bind_dest:" + hashlib.sha256(DEST.lower().encode()).hexdigest()
        redis = _FakeRedis(held=[held])
        endpoint.set_redis(redis)
        with pytest.raises(HTTPException):
            await git_router.bind_agent_to_own_repo(
                agent_name="bot",
                body=BindAgentRepoRequest(
                    destination_repo=DEST.upper().replace(
                        "MY-AGENT-BRAIN", "My-Agent-Brain"
                    ),
                    github_pat=PAT,
                    private=True,
                ),
                request=_request(),
                current_user=_user(),
                idempotency_key=None,
            )

    @pytest.mark.asyncio
    async def test_agent_lock_blocks_a_double_submit(self, endpoint):
        endpoint.set_redis(_FakeRedis(held=["agent:bind_op:bot"]))
        with pytest.raises(HTTPException) as exc:
            await _call(endpoint)
        assert exc.value.status_code == 503

    @pytest.mark.asyncio
    async def test_locks_are_released_on_success(self, endpoint):
        await _call(endpoint)
        assert len(endpoint.redis.deleted) == 2

    @pytest.mark.asyncio
    async def test_locks_are_released_on_failure(self, endpoint):
        endpoint.bind_raises = endpoint.BindError(
            409, "BIND_DESTINATION_EXISTS", "nope"
        )
        with pytest.raises(HTTPException):
            await _call(endpoint)
        assert (
            len(endpoint.redis.deleted) == 2
        ), "a failed bind must not wedge the destination until the TTL expires"


# ---------------------------------------------------------------------------
# 4. Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_key_is_verb_folded(self, endpoint):
        await _call(endpoint, idempotency_key="abc123")
        begin = next(c for c in endpoint.idem if c[0] == "begin")
        assert begin[1] == "agent:bot"
        assert begin[2] == "bind_to_own_repo:abc123", (
            "a client reusing one key across verbs must not replay the wrong "
            "snapshot (learnings 2026-07-01)"
        )

    @pytest.mark.asyncio
    async def test_absent_header_derives_no_key(self, endpoint):
        """Unlike a webhook retry this is a deliberate human action, and a
        derived key would swallow an intentional re-bind to the same
        destination — the documented recovery from a partial failure."""
        await _call(endpoint)
        begin = next(c for c in endpoint.idem if c[0] == "begin")
        assert begin[2] is None

    @pytest.mark.asyncio
    async def test_in_flight_replay_is_409(self, endpoint):
        endpoint.decision.replay = True
        endpoint.decision.in_flight = True
        with pytest.raises(HTTPException) as exc:
            await _call(endpoint, idempotency_key="abc")
        assert exc.value.status_code == 409
        assert endpoint.bind_calls == []

    @pytest.mark.asyncio
    async def test_completed_replay_returns_the_snapshot_with_the_header(
        self, endpoint
    ):
        endpoint.decision.replay = True
        endpoint.decision.snapshot = {"agent_name": "bot", "github_repo": DEST}
        resp = await _call(endpoint, idempotency_key="abc")
        assert resp.headers["X-Idempotent-Replay"] == "true"
        assert endpoint.bind_calls == [], "a replay must not re-run the binding"

    @pytest.mark.asyncio
    async def test_failure_releases_the_claim_so_a_retry_can_proceed(self, endpoint):
        endpoint.bind_raises = endpoint.BindError(502, "BIND_PUSH_FAILED", "boom")
        with pytest.raises(HTTPException):
            await _call(endpoint, idempotency_key="abc")
        assert ("fail",) in endpoint.idem


# ---------------------------------------------------------------------------
# 5. Audit on every exit path (#905)
# ---------------------------------------------------------------------------


class TestAuditCoverage:
    @pytest.mark.asyncio
    async def test_success_audits(self, endpoint):
        await _call(endpoint)
        assert len(endpoint.audits) == 1
        a = endpoint.audits[0]
        assert a["success"] is True
        assert a["action"] == "bind_to_own_repo"
        assert a["details"]["github_repo"] == DEST

    @pytest.mark.asyncio
    async def test_named_refusal_audits_before_raising(self, endpoint):
        endpoint.bind_raises = endpoint.BindError(
            409, "BIND_DESTINATION_EXISTS", "has data"
        )
        with pytest.raises(HTTPException):
            await _call(endpoint)
        assert len(endpoint.audits) == 1
        a = endpoint.audits[0]
        assert a["success"] is False
        assert a["details"]["code"] == "BIND_DESTINATION_EXISTS"

    @pytest.mark.asyncio
    async def test_partial_failure_records_that_it_was_partial(self, endpoint):
        endpoint.bind_raises = endpoint.BindError(
            502, "BIND_RECREATE_FAILED", "docker died", partial=True
        )
        with pytest.raises(HTTPException):
            await _call(endpoint)
        assert endpoint.audits[0]["details"]["partial"] is True

    @pytest.mark.asyncio
    async def test_lock_contention_audits(self, endpoint):
        endpoint.set_redis(_FakeRedis(ping_raises=True))
        with pytest.raises(HTTPException):
            await _call(endpoint)
        assert len(endpoint.audits) == 1
        assert endpoint.audits[0]["success"] is False

    @pytest.mark.asyncio
    async def test_unexpected_error_audits_and_becomes_a_500(self, endpoint):
        endpoint.bind_raises = RuntimeError("something nobody predicted")
        with pytest.raises(HTTPException) as exc:
            await _call(endpoint)
        assert exc.value.status_code == 500
        assert endpoint.audits[0]["details"]["code"] == "BIND_UNEXPECTED_ERROR"

    @pytest.mark.asyncio
    async def test_pat_never_reaches_the_audit_trail(self, endpoint):
        await _call(endpoint)
        assert PAT not in repr(endpoint.audits)


# ---------------------------------------------------------------------------
# 5b. The PAT cannot become a header-injection / log-leak vector (/cso S1)
# ---------------------------------------------------------------------------


class TestPatCharsetGuard:
    """A PAT is sent as `Authorization: Bearer <pat>` and embedded in a git
    remote URL. h11 rejects an illegal header value by ECHOING it, so a token
    carrying `\r` or `\n` — what a paste from a terminal or clipboard routinely
    picks up — puts the RAW token in the exception message, which then reaches
    the 500 response body and the Vector-captured platform log.

    Guarded at the MODEL boundary rather than at each error handler, so every
    consumer of a PAT field inherits it and a future one cannot forget.
    """

    @pytest.mark.parametrize(
        "bad",
        [
            "ghp_token\r\nX-Injected: 1",   # header injection, the malicious shape
            "ghp_tok en",                   # embedded space
            "ghp_token\x00",                # NUL
            "ghp_tokené",                   # non-ASCII → UnicodeEncodeError downstream
            "ghp_a\tb",                     # tab
        ],
    )
    def test_header_unsafe_tokens_are_rejected(self, bad):
        with pytest.raises(ValidationError):
            models.BindAgentRepoRequest(
                destination_repo=DEST, github_pat=bad, private=True
            )

    def test_the_rejection_message_does_not_quote_the_value(self):
        """The validator's OWN message must not echo what it rejected. Pydantic
        separately puts the value in `errors()[0]["input"]`, which is why
        `main.py` strips that field from every 422 body — asserted in
        `test_ent109_validation_error_no_input.py`."""
        secret = "ghp_THE_USERS_REAL_TOKEN bad"
        with pytest.raises(ValidationError) as exc:
            models.BindAgentRepoRequest(
                destination_repo=DEST, github_pat=secret, private=True
            )
        assert secret not in exc.value.errors()[0]["msg"]

    @pytest.mark.parametrize(
        "padded",
        ["  ghp_valid_token\n", "ghp_valid_token\r\n", "\tghp_valid_token ", "ghp_valid_token\r"],
    )
    def test_surrounding_whitespace_is_stripped_not_rejected(self, padded):
        """The single commonest real input by far: a good token that picked up a
        trailing newline from a terminal or clipboard. Rejecting it would be
        user-hostile for the case that motivates the guard; stripping keeps it
        working, and passing it through unstripped is the leak."""
        m = models.BindAgentRepoRequest(
            destination_repo=DEST, github_pat=padded, private=True
        )
        assert m.github_pat.get_secret_value() == "ghp_valid_token"

    @pytest.mark.parametrize(
        "good",
        ["ghp_" + "a" * 36, "github_pat_" + "B9_" * 10, "ghs_xyz", "v1.abcdef0123"],
    )
    def test_real_token_formats_still_pass(self, good):
        m = models.BindAgentRepoRequest(
            destination_repo=DEST, github_pat=good, private=True
        )
        assert m.github_pat.get_secret_value() == good

    def test_empty_is_still_rejected(self):
        with pytest.raises(ValidationError):
            models.BindAgentRepoRequest(
                destination_repo=DEST, github_pat="   ", private=True
            )

    def test_the_create_path_model_carries_the_same_guard(self):
        """ForkToOwnRequest (ent#93) feeds the SAME GitHubService constructor
        through crud._apply_fork_to_own, which sits deliberately outside
        create_agent_internal's try. Same class, same fix site — asserted so the
        variant cannot regress independently."""
        with pytest.raises(ValidationError):
            models.ForkToOwnRequest(
                destination_repo=DEST, github_pat="ghp_x\nX-Injected: 1", private=True
            )
        m = models.ForkToOwnRequest(
            destination_repo=DEST, github_pat=" ghp_ok\n", private=True
        )
        assert m.github_pat.get_secret_value() == "ghp_ok"


class TestUnexpectedErrorIsScrubbed:
    @pytest.mark.asyncio
    async def test_a_foreign_exception_echoing_the_pat_does_not_reach_the_response(
        self, endpoint
    ):
        """Belt for the class the charset guard closes at the boundary: the next
        library that echoes a credential in an exception message is not knowable
        in advance, so the one handler that surfaces a raw exception string
        scrubs it."""
        endpoint.bind_raises = RuntimeError(
            f"Illegal header value b'Bearer {PAT}'"
        )
        with pytest.raises(HTTPException) as exc:
            await _call(endpoint)
        assert exc.value.status_code == 500
        assert PAT not in str(exc.value.detail)
        assert "***" in exc.value.detail["error"]


# ---------------------------------------------------------------------------
# 6. Response body
# ---------------------------------------------------------------------------


class TestResponse:
    @pytest.mark.asyncio
    async def test_carries_no_credential_and_names_both_repos(self, endpoint):
        r = await _call(endpoint)
        assert PAT not in r.model_dump_json()
        assert r.github_repo == DEST
        assert r.previous_repo == "Abilityai/cornelius"
        assert r.repo_url == f"https://github.com/{DEST}"
        assert r.recreated is True
