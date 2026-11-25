"""trinity-enterprise#1279 — backend GitHub call sites and identity checks in app mode.

Pins the open-core half:

* `wants_broker` puts a covered repo in broker mode on an install with no PAT
  at all, and the recreate matcher and env writer converge on it in one start,
  including the `/git/initialize` row shape the never-introduce guard used to
  stop;
* a working-branch agent that leaves broker mode does not get the platform
  PAT; a pull-only one does;
* a create read through the app tier never walks the PAT ladder;
* the app tier's working-branch rule: Contents write, not shared, probe ok;
* the token route mints read-only for a pull-only agent;
* the PAT-only flows name the PAT requirement when an app is configured;
* the skills-library sweep clones a covered repo with the app's token and
  never falls back to the PAT;
* `token_for_repo_blocking` refuses a running loop and bounds the call.

The end-to-end create (AC1) runs through the real `create_agent_internal` in
`test_1484_create_agent_characterization.py` (the ent#1279 section).

No network.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", "ab" * 32)
os.environ.setdefault(
    "TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity_test_ent1279.db"))

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = str(_ROOT / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi import HTTPException  # noqa: E402

import routers.git as gitmod  # noqa: E402
import services.agent_service.crud as crud  # noqa: E402
import services.agent_service.fork_to_own as fork_to_own  # noqa: E402
import services.agent_service.helpers as helpers_mod  # noqa: E402
import services.agent_service.lifecycle as lc  # noqa: E402
import services.skill_service as skill_service  # noqa: E402
from services import github_token_provider as gtp  # noqa: E402
from services import settings_service as ss  # noqa: E402

FLAG = gtp.BROKER_ENV
REPO = "acme/app"
APP_TOKEN = "ghs_1279_installation_token"


class _Provider:
    def __init__(self, answer=None, error=None, permissions=None):
        self.answer, self.error = answer, error
        self.permissions = ({"contents": "write", "metadata": "read"}
                            if permissions is None else permissions)
        self.calls = []

    async def token_for_repo(self, repo, permissions=None):
        self.calls.append((repo, permissions))
        if self.error:
            raise self.error
        if self.answer is False:
            return None
        return gtp.RepoToken(APP_TOKEN, "2026-11-25T12:00:00Z",
                             permissions=self.permissions,
                             repository_selection="all")


@pytest.fixture(autouse=True)
def _no_provider():
    gtp.clear_provider()
    yield
    gtp.clear_provider()


def _container(**env):
    return SimpleNamespace(attrs={"Config": {"Env": [f"{k}={v}" for k, v in env.items()]}})


def _container_env(env: dict) -> dict:
    return {k: v for k, v in env.items()}


# --- the mode decision ---------------------------------------------------------

@pytest.mark.parametrize("coverage,carried,would_bake,own,no_pat,expected", [
    (True, False, False, False, True, True),     # no PAT anywhere: the app is the credential
    (True, False, False, False, False, False),   # platform PAT exists, tokenless stays tokenless
    (False, False, False, False, True, False),   # uncovered
    (None, False, False, False, True, False),    # unknown keeps the mode
    (True, False, False, True, True, False),     # a per-agent PAT outranks
])
def test_wants_broker_on_an_install_with_no_pat(coverage, carried, would_bake, own, no_pat, expected):
    assert gtp.wants_broker(coverage=coverage, carried_flag=carried,
                            would_bake_pat=would_bake, has_own_pat=own,
                            no_pat_configured=no_pat) is expected


# --- env writer and matcher converge in one start ------------------------------

@pytest.fixture
def no_pat_install(monkeypatch):
    """An agent bound to REPO on an install with no PAT of any tier."""
    state = {"row": {"github_repo": REPO, "source_mode": True,
                     "source_branch": "main", "auto_sync_enabled": False},
             "global": None, "own": None}
    for mod in (lc, helpers_mod):
        monkeypatch.setattr(mod.db, "get_git_config", lambda n: state["row"])
        monkeypatch.setattr(mod.db, "get_agent_github_pat", lambda n: state["own"])
    monkeypatch.setattr(gitmod, "get_github_pat_for_agent",
                        lambda n: state["own"] or state["global"])
    return state


def _start(env: dict, coverage):
    """One start: the env writer on the carried env, then the matcher on the
    container that env would produce. Returns (new env, matcher verdict)."""
    new = dict(env)
    lc._apply_git_env_from_db("a1", new, pat_gate="per_agent_only", coverage=coverage)
    return new, helpers_mod.check_github_pat_env_matches(_container(**new), "a1", coverage)


def test_a_create_baked_app_agent_stays_in_broker_mode(no_pat_install):
    env = {"GITHUB_REPO": REPO, "GIT_SYNC_ENABLED": "true", FLAG: "1"}
    assert helpers_mod.check_github_pat_env_matches(_container(**env), "a1", True) is True
    new, matches = _start(env, True)
    assert new[FLAG] == "1" and "GITHUB_PAT" not in new and matches is True


def test_the_git_initialize_row_shape_converges_in_one_start(no_pat_install):
    """`/git/initialize` bakes no git env. The never-introduce guard used to
    return before the flag was written, so the matcher asked for a recreate on
    every start."""
    env = {}
    assert helpers_mod.check_github_pat_env_matches(_container(**env), "a1", True) is False
    new, matches = _start(env, True)
    assert new[FLAG] == "1" and new["GITHUB_REPO"] == REPO
    assert matches is True


def test_a_pull_only_agent_leaving_broker_mode_gets_the_platform_pat(no_pat_install):
    no_pat_install["global"] = "ghp_platform"
    env = {"GITHUB_REPO": REPO, "GIT_SYNC_ENABLED": "true", FLAG: "1"}
    new, matches = _start(env, False)
    assert FLAG not in new and new["GITHUB_PAT"] == "ghp_platform" and matches is True


def test_a_working_branch_agent_leaving_broker_mode_gets_no_platform_pat(no_pat_install):
    """The platform PAT is the install-wide credential the ent#162 rule keeps
    off working branches. The agent comes up without a credential."""
    no_pat_install["global"] = "ghp_platform"
    no_pat_install["row"]["source_mode"] = False
    env = {"GITHUB_REPO": REPO, "GIT_SYNC_ENABLED": "true", FLAG: "1"}
    new, matches = _start(env, False)
    assert FLAG not in new and "GITHUB_PAT" not in new
    assert matches is True


def test_a_per_agent_pat_survives_leaving_broker_mode(no_pat_install):
    no_pat_install["own"] = "ghp_own"
    no_pat_install["row"]["source_mode"] = False
    env = {"GITHUB_REPO": REPO, "GIT_SYNC_ENABLED": "true", FLAG: "1"}
    new, _ = _start(env, False)
    assert new["GITHUB_PAT"] == "ghp_own"


def _rebuild(coverage):
    env = {}
    lc._apply_git_env_from_db("a1", env, pat_gate="effective", coverage=coverage)
    return env


@pytest.mark.parametrize("coverage", [False, None])
def test_a_rebuilt_working_branch_agent_never_gets_the_platform_pat(no_pat_install, coverage):
    """Rebuild-from-nothing cannot see whether the agent was in broker mode.
    Uncovered: no credential. Unknown: the broker, which never answers with a
    PAT."""
    gtp.register_provider(_Provider())
    no_pat_install["global"] = "ghp_platform"
    no_pat_install["row"]["source_mode"] = False
    env = _rebuild(coverage)
    assert "GITHUB_PAT" not in env
    assert (env.get(FLAG) == "1") is (coverage is None)


def test_a_rebuilt_pull_only_agent_keeps_the_platform_pat(no_pat_install):
    gtp.register_provider(_Provider())
    no_pat_install["global"] = "ghp_platform"
    assert _rebuild(False)["GITHUB_PAT"] == "ghp_platform"


@pytest.mark.parametrize("configured", [False, True])
def test_a_rebuilt_working_branch_agent_without_a_configured_app_is_unchanged(
        no_pat_install, configured):
    """No provider, or one with no credentials: the PAT as before."""
    if configured:
        gtp.register_provider(_Provider())
        no_pat_install["own"] = "ghp_own"   # a per-agent PAT always wins
    no_pat_install["global"] = "ghp_platform"
    no_pat_install["row"]["source_mode"] = False
    assert _rebuild(False)["GITHUB_PAT"] == ("ghp_own" if configured else "ghp_platform")


def test_an_unconfigured_provider_is_not_named_in_messages():
    class _Unconfigured(_Provider):
        def is_configured(self):
            return False

    gtp.register_provider(_Unconfigured())
    assert gtp.is_configured() is False
    assert crud._app_not_installed_note(REPO) == ""
    assert "GitHub App" not in fork_to_own.fork_pat_required().detail["error"]


def test_the_matcher_and_the_writer_pass_the_same_no_pat_input():
    """Both call sites of `wants_broker` outside creation name the input, so
    they cannot disagree about an install with no PAT."""
    import inspect

    for fn in (lc._apply_git_env_from_db, helpers_mod.check_github_pat_env_matches):
        assert "no_pat_configured=" in inspect.getsource(fn)


# --- create-time read and token -------------------------------------------------

def test_no_provider_resolves_the_pat_ladder(monkeypatch):
    monkeypatch.setattr(crud, "resolve_github_pat", lambda owner_id: ("ghp_x", "global"))
    assert asyncio.run(crud._create_time_token(REPO, 7)) == ("ghp_x", "global")


def test_a_covered_repo_resolves_to_the_app(monkeypatch):
    gtp.register_provider(_Provider())
    monkeypatch.setattr(ss, "resolve_github_pat", lambda agent_name=None, owner_id=None: ("", "none"))
    assert asyncio.run(crud._create_time_token(REPO, 7)) == (APP_TOKEN, "app")


@pytest.mark.parametrize("error", [gtp.GithubTokenUnavailable("suspended"), RuntimeError("boom")])
def test_a_failed_mint_is_a_named_503_and_no_pat(monkeypatch, error):
    gtp.register_provider(_Provider(error=error))
    monkeypatch.setattr(ss, "resolve_github_pat",
                        lambda agent_name=None, owner_id=None: ("ghp_platform", "global"))
    with pytest.raises(HTTPException) as raised:
        asyncio.run(crud._create_time_token(REPO, 7))
    assert raised.value.status_code == 503
    assert raised.value.detail["code"] == "GITHUB_APP_TOKEN_UNAVAILABLE"
    assert "ghp_platform" not in str(raised.value.detail)


def test_an_app_read_that_fails_does_not_walk_the_pat_ladder(monkeypatch):
    reads = []

    def _fetch(repo, pat=None, ref=None):
        reads.append(pat)
        return {}, "HTTP 403"

    monkeypatch.setattr(crud, "fetch_template_metadata_result_for_create", _fetch)
    monkeypatch.setattr(crud, "resolve_github_pat_candidates",
                        lambda owner_id=None: [("ghp_platform", "global")])
    read = asyncio.run(crud._read_source_template(REPO, APP_TOKEN, None, tier="app", owner_id=7))
    assert reads == [APP_TOKEN]
    assert read.reason == "HTTP 403" and read.read_tier is None and read.read_pat is None


# --- the working-branch rule ----------------------------------------------------

def _kind(monkeypatch, *, permissions, others=(), probe="ok", catalog=False, name="a1"):
    gtp.register_provider(_Provider(permissions=permissions))
    probes = []

    async def _probe(repo, token):
        probes.append((repo, token))
        return probe, ""

    monkeypatch.setattr(crud.git_service, "probe_push_access", _probe)
    monkeypatch.setattr(crud.db, "get_git_config_agent_names_for_repo",
                        lambda repo: list(others))
    config = crud.AgentConfig(name=name, template=f"github:{REPO}")
    decision = asyncio.run(crud._apply_agent_kind_default(
        config, REPO, APP_TOKEN, None, github_pat_tier="app",
        catalog_template=catalog))
    return config, decision, probes


def test_a_writable_unshared_repo_gets_a_working_branch(monkeypatch):
    config, decision, probes = _kind(monkeypatch, permissions={"contents": "write"})
    assert decision["source_mode"] is False and config.source_mode is False
    assert decision["push_verified"] is True
    assert probes == [(REPO, APP_TOKEN)]


@pytest.mark.parametrize("kw,needle", [
    ({"permissions": {"contents": "read"}}, "Contents: Read and write"),
    ({"permissions": {}}, "Contents: Read and write"),
    ({"permissions": {"contents": "write"}, "others": ["someone-elses"]}, "shared repository"),
    ({"permissions": {"contents": "write"}, "catalog": True}, "catalog template"),
    ({"permissions": {"contents": "write"}, "probe": "denied"}, "cannot push"),
    ({"permissions": {"contents": "write"}, "probe": "transient"}, "could not be verified"),
])
def test_every_other_case_is_pull_only_with_a_reason(monkeypatch, kw, needle):
    config, decision, _ = _kind(monkeypatch, **kw)
    assert decision["source_mode"] is True and config.source_mode is True
    assert needle in decision["reason"]


def test_the_shared_reason_never_names_the_other_agent(monkeypatch):
    _, decision, _ = _kind(monkeypatch, permissions={"contents": "write"},
                           others=["private-agent-of-bob"])
    assert "private-agent-of-bob" not in decision["reason"]


def test_the_agent_itself_does_not_make_its_repo_shared(monkeypatch):
    _, decision, _ = _kind(monkeypatch, permissions={"contents": "write"}, others=["a1"])
    assert decision["source_mode"] is False


def test_an_installation_token_is_never_baked():
    env = {}
    config = crud.AgentConfig(name="a1", template=f"github:{REPO}")
    crud._apply_github_env(config, env, REPO, APP_TOKEN, None, None)
    assert not any(v == APP_TOKEN for v in env.values())
    assert "GITHUB_PAT" not in env and "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env


# --- the token route ------------------------------------------------------------

@pytest.fixture
def route(monkeypatch):
    import services.docker_service as docker_service

    state = {"row": SimpleNamespace(github_repo=REPO, source_mode=True), "asked": []}
    monkeypatch.setattr(docker_service, "get_agent_container", lambda n: _container(**{FLAG: "1"}))
    monkeypatch.setattr(gitmod.db, "get_git_config", lambda n: state["row"])

    async def _resolve(repo, agent_name=None, owner_id=None, permissions=None):
        state["asked"].append(permissions)
        return APP_TOKEN, "app", "2026-11-25T12:00:00Z"

    monkeypatch.setattr(ss, "resolve_github_token", _resolve)
    return state


def _agent(name="a1"):
    return SimpleNamespace(agent_name=name, mcp_scope="agent", role="user", id=1, username="o")


def test_a_pull_only_agent_gets_a_read_only_token(route):
    asyncio.run(gitmod.get_git_token("a1", current_user=_agent()))
    assert route["asked"] == [{"contents": "read", "metadata": "read"}]


def test_a_working_branch_agent_gets_the_default_write_token(route):
    route["row"] = SimpleNamespace(github_repo=REPO, source_mode=False)
    asyncio.run(gitmod.get_git_token("a1", current_user=_agent()))
    assert route["asked"] == [None]


# --- PAT-only flows name the PAT -------------------------------------------------

def test_fork_and_bind_name_the_pat_when_an_app_is_configured():
    plain = fork_to_own.fork_pat_required().detail
    gtp.register_provider(_Provider())
    with_app = fork_to_own.fork_pat_required().detail
    assert plain["code"] == with_app["code"] == "FORK_PAT_REQUIRED"
    assert "GitHub App" not in plain["error"]
    assert "GitHub App" in with_app["error"] and "personal access token" in with_app["error"]


@pytest.fixture
def init_route(monkeypatch):
    import services.docker_service as docker_service

    monkeypatch.setattr(docker_service, "get_agent_container",
                        lambda n: SimpleNamespace(status="running"))
    monkeypatch.setattr(gitmod.git_service, "get_agent_git_config", lambda n: None)
    monkeypatch.setattr(gitmod, "get_github_pat_for_agent", lambda n: "")
    body = SimpleNamespace(repo_owner="acme", repo_name="app", create_repo=True,
                           private=True, description="")
    request = SimpleNamespace(client=None, scope={"path": "/x"}, state=SimpleNamespace())
    return lambda: asyncio.run(gitmod.initialize_github_sync(
        "a1", body, request, current_user=SimpleNamespace(id=1, username="o")))


def test_git_initialize_names_the_pat_when_only_an_app_is_configured(init_route):
    gtp.register_provider(_Provider())
    with pytest.raises(HTTPException) as raised:
        init_route()
    assert raised.value.status_code == 400
    assert raised.value.detail["code"] == "GITHUB_PAT_REQUIRED"
    assert "personal access token" in raised.value.detail["error"]


def test_git_initialize_without_an_app_is_unchanged(init_route):
    with pytest.raises(HTTPException) as raised:
        init_route()
    assert raised.value.status_code == 400
    assert isinstance(raised.value.detail, str)


def test_the_tokenless_refusal_says_the_app_is_not_installed():
    assert crud._app_not_installed_note(REPO) == ""
    gtp.register_provider(_Provider())
    assert f"not installed on '{REPO}'" in crud._app_not_installed_note(REPO)


# --- the skills-library sweep ----------------------------------------------------

def test_skills_clone_a_covered_repo_with_a_read_only_app_token():
    provider = _Provider()
    gtp.register_provider(provider)
    assert skill_service.SkillService._app_token_for(f"https://github.com/{REPO}.git") == APP_TOKEN
    assert provider.calls == [(REPO, {"contents": "read", "metadata": "read"})]


@pytest.mark.parametrize("url", ["https://gitlab.com/acme/app.git", "http://github.com/acme/app",
                                 "https://github.com/acme"])
def test_skills_never_ask_the_app_about_another_host_or_a_non_repo(url):
    provider = _Provider()
    gtp.register_provider(provider)
    assert skill_service.SkillService._app_token_for(url) is None
    assert provider.calls == []


def test_skills_uncovered_repo_keeps_the_platform_pat():
    gtp.register_provider(_Provider(answer=False))
    assert skill_service.SkillService._app_token_for(f"https://github.com/{REPO}") is None


def test_skills_covered_repo_with_a_failed_mint_raises():
    gtp.register_provider(_Provider(error=gtp.GithubTokenUnavailable("suspended")))
    with pytest.raises(gtp.GithubTokenUnavailable):
        skill_service.SkillService._app_token_for(f"https://github.com/{REPO}")


# --- the blocking bridge ---------------------------------------------------------

def test_the_blocking_bridge_refuses_a_running_loop():
    gtp.register_provider(_Provider())

    async def _on_loop():
        return gtp.token_for_repo_blocking(REPO)

    with pytest.raises(RuntimeError):
        asyncio.run(_on_loop())


def test_the_blocking_bridge_is_bounded():
    class _Slow:
        async def token_for_repo(self, repo, permissions=None):
            await asyncio.sleep(5)

    gtp.register_provider(_Slow())
    with pytest.raises(gtp.GithubTokenUnavailable):
        gtp.token_for_repo_blocking(REPO, timeout=0.05)


def test_the_sweep_records_a_covered_source_whose_mint_failed_and_moves_on(monkeypatch):
    gtp.register_provider(_Provider(error=gtp.GithubTokenUnavailable("suspended")))
    recorded = []
    monkeypatch.setattr(skill_service.db, "record_skill_source_sync",
                        lambda sid, **kw: recorded.append((sid, kw)))
    svc = skill_service.SkillService.__new__(skill_service.SkillService)
    svc.library_root = Path(tempfile.mkdtemp(prefix="ent1279_skills_"))
    svc._last_sync = None
    monkeypatch.setattr(skill_service, "validate_skills_library_url", lambda url: url)
    cloned = []
    monkeypatch.setattr(skill_service.SkillSourceClone, "sync",
                        lambda self, *a, **k: cloned.append(a) or {"success": True})
    src = SimpleNamespace(id="src_0123456789abcdef", name="lib", url=f"https://github.com/{REPO}",
                          ref_type="branch", ref="main", last_commit_sha=None)
    out = svc._sync_sources_locked([src], single_source=True)
    assert recorded and recorded[0][0] == "src_0123456789abcdef" and recorded[0][1]["success"] is False
    assert "GitHub App" in recorded[0][1]["error"]
    assert cloned == []          # never cloned with the platform PAT instead
    assert out is not None
