"""
Post-creation repo binding — "bind to your own repo" (trinity-enterprise#109).

Covers the properties that would otherwise only be true by inspection:

1. **Classification** (§11.12 FR-1) — the supported row shape succeeds and
   every other shape is refused BY NAME, including the two that were argued
   rather than asserted in review: an already-writable agent is an ORDINARY
   rebind (no `ALREADY_WRITABLE` refusal — that refusal is what made the
   documented retry path unreachable), and `trinity-system` is refused through
   the existing no-git-config path so it never reaches a container recreate.

2. **The CAS is the commit point** — a row that moved under us yields 409 with
   the row untouched, and the post-commit loser is restored to its PREVIOUS
   values, never deleted. `delete_git_config` on a pre-existing row is
   destruction: it strips a live agent's binding, so its next recreate drops
   `GITHUB_REPO` and the agent comes back empty (#843/#1439).

3. **Ordering** — the PAT is persisted LAST (after the rewire) and strictly
   BEFORE the recreate, and a failure at any step leaves a state a retry
   converges from. The step-4-failure → retry → success case is the §0.4
   contradiction as an explicit regression test.

4. **Locks** — two agents racing one destination produce exactly one winner,
   and a Redis outage FAILS CLOSED with 503 rather than admitting two repo
   creates.

5. **Secret hygiene** — the PAT never appears in a response, a log record, or
   any error path, and a *stale baked* token in git output is redacted too
   (it is not `user_pat`, so `scrub_secret` alone would miss it).

Modules: src/backend/services/agent_service/repo_binding.py
         src/backend/services/git_service.py (rebind_origin_and_push)
         src/backend/db/schedules/git_config.py (rebind_git_config)
         src/backend/routers/git.py (locks, audit, idempotency)
Issue:   abilityai/trinity-enterprise#109 (Epic ent#122)
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
_TMP_DB = Path(tempfile.gettempdir()) / "trinity_test_ent109_binding.db"
os.environ.setdefault("TRINITY_DB_PATH", str(_TMP_DB))

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = str(_PROJECT_ROOT / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# Pinned at IMPORT time — a module resolved lazily inside a fixture inherits
# whatever Mock a sibling test left in sys.modules at collection time.
import database  # noqa: E402
import services.agent_service.fork_to_own as f2o  # noqa: E402
import services.agent_service.repo_binding as rb  # noqa: E402
import services.git_service as git_service  # noqa: E402
from services.agent_service.repo_binding import BindError  # noqa: E402

PAT = "ghp_user_secret_token"
DEST = "alice/my-agent-brain"
OLD_REPO = "Abilityai/cornelius"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


def _config(
    github_repo=OLD_REPO,
    source_mode=True,
    working_branch="main",
    source_branch="main",
    auto_sync_enabled=False,
):
    return SimpleNamespace(
        agent_name="cornelius",
        github_repo=github_repo,
        working_branch=working_branch,
        source_branch=source_branch,
        source_mode=source_mode,
        auto_sync_enabled=auto_sync_enabled,
    )


class _FakeDB:
    """Whole-object stand-in for `database.db`.

    Swapped wholesale rather than `setattr`-ing individual methods: a sibling
    test module leaks a method-less `database.db` stub, and patching attributes
    onto it fails under `pytest-randomly` (the `test_904` trap).
    """

    def __init__(self, config=None, bound_elsewhere=None, cas_ok=True):
        self.config = config
        self.bound_elsewhere = bound_elsewhere or []
        self.cas_ok = cas_ok
        self.calls = []
        self.pat_saved = None
        self.pat_save_ok = True
        self.restored = None

    def get_git_config(self, agent_name):
        return self.config

    def get_git_config_agent_names_for_repo(self, repo):
        return list(self.bound_elsewhere)

    def rebind_git_config(
        self, agent_name, *, new_github_repo, expected_github_repo, source_branch
    ):
        self.calls.append(("cas", new_github_repo, expected_github_repo, source_branch))
        if not self.cas_ok:
            return False
        self.config = _config(
            github_repo=new_github_repo,
            source_branch=source_branch,
            auto_sync_enabled=True,
        )
        return True

    def restore_git_config_binding(
        self, agent_name, *, github_repo, source_branch, auto_sync_enabled
    ):
        self.calls.append(("restore", github_repo, source_branch, auto_sync_enabled))
        self.restored = (github_repo, source_branch, auto_sync_enabled)
        self.config = _config(
            github_repo=github_repo,
            source_branch=source_branch,
            auto_sync_enabled=auto_sync_enabled,
        )
        return True

    def set_agent_github_pat(self, agent_name, pat):
        self.calls.append(("pat", agent_name))
        self.pat_saved = pat
        return self.pat_save_ok

    def has_agent_github_pat(self, agent_name):
        return self.pat_saved is not None

    def delete_git_config(self, agent_name):  # must never be called on this path
        self.calls.append(("DELETE", agent_name))
        return True


@pytest.fixture
def bind_env(monkeypatch):
    """Patch every boundary `bind_agent_to_own_repo` touches; record the order."""
    fake_db = _FakeDB(config=_config())
    order = []

    monkeypatch.setattr(rb, "db", fake_db)
    monkeypatch.setattr(database, "db", fake_db)

    running = SimpleNamespace(status="running", attrs={"Config": {"Env": []}})
    docker_mod = SimpleNamespace(get_agent_container=lambda name: running)
    monkeypatch.setitem(sys.modules, "services.docker_service", docker_mod)

    # The container's `origin` is tracked INDEPENDENTLY of the DB row, because
    # that is the whole subject: the CAS is the commit point, so between it and
    # the in-container rewire the row and the container legitimately disagree.
    # Deriving this from `fake_db.config` (as the first version of this fixture
    # did) makes the two agree by construction — the double then cannot witness
    # the one state every post-commit failure produces, and the retry test that
    # exists to prove convergence passes without exercising it.
    world = {"origin": OLD_REPO, "branch": "main"}

    async def fake_inspect(agent_name):
        return git_service.ContainerGitState(
            origin_repo=world["origin"], branch=world["branch"]
        )

    monkeypatch.setattr(git_service, "inspect_container_git", fake_inspect)

    async def fake_validate(gh):
        order.append("validate_pat")
        return "alice"

    async def fake_inspect_dest(gh, dest, login, *, private, description, user_pat):
        order.append("inspect_dest")
        return f2o.DestinationState(state["dest_state"], state["dest_branches"])

    monkeypatch.setattr(f2o, "validate_destination_pat", fake_validate)
    monkeypatch.setattr(f2o, "inspect_or_create_destination_repo", fake_inspect_dest)
    monkeypatch.setattr(rb, "GitHubService", lambda pat: MagicMock(), raising=False)

    async def fake_rebind(**kwargs):
        order.append("rebind")
        calls["rebind_kwargs"] = kwargs
        res = state["rebind_result"]
        # Mirror the real side effects so a later step observes the world the
        # production code would have left behind.
        if res.success:
            state["dest_state"] = "branches"      # our own history now occupies it
            state["dest_branches"] = [{"name": "main", "sha": "deadbeef"}]
            world["origin"] = kwargs["destination_repo"]
        elif res.stage == "rewire":
            # Push landed, origin did not move.
            state["dest_state"] = "branches"
            state["dest_branches"] = [{"name": "main", "sha": "deadbeef"}]
        return res

    monkeypatch.setattr(git_service, "rebind_origin_and_push", fake_rebind)

    async def fake_recreate(agent_name, container, owner):
        order.append("recreate")
        if state["recreate_raises"]:
            raise RuntimeError("docker exploded")

    lifecycle = SimpleNamespace(recreate_container_with_updated_config=fake_recreate)
    monkeypatch.setitem(sys.modules, "services.agent_service.lifecycle", lifecycle)

    def fake_clear_breakers(agent_name):
        order.append("clear_breakers")

    monkeypatch.setitem(
        sys.modules,
        "services.agent_runtime_state",
        SimpleNamespace(clear_agent_breakers=fake_clear_breakers),
    )

    # `set_agent_github_pat` goes through the recorded fake_db, but stamp the
    # ordering marker too so PAT-vs-recreate order is directly assertable.
    real_set = fake_db.set_agent_github_pat

    def ordered_set(agent_name, pat):
        order.append("pat")
        return real_set(agent_name, pat)

    fake_db.set_agent_github_pat = ordered_set

    calls = {}
    state = {
        "dest_state": "created",
        "dest_branches": [],
        "rebind_result": git_service.RebindResult(True, "done", branch="main"),
        "recreate_raises": False,
    }

    yield SimpleNamespace(
        db=fake_db, order=order, state=state, world=world, calls=calls
    )


async def _bind(**over):
    kwargs = dict(
        agent_name="cornelius",
        destination_repo=DEST,
        user_pat=PAT,
        private=True,
        owner_username="alice",
    )
    kwargs.update(over)
    return await rb.bind_agent_to_own_repo(**kwargs)


# ---------------------------------------------------------------------------
# 1. Classification (FR-1)
# ---------------------------------------------------------------------------


class TestClassification:
    @pytest.mark.asyncio
    async def test_tokenless_source_mode_agent_is_the_happy_path(self, bind_env):
        out = await _bind()
        assert out.github_repo == DEST
        assert out.previous_repo == OLD_REPO
        assert out.created_repo is True
        assert out.recreated is True
        # source_mode stays 1 and working_branch is untouched (FR-2): flipping
        # would carve trinity/<agent>/<id> and move the row INTO the partial
        # unique index.
        assert bind_env.db.config.source_mode is True
        assert bind_env.db.config.working_branch == "main"

    @pytest.mark.asyncio
    async def test_already_writable_agent_is_an_ordinary_rebind(self, bind_env):
        """The reframe (§2). An agent that already owns a writable repo used to
        be refused ALREADY_WRITABLE — which is exactly what made the documented
        retry after a partial failure return 409 instead of converging."""
        bind_env.db.config = _config(github_repo="alice/old-brain")
        bind_env.world["origin"] = "alice/old-brain"  # steady state: row and container agree
        bind_env.db.pat_saved = "ghp_existing"
        out = await _bind()
        assert out.github_repo == DEST
        assert out.previous_repo == "alice/old-brain"

    @pytest.mark.asyncio
    async def test_no_git_config_row_is_refused_by_name(self, bind_env):
        bind_env.db.config = None
        with pytest.raises(BindError) as exc:
            await _bind()
        assert exc.value.status_code == 400
        assert exc.value.code == "BIND_NO_GIT_CONFIG"
        assert "Initialize GitHub Sync" in exc.value.message

    @pytest.mark.asyncio
    async def test_system_agent_is_refused_before_any_recreate(self, bind_env):
        """D4: `trinity-system` has no git-config row, so it is refused through
        the existing path and NEVER reaches `recreate_container_with_updated_config`
        — which, called directly, bypasses #1816's running-system-agent gate.
        Asserted rather than reasoned."""
        bind_env.db.config = None
        with pytest.raises(BindError) as exc:
            await _bind(agent_name="trinity-system")
        assert exc.value.code == "BIND_NO_GIT_CONFIG"
        assert "recreate" not in bind_env.order

    @pytest.mark.asyncio
    async def test_working_branch_mode_is_refused_by_name(self, bind_env):
        bind_env.db.config = _config(
            source_mode=False, working_branch="trinity/cornelius/ab12cd34"
        )
        with pytest.raises(BindError) as exc:
            await _bind()
        assert exc.value.status_code == 409
        assert exc.value.code == "BIND_WORKING_BRANCH_MODE_UNSUPPORTED"

    @pytest.mark.asyncio
    async def test_credential_state_does_not_route_classification(self, bind_env):
        """§0.3: the index keys on `source_mode`, the old gate keyed on write
        credentials. A source_mode=0 row WITHOUT credentials must still be
        refused — routing it here by credential state would move the row within
        `idx_git_config_repo_branch_unique`."""
        bind_env.db.config = _config(source_mode=False)
        bind_env.db.pat_saved = None  # no write credentials at all
        with pytest.raises(BindError) as exc:
            await _bind()
        assert exc.value.code == "BIND_WORKING_BRANCH_MODE_UNSUPPORTED"

    @pytest.mark.asyncio
    async def test_stopped_agent_is_refused(self, bind_env, monkeypatch):
        stopped = SimpleNamespace(status="exited", attrs={})
        monkeypatch.setitem(
            sys.modules,
            "services.docker_service",
            SimpleNamespace(get_agent_container=lambda n: stopped),
        )
        with pytest.raises(BindError) as exc:
            await _bind()
        assert exc.value.status_code == 400
        assert exc.value.code == "BIND_AGENT_NOT_RUNNING"

    @pytest.mark.asyncio
    async def test_origin_disagreement_is_unclassified_and_reports_both(
        self, bind_env, monkeypatch
    ):
        async def divergent(agent_name):
            return git_service.ContainerGitState(
                origin_repo="someone/else", branch="main"
            )

        monkeypatch.setattr(git_service, "inspect_container_git", divergent)
        with pytest.raises(BindError) as exc:
            await _bind()
        assert exc.value.status_code == 409
        assert exc.value.code == "BIND_STATE_UNCLASSIFIED"
        assert exc.value.context["observed_origin"] == "someone/else"
        assert exc.value.context["configured_repo"] == OLD_REPO

    @pytest.mark.asyncio
    async def test_missing_git_dir_is_unclassified_never_assumed_to_agree(
        self, bind_env, monkeypatch
    ):
        async def unreadable(agent_name):
            return git_service.ContainerGitState()

        monkeypatch.setattr(git_service, "inspect_container_git", unreadable)
        with pytest.raises(BindError) as exc:
            await _bind()
        assert exc.value.code == "BIND_STATE_UNCLASSIFIED"

    @pytest.mark.asyncio
    async def test_origin_comparison_is_case_insensitive(self, bind_env, monkeypatch):
        """GitHub slugs are case-insensitive; this check asks "do these name the
        same repo?", unlike the CAS which asks "did the row change?"."""

        async def recased(agent_name):
            return git_service.ContainerGitState(
                origin_repo=OLD_REPO.upper(), branch="main"
            )

        monkeypatch.setattr(git_service, "inspect_container_git", recased)
        out = await _bind()
        assert out.github_repo == DEST

    @pytest.mark.asyncio
    async def test_destination_already_bound_to_another_agent_is_refused(
        self, bind_env
    ):
        bind_env.db.bound_elsewhere = ["some-other-agent"]
        with pytest.raises(BindError) as exc:
            await _bind()
        assert exc.value.status_code == 409
        assert exc.value.code == "BIND_DESTINATION_IN_USE"
        # Refused BEFORE the commit point.
        assert not any(c[0] == "cas" for c in bind_env.db.calls)

    @pytest.mark.asyncio
    async def test_destination_holding_data_is_refused(self, bind_env):
        bind_env.state["dest_state"] = "branches"
        bind_env.state["dest_branches"] = [{"name": "main", "sha": "abc"}]
        with pytest.raises(BindError) as exc:
            await _bind()
        assert exc.value.status_code == 409
        assert exc.value.code == "BIND_DESTINATION_EXISTS"

    @pytest.mark.asyncio
    async def test_empty_destination_is_reused_not_refused(self, bind_env):
        bind_env.state["dest_state"] = "empty"
        out = await _bind()
        assert out.created_repo is False
        assert out.reused_existing is True


