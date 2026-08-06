"""
GitHub import intents (trinity-enterprise#15) — backend unit tests.

Covers the three seams the feature added:

1. ``services/agent_service/snapshot_import.py::stage_github_snapshot`` — the
   backend-materialized "copy" snapshot: clone → head SHA capture → ``.git``
   strip → escaping-symlink prune → empty-tree refusal, with structured 4xx/5xx
   errors and staging-dir cleanup on every failure path. ``_run_git`` is faked
   BY OBJECT (the module-global the code calls), and the fake plays the clone
   by populating the staging dir it receives as the last clone argument.

2. ``services/agent_service/crud.py::_resolve_template`` intake gates — the
   pre-side-effect ``import_intent`` 400s (INTENT_REQUIRES_GITHUB_TEMPLATE /
   FORK_PARAMS_REQUIRED / INTENT_FORK_BLOCK_CONFLICT), the intent-less local
   path staying gate-free, and the copy-branch wiring (snapshot staged, NO
   github repo / git-config reservation, and the ent#123 tokenless
   source-mode gate deliberately NOT firing for copy).

3. ``routers/agents.py::create_agent_endpoint`` idempotency — the scope folds
   the CALLER (2026-07-20 learning: two principals with the same key must
   never share a replay), replay returns the stored snapshot with
   ``X-Idempotent-Replay`` and no re-create, in-flight returns the named
   CREATE_IN_FLIGHT 409.

Harness discipline (learnings 2026-07-12 / 2026-07-06): real modules are
captured at module scope (collection time is leak-free) and re-owned per test
via an autouse ``monkeypatch.setitem`` fixture; every patch is by OBJECT on a
captured real module, never by string target.
"""
import importlib
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# ---------------------------------------------------------------------------
# #762/#1446 leak defense: sibling test modules install session-persistent
# sys.modules stubs (module-level `setdefault`, never restored) for
# `database` / `services.*` / `dependencies`. Capture the REAL module objects
# at collection time and re-own them per test via `monkeypatch.setitem`
# (auto-restored, last-write-wins). All patches below go through
# `_REAL_MODULES[...]` objects — never string targets — so a leaked stale
# sys.modules entry can never make a patch land on the wrong module object.
# ---------------------------------------------------------------------------
_OWNED_MODULE_NAMES = (
    "database",
    "dependencies",
    "models",
    "db_models",
    "utils.helpers",
    "utils.safe_yaml",
    "services",
    "services.idempotency_service",
    "services.platform_audit_service",
    "services.settings_service",
    "services.template_service",
    "services.docker_service",
    "services.agent_service",
    "services.agent_service.fork_to_own",
    "services.agent_service.snapshot_import",
    "services.agent_service.crud",
    "routers",
    "routers.agents",
)
_REAL_MODULES = {
    name: importlib.import_module(name) for name in _OWNED_MODULE_NAMES
}

_models = _REAL_MODULES["models"]
_crud = _REAL_MODULES["services.agent_service.crud"]
_snapshot_import = _REAL_MODULES["services.agent_service.snapshot_import"]
_idem = _REAL_MODULES["services.idempotency_service"]
_agents_router = _REAL_MODULES["routers.agents"]


@pytest.fixture(autouse=True)
def _own_real_modules(monkeypatch):
    for name, mod in _REAL_MODULES.items():
        monkeypatch.setitem(sys.modules, name, mod)


def _user(uid=1, username="creator", role="creator"):
    return _models.User(id=uid, username=username, role=role, agent_name=None)


def _fork_block():
    return _models.ForkToOwnRequest(
        destination_repo="me/dest", github_pat="ghp_test123"
    )


# ===========================================================================
# 1. snapshot_import.stage_github_snapshot
# ===========================================================================


@pytest.fixture
def staging_root(monkeypatch, tmp_path):
    """Point _STAGING_ROOT at a tmp dir so tests never touch /data."""
    root = tmp_path / "staging-root"
    monkeypatch.setattr(_snapshot_import, "_STAGING_ROOT", root)
    return root


