"""#3725 checkpoint D — identity, validation and disclosure rows of the chain.

Sibling of `test_3725_public_template_tokenless_metadata.py` (that file holds the
helpers, the raw reader and the chain's read decisions). This one pins what the
chain does to the rest of the create:

  * `_validate_github_access` on a REST 403: the same token re-checks READ over
    the git transport — ok proceeds, a refusal or outage is today's 502, a
    missing branch is a named 400. A 401 is never re-checked.
  * identity after a 401: the platform token that read the file becomes this
    create's identity, never persisted (tier `global`), pull-only with the
    platform-token reason, and the create response's `git_mode` names both the
    tier used and the one set aside.
  * the fork path owns its identity: a 401 never swaps it, and a failing saved
    token is still reported as `token_source: "saved"`.
  * equal token values are read once through the chain.
  * a non-default `TRINITY_GIT_BASE_URL` skips the credential-free path, fail
    closed.
  * no WARNING carries a token value.

Sync throughout (`tests/unit/pytest.ini`: a bare `async def test_*` is collected
and never awaited), so async calls are driven with `asyncio.run`. No network:
the contents API, the raw reader and both git probes are stubbed.
"""
import asyncio
import logging
import types
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import services.agent_service.crud as crud
import services.settings_service as settings_service
import services.template_service as ts
from services.github_service import GitHubError

pytestmark = pytest.mark.unit

SHA = "b" * 40
REPO = "acme/public-tpl"
USER_PAT = "user-pat-3725-d"
PLATFORM_PAT = "platform-pat-3725-d"
SAME_PAT = "same-pat-3725-d"


# ---------------------------------------------------------------------------
# _validate_github_access — the 403 re-check over the git transport
# ---------------------------------------------------------------------------

@pytest.fixture
def rest(monkeypatch):
    """`check_repo_exists` raises a GitHubError carrying `status`; the tokened
    git probe answers what the test says."""
    state = {"status": 403, "probe": ("ok", SHA)}

    class _GH:
        def __init__(self, pat):
            self.pat = pat

        async def check_repo_exists(self, owner, name):
            raise GitHubError(f"GitHub API error: {state['status']}",
                              status=state["status"])

    probe = AsyncMock(side_effect=lambda *a, **k: state["probe"])
    push = AsyncMock()
    monkeypatch.setattr(crud, "GitHubService", _GH)
    monkeypatch.setattr(crud.git_service, "probe_tokened_repo_access", probe)
    monkeypatch.setattr(crud, "_validate_push_access", push)

    class Rest:
        def __init__(self):
            self.probe, self.push = probe, push

        def says(self, status):
            state["status"] = status

        def probe_says(self, outcome, sha=""):
            state["probe"] = (outcome, sha)

        def validate(self, branch=None, **kw):
            config = types.SimpleNamespace(source_branch=branch)
            return asyncio.run(crud._validate_github_access(
                config, REPO, USER_PAT, **kw))

    return Rest()


class TestValidateGithubAccess403:
    def test_a_rest_403_that_git_can_read_proceeds(self, rest):
        assert rest.validate(branch="dev") is None
        rest.probe.assert_awaited_once_with(REPO, USER_PAT, "dev")

    def test_the_recheck_asks_for_the_default_branch_when_none_is_named(self, rest):
        rest.validate()
        assert rest.probe.await_args.args == (REPO, USER_PAT, None)

    def test_a_pushing_agent_still_gets_its_push_probe_after_the_recheck(self, rest):
        rest.validate(will_push=True)
        rest.push.assert_awaited_once_with(REPO, USER_PAT)

    @pytest.mark.parametrize("outcome", ["unavailable", "transient"])
    def test_a_recheck_that_cannot_read_is_todays_502(self, rest, outcome):
        rest.probe_says(outcome)
        with pytest.raises(HTTPException) as exc:
            rest.validate(branch="dev")
        assert exc.value.status_code == 502
        assert USER_PAT not in str(exc.value.detail)
        rest.push.assert_not_awaited()

    def test_a_missing_branch_on_the_recheck_is_a_named_400(self, rest):
        rest.probe_says("missing_ref")
        with pytest.raises(HTTPException) as exc:
            rest.validate(branch="gone")
        assert exc.value.status_code == 400
        assert "Branch 'gone' not found" in exc.value.detail

    def test_a_401_is_never_rechecked(self, rest):
        """A token GitHub answered 401 is dead on git too: today's 502."""
        rest.says(401)
        with pytest.raises(HTTPException) as exc:
            rest.validate(branch="dev")
        assert exc.value.status_code == 502
        rest.probe.assert_not_awaited()