# ---------------------------------------------------------------------------
# 2. The CAS is the commit point
# ---------------------------------------------------------------------------


class TestCommitPoint:
    @pytest.mark.asyncio
    async def test_cas_predicate_carries_the_value_read_before_github_state(
        self, bind_env
    ):
        await _bind()
        cas = next(c for c in bind_env.db.calls if c[0] == "cas")
        _, new, expected, branch = cas
        assert new == DEST
        assert expected == OLD_REPO
        assert branch == "main"

    @pytest.mark.asyncio
    async def test_concurrent_modification_is_409_with_nothing_partial(self, bind_env):
        bind_env.db.cas_ok = False
        with pytest.raises(BindError) as exc:
            await _bind()
        assert exc.value.status_code == 409
        assert exc.value.code == "BIND_CONCURRENT_MODIFICATION"
        assert exc.value.partial is False
        # Nothing past the commit point ran.
        assert "rebind" not in bind_env.order
        assert "pat" not in bind_env.order
        assert "recreate" not in bind_env.order

    @pytest.mark.asyncio
    async def test_post_commit_loser_is_restored_never_deleted(self, bind_env):
        """The §0.2 regression. ent#93's loser path deletes its row; on a
        PRE-EXISTING row that strips a live agent's binding, so the next
        recreate drops GITHUB_REPO and the agent returns empty (#843/#1439)."""
        calls_before = {"n": 0}
        real = bind_env.db.get_git_config_agent_names_for_repo

        def racing(repo):
            # Clean before the CAS, contended on the post-commit belt re-check.
            calls_before["n"] += 1
            return [] if calls_before["n"] == 1 else ["a-second-agent"]

        bind_env.db.get_git_config_agent_names_for_repo = racing

        with pytest.raises(BindError) as exc:
            await _bind()
        assert exc.value.code == "BIND_DESTINATION_IN_USE"

        assert bind_env.db.restored == (
            OLD_REPO,
            "main",
            False,
        ), "the loser must be restored to its captured PREVIOUS values"
        assert not any(
            c[0] == "DELETE" for c in bind_env.db.calls
        ), "delete_git_config on a pre-existing row is destruction, not rollback"
        assert bind_env.db.config.github_repo == OLD_REPO


