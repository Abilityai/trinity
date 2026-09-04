"""The `.trinity/` materializers — persistent-state (#383), data-paths (#1169), plugins (#1704) — and their injection-safe YAML writers.

Carved out of the 2,322-line `services/git_service.py` (#1028). The package
`__init__` re-exports the public surface, so `from services.git_service
import …` and `git_service.<name>` callers are unchanged.

Cross-module calls go THROUGH the sibling module object
(`gitignore._detect_git_dir(...)`, never `from .gitignore import
_detect_git_dir`) so a test that patches the owning module reaches every
caller — a from-import freezes the binding and quietly detaches such a
patch.
"""
import asyncio
import os
import re
import shlex
import uuid
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any, List, Tuple
from sqlalchemy.exc import IntegrityError
from database import db, AgentGitConfig, GitSyncResult
from services.agent_auth import agent_httpx_client
from services.docker_service import get_agent_container, execute_command_in_container
from utils.credential_sanitizer import scrub_secret_and_urls
from utils.safe_yaml import (  # ent#314
    AliasPolicy as _AliasPolicy,
    HardenedYamlError as _HardenedYamlError,
    load_hardened_yaml as _load_hardened_yaml,
)

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Conflict classification (S5 — operator-readable diagnosis, issue #386)
# ----------------------------------------------------------------------------


from . import gitignore

logger = logging.getLogger(__name__)

DEFAULT_PERSISTENT_STATE: list[str] = [
    "workspace/**",
    ".trinity/**",
    ".mcp.json",
    ".claude.json",
    ".claude/.credentials.json",
]

_PERSISTENT_STATE_PATH = "/home/developer/.trinity/persistent-state.yaml"

DEFAULT_DATA_PATHS: list[str] = []

_DATA_PATHS_PATH = "/home/developer/.trinity/data-paths.yaml"

_DATA_ROOT_GITIGNORE = "data/"

_PLUGINS_PATH = "/home/developer/.trinity/plugins.yaml"

_SAFE_DATA_PATH_RE = re.compile(r"[A-Za-z0-9_./*?{},\[\] -]+")

async def _write_trinity_yaml_file(
    agent_name: str,
    *,
    path: str,
    body: str,
    heredoc: str,
    timeout: int = 10,
) -> None:
    """Write a pre-serialized YAML `body` to `path` inside the agent container.

    THE single container-write mechanism for every `.trinity/*.yaml` the backend
    materializes (#953: never hand-roll a raw container write). The single-quoted
    heredoc preserves the body verbatim and is injection-safe (no shell expansion
    inside `<<'HEREDOC'`); callers keep the body's values free of the outer
    `bash -c "..."` metacharacters (`$`, backtick, `"`, `\\`) — which the reused
    `_SAFE_DATA_PATH_RE` / `template_plugins` charset validators guarantee.
    """
    cmd = (
        f"mkdir -p /home/developer/.trinity && "
        f"cat > {path} <<'{heredoc}'\n{body}{heredoc}"
    )
    await execute_command_in_container(
        container_name=f"agent-{agent_name}",
        command=f'bash -c "{cmd}"',
        timeout=timeout,
    )


async def materialize_trinity_yaml_list(
    agent_name: str,
    *,
    path: str,
    key: str,
    patterns: list[str],
    heredoc: str,
    timeout: int = 10,
) -> None:
    """Write `{key: [patterns]}` as YAML to `path` inside the agent container.

    The single-quoted heredoc preserves glob characters verbatim and is
    injection-safe (no shell expansion inside the body).
    """
    import yaml as _yaml

    body = _yaml.safe_dump({key: list(patterns)}, sort_keys=False)
    await _write_trinity_yaml_file(
        agent_name, path=path, body=body, heredoc=heredoc, timeout=timeout
    )


async def _read_trinity_yaml_list(
    agent_name: str,
    *,
    path: str,
    key: str,
    default: list[str],
    timeout: int = 5,
) -> list[str]:
    """Read a `{key: [...]}` list from `path`, falling back to `default`.

    Returns the on-disk list when present and valid; otherwise returns a
    fresh copy of `default` (never a reference — callers may mutate the
    result, and must not mutate a shared default constant).
    """
    import yaml as _yaml
    result = await execute_command_in_container(
        container_name=f"agent-{agent_name}",
        command=f'bash -c "cat {path} 2>/dev/null || true"',
        timeout=timeout,
    )
    if result.get("exit_code", 0) != 0:
        return list(default)
    raw = result.get("output", "").strip()
    if not raw:
        return list(default)
    try:
        # ent#314: agent-written file read out of the container.
        data = _load_hardened_yaml(
            raw, kind="agent_yaml", alias_policy=_AliasPolicy.REJECT
        ) or {}
    except (_yaml.YAMLError, _HardenedYamlError):
        return list(default)
    patterns = data.get(key)
    if not isinstance(patterns, list) or not patterns:
        return list(default)
    return [str(p) for p in patterns]


async def materialize_persistent_state(
    agent_name: str, patterns: list[str]
) -> None:
    """Write `.trinity/persistent-state.yaml` inside the agent container.

    Called once from `agent_service.crud` after the container is running.
    Operators may edit the file thereafter; runtime readers treat the
    on-disk copy as authoritative.
    """
    await materialize_trinity_yaml_list(
        agent_name,
        path=_PERSISTENT_STATE_PATH,
        key="persistent_state",
        patterns=patterns,
        heredoc="PSTATE_EOF",
    )


async def _persistent_state_for(agent_name: str) -> list[str]:
    """Read the persistent-state allowlist for an agent.

    Returns the on-disk list when `.trinity/persistent-state.yaml` is
    present and valid; otherwise returns a fresh copy of
    `DEFAULT_PERSISTENT_STATE`. Consumers of this helper (e.g. the future
    reset-preserve-state operation from #384) must not mutate the default
    constant, hence the defensive `list(...)` copies on every fallback.
    """
    return await _read_trinity_yaml_list(
        agent_name,
        path=_PERSISTENT_STATE_PATH,
        key="persistent_state",
        default=DEFAULT_PERSISTENT_STATE,
    )


def _is_safe_data_path(path: str) -> bool:
    """True if `path` is a shell-safe glob over the data root (#1169 L1)."""
    return bool(path) and _SAFE_DATA_PATH_RE.fullmatch(path) is not None


async def materialize_data_paths(agent_name: str, paths: list[str]) -> None:
    """Materialize an agent's declared `data_paths` (#1169).

    Writes `.trinity/data-paths.yaml` (key `data_paths`) AND appends the
    runtime-data root + each declared path to the agent's own `.gitignore`,
    so the declaration and its ignore rule are materialized together.

    Opt-in: a falsy/whitespace-only declaration is a complete no-op — no
    file is written and no `.gitignore` is touched — so undeclared agents
    are entirely unaffected. Entries carrying shell metacharacters are
    dropped (logged) before any container write (#1169 L1).
    """
    cleaned = [str(p).strip() for p in (paths or []) if str(p).strip()]
    safe: list[str] = []
    dropped: list[str] = []
    for p in cleaned:
        (safe if _is_safe_data_path(p) else dropped).append(p)
    if dropped:
        logger.warning(
            "[#1169] Dropping %d unsafe data_paths entr%s for %s "
            "(shell metacharacters): %r",
            len(dropped),
            "y" if len(dropped) == 1 else "ies",
            agent_name,
            dropped,
        )
    if not safe:
        return
    await materialize_trinity_yaml_list(
        agent_name,
        path=_DATA_PATHS_PATH,
        key="data_paths",
        patterns=safe,
        heredoc="DATAPATHS_EOF",
    )
    await _append_agent_gitignore(
        agent_name, _data_paths_gitignore_entries(safe)
    )


async def _data_paths_for(agent_name: str) -> list[str]:
    """Read an agent's declared `data_paths` (#1169).

    Returns the on-disk list when `.trinity/data-paths.yaml` is present and
    valid; otherwise a fresh empty list (data_paths is opt-in).
    """
    return await _read_trinity_yaml_list(
        agent_name,
        path=_DATA_PATHS_PATH,
        key="data_paths",
        default=DEFAULT_DATA_PATHS,
    )


async def materialize_plugins(agent_name: str, plugins: dict) -> None:
    """Materialize an agent's declared Claude Code plugins (#1704).

    Writes `.trinity/plugins.yaml` as nested
    `{plugins: {marketplaces: [{name, source}], installed: ["plugin@mkt"]}}` via
    the shared injection-safe heredoc writer. The file is COMMITTED (it is in
    `_TRINITY_AUTHORED_PATHS`), so the declaration survives a git-based
    reconstitution onto a fresh volume or a new host, where the gitignored
    `~/.claude.json` + `~/.claude/plugins/` cache are dropped.

    `plugins` is the ALREADY-normalized dict from
    `template_plugins.normalize_declared_plugins` — every value is
    charset-validated, so the nested body is safe inside the outer `bash -c`
    double quotes (see `_write_trinity_yaml_file`).

    Opt-in: a falsy declaration (`{}` / None / no marketplaces + no installed)
    is a complete no-op — no file is written.

    Determinism (a correctness property): `sort_keys=True` + the normalizer's
    sorted, de-duplicated lists mean a stable plugin set produces a byte-
    identical file, so the 15-min auto-sync loop never re-commits a churning
    manifest — unlike the flat `materialize_trinity_yaml_list` (`sort_keys=False`).
    """
    import yaml as _yaml

    if not isinstance(plugins, dict):
        return
    marketplaces = plugins.get("marketplaces") or []
    installed = plugins.get("installed") or []
    if not marketplaces and not installed:
        return

    body = _yaml.safe_dump(
        {"plugins": {"marketplaces": marketplaces, "installed": installed}},
        sort_keys=True,
        default_flow_style=False,
    )
    await _write_trinity_yaml_file(
        agent_name, path=_PLUGINS_PATH, body=body, heredoc="PLUGINS_EOF"
    )


def _data_paths_gitignore_entries(paths: list[str]) -> list[str]:
    """Gitignore lines for declared data_paths: the `data/` root plus each
    declared path, deduped and order-preserving.
    """
    entries = [_DATA_ROOT_GITIGNORE]
    for p in paths:
        if p not in entries:
            entries.append(p)
    return entries


async def _append_agent_gitignore(agent_name: str, patterns: list[str]) -> None:
    """Append `patterns` to the agent's own `/home/developer/.gitignore` (#1169).

    Idempotent (exact-line `grep -qxF` gate). Best-effort and scoped to the
    agent's repo root — never the fleet-wide `_GITIGNORE_PATTERNS`.
    """
    if not patterns:
        return
    await execute_command_in_container(
        container_name=f"agent-{agent_name}",
        command=gitignore._build_gitignore_append_command("/home/developer", patterns),
        timeout=10,
    )