# ---------------------------------------------------------------------------
# The chain through the real `_resolve_template`
# ---------------------------------------------------------------------------

@pytest.fixture
def chain(monkeypatch):
    """A GitHub keyed on WHICH token asks (see the sibling file's fixture).

    `ladder(...)` stubs the tier ladder; `tokens(...)` instead keeps the real
    `resolve_github_pat_candidates` and stubs only the stores it reads."""
    state = {"answers": {}, "calls": [], "raw": ({}, "HTTP 404"), "raw_calls": [],
             "probe": ("ok", SHA)}
    tiers = {"agent": "", "user": "", "global": ""}

    def _fetch(repo, pat, ref=None):
        state["calls"].append(pat)
        return state["answers"].get(pat or "", ({}, "HTTP 404"))

    def _raw(repo, sha, *a, **k):
        state["raw_calls"].append((repo, sha))
        return state["raw"]

    probe = AsyncMock(side_effect=lambda *a, **k: state["probe"])
    monkeypatch.setattr(ts, "_fetch_template_yaml_result", _fetch)
    monkeypatch.setattr(ts, "_fetch_template_yaml_raw_result", _raw)
    monkeypatch.setattr(ts, "_get_github_pat", lambda: PLATFORM_PAT)
    monkeypatch.setattr(crud.git_service, "probe_anonymous_ref", probe)
    monkeypatch.delenv("TRINITY_GIT_BASE_URL", raising=False)
    ts._metadata_cache.clear()

    async def _ok(*a, **k):
        return None

    async def _instance(*a, **k):
        return None, None

    monkeypatch.setattr(crud, "_validate_github_access", _ok)
    monkeypatch.setattr(crud, "_reserve_git_instance", _instance)

    class Chain:
        calls = state["calls"]
        raw_calls = state["raw_calls"]

        def __init__(self):
            self.probe = probe

        def answers(self, **by_pat):
            state["answers"].update(by_pat)

        def raw(self, answer):
            state["raw"] = answer

        def probe_says(self, outcome, sha=""):
            state["probe"] = (outcome, sha)

        def tokens(self, **by_tier):
            """Use the REAL resolver over these token stores."""
            tiers.update(by_tier)
            monkeypatch.setattr(settings_service.db, "get_agent_github_pat",
                                lambda name: tiers["agent"])
            monkeypatch.setattr(settings_service.db, "get_user_github_pat",
                                lambda owner_id: tiers["user"])
            monkeypatch.setattr(settings_service, "get_github_pat",
                                lambda: tiers["global"])

        def ladder(self, *entries):
            monkeypatch.setattr(crud, "resolve_github_pat_candidates",
                                lambda *a, **k: list(entries))

        def contents_calls(self, pat):
            return [c for c in state["calls"] if c == pat]

        def create(self, *, pat, tier, fork_to_own=None, source_mode=True,
                   explicit_source_mode=True):
            catalog = ts.get_github_template("github:" + REPO)
            monkeypatch.setattr(
                crud, "_resolve_github_repo_and_pat",
                lambda *a, **k: (catalog, REPO, pat, tier),
            )
            fields = {"template", "fork_to_own", "source_branch", "import_intent"}
            if explicit_source_mode:
                fields.add("source_mode")
            config = types.SimpleNamespace(
                template="github:" + REPO, fork_to_own=fork_to_own,
                source_branch=None, source_mode=source_mode, import_intent=None,
                name="public-tpl", kind=None, ephemeral=None,
                model_fields_set=fields,
            )
            # A signed-in session (`mcp_scope=None`, `models.User`'s default):
            # the saved token's fork arm is session-only (trinity-enterprise#719).
            user = types.SimpleNamespace(id=7, username="owner",
                                         email="o@example.com", agent_name=None,
                                         mcp_scope=None)
            return asyncio.run(crud._resolve_template(config, user))

    yield Chain()
    ts._metadata_cache.clear()