# ---------------------------------------------------------------------------
# 3. Ordering — the PAT is written last, and failures converge on retry
# ---------------------------------------------------------------------------


class TestOrdering:
    @pytest.mark.asyncio
    async def test_breakers_are_cleared_before_the_container_is_replaced(
        self, bind_env
    ):
        """Both circuit breakers are keyed by agent NAME with no TTL, so the
        replacement container inherits its predecessor's verdict and can come up
        fast-failed without ever being contacted (#1560).

        `start_agent_internal` clears them immediately before its own call to
        `recreate_container_with_updated_config`; this is that helper's SECOND
        production call site and owes the same. A dispatch breaker can be open
        on an agent that is running and answering `docker exec` — exactly the
        state a bind requires — so the inheritance is reachable, not theoretical.

        Order matters: clearing AFTER the recreate would reset a breaker the
        fresh container had already legitimately tripped.
        """
        await _bind()
        assert "clear_breakers" in bind_env.order, (
            "the bind path replaces the container without clearing name-keyed "
            "breaker state (#1560)"
        )
        assert bind_env.order.index("clear_breakers") < bind_env.order.index(
            "recreate"
        )

    @pytest.mark.asyncio
    async def test_breakers_are_not_cleared_when_the_bind_never_gets_that_far(
        self, bind_env
    ):
        """A refusal must not reset a live agent's breaker as a side effect —
        no container is replaced, so there is nothing to clear for."""
        bind_env.db.config = None  # BIND_NO_GIT_CONFIG
        with pytest.raises(BindError):
            await _bind()
        assert "clear_breakers" not in bind_env.order

    @pytest.mark.asyncio
    async def test_pat_is_persisted_after_the_rewire_and_before_the_recreate(
        self, bind_env
    ):
        await _bind()
        o = bind_env.order
        assert (
            o.index("rebind") < o.index("pat") < o.index("recreate")
        ), f"expected rebind → pat → recreate, got {o}"

    @pytest.mark.asyncio
    async def test_push_failure_does_not_persist_the_pat(self, bind_env):
        """§0.4: writing the PAT early makes `_agent_has_write_credentials`
        report the agent as already-writable, which is what made the documented
        retry return 409 — and lets a mid-window manual Push succeed against
        the OLD repo with the NEW token."""
        bind_env.state["rebind_result"] = git_service.RebindResult(
            False, "push", branch="main", error="remote rejected."
        )
        with pytest.raises(BindError) as exc:
            await _bind()
        assert exc.value.status_code == 502
        assert exc.value.code == "BIND_PUSH_FAILED"
        assert exc.value.partial is True
        assert bind_env.db.pat_saved is None
        assert "recreate" not in bind_env.order

    @pytest.mark.asyncio
    async def test_fail_at_push_then_retry_succeeds(self, bind_env):
        """The §0.4 contradiction as an explicit regression test: the retry after
        a partial failure must converge, not hit a refusal.

        Nothing is hand-waved back into place between the two calls. The fixture
        moves the world the way production does — the CAS lands, the push does
        not, so the row names the destination while the container's origin still
        names the old repo. An earlier version of this test derived origin FROM
        the row and reset `dest_state` by hand, which made it pass against code
        that refused the retry with `BIND_STATE_UNCLASSIFIED`.
        """
        bind_env.state["rebind_result"] = git_service.RebindResult(
            False, "push", branch="main", error="transient network error."
        )
        with pytest.raises(BindError):
            await _bind()

        # The state a real partial failure leaves: row moved, container did not.
        assert bind_env.db.config.github_repo == DEST
        assert bind_env.world["origin"] == OLD_REPO

        bind_env.state["rebind_result"] = git_service.RebindResult(
            True, "done", branch="main"
        )
        out = await _bind()
        assert out.github_repo == DEST
        assert bind_env.db.pat_saved == PAT
        assert out.recreated is True

    @pytest.mark.asyncio
    async def test_retry_after_a_failed_recreate_converges(self, bind_env):
        """The other half: push and rewire landed, the rebuild did not.

        Here the row and the container AGREE (both name the destination) — so
        classification is happy — but the destination now holds the history the
        first attempt pushed. Reading those refs as foreign data refuses the
        retry with `BIND_DESTINATION_EXISTS`; they are this agent's own.
        """
        bind_env.state["recreate_raises"] = True
        with pytest.raises(BindError) as exc:
            await _bind()
        assert exc.value.code == "BIND_RECREATE_FAILED"
        assert exc.value.partial is True
        assert bind_env.world["origin"] == DEST
        assert bind_env.state["dest_state"] == "branches"  # our own pushed history

        bind_env.state["recreate_raises"] = False
        out = await _bind()
        assert out.github_repo == DEST
        assert out.recreated is True

    @pytest.mark.asyncio
    async def test_resumption_does_not_repoint_upstream_at_the_destination(
        self, bind_env
    ):
        """`previous_repo` on a resume already IS the destination.

        Passing it through would set `upstream` to the destination itself and
        erase the record of the real upstream (the public template the agent was
        cloned from), which is the one piece of provenance the rebind exists to
        preserve. The first attempt already set it, and `.git/config` lives on
        the workspace volume every recreate reuses (#1664).
        """
        bind_env.state["recreate_raises"] = True
        with pytest.raises(BindError):
            await _bind()
        assert bind_env.calls["rebind_kwargs"]["previous_repo"] == OLD_REPO

        bind_env.state["recreate_raises"] = False
        await _bind()
        assert bind_env.calls["rebind_kwargs"]["previous_repo"] is None

    @pytest.mark.asyncio
    async def test_a_resume_tolerates_an_unexpected_origin(self, bind_env):
        """A resumption proceeds even when `origin` is a third, unexpected repo.

        This looks like a weakened guard and is not, because `origin` never
        selects what gets pushed: step 4 pushes `refs/heads/<branch>` from the
        workspace to the destination by EXPLICIT URL, and only then writes
        `origin`. So the "could push the wrong history" rationale the
        unclassified refusal is built on does not apply once the row
        unambiguously names this destination — the user has now asked for it
        twice, and the content is the workspace either way.

        Refusing here would instead recreate the very dead end this carve-out
        exists to remove: after a committed CAS the old repo name is gone from
        the row, so "still the old repo" and "something else" are
        indistinguishable, and treating the ambiguity as fatal would strand any
        agent whose origin drifted out of band with no recovery path at all.
        """
        bind_env.db.config = _config(github_repo=DEST)
        bind_env.world["origin"] = "someone/else"

        out = await _bind()
        assert out.github_repo == DEST
        # ...and it did NOT invent an upstream from the resumed row.
        assert bind_env.calls["rebind_kwargs"]["previous_repo"] is None

    @pytest.mark.asyncio
    async def test_a_mismatch_against_any_other_repo_is_still_unclassified(
        self, bind_env
    ):
        """The carve-out is scoped to the requested destination, not to mismatch
        in general: a row naming some THIRD repo, with a container that
        disagrees, is still the unknown state the guard exists for."""
        bind_env.db.config = _config(github_repo="alice/some-other-repo")
        bind_env.world["origin"] = "someone/else"
        with pytest.raises(BindError) as exc:
            await _bind()
        assert exc.value.code == "BIND_STATE_UNCLASSIFIED"

    @pytest.mark.asyncio
    async def test_rewire_failure_is_reported_as_partial(self, bind_env):
        bind_env.state["rebind_result"] = git_service.RebindResult(
            False, "rewire", branch="main", error="set-url failed."
        )
        with pytest.raises(BindError) as exc:
            await _bind()
        assert exc.value.code == "BIND_REWIRE_FAILED"
        assert exc.value.partial is True

    @pytest.mark.asyncio
    async def test_pat_persist_failure_blocks_the_recreate(self, bind_env):
        """A recreate with no PAT row bakes a repo-bound container with no token
        (the config-drift gate is `per_agent_only`), and startup.sh then
        blackholes its push remote."""
        bind_env.db.pat_save_ok = False
        with pytest.raises(BindError) as exc:
            await _bind()
        assert exc.value.code == "BIND_PAT_PERSIST_FAILED"
        assert "recreate" not in bind_env.order

    @pytest.mark.asyncio
    async def test_recreate_failure_warns_against_a_plain_restart(self, bind_env):
        """A plain restart re-runs startup.sh, which rewrites origin from the
        container's stale baked env and would UNDO the rebind."""
        bind_env.state["recreate_raises"] = True
        with pytest.raises(BindError) as exc:
            await _bind()
        assert exc.value.code == "BIND_RECREATE_FAILED"
        assert exc.value.partial is True
        assert "restart" in exc.value.message.lower()
        # Honest about retry rather than promising self-healing convergence.
        assert "idempotent" in exc.value.message.lower()


