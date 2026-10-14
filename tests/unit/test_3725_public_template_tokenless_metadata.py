"""A public `github:` template's metadata read must not hinge on ONE tier's token (#3725).

The bug: the create path reads `template.yaml` once, with the preferred tier's
token (`_fetch_template_yaml_result`). A saved token GitHub rejects (401), a
rate-limited one (403) or an anonymous REST call over the 60/h cap answers
`HTTP 4xx`, `metadata_reason_is_unreadable` says "unreadable", and the ent#14
fail-closed gate 503s `TEMPLATE_METADATA_UNAVAILABLE` — for a PUBLIC repo any
anonymous clone could read, and while a platform token that could read it sat
one tier down.

The fix keeps the first read byte-identical and adds a chain only when it is
unreadable: the remaining tiers (only a 200 ends that walk), then a
credential-free read pinned to a SHA an anonymous `ls-remote` resolved — the
same transport the clone uses. The fail-closed property stays for a template no
path can read, and the private-`required` false pass stays closed.

Sections:
  * helpers (checkpoint A, GREEN): `resolve_github_pat_candidates`,
    `probe_anonymous_ref`, `probe_tokened_repo_access`, `GitHubError.status`.
  * the raw reader + 401/403 classifier (checkpoint B, strict xfail until then).
  * the chain driven through the real `_resolve_template` (checkpoint C, strict
    xfail until then), plus the rows that already hold today and must keep
    holding (green now, never xfail).

Sync throughout (`tests/unit/pytest.ini`: a bare `async def test_*` is collected
and never awaited), so async calls are driven with `asyncio.run`.

Related flow: ent#14 F2/S2 (test_ent14_fork_to_own_failclosed.py), ent#123.
"""
import asyncio
import json
import types
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

import services.agent_service.crud as crud
import services.git_service as gs
import services.settings_service as settings_service
import services.template_service as ts
from services.github_service import GitHubError, GitHubService

pytestmark = pytest.mark.unit

SHA = "a" * 40
USER_PAT = "user-pat-3725"
PLATFORM_PAT = "platform-pat-3725"

CHECKPOINT_B = pytest.mark.xfail(strict=True, reason="checkpoint B")
CHECKPOINT_C = pytest.mark.xfail(strict=True, reason="checkpoint C")


# ---------------------------------------------------------------------------
# resolve_github_pat_candidates — the ladder the chain walks
# ---------------------------------------------------------------------------

@pytest.fixture
def tiers(monkeypatch):
    """Per-agent / per-user / global token stores the resolver reads."""
    store = {"agent": "", "user": "", "global": ""}
    db = types.SimpleNamespace(
        get_agent_github_pat=lambda name: store["agent"],
        get_user_github_pat=lambda owner_id: store["user"],
    )
    monkeypatch.setattr(settings_service, "db", db)
    monkeypatch.setattr(settings_service, "get_github_pat", lambda: store["global"])
    return store


class TestResolveGithubPatCandidates:
    def test_tiers_come_back_in_resolver_order(self, tiers):
        tiers.update(user=USER_PAT, **{"global": PLATFORM_PAT})
        assert settings_service.resolve_github_pat_candidates(owner_id=7) == [
            (USER_PAT, "per_user"), (PLATFORM_PAT, "global"),
        ]

    def test_per_agent_leads_when_asked_for(self, tiers):
        tiers.update(agent="agent-pat", user=USER_PAT, **{"global": PLATFORM_PAT})
        ladder = settings_service.resolve_github_pat_candidates(
            agent_name="a1", owner_id=7)
        assert [t for _, t in ladder] == ["per_agent", "per_user", "global"]

    def test_the_same_token_value_is_tried_once(self, tiers):
        """Same credential ⇒ same answer from GitHub; the later tier is dropped."""
        tiers.update(user=PLATFORM_PAT, **{"global": PLATFORM_PAT})
        assert settings_service.resolve_github_pat_candidates(owner_id=7) == [
            (PLATFORM_PAT, "per_user"),
        ]

    def test_empty_tiers_are_skipped(self, tiers):
        tiers.update(**{"global": PLATFORM_PAT})
        assert settings_service.resolve_github_pat_candidates(owner_id=7) == [
            (PLATFORM_PAT, "global"),
        ]

    def test_no_token_anywhere_is_an_empty_ladder(self, tiers):
        assert settings_service.resolve_github_pat_candidates(owner_id=7) == []

    def test_no_owner_means_no_per_user_tier(self, tiers):
        tiers.update(user=USER_PAT, **{"global": PLATFORM_PAT})
        assert settings_service.resolve_github_pat_candidates() == [
            (PLATFORM_PAT, "global"),
        ]

    @pytest.mark.parametrize("store", [
        {"agent": "", "user": "", "global": ""},
        {"agent": "", "user": USER_PAT, "global": PLATFORM_PAT},
        {"agent": "", "user": "", "global": PLATFORM_PAT},
        {"agent": "", "user": PLATFORM_PAT, "global": PLATFORM_PAT},
    ])
    def test_resolve_github_pat_is_untouched_and_agrees_with_the_first_entry(
        self, tiers, store
    ):
        tiers.update(store)
        ladder = settings_service.resolve_github_pat_candidates(owner_id=7)
        assert settings_service.resolve_github_pat(owner_id=7) == (
            ladder[0] if ladder else ("", "none"))