def _clone_fake(populate, sha="abc123deadbeef\n", default_branch="main\n"):
    """Async _run_git double. `populate(staging_dir)` plays the clone; the
    rev-parse calls return canned output. Returns (fake, calls) where calls
    records (args, auth_pat) per invocation."""
    calls = []

    async def fake(args, timeout=None, auth_pat=""):
        args = list(args)
        calls.append((args, auth_pat))
        if args[0] == "clone":
            # The REAL _run_git receives the staging dir as the last clone
            # arg (mkdtemp already created it) — simulate the clone into it.
            result = populate(Path(args[-1]))
            if result is not None:
                return result
            return (0, "")
        if "--abbrev-ref" in args:
            return (0, default_branch)
        if "rev-parse" in args:
            return (0, sha)
        raise AssertionError(f"unexpected git call: {args}")

    return fake, calls


def _populate_full_tree(staging: Path):
    """A realistic clone result: .git dir with a file, two regular files,
    one in-tree symlink (dir target) and one symlink escaping to /etc/passwd."""
    git_dir = staging / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / "config").write_text("[core]\n")
    (staging / "README.md").write_text("hello")
    sub = staging / "sub"
    sub.mkdir()
    (sub / "notes.txt").write_text("n")
    # In-tree symlink: relative target inside the staged tree. Deliberately a
    # DIRECTORY target so `file_count` isolates regular files — os.walk puts a
    # symlink-to-file in `filenames`, so a file-target link would be counted
    # (see the count note in the final report).
    (staging / "link-in").symlink_to("sub")
    # Escaping symlink: resolves outside the staged tree — must be pruned.
    (staging / "link-out").symlink_to("/etc/passwd")
    return None


@pytest.mark.asyncio
async def test_stage_success_strips_git_and_prunes_escaping_symlinks(
    monkeypatch, staging_root
):
    fake, calls = _clone_fake(_populate_full_tree)
    monkeypatch.setattr(_snapshot_import, "_run_git", fake)

    staging = await _snapshot_import.stage_github_snapshot(
        "o/r", None, "ghp_secret123"
    )

    assert staging.head_sha == "abc123deadbeef"
    assert staging.source_repo == "o/r"
    # branch=None → resolved from `rev-parse --abbrev-ref`
    assert staging.source_branch == "main"
    # Only the 2 regular files count: .git stripped, escaping symlink pruned,
    # the surviving in-tree (dir) symlink is not a regular file.
    assert staging.file_count == 2

    staged = Path(staging.staging_dir)
    assert staged.is_dir()
    assert staged.parent == staging_root  # under the patched root, not /data
    assert not (staged / ".git").exists()          # .git stripped
    assert not (staged / "link-out").is_symlink()  # escaping symlink GONE
    assert not (staged / "link-out").exists()
    assert (staged / "link-in").is_symlink()       # in-tree symlink SURVIVES
    assert (staged / "README.md").is_file()
    assert (staged / "sub" / "notes.txt").is_file()

    # Clone call: no --branch when branch=None; PAT travels ONLY on the clone.
    clone_args, clone_pat = calls[0]
    assert clone_args[0] == "clone"
    assert "--branch" not in clone_args
    assert "https://github.com/o/r.git" in clone_args
    assert clone_pat == "ghp_secret123"
    for args, pat in calls[1:]:
        assert "rev-parse" in args
        assert pat == ""  # local rev-parse never re-carries the PAT

    # cleanup_staging is idempotent (success path AND rollback both call it).
    _snapshot_import.cleanup_staging(staging.staging_dir)
    _snapshot_import.cleanup_staging(staging.staging_dir)
    assert not staged.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stderr",
    [
        "fatal: Authentication failed for 'https://github.com/o/r.git/'",
        "remote: Repository not found.\nfatal: repository "
        "'https://github.com/o/r.git/' not found",
        "fatal: Remote branch dev not found in upstream origin",
    ],
)
async def test_stage_unreadable_source_is_400_and_cleans_up(
    monkeypatch, staging_root, stderr
):
    async def fake(args, timeout=None, auth_pat=""):
        assert args[0] == "clone"
        return (128, stderr)

    monkeypatch.setattr(_snapshot_import, "_run_git", fake)

    with pytest.raises(_snapshot_import.HTTPException) as exc:
        await _snapshot_import.stage_github_snapshot("o/r", None, None)

    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "COPY_SOURCE_UNREADABLE"
    # Staging dir was reaped on the failure path.
    assert list(staging_root.iterdir()) == []