# ---------------------------------------------------------------------------
# 4. Secret hygiene
# ---------------------------------------------------------------------------


class TestSecretHygiene:
    @pytest.mark.asyncio
    async def test_pat_never_appears_in_the_outcome_or_its_audit(self, bind_env):
        out = await _bind()
        blob = repr(out) + repr(out.audit)
        assert PAT not in blob

    @pytest.mark.asyncio
    async def test_pat_never_appears_on_any_error_path(self, bind_env):
        for setup in (
            lambda: bind_env.state.update(
                dest_state="branches", dest_branches=[{"name": "m", "sha": "s"}]
            ),
            lambda: setattr(bind_env.db, "cas_ok", False),
            lambda: bind_env.state.update(
                rebind_result=git_service.RebindResult(
                    False, "push", branch="main", error=f"auth failed for {PAT}"
                )
            ),
            lambda: bind_env.state.update(recreate_raises=True),
        ):
            bind_env.state["dest_state"] = "created"
            bind_env.state["dest_branches"] = []
            bind_env.state["rebind_result"] = git_service.RebindResult(
                True, "done", branch="main"
            )
            bind_env.state["recreate_raises"] = False
            bind_env.db.cas_ok = True
            bind_env.db.config = _config()
            setup()
            with pytest.raises(BindError) as exc:
                await _bind()
            assert PAT not in exc.value.message, exc.value.code
            assert PAT not in repr(exc.value.context), exc.value.code

    def test_stale_baked_token_is_redacted_not_just_the_request_pat(self):
        """§5 / D3. Git stderr embeds whatever userinfo is in the remote URL,
        which on a rebind is often a *stale baked* token that is NOT `user_pat`
        — so `scrub_secret` alone leaks it."""
        stale = "ghp_stale_baked_token"
        raw = (
            f"fatal: unable to access 'https://oauth2:{stale}@github.com/o/r.git': "
            f"403 while using {PAT}"
        )
        cleaned = git_service._scrub_git_output(raw, PAT)
        assert stale not in cleaned
        assert PAT not in cleaned
        assert "***" in cleaned

    def test_origin_parsing_discards_userinfo(self):
        """The parsed value reaches the API response and the audit row."""
        parsed = git_service._parse_repo_from_remote_url(
            f"https://oauth2:{PAT}@github.com/alice/brain.git"
        )
        assert parsed == "alice/brain"
        assert PAT not in parsed