# ---------------------------------------------------------------------------
# probe_anonymous_ref / probe_tokened_repo_access — ls-remote → SHA
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, returncode, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._out = stdout
        self._err = stderr

    async def communicate(self):
        return self._out, self._err

    def kill(self):
        pass

    async def wait(self):
        pass


def _spawn(proc):
    """Patch the subprocess spawn on the module that owns the probe."""
    return patch.object(gs.ref_probes.asyncio, "create_subprocess_exec",
                        AsyncMock(return_value=proc))


class TestProbeAnonymousRef:
    def test_head_resolves_to_the_default_branch_sha(self):
        proc = _FakeProc(0, f"{SHA}\tHEAD\n".encode())
        with _spawn(proc) as spawn:
            assert asyncio.run(gs.probe_anonymous_ref("o/r")) == ("ok", SHA)
        argv = spawn.await_args.args
        assert argv == ("git", "ls-remote", "https://github.com/o/r.git", "HEAD")

    def test_a_branch_is_asked_for_by_its_full_ref(self):
        proc = _FakeProc(0, f"{SHA}\trefs/heads/feature/x\n".encode())
        with _spawn(proc) as spawn:
            assert asyncio.run(gs.probe_anonymous_ref("o/r", "feature/x")) == ("ok", SHA)
        assert spawn.await_args.args[-1] == "refs/heads/feature/x"

    def test_a_same_named_tag_never_answers_for_the_branch(self):
        proc = _FakeProc(0, f"{SHA}\trefs/tags/main\n".encode())
        with _spawn(proc):
            assert asyncio.run(gs.probe_anonymous_ref("o/r", "main")) == ("missing_ref", "")

    def test_no_matching_line_is_a_missing_ref_on_a_readable_repo(self):
        with _spawn(_FakeProc(0, b"")):
            assert asyncio.run(gs.probe_anonymous_ref("o/r", "gone")) == ("missing_ref", "")

    def test_the_probe_carries_no_credential(self, monkeypatch):
        monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
        with _spawn(_FakeProc(0, f"{SHA}\tHEAD\n".encode())) as spawn:
            asyncio.run(gs.probe_anonymous_ref("o/r"))
        env = spawn.await_args.kwargs["env"]
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert not any(k.startswith("GIT_CONFIG_") for k in env)

    def test_the_base_url_override_is_honoured(self, monkeypatch):
        monkeypatch.setenv("TRINITY_GIT_BASE_URL", "https://git.example.com/")
        with _spawn(_FakeProc(0, f"{SHA}\tHEAD\n".encode())) as spawn:
            asyncio.run(gs.probe_anonymous_ref("o/r"))
        assert spawn.await_args.args[2] == "https://git.example.com/o/r.git"

    @pytest.mark.parametrize("stderr", [
        b"fatal: could not read Username for 'https://github.com': terminal prompts disabled",
        b"fatal: Authentication failed for 'https://github.com/o/r.git/'",
        b"remote: Repository not found.",
    ])
    def test_the_same_definitive_classifier_as_the_repo_probe(self, stderr):
        with _spawn(_FakeProc(128, stderr=stderr)):
            assert asyncio.run(gs.probe_anonymous_ref("o/r")) == ("unavailable", "")
        # …and the existing probe agrees on the same stderr.
        with patch.object(gs.provisioning.asyncio, "create_subprocess_exec",
                          AsyncMock(return_value=_FakeProc(128, stderr=stderr))):
            assert asyncio.run(gs.probe_anonymous_repo_access("o/r")) == "unavailable"

    def test_a_network_error_is_transient(self):
        proc = _FakeProc(128, stderr=b"fatal: unable to access: Could not resolve host: github.com")
        with _spawn(proc):
            assert asyncio.run(gs.probe_anonymous_ref("o/r")) == ("transient", "")

    def test_a_spawn_failure_is_transient(self):
        with patch.object(gs.ref_probes.asyncio, "create_subprocess_exec",
                          AsyncMock(side_effect=FileNotFoundError("no git"))):
            assert asyncio.run(gs.probe_anonymous_ref("o/r")) == ("transient", "")

    def test_a_timeout_is_transient(self):
        class _Slow(_FakeProc):
            async def communicate(self):
                raise asyncio.TimeoutError

        with _spawn(_Slow(0)):
            assert asyncio.run(gs.probe_anonymous_ref("o/r")) == ("transient", "")

    @pytest.mark.parametrize("oid", [b"not-a-sha", b"g" * 40, b"a" * 39, b"../x"])
    def test_a_malformed_object_id_never_becomes_a_url_segment(self, oid):
        with _spawn(_FakeProc(0, oid + b"\tHEAD\n")):
            assert asyncio.run(gs.probe_anonymous_ref("o/r")) == ("transient", "")


