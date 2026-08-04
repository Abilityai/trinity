"""
Post-creation repo binding — "bind this agent to a repo you own" (ent#109).

Points a LIVE agent at a GitHub repo the user owns, creating it if needed,
from the agent's *current workspace* rather than from its template. This is
the ownership retrofit ent#123 left open: a tokenless public-template agent
(the default Cornelius) accumulates a knowledge base it cannot push anywhere,
and the only previously documented escape was "create a new agent with
fork-to-own and import your data" — which throws away the agent's identity,
its name reservation, and its history.

It is a **rebind, not a fork verb**. An agent that already has a writable repo
is therefore an ordinary rebind (typo'd destination, wrong PAT account, org
migration, partial-failure retry), not a refusal. That is what makes ent#109
AC #3 ("works for any agent") literally true instead of quietly redefined.

Placement: the orchestration lives HERE and not in ``routers/git.py``
(Invariant #1 — Router → Service → DB). The router is a thin HTTP mapper that
owns only the two locks and the audit row; this module raises ``BindError``
and never an ``HTTPException``, mirroring ``chat_execution_service`` (#1483).

Ordering is load-bearing and is documented per-step in ``bind_agent_to_own_repo``.
The two properties worth stating up front:

* **The CAS is the commit point.** Everything before it is either read-only or
  external-only (a GitHub repo that a retry reuses); everything after it is
  reported honestly as partially applied rather than rolled back.
* **The PAT is persisted LAST.** Writing it earlier makes the agent look
  already-writable to ``_agent_has_write_credentials`` on a retry, and lets a
  mid-window manual Push succeed against the OLD repo with the NEW token.

Security: the user PAT arrives as a ``SecretStr`` unwrapped exactly once at
the router boundary, is never logged, and every message that could carry git
output is scrubbed for BOTH the request's token and any URL userinfo (which on
a rebind can be a *stale baked* token that is not the request's).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from database import db
from services import git_service
from utils.credential_sanitizer import scrub_secret_and_urls

logger = logging.getLogger(__name__)


class BindError(Exception):
    """A named, structured refusal or failure of a repo binding.

    Carries the HTTP status the router should map it to, but is NOT an
    ``HTTPException`` — this module stays HTTP-free so it can be unit-tested
    and reused without FastAPI in the loop (Invariant #1).

    ``partial`` marks a failure that happened AFTER the CAS commit point: the
    DB binding is saved and the operation is genuinely half-applied. The
    router surfaces that distinction rather than letting a 502 read like a
    clean no-op.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        partial: bool = False,
        context: Optional[dict] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.partial = partial
        self.context = context or {}


@dataclass
class BindOutcome:
    """A successful binding, as the router should report it."""

    agent_name: str
    github_repo: str
    previous_repo: str
    default_branch: str
    private: bool
    created_repo: bool
    reused_existing: bool
    recreated: bool
    audit: dict = field(default_factory=dict)


def _scrub(text: str, user_pat: str) -> str:
    """Belt at the boundary where the PAT is in scope.

    Every message this module builds from FOREIGN text is scrubbed here even
    though the producer is supposed to have scrubbed it already. Relying on
    "the producer handled it" is the single point of failure the dual-scrub
    rule exists to remove: `git_service` scrubs what it reads from a container,
    but the docker/GitHub/httpx exception paths reach us through libraries that
    never saw the token and have no reason to.
    """
    return scrub_secret_and_urls(text, user_pat)


def _translate_destination_error(exc) -> BindError:
    """Map the shared destination primitive's ``HTTPException`` to a ``BindError``.

    ``inspect_or_create_destination_repo`` lives in ``fork_to_own``, which
    predates this seam and raises structured ``HTTPException``s directly into
    ``crud``'s create path. Translating at this single call site keeps
    ``repo_binding`` HTTP-free without forking the ``FORK_*`` error registry —
    which is the whole point of sharing the primitive (AC #4).
    """
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        return BindError(
            getattr(exc, "status_code", 502),
            detail.get("code", "BIND_DESTINATION_UNREACHABLE"),
            detail.get("error", "Could not resolve the destination repository."),
        )
    return BindError(
        getattr(exc, "status_code", 502),
        "BIND_DESTINATION_UNREACHABLE",
        str(detail or "Could not resolve the destination repository."),
    )


async def _classify(agent_name: str, destination_repo: str):
    """Pre-flight: refuse every shape this feature does not build machinery for.

    Returns ``(config, container, branch)`` for a supported agent. Raises a
    NAMED ``BindError`` otherwise — requirements §11.12 FR-1. Nothing here
    writes, so every refusal below leaves the agent untouched.

    The partition is on ``source_mode``, which is the column
    ``idx_git_config_repo_branch_unique`` actually keys on
    (``WHERE source_mode = 0``). Partitioning on write-credential state
    instead — the intuitive reading of "is this agent writable yet?" — is an
    orthogonal column, and would route a credential-less ``source_mode = 0``
    row into this engine, whose rebind then moves it *within* that index.

    **Resumption is not an unclassified state.** The CAS (step 3) is the commit
    point, and after it the row names the destination while the container's
    ``origin`` still names the old repo until step 4 lands. That skew is the
    NORMAL, expected shape of a partially-applied bind — every post-commit
    failure message tells the user to retry — so reading it as "unknown state"
    would refuse the exact recovery the feature documents. A row that already
    names *this* destination is therefore treated as a resumption: the origin is
    allowed to lag, because the row is the record of intent and the retry's job
    is to make the container catch up. A mismatch against any OTHER repo is
    still unclassified, which is the case the guard actually exists for.

    A resumption tolerates an origin that is neither the old repo nor the
    destination, and that is not a weakened guard: ``origin`` never selects what
    gets pushed. Step 4 pushes ``refs/heads/<branch>`` from the workspace to the
    destination by EXPLICIT URL and only then writes ``origin``, so the "could
    push the wrong history" rationale does not reach this case. It also cannot
    be tightened without cost — a committed CAS has already overwritten the old
    repo name, so "still the old repo" and "something else entirely" are
    indistinguishable from the row alone, and treating that ambiguity as fatal
    would strand the agent with no recovery path, which is the dead end this
    carve-out removes.
    """
    from services.docker_service import get_agent_container

    config = db.get_git_config(agent_name)
    if not config:
        # Also the refusal that covers `local:` template agents AND the
        # is_system `trinity-system` orchestrator, neither of which has a row.
        raise BindError(
            400,
            "BIND_NO_GIT_CONFIG",
            "This agent has no GitHub sync configured, so there is no binding "
            "to move. Use 'Initialize GitHub Sync' on the Git tab to connect "
            "it to a repository first.",
        )

    if not getattr(config, "source_mode", False):
        raise BindError(
            409,
            "BIND_WORKING_BRANCH_MODE_UNSUPPORTED",
            "This agent tracks a carved working branch "
            f"('{config.working_branch}') rather than its repository's default "
            "branch. Rebinding it would need the working branch to be "
            "re-reserved against the destination, which this action does not "
            "do. Move it by hand, or open an issue if you need this.",
        )

    container = get_agent_container(agent_name)
    if not container or getattr(container, "status", None) != "running":
        raise BindError(
            400,
            "BIND_AGENT_NOT_RUNNING",
            "The agent must be running to bind it to a repository — its "
            "current workspace is the content being pushed.",
        )

    state = await git_service.inspect_container_git(agent_name)

    if not state.origin_repo or not state.branch:
        raise BindError(
            409,
            "BIND_STATE_UNCLASSIFIED",
            "Could not read the agent's git state: it has no readable 'origin' "
            "remote or is on a detached HEAD. Nothing was changed — resolve the "
            "repository state in the agent, then retry.",
            context={
                "observed_origin": state.origin_repo,
                "observed_branch": state.branch,
                "configured_repo": config.github_repo,
            },
        )

    # Case-INSENSITIVE, unlike the CAS predicate, and deliberately so: this
    # asks "do the container and the row agree about the same GitHub repo?",
    # and GitHub slugs are case-insensitive. The CAS asks the different
    # question "did the row change under me?", where any write — including a
    # pure re-casing — is a change worth losing the race over.
    resuming = (config.github_repo or "").lower() == destination_repo.lower()
    if not resuming and state.origin_repo.lower() != (config.github_repo or "").lower():
        raise BindError(
            409,
            "BIND_STATE_UNCLASSIFIED",
            f"The agent's live origin ('{state.origin_repo}') does not match "
            f"its recorded repository ('{config.github_repo}'). Rebinding from "
            f"an unknown state could push the wrong history, so nothing was "
            f"changed.",
            context={
                "observed_origin": state.origin_repo,
                "configured_repo": config.github_repo,
            },
        )

    return config, container, state.branch, resuming


def _refuse_if_destination_bound_elsewhere(
    agent_name: str, destination_repo: str
) -> None:
    """The ent#93 destination guard, reused rather than re-derived.

    Source-mode rows bypass the partial unique index, so nothing in the schema
    stops two auto-pushing agents from binding one repo — which is how the
    fleet-wide `duplicate_binding` audit flag gets lit. Note this matches
    SOFT-deleted agents too, on purpose: admin recovery (#834) would resurrect
    the binding.
    """
    try:
        bound = db.get_git_config_agent_names_for_repo(destination_repo)
    except Exception as e:  # noqa: BLE001 — a read failure must not open the gate
        raise BindError(
            502,
            "BIND_DESTINATION_UNREACHABLE",
            f"Could not check whether '{destination_repo}' is already bound to "
            f"another agent: {e}",
        )
    others = [name for name in bound if name != agent_name]
    if others:
        raise BindError(
            409,
            "BIND_DESTINATION_IN_USE",
            f"'{destination_repo}' is already bound to another Trinity agent "
            f"({others[0]}). Two agents auto-pushing to one repository "
            f"overwrite each other's work — choose a different repository.",
        )


async def bind_agent_to_own_repo(
    agent_name: str,
    destination_repo: str,
    user_pat: str,
    private: bool,
    owner_username: str,
) -> BindOutcome:
    """Bind ``agent_name`` to ``destination_repo``. See the module docstring.

    Raises ``BindError`` (never ``HTTPException``). The caller holds the two
    locks and writes the audit row.
    """
    from services.agent_service import fork_to_own as f2o
    from services.agent_service.lifecycle import recreate_container_with_updated_config
    from services.agent_runtime_state import clear_agent_breakers
    from services.github_service import GitHubService

    # --- 1. Classify. Read-only; every refusal leaves the agent untouched. ---
    config, container, branch, resuming = await _classify(
        agent_name, destination_repo
    )
    previous_repo = config.github_repo
    previous_source_branch = config.source_branch
    previous_auto_sync = bool(getattr(config, "auto_sync_enabled", False))

    _refuse_if_destination_bound_elsewhere(agent_name, destination_repo)

    # --- 2. External state only. A repo created here is reused by a retry, so
    #        failing after this point costs the user nothing but a repo. -----
    user_gh = GitHubService(user_pat)
    try:
        login = await f2o.validate_destination_pat(user_gh)
        dest = await f2o.inspect_or_create_destination_repo(
            user_gh,
            destination_repo,
            login,
            private=private,
            description=f"Trinity agent workspace ({agent_name})",
            user_pat=user_pat,
        )
    except BindError:
        raise
    except Exception as exc:
        if hasattr(exc, "status_code") and hasattr(exc, "detail"):
            raise _translate_destination_error(exc)
        raise

    # Reuse/refuse POLICY — this caller's, not the primitive's. There is no
    # template tip to compare against here (the content source is the agent's
    # workspace volume), so ANY existing ref is data we must not push over —
    # UNLESS this is a resumption, in which case those refs are this agent's
    # OWN history from the attempt that already committed the CAS, and refusing
    # them would refuse the documented retry.
    #
    # Relaxing the gate is bounded by git, not by trust: the push at step 4 is a
    # plain `git push <url> refs/heads/X:refs/heads/X` with no `--force` and no
    # `+` refspec, so unrelated history is rejected non-fast-forward and an
    # unrelated branch is left untouched. This gate is a UX guard against
    # tangling an agent into an occupied repo; integrity is enforced one layer
    # down.
    if dest.state == "branches" and not resuming:
        raise BindError(
            409,
            "BIND_DESTINATION_EXISTS",
            f"Repository '{destination_repo}' already exists and contains "
            f"branches. Binding would push this agent's history over it. "
            f"Choose an empty repository or a new name, and let Trinity create "
            f"it. (A repo pre-created WITH a README also lands here.)",
        )
    created_repo = dest.state == "created"

    # --- 3. COMMIT POINT — a single CAS. --------------------------------------
    # `expected_github_repo` is the value read in step 1, before any GitHub
    # state existed. rowcount 0 means the row moved under us, and because this
    # one statement is the whole commit, nothing is partially written.
    if not db.rebind_git_config(
        agent_name,
        new_github_repo=destination_repo,
        expected_github_repo=previous_repo,
        source_branch=branch,
    ):
        raise BindError(
            409,
            "BIND_CONCURRENT_MODIFICATION",
            "This agent's repository binding changed while the operation was "
            "running, so it was abandoned before anything was written. "
            "Re-check the agent's Git tab and retry if you still want to move it.",
        )

    def _compensate() -> None:
        """Restore the captured previous values — NOT a delete.

        ent#93's create-path loser deletes its row, which is sound only
        because the row was INSERTed microseconds earlier and the whole agent
        creation aborts with it. Here the row pre-exists a live agent, and
        deleting it would strip the binding entirely: the next recreate would
        find no row, drop `GITHUB_REPO`, and bring the agent back with no repo
        at all (#843/#1439).
        """
        try:
            db.restore_git_config_binding(
                agent_name,
                github_repo=previous_repo,
                source_branch=previous_source_branch,
                auto_sync_enabled=previous_auto_sync,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(
                "repo-bind: FAILED to restore %s's binding to %s after losing "
                "the destination race — the row now points at %s with no "
                "container backing it: %s",
                agent_name,
                previous_repo,
                destination_repo,
                e,
            )

    # Belt, not the mechanism (the destination-scoped lock is). One query, and
    # it catches a lock-layer failure that let a second binder through.
    try:
        _refuse_if_destination_bound_elsewhere(agent_name, destination_repo)
    except BindError:
        _compensate()
        raise

    # --- 4. In-container: push the workspace, repoint origin. ----------------
    # `previous_repo` is the row's value, which on a resumption already IS the
    # destination — passing it through would point `upstream` at the destination
    # itself and erase the record of the real upstream (the public template the
    # agent was cloned from). None means "leave upstream alone"; the first
    # attempt already set it if it got that far.
    rebind = await git_service.rebind_origin_and_push(
        agent_name=agent_name,
        destination_repo=destination_repo,
        user_pat=user_pat,
        previous_repo=None if resuming else previous_repo,
        branch=branch,
    )
    if not rebind.success:
        raise BindError(
            502,
            "BIND_PUSH_FAILED" if rebind.stage == "push" else "BIND_REWIRE_FAILED",
            f"{_scrub(rebind.error or '', user_pat)} Trinity has recorded the new repository "
            f"('{destination_repo}'), but the agent's origin, credential and "
            f"container environment are unchanged. Retrying this action is "
            f"safe — the repository is reused and the push is idempotent.",
            partial=True,
            context={"stage": rebind.stage},
        )

    # --- 5. Persist the PAT — LAST, now the container genuinely owns the repo.
    # Also strictly BEFORE the recreate: the config-drift recreate resolves the
    # PAT with `pat_gate="per_agent_only"`, so without this row first it would
    # bake a repo-bound container with NO token and startup.sh would blackhole
    # its push remote.
    try:
        pat_saved = db.set_agent_github_pat(agent_name, user_pat)
    except Exception as e:  # noqa: BLE001
        # Scrubbed like every other foreign-text path: the encryption layer is
        # handed the raw token, so an exception from it is the one place a
        # persist failure could echo the value into the platform log.
        logger.error(
            "repo-bind: PAT persist raised for %s: %s",
            agent_name, _scrub(str(e), user_pat),
        )
        pat_saved = False
    if not pat_saved:
        raise BindError(
            502,
            "BIND_PAT_PERSIST_FAILED",
            f"The agent's history was pushed to '{destination_repo}' and its "
            f"origin now points there, but storing the GitHub token failed, so "
            f"the container was NOT rebuilt. Retry this action to finish — it "
            f"is safe to repeat.",
            partial=True,
        )

    # --- 6. Re-bake the container env. NOT optional: startup.sh rewrites
    #        origin unconditionally from the baked GITHUB_REPO, so without this
    #        the rebind is reverted by the next plain restart (FR-5).
    recreated = False
    try:
        # Both circuit breakers are keyed by agent NAME with no TTL, so the
        # replacement container inherits its predecessor's verdict and can come
        # up fast-failed without ever being contacted (#1560). `start_agent_internal`
        # clears them immediately before its own recreate call (lifecycle.py) —
        # this is the second production call site of that helper and owes the
        # same. Slots are deliberately NOT cleared: `force_clear_slots` would
        # drop capacity accounting for an in-flight execution.
        clear_agent_breakers(agent_name)
        await recreate_container_with_updated_config(
            agent_name, container, owner_username
        )
        recreated = True
    except Exception as e:  # noqa: BLE001 — reported, never swallowed
        logger.error("repo-bind: recreate failed for %s: %s", agent_name, e)
        raise BindError(
            502,
            "BIND_RECREATE_FAILED",
            f"'{destination_repo}' now holds the agent's history and Trinity "
            f"has recorded the binding, but rebuilding the container to pick up "
            f"the new repository failed: {_scrub(str(e), user_pat)}. Retry this "
            f"action to finish — it "
            f"is idempotent. Do NOT rely on a plain container restart: that "
            f"re-runs the startup script, which rewrites origin from the "
            f"container's stale environment and would undo the rebind.",
            partial=True,
        )

    logger.info(
        "repo-bind: %s bound to %s (was %s, branch %s, private=%s, created=%s)",
        agent_name,
        destination_repo,
        previous_repo,
        branch,
        private,
        created_repo,
    )
    return BindOutcome(
        agent_name=agent_name,
        github_repo=destination_repo,
        previous_repo=previous_repo,
        default_branch=branch,
        private=private,
        created_repo=created_repo,
        reused_existing=not created_repo,
        recreated=recreated,
        audit={
            "github_repo": destination_repo,
            "previous_repo": previous_repo,
            "branch": branch,
            "private": private,
            "created_repo": created_repo,
            "reused_existing": not created_repo,
            "recreated": recreated,
        },
    )