# ---------------------------------------------------------------------------
# 5. The CAS statement itself (real SQL, not a double)
# ---------------------------------------------------------------------------


class TestRebindCasSql:
    """Exercises the actual UPDATE against a real SQLite engine, because the
    predicate is the whole safety argument and a double cannot verify it."""

    @pytest.fixture
    def ops(self, tmp_path, monkeypatch):
        import db.engine as engine_mod
        from db.schedules.git_config import ScheduleGitConfigMixin
        from sqlalchemy import create_engine
        from db.tables import metadata

        db_file = tmp_path / "cas.db"
        eng = create_engine(f"sqlite:///{db_file}")
        metadata.create_all(eng)
        monkeypatch.setattr(engine_mod, "get_engine", lambda: eng)
        import db.schedules.git_config as gc_mod

        monkeypatch.setattr(gc_mod, "get_engine", lambda: eng)

        class Ops(ScheduleGitConfigMixin):
            def _generate_id(self):
                return "cfg-1"

        o = Ops()
        o.create_git_config(
            agent_name="cornelius",
            github_repo=OLD_REPO,
            working_branch="main",
            instance_id="i1",
            source_mode=True,
        )
        return o

    def test_matching_predicate_swaps_the_binding(self, ops):
        assert (
            ops.rebind_git_config(
                "cornelius",
                new_github_repo=DEST,
                expected_github_repo=OLD_REPO,
                source_branch="main",
            )
            is True
        )
        row = ops.get_git_config("cornelius")
        assert row.github_repo == DEST
        assert row.auto_sync_enabled is True
        # FR-2: untouched.
        assert row.source_mode is True
        assert row.working_branch == "main"

    def test_stale_expected_value_loses_and_writes_nothing(self, ops):
        assert (
            ops.rebind_git_config(
                "cornelius",
                new_github_repo=DEST,
                expected_github_repo="someone/moved-it",
                source_branch="main",
            )
            is False
        )
        assert ops.get_git_config("cornelius").github_repo == OLD_REPO

    def test_unknown_agent_loses(self, ops):
        assert (
            ops.rebind_git_config(
                "ghost",
                new_github_repo=DEST,
                expected_github_repo=OLD_REPO,
                source_branch="main",
            )
            is False
        )

    def test_only_one_of_two_racers_can_win(self, ops):
        """Both read the same `expected`; the second finds it changed."""
        assert (
            ops.rebind_git_config(
                "cornelius",
                new_github_repo="alice/first",
                expected_github_repo=OLD_REPO,
                source_branch="main",
            )
            is True
        )
        assert (
            ops.rebind_git_config(
                "cornelius",
                new_github_repo="bob/second",
                expected_github_repo=OLD_REPO,
                source_branch="main",
            )
            is False
        )
        assert ops.get_git_config("cornelius").github_repo == "alice/first"

    def test_restore_puts_the_previous_values_back(self, ops):
        ops.rebind_git_config(
            "cornelius",
            new_github_repo=DEST,
            expected_github_repo=OLD_REPO,
            source_branch="main",
        )
        assert (
            ops.restore_git_config_binding(
                "cornelius",
                github_repo=OLD_REPO,
                source_branch="main",
                auto_sync_enabled=False,
            )
            is True
        )
        row = ops.get_git_config("cornelius")
        assert row.github_repo == OLD_REPO
        assert row.auto_sync_enabled is False
        # The row still EXISTS — that is the whole point versus a delete.
        assert row is not None