@pytest.mark.asyncio
async def test_stage_transient_clone_failure_is_502(monkeypatch, staging_root):
    async def fake(args, timeout=None, auth_pat=""):
        return (
            128,
            "fatal: unable to access 'https://github.com/o/r.git/': "
            "Could not resolve host: github.com",
        )

    monkeypatch.setattr(_snapshot_import, "_run_git", fake)

    with pytest.raises(_snapshot_import.HTTPException) as exc:
        await _snapshot_import.stage_github_snapshot("o/r", None, None)

    assert exc.value.status_code == 502
    assert exc.value.detail["code"] == "COPY_CLONE_FAILED"
    assert list(staging_root.iterdir()) == []


@pytest.mark.asyncio
async def test_stage_empty_repo_is_400_source_empty(monkeypatch, staging_root):
    def populate(staging: Path):
        # A depth-1 clone of an empty repo: exit 0, only .git/ inside.
        git_dir = staging / ".git"
        git_dir.mkdir(parents=True, exist_ok=True)
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
        return None

    fake, _calls = _clone_fake(populate)
    monkeypatch.setattr(_snapshot_import, "_run_git", fake)

    with pytest.raises(_snapshot_import.HTTPException) as exc:
        await _snapshot_import.stage_github_snapshot("o/r", None, None)

    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "COPY_SOURCE_EMPTY"
    assert list(staging_root.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", [None, "dev"])
async def test_stage_clone_branch_flag(monkeypatch, staging_root, branch):
    fake, calls = _clone_fake(_populate_full_tree)
    monkeypatch.setattr(_snapshot_import, "_run_git", fake)

    staging = await _snapshot_import.stage_github_snapshot("o/r", branch, None)

    clone_args, _pat = calls[0]
    assert clone_args[0] == "clone"
    if branch:
        i = clone_args.index("--branch")
        assert clone_args[i + 1] == branch  # contiguous ["--branch", "dev"]
        assert staging.source_branch == branch
    else:
        assert "--branch" not in clone_args
        assert staging.source_branch == "main"  # from rev-parse --abbrev-ref


# ===========================================================================
# 2. crud._resolve_template intake gates (+ copy-branch wiring)
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "template,intent",
    [
        ("local:default", "fork"),
        ("local:default", "copy"),
        ("local:default", "clone"),
        (None, "copy"),  # no template at all is equally intent-less
    ],
)
async def test_intent_requires_github_template(template, intent):
    config = _models.AgentConfig(
        name="gate-a", template=template, import_intent=intent
    )
    with pytest.raises(_crud.HTTPException) as exc:
        await _crud._resolve_template(config, _user())
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "INTENT_REQUIRES_GITHUB_TEMPLATE"


@pytest.mark.asyncio
async def test_fork_intent_without_fork_block_is_400():
    config = _models.AgentConfig(
        name="gate-b", template="github:o/r", import_intent="fork"
    )
    with pytest.raises(_crud.HTTPException) as exc:
        await _crud._resolve_template(config, _user())
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "FORK_PARAMS_REQUIRED"


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["copy", "clone"])
async def test_copy_clone_intent_with_fork_block_is_conflict(intent):
    config = _models.AgentConfig(
        name="gate-c",
        template="github:o/r",
        import_intent=intent,
        fork_to_own=_fork_block(),
    )
    with pytest.raises(_crud.HTTPException) as exc:
        await _crud._resolve_template(config, _user())
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "INTENT_FORK_BLOCK_CONFLICT"


