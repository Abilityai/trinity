"""#3164 — fork-to-own and repo binding use the creator's SAVED personal GitHub
token when the form leaves the token out — and never the platform token.

Before: `ForkToOwnRequest.github_pat` / `BindAgentRepoRequest.github_pat` were
required at every layer, so a user who had saved their own token in Settings
was asked for it again. The resolver already found it (`resolve_github_pat`,
tier `per_user`); the fork path used it only to READ the template.

The tiers are not equivalent: the per-user token is the user's own GitHub
identity — exactly what a fork needs — while the GLOBAL platform token must
never become a fork's write identity (the destination would belong to the
platform account, and `github_pat_tier = "fork"` would bake the platform PAT
into the agent's per-agent row, ent#162 Decision 2).

Sync tests, driving the async code with `asyncio.run` (the ent#14 suite's
convention: `tests/unit/pytest.ini` runs `asyncio_mode = auto`).
"""
import asyncio
import types

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import models
import services.agent_service.crud as crud

pytestmark = pytest.mark.unit

FORM = "ghp_form_token"
SAVED = "ghp_saved_personal"
GLOBAL = "ghp_platform_global"
DEST = "alice/my-brain"


def _config(pat=None):
    block = types.SimpleNamespace(
        destination_repo=DEST,
        github_pat=types.SimpleNamespace(get_secret_value=lambda: pat) if pat else None,
        private=True,
    )
    return types.SimpleNamespace(
        template="github:acme/second-brain", fork_to_own=block, source_branch=None,
        import_intent=None, source_mode=None, kind=None,
    )


def _user():
    return types.SimpleNamespace(id=1, username="alice", email="alice@example.com")


@pytest.fixture
def fork(monkeypatch):
    calls = []

    async def fake_fork(**kw):
        calls.append(kw)
        return types.SimpleNamespace(destination_repo=kw["destination_repo"], default_branch="main")

    monkeypatch.setattr(crud, "fork_template_to_own_repo", fake_fork)
    monkeypatch.setattr(crud.db, "get_git_config_agent_names_for_repo", lambda repo: [])
    return calls


def _apply(config, *, pat, tier):
    return asyncio.run(crud._apply_fork_to_own(
        config=config, current_user=_user(), gh_template=None,
        github_repo_for_agent="acme/second-brain", github_pat_for_agent=pat,
        github_pat_tier=tier, url_branch=None, source_metadata={}, source_metadata_reason=None,
    ))


# ---------------------------------------------------------------------------
# The request models
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model", [models.ForkToOwnRequest, models.BindAgentRepoRequest])
def test_the_token_is_optional(model):
    m = model(destination_repo=DEST, private=True)
    assert m.github_pat is None


@pytest.mark.parametrize("model", [models.ForkToOwnRequest, models.BindAgentRepoRequest])
def test_a_supplied_token_is_still_validated(model):
    with pytest.raises(ValidationError):
        model(destination_repo=DEST, github_pat="   ", private=True)
    with pytest.raises(ValidationError):
        model(destination_repo=DEST, github_pat="ghp_x\nX-Injected: 1", private=True)


# ---------------------------------------------------------------------------
# Fork-to-own at create
# ---------------------------------------------------------------------------

def test_a_saved_personal_token_forks_when_the_form_leaves_it_out(fork):
    repo, pat, tier, upstream = _apply(_config(), pat=SAVED, tier="per_user")
    assert fork[0]["user_pat"] == SAVED
    assert (pat, tier) == (SAVED, "fork")          # persisted as the per-agent PAT, as today
    assert repo == DEST and upstream == "acme/second-brain"


@pytest.mark.parametrize("tier,pat", [("global", GLOBAL), ("none", None)])
def test_the_platform_token_is_never_the_fork_identity(fork, tier, pat):
    with pytest.raises(HTTPException) as e:
        _apply(_config(), pat=pat, tier=tier)
    assert e.value.status_code == 400
    assert e.value.detail["code"] == "FORK_PAT_REQUIRED"
    assert "Settings" in e.value.detail["error"]
    assert fork == []                              # nothing reached GitHub


def test_a_form_token_wins_over_the_saved_one(fork):
    _repo, pat, tier, _up = _apply(_config(FORM), pat=SAVED, tier="per_user")
    assert fork[0]["user_pat"] == FORM and pat == FORM and tier == "fork"


def test_a_failing_saved_token_says_it_was_the_saved_one(monkeypatch, fork):
    async def refuse(**kw):
        raise HTTPException(status_code=400, detail={"error": "GitHub token is invalid or expired.",
                                                     "code": "FORK_PAT_INVALID"})
    monkeypatch.setattr(crud, "fork_template_to_own_repo", refuse)
    with pytest.raises(HTTPException) as e:
        _apply(_config(), pat=SAVED, tier="per_user")
    detail = e.value.detail
    assert detail["code"] == "FORK_PAT_INVALID"
    assert detail["token_source"] == "saved"
    assert "saved GitHub token" in detail["error"] and "Settings" in detail["error"]