class TestIdentityAfterA401:
    def test_platform_read_is_the_identity_never_persisted_and_pull_only(self, chain):
        """Per-user 401 + platform 200. The create-site persist predicate is
        `tier in ("fork", "per_user")` (pinned by test_1484 case 8: a `global`
        tier is never written to the per-agent row); the swapped tier lands
        outside it."""
        chain.ladder((USER_PAT, "per_user"), (PLATFORM_PAT, "global"))
        chain.answers(**{USER_PAT: ({}, "HTTP 401"),
                         PLATFORM_PAT: ({"display_name": "Public"}, None)})
        tr = chain.create(pat=USER_PAT, tier="per_user", explicit_source_mode=False)
        assert (tr.github_pat_for_agent, tr.github_pat_tier) == (PLATFORM_PAT, "global")
        assert tr.github_pat_tier not in ("fork", "per_user")
        mode = tr.git_mode_decision
        assert mode["source_mode"] is True and mode["pushes"] is False
        assert "only the platform-wide GitHub token" in mode["reason"]
        assert "pull-only" in mode["reason"]

    def test_the_create_response_names_the_tier_used_and_the_one_set_aside(self, chain):
        chain.ladder((USER_PAT, "per_user"), (PLATFORM_PAT, "global"))
        chain.answers(**{USER_PAT: ({}, "HTTP 401"),
                         PLATFORM_PAT: ({"display_name": "Public"}, None)})
        tr = chain.create(pat=USER_PAT, tier="per_user")
        assert tr.git_mode_decision["token_tier"] == "global"
        assert tr.git_mode_decision["rejected_token_tier"] == "per_user"

    def test_a_read_that_decides_today_carries_the_tier_and_no_rejection(self, chain):
        chain.ladder((USER_PAT, "per_user"), (PLATFORM_PAT, "global"))
        chain.answers(**{USER_PAT: ({"display_name": "Fine"}, None)})
        tr = chain.create(pat=USER_PAT, tier="per_user")
        assert tr.git_mode_decision["token_tier"] == "per_user"
        assert "rejected_token_tier" not in tr.git_mode_decision

    def test_the_credential_free_read_reports_tier_none(self, chain):
        chain.ladder((USER_PAT, "per_user"))
        chain.answers(**{USER_PAT: ({}, "HTTP 401")})
        chain.raw(({"display_name": "Public"}, None))
        tr = chain.create(pat=USER_PAT, tier="per_user")
        assert tr.github_pat_for_agent is None
        assert tr.git_mode_decision["token_tier"] == "none"
        assert tr.git_mode_decision["rejected_token_tier"] == "per_user"


class TestForkingOwnsItsIdentity:
    def test_a_401_never_swaps_and_the_saved_token_is_named(self, chain, monkeypatch):
        fork_calls = []

        async def refuse(**kw):
            fork_calls.append(kw)
            raise HTTPException(status_code=400, detail={
                "error": "GitHub token is invalid or expired.",
                "code": "FORK_PAT_INVALID"})

        monkeypatch.setattr(crud, "fork_template_to_own_repo", refuse)
        monkeypatch.setattr(crud.db, "get_git_config_agent_names_for_repo",
                            lambda repo: [])
        chain.ladder((USER_PAT, "per_user"), (PLATFORM_PAT, "global"))
        chain.answers(**{USER_PAT: ({}, "HTTP 401"),
                         PLATFORM_PAT: ({"display_name": "Public"}, None)})
        block = types.SimpleNamespace(destination_repo="owner/brain",
                                      github_pat=None, private=True)
        with pytest.raises(HTTPException) as exc:
            chain.create(pat=USER_PAT, tier="per_user", fork_to_own=block)
        assert exc.value.detail["token_source"] == "saved"
        assert exc.value.detail["code"] == "FORK_PAT_INVALID"
        assert [c["user_pat"] for c in fork_calls] == [USER_PAT]
        assert PLATFORM_PAT not in str(exc.value.detail)