class TestProbeTokenedRepoAccess:
    def test_the_token_rides_the_env_never_argv(self):
        with _spawn(_FakeProc(0, f"{SHA}\trefs/heads/main\n".encode())) as spawn:
            assert asyncio.run(
                gs.probe_tokened_repo_access("o/r", USER_PAT, "main")) == ("ok", SHA)
        argv = spawn.await_args.args
        assert all(USER_PAT not in a for a in argv), argv
        assert argv[-1] == "refs/heads/main"
        env = spawn.await_args.kwargs["env"]
        import base64
        token_b64 = base64.b64encode(f"x-access-token:{USER_PAT}".encode()).decode()
        assert env["GIT_CONFIG_VALUE_0"].endswith(token_b64), "git_auth_env form"
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_a_missing_branch_is_named_not_treated_as_ok(self):
        with _spawn(_FakeProc(0, b"")):
            assert asyncio.run(
                gs.probe_tokened_repo_access("o/r", USER_PAT, "gone")) == ("missing_ref", "")

    def test_a_refused_token_is_unavailable(self):
        with _spawn(_FakeProc(128, stderr=b"remote: Repository not found.")):
            assert asyncio.run(
                gs.probe_tokened_repo_access("o/r", USER_PAT)) == ("unavailable", "")

    def test_an_outage_is_transient(self):
        with _spawn(_FakeProc(128, stderr=b"fatal: unable to access: Connection timed out")):
            assert asyncio.run(
                gs.probe_tokened_repo_access("o/r", USER_PAT)) == ("transient", "")

    def test_a_crash_never_logs_the_tokened_exception_text(self, caplog):
        boom = RuntimeError(f"env had {USER_PAT}")
        with patch.object(gs.ref_probes.asyncio, "create_subprocess_exec",
                          AsyncMock(side_effect=boom)):
            assert asyncio.run(
                gs.probe_tokened_repo_access("o/r", USER_PAT)) == ("transient", "")
        assert USER_PAT not in caplog.text


# ---------------------------------------------------------------------------
# GitHubError.status — tell a refused token from an outage without parsing text
# ---------------------------------------------------------------------------