def test_a_failing_form_token_keeps_todays_message(monkeypatch, fork):
    async def refuse(**kw):
        raise HTTPException(status_code=400, detail={"error": "GitHub token is invalid or expired.",
                                                     "code": "FORK_PAT_INVALID"})
    monkeypatch.setattr(crud, "fork_template_to_own_repo", refuse)
    with pytest.raises(HTTPException) as e:
        _apply(_config(FORM), pat=SAVED, tier="per_user")
    assert e.value.detail["error"] == "GitHub token is invalid or expired."
    assert "token_source" not in e.value.detail


# ---------------------------------------------------------------------------
# Repo binding (POST /api/agents/{name}/git/bind-to-own-repo)
# ---------------------------------------------------------------------------

@pytest.fixture
def bind(monkeypatch):
    """The real router handler with locks, audit, idempotency and the service
    stubbed (the ent#109 endpoint suite's shape, trimmed)."""
    import sys
    import contextlib
    import routers.git as git_router
    from services.agent_service.repo_binding import BindError
    import services.idempotency_service as idem_mod

    state = types.SimpleNamespace(calls=[], saved=None, raises=None, resolver_calls=0)

    async def fake_audit(**kw):
        return None

    @contextlib.asynccontextmanager
    async def no_locks(agent, dest):
        yield

    async def fake_bind(**kw):
        state.calls.append(kw)
        if state.raises:
            raise state.raises
        return types.SimpleNamespace(
            agent_name="bot", github_repo=DEST, previous_repo="acme/x", default_branch="main",
            private=True, created_repo=True, reused_existing=False, recreated=True,
            audit={"github_repo": DEST})

    decision = types.SimpleNamespace(replay=False, in_flight=False, snapshot=None)
    monkeypatch.setattr(git_router, "_audit_git", fake_audit)
    monkeypatch.setattr(git_router, "_bind_locks", no_locks)
    monkeypatch.setattr(idem_mod, "begin", lambda s, k: decision)
    monkeypatch.setattr(idem_mod, "complete", lambda *a: None)
    monkeypatch.setattr(idem_mod, "fail", lambda *a: None)
    monkeypatch.setitem(sys.modules, "services.agent_service.repo_binding",
                        types.SimpleNamespace(BindError=BindError, bind_agent_to_own_repo=fake_bind))
    monkeypatch.setattr(git_router.db, "get_user_github_pat", lambda uid: state.saved)

    import services.settings_service as ss

    def no_resolver(*a, **k):
        state.resolver_calls += 1
        return GLOBAL, "global"
    monkeypatch.setattr(ss, "resolve_github_pat", no_resolver)
    state.BindError = BindError
    state.router = git_router
    return state


def _bind_call(state, pat=None):
    req = types.SimpleNamespace(client=types.SimpleNamespace(host="127.0.0.1"),
                                scope={"path": "/x"}, state=types.SimpleNamespace(request_id="r"))
    body = models.BindAgentRepoRequest(destination_repo=DEST, private=True,
                                       **({"github_pat": pat} if pat else {}))
    user = types.SimpleNamespace(id=1, username="alice", email="alice@example.com", role="user",
                                 agent_name=None)
    return asyncio.run(state.router.bind_agent_to_own_repo(
        agent_name="bot", body=body, request=req, current_user=user, idempotency_key=None))


def test_bind_uses_the_saved_personal_token(bind):
    bind.saved = SAVED
    _bind_call(bind)
    assert bind.calls[0]["user_pat"] == SAVED


def test_bind_without_any_personal_token_is_a_named_400_and_never_the_platform_token(bind):
    bind.saved = None
    with pytest.raises(HTTPException) as e:
        _bind_call(bind)
    assert e.value.status_code == 400 and e.value.detail["code"] == "FORK_PAT_REQUIRED"
    assert bind.calls == [] and bind.resolver_calls == 0


def test_bind_form_token_wins_over_the_saved_one(bind):
    bind.saved = SAVED
    _bind_call(bind, FORM)
    assert bind.calls[0]["user_pat"] == FORM


def test_bind_names_a_failing_saved_token(bind):
    bind.saved = SAVED
    bind.raises = bind.BindError(400, "FORK_PAT_INVALID", "GitHub token is invalid or expired.")
    with pytest.raises(HTTPException) as e:
        _bind_call(bind)
    assert e.value.detail["code"] == "FORK_PAT_INVALID"
    assert e.value.detail["token_source"] == "saved"
    assert "saved GitHub token" in e.value.detail["error"]