class TestEqualTokensAreReadOnce:
    def test_same_value_in_two_tiers_is_one_contents_call(self, chain):
        """Real resolver: per-user and platform hold the same string. Same
        credential ⇒ same answer from GitHub, so the chain never asks twice."""
        chain.tokens(user=SAME_PAT, **{"global": SAME_PAT})
        chain.answers(**{SAME_PAT: ({}, "HTTP 401")})
        chain.raw(({"display_name": "Public"}, None))
        chain.create(pat=SAME_PAT, tier="per_user")
        assert len(chain.contents_calls(SAME_PAT)) == 1

    def test_the_chain_dedupes_even_a_ladder_that_repeats_a_value(self, chain):
        chain.ladder((SAME_PAT, "per_user"), (SAME_PAT, "global"))
        chain.answers(**{SAME_PAT: ({}, "HTTP 403")})
        chain.raw(({"display_name": "Public"}, None))
        chain.create(pat=SAME_PAT, tier="per_agent")
        assert len(chain.contents_calls(SAME_PAT)) == 1


class TestNonDefaultGitBase:
    def test_the_credential_free_path_is_skipped_fail_closed(self, chain, monkeypatch):
        monkeypatch.setenv("TRINITY_GIT_BASE_URL", "https://git.example.com")
        chain.ladder((USER_PAT, "per_user"))
        chain.answers(**{USER_PAT: ({}, "HTTP 401")})
        chain.raw(({"display_name": "would read"}, None))
        with pytest.raises(HTTPException) as exc:
            chain.create(pat=USER_PAT, tier="per_user")
        assert exc.value.status_code == 503
        assert exc.value.detail["code"] == "TEMPLATE_METADATA_UNAVAILABLE"
        assert exc.value.detail["attempts"][-1]["tier"] == "anonymous"
        assert exc.value.detail["attempts"][-1]["status"].startswith("skipped")
        chain.probe.assert_not_awaited()
        assert chain.raw_calls == []

    def test_a_trailing_slash_on_the_default_base_still_reads(self, chain, monkeypatch):
        monkeypatch.setenv("TRINITY_GIT_BASE_URL", "https://github.com/")
        chain.ladder((USER_PAT, "per_user"))
        chain.answers(**{USER_PAT: ({}, "HTTP 403")})
        chain.raw(({"display_name": "Public"}, None))
        chain.create(pat=USER_PAT, tier="per_user")
        assert chain.raw_calls == [(REPO, SHA)]


class TestNoTokenInTheLog:
    @pytest.mark.parametrize("platform_answer,raises", [
        (({"display_name": "Public"}, None), False),   # swap → the WARNING
        (({}, "HTTP 404"), True),                       # nothing reads → refusal
    ])
    def test_no_record_carries_a_token_value(self, chain, caplog,
                                             platform_answer, raises):
        chain.ladder((USER_PAT, "per_user"), (PLATFORM_PAT, "global"))
        chain.answers(**{USER_PAT: ({}, "HTTP 401"), PLATFORM_PAT: platform_answer})
        chain.probe_says("unavailable")
        with caplog.at_level(logging.DEBUG):
            if raises:
                with pytest.raises(HTTPException):
                    chain.create(pat=USER_PAT, tier="per_user")
            else:
                chain.create(pat=USER_PAT, tier="per_user")
        assert caplog.records, "the path under test logs nothing — the check is vacuous"
        if not raises:
            assert any("[#3725]" in r.getMessage() and r.levelno == logging.WARNING
                       for r in caplog.records)
        for record in caplog.records:
            text = record.getMessage() + repr(record.args)
            assert USER_PAT not in text and PLATFORM_PAT not in text, text