class TestGitHubErrorStatus:
    @pytest.mark.parametrize("code", [401, 403, 500])
    def test_check_repo_exists_carries_the_status(self, code):
        svc = GitHubService(USER_PAT)
        svc._request = AsyncMock(return_value=types.SimpleNamespace(status_code=code))
        with pytest.raises(GitHubError) as exc:
            asyncio.run(svc.check_repo_exists("o", "r"))
        assert exc.value.status == code
        assert str(exc.value) == f"GitHub API error: {code}", "message unchanged"

    def test_a_network_error_has_no_status(self):
        import httpx

        svc = GitHubService(USER_PAT)
        svc._request = AsyncMock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(GitHubError) as exc:
            asyncio.run(svc.check_repo_exists("o", "r"))
        assert exc.value.status is None

    def test_existing_raises_are_unchanged(self):
        e = GitHubError("Repository 'o/r' not found")
        assert e.status is None and str(e) == "Repository 'o/r' not found"


# ---------------------------------------------------------------------------
# Checkpoint B — the credential-free reader and the rejection classifier
# ---------------------------------------------------------------------------

class TestRawReader:
    @CHECKPOINT_B
    def test_reads_by_sha_without_any_authorization_header(self, monkeypatch):
        seen = {}

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, headers=None, **k):
                seen["url"], seen["headers"] = url, dict(headers or {})
                return types.SimpleNamespace(status_code=200, text="display_name: X\n")

        monkeypatch.setattr(ts.httpx, "Client", _Client)
        metadata, reason = ts._fetch_template_yaml_raw_result("o/r", SHA)
        assert (metadata, reason) == ({"display_name": "X"}, None)
        assert seen["url"] == f"https://raw.githubusercontent.com/o/r/{SHA}/template.yaml"
        assert not any(k.lower() == "authorization" for k in seen["headers"])

    @CHECKPOINT_B
    @pytest.mark.parametrize("reason,rejected", [
        ("HTTP 401", True), ("HTTP 403", True), ("HTTP 404", False),
        ("HTTP 500", False), (None, False), ("ReadTimeout: x", False),
    ])
    def test_token_rejection_classifier(self, reason, rejected):
        assert ts.metadata_reason_is_token_rejection(reason) is rejected


# ---------------------------------------------------------------------------
# The chain, driven through the real `_resolve_template`
# ---------------------------------------------------------------------------

@pytest.fixture
def chain(monkeypatch):
    """A GitHub keyed on WHICH token asks, plus the credential-free seams.

    Contents-API answers are keyed on the token ("" = anonymous REST); the raw
    reader and the anonymous ref probe are stubbed so no test reaches the
    network. Seams that land in later checkpoints are set with
    ``raising=False`` so this fixture works before and after them.
    """
    state = {"answers": {}, "calls": [], "raw": ({}, "HTTP 404"), "raw_calls": [],
             "ladder": [], "probe": ("ok", SHA)}

    def _fetch(repo, pat, ref=None):
        state["calls"].append({"repo": repo, "pat": pat, "ref": ref})
        return state["answers"].get(pat or "", ({}, "HTTP 404"))

    def _raw(repo, sha, *a, **k):
        state["raw_calls"].append((repo, sha))
        return state["raw"]

    probe = AsyncMock(side_effect=lambda *a, **k: state["probe"])
    monkeypatch.setattr(ts, "_fetch_template_yaml_result", _fetch)
    monkeypatch.setattr(ts, "_fetch_template_yaml_raw_result", _raw, raising=False)
    monkeypatch.setattr(ts, "_get_github_pat", lambda: PLATFORM_PAT)
    monkeypatch.setattr(crud, "resolve_github_pat_candidates",
                        lambda *a, **k: list(state["ladder"]), raising=False)
    monkeypatch.setattr(crud.git_service, "probe_anonymous_ref", probe)
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

        def ladder(self, *entries):
            state["ladder"] = list(entries)

        def create(self, *, pat, tier, repo="acme/public-tpl", source_mode=True):
            catalog = ts.get_github_template("github:" + repo)
            monkeypatch.setattr(
                crud, "_resolve_github_repo_and_pat",
                lambda *a, **k: (catalog, repo, pat, tier),
            )
            config = types.SimpleNamespace(
                template="github:" + repo, fork_to_own=None,
                source_branch=None, source_mode=source_mode, import_intent=None,
                name="public-tpl", kind=None, ephemeral=None,
                model_fields_set={"template", "fork_to_own", "source_branch",
                                  "source_mode", "import_intent"},
            )
            user = types.SimpleNamespace(id=7, username="owner", email="o@example.com")
            return asyncio.run(crud._resolve_template(config, user))

    yield Chain()
    ts._metadata_cache.clear()