@pytest.mark.asyncio
async def test_copy_intent_with_ephemeral_is_400():
    """Review F2: ghosts are volume-less (ent#69) and the snapshot lives on the
    workspace volume — the combination would boot a green blank ghost and
    strand the populated volume, so it is refused by name."""
    config = _models.AgentConfig(
        name="gate-e",
        template="github:o/r",
        import_intent="copy",
        ephemeral=_models.EphemeralConfig(max_executions=1),
    )
    with pytest.raises(_crud.HTTPException) as exc:
        await _crud._resolve_template(config, _user())
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "COPY_EPHEMERAL_UNSUPPORTED"


@pytest.mark.asyncio
async def test_no_intent_local_template_resolves_without_intent_gates():
    """import_intent=None + local template: no intent gate fires. #1759 ships
    config/agent-templates/default/, so `local:default` resolves via the real
    local-template loader and the whole phase succeeds."""
    config = _models.AgentConfig(
        name="gate-d", template="local:default", import_intent=None
    )
    tr = await _crud._resolve_template(config, _user())
    assert isinstance(tr.template_data, dict) and tr.template_data
    assert tr.template_data.get("name") == "default"
    assert tr.copy_snapshot is None
    assert tr.github_repo_for_agent is None


@pytest.mark.asyncio
@pytest.mark.parametrize("source_mode", [None, False, True])
async def test_copy_branch_wiring_stages_snapshot_without_git_binding(
    monkeypatch, source_mode
):
    """The copy branch stages the snapshot and returns with NO github repo,
    NO git-config reservation — and the ent#123 tokenless source-mode gate
    must NOT fire (source_mode False/None + no PAT would 400 on the non-copy
    path; copy never pushes). Parametrized over source_mode (2026-07-23
    learning: gates read Optional[bool] fields — cover all three states)."""
    staged = _snapshot_import.SnapshotStaging(
        staging_dir="/tmp/x",
        source_repo="o/r",
        source_branch="main",
        head_sha="s",
        file_count=1,
    )
    stage_calls = []

    async def fake_stage(repo, branch, pat):
        stage_calls.append((repo, branch, pat))
        return staged

    monkeypatch.setattr(_crud, "get_github_template", lambda lookup: None)
    monkeypatch.setattr(
        _crud, "resolve_github_pat", lambda owner_id=None: ("", "none")
    )
    monkeypatch.setattr(
        _crud.snapshot_import, "stage_github_snapshot", fake_stage
    )
    monkeypatch.setattr(
        _crud, "_declared_schedules_for_snapshot", lambda snap: []
    )

    config = _models.AgentConfig(
        name="copy-agent",
        template="github:o/r",
        import_intent="copy",
        source_mode=source_mode,
    )
    tr = await _crud._resolve_template(config, _user(uid=7, username="u7"))

    assert tr.copy_snapshot is staged
    assert tr.github_repo_for_agent is None   # container gets no GitHub env
    assert tr.git_instance_id is None         # never reserved for a copy
    assert tr.declared_schedules == []
    # resolve_github_pat returned "" → normalized to None for the stage call;
    # default source_branch "main" means "repo default" → branch None.
    assert stage_calls == [("o/r", None, None)]


# ===========================================================================
# 3. create_agent_endpoint idempotency (scope isolation + replay + in-flight)
# ===========================================================================


@pytest.fixture
def endpoint_env(monkeypatch):
    """Wire the endpoint's collaborators: capture begin/complete/fail on the
    REAL idempotency_service module, fake create_agent_internal on the router
    module (the name the endpoint actually calls — the line-99 facade), and
    silence the audit singleton."""
    env = {
        "begin_calls": [],
        "complete_calls": [],
        "fail_calls": [],
        "begin_result": _idem.IdempotencyDecision(
            enabled=True, replay=False, in_flight=False
        ),
    }

    def fake_begin(scope, key):
        env["begin_calls"].append((scope, key))
        return env["begin_result"]

    monkeypatch.setattr(_idem, "begin", fake_begin)
    monkeypatch.setattr(
        _idem,
        "complete",
        lambda decision, execution_id, snapshot=None: env["complete_calls"].append(
            (decision, execution_id, snapshot)
        ),
    )
    monkeypatch.setattr(
        _idem, "fail", lambda decision: env["fail_calls"].append(decision)
    )

    create_mock = AsyncMock(side_effect=lambda config, *a, **k: {"name": config.name})
    monkeypatch.setattr(_agents_router, "create_agent_internal", create_mock)
    env["create_mock"] = create_mock
    monkeypatch.setattr(
        _agents_router.platform_audit_service, "log", AsyncMock()
    )
    return env


@pytest.mark.asyncio
async def test_create_idempotency_scope_folds_the_caller(endpoint_env):
    """Two principals, same Idempotency-Key: the constructed scopes must
    DIFFER and each must carry its caller's id (2026-07-20 learning — a
    caller-less scope is a cross-user replay/read)."""
    config = _models.AgentConfig(name="idem-a")
    request = MagicMock()

    user1 = _user(uid=1, username="alice")
    user2 = _user(uid=2, username="bob")

    r1 = await _agents_router.create_agent_endpoint(
        config, request, current_user=user1, idempotency_key="k1"
    )
    r2 = await _agents_router.create_agent_endpoint(
        config, request, current_user=user2, idempotency_key="k1"
    )
    assert r1 == {"name": "idem-a"} and r2 == {"name": "idem-a"}

    assert len(endpoint_env["begin_calls"]) == 2
    (scope1, key1), (scope2, key2) = endpoint_env["begin_calls"]
    assert key1 == key2 == "k1"
    assert scope1 != scope2                    # same key, different principals
    assert str(user1.id) in scope1
    assert str(user2.id) in scope2

    # Both fresh claims completed with the agent name + response snapshot.
    assert [
        (execution_id, snapshot)
        for _d, execution_id, snapshot in endpoint_env["complete_calls"]
    ] == [("idem-a", {"name": "idem-a"}), ("idem-a", {"name": "idem-a"})]
    assert endpoint_env["fail_calls"] == []


@pytest.mark.asyncio
async def test_create_idempotency_replay_short_circuits(endpoint_env):
    endpoint_env["begin_result"] = _idem.IdempotencyDecision(
        enabled=True,
        replay=True,
        in_flight=False,
        scope="agent_create:1",
        key="k1",
        snapshot={"name": "prior-agent"},
    )
    config = _models.AgentConfig(name="idem-b")

    resp = await _agents_router.create_agent_endpoint(
        config, MagicMock(), current_user=_user(), idempotency_key="k1"
    )

    assert resp.headers["x-idempotent-replay"] == "true"
    assert json.loads(resp.body) == {"name": "prior-agent"}
    assert endpoint_env["create_mock"].await_count == 0  # no second create
    assert endpoint_env["complete_calls"] == []


@pytest.mark.asyncio
async def test_create_idempotency_in_flight_is_named_409(endpoint_env):
    endpoint_env["begin_result"] = _idem.IdempotencyDecision(
        enabled=True,
        replay=True,
        in_flight=True,
        scope="agent_create:1",
        key="k1",
    )
    config = _models.AgentConfig(name="idem-c")

    with pytest.raises(_agents_router.HTTPException) as exc:
        await _agents_router.create_agent_endpoint(
            config, MagicMock(), current_user=_user(), idempotency_key="k1"
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "CREATE_IN_FLIGHT"
    assert endpoint_env["create_mock"].await_count == 0
    assert endpoint_env["fail_calls"] == []  # raised before the claim's try