# -- rows that hold today and must keep holding (never xfail) ---------------

def test_a_tokened_read_that_succeeds_today_makes_no_further_call(chain):
    """Operator rider: success today behaves exactly as today."""
    chain.ladder((USER_PAT, "per_user"), (PLATFORM_PAT, "global"))
    chain.answers(**{USER_PAT: ({"display_name": "Fine"}, None)})
    tr = chain.create(pat=USER_PAT, tier="per_user")
    assert [c["pat"] for c in chain.calls if c["pat"] != PLATFORM_PAT] == [USER_PAT]
    assert chain.raw_calls == []
    chain.probe.assert_not_awaited()
    assert (tr.github_pat_for_agent, tr.github_pat_tier) == (USER_PAT, "per_user")


def test_a_clean_404_on_the_preferred_tier_is_absence_with_no_fallback(chain):
    chain.ladder((USER_PAT, "per_user"), (PLATFORM_PAT, "global"))
    chain.answers(**{USER_PAT: ({}, "HTTP 404")})
    tr = chain.create(pat=USER_PAT, tier="per_user")
    assert [c["pat"] for c in chain.calls if c["pat"] != PLATFORM_PAT] == [USER_PAT]
    assert chain.raw_calls == []
    chain.probe.assert_not_awaited()
    assert tr.github_pat_tier == "per_user"


def test_a_private_required_template_no_path_can_read_still_refuses(chain):
    """ent#14 S2: a fallback-tier 404 is never "absent" on its own. User token
    rate-limited, platform cannot see the private repo, anonymous git says
    private-or-missing → the gate stays closed."""
    chain.ladder((USER_PAT, "per_user"), (PLATFORM_PAT, "global"))
    chain.answers(**{USER_PAT: ({}, "HTTP 403"), PLATFORM_PAT: ({}, "HTTP 404")})
    chain.probe_says("unavailable")
    with pytest.raises(HTTPException) as exc:
        chain.create(pat=USER_PAT, tier="per_user")
    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "TEMPLATE_METADATA_UNAVAILABLE"


# -- the chain (checkpoint C) -----------------------------------------------

@CHECKPOINT_C
def test_AC5_1_stale_user_token_platform_token_reads_and_becomes_the_identity(chain):
    chain.ladder((USER_PAT, "per_user"), (PLATFORM_PAT, "global"))
    chain.answers(**{USER_PAT: ({}, "HTTP 401"),
                     PLATFORM_PAT: ({"display_name": "Public"}, None)})
    tr = chain.create(pat=USER_PAT, tier="per_user")
    assert (tr.github_pat_for_agent, tr.github_pat_tier) == (PLATFORM_PAT, "global")
    assert chain.raw_calls == []


@CHECKPOINT_C
def test_AC5_2_no_token_anonymous_REST_capped_reads_credential_free(chain):
    chain.ladder()
    chain.answers(**{"": ({}, "HTTP 403")})
    chain.raw(({"display_name": "Public"}, None))
    tr = chain.create(pat="", tier="none")
    assert chain.raw_calls == [("acme/public-tpl", SHA)]
    assert not tr.github_pat_for_agent


@CHECKPOINT_C
def test_AC5_3_nothing_can_read_names_every_tier_and_no_token(chain):
    chain.ladder((USER_PAT, "per_user"), (PLATFORM_PAT, "global"))
    chain.answers(**{USER_PAT: ({}, "HTTP 401"), PLATFORM_PAT: ({}, "HTTP 404")})
    chain.probe_says("unavailable")
    with pytest.raises(HTTPException) as exc:
        chain.create(pat=USER_PAT, tier="per_user")
    assert exc.value.status_code == 503
    detail = exc.value.detail
    assert detail["code"] == "TEMPLATE_METADATA_UNAVAILABLE"
    assert [a["tier"] for a in detail["attempts"]] == ["per_user", "global", "anonymous"]
    assert [a["status"] for a in detail["attempts"][:2]] == ["HTTP 401", "HTTP 404"]
    dumped = json.dumps(detail)
    assert USER_PAT not in dumped and PLATFORM_PAT not in dumped
    assert "https://" not in dumped


@CHECKPOINT_C
def test_a_public_repo_with_no_template_yaml_creates_after_401_and_404(chain):
    """Orchestrator ruling T-a: user 401, platform 404, anonymous ls-remote ok,
    SHA-pinned raw 404 → absence. A raw 404 on a probe-proven public repo is
    definitive and clears the earlier rejections."""
    chain.ladder((USER_PAT, "per_user"), (PLATFORM_PAT, "global"))
    chain.answers(**{USER_PAT: ({}, "HTTP 401"), PLATFORM_PAT: ({}, "HTTP 404")})
    chain.probe_says("ok", SHA)
    chain.raw(({}, "HTTP 404"))
    tr = chain.create(pat=USER_PAT, tier="per_user")
    assert tr.github_repo_for_agent == "acme/public-tpl"
    assert chain.raw_calls == [("acme/public-tpl", SHA)]


@CHECKPOINT_C
def test_a_swap_to_tokenless_is_regated_after_the_swap(chain):
    """Orchestrator ruling T-e: bidirectional + saved token 401 + only the
    credential-free path reads → the ent#123 400 naming the rejected saved
    token, never a silently pull-only agent."""
    chain.ladder((USER_PAT, "per_user"))
    chain.answers(**{USER_PAT: ({}, "HTTP 401")})
    chain.raw(({"display_name": "Public"}, None))
    with pytest.raises(HTTPException) as exc:
        chain.create(pat=USER_PAT, tier="per_user", source_mode=False)
    assert exc.value.status_code == 400
    text = json.dumps(exc.value.detail).lower()
    assert "saved github token" in text and "401" in text
    assert USER_PAT not in text


@CHECKPOINT_C
def test_an_outage_never_swaps_the_identity(chain):
    chain.ladder((USER_PAT, "per_user"), (PLATFORM_PAT, "global"))
    chain.answers(**{USER_PAT: ({}, "HTTP 502"),
                     PLATFORM_PAT: ({"display_name": "Public"}, None)})
    tr = chain.create(pat=USER_PAT, tier="per_user")
    assert (tr.github_pat_for_agent, tr.github_pat_tier) == (USER_PAT, "per_user")


@CHECKPOINT_C
def test_a_403_reads_through_but_never_swaps_the_identity(chain):
    chain.ladder((USER_PAT, "per_user"), (PLATFORM_PAT, "global"))
    chain.answers(**{USER_PAT: ({}, "HTTP 403"),
                     PLATFORM_PAT: ({"display_name": "Public"}, None)})
    tr = chain.create(pat=USER_PAT, tier="per_user")
    assert (tr.github_pat_for_agent, tr.github_pat_tier) == (USER_PAT, "per_user")


@CHECKPOINT_C
def test_a_required_template_read_by_a_fallback_tier_still_refuses(chain):
    """The false pass stays closed: the fallback read decides the gate."""
    chain.ladder((USER_PAT, "per_user"), (PLATFORM_PAT, "global"))
    chain.answers(**{USER_PAT: ({}, "HTTP 401"),
                     PLATFORM_PAT: ({"fork_to_own": "required"}, None)})
    with pytest.raises(HTTPException) as exc:
        chain.create(pat=USER_PAT, tier="per_user")
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "FORK_TO_OWN_REQUIRED"


@CHECKPOINT_C
def test_a_transient_anonymous_probe_after_a_fallback_404_refuses(chain):
    chain.ladder((USER_PAT, "per_user"), (PLATFORM_PAT, "global"))
    chain.answers(**{USER_PAT: ({}, "HTTP 401"), PLATFORM_PAT: ({}, "HTTP 404")})
    chain.probe_says("transient")
    with pytest.raises(HTTPException) as exc:
        chain.create(pat=USER_PAT, tier="per_user")
    assert exc.value.status_code == 503
    assert [a["tier"] for a in exc.value.detail["attempts"]][-1] == "anonymous"
