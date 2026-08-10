"""Execution environment assembly for spawned runtime subprocesses (#1999).

Before this module, `.env` and the environment executions actually received
were two independent channels. The credential endpoints mirrored every
submitted key into the **long-lived agent-server process's** `os.environ`, and
every spawn passed `env={**os.environ, ...}` — so the file an operator inspects
and the values a subprocess inherits synced only inside one code path, in one
direction, with no delete phase.

Consequence (the reported bug): a key removed from `.env` by any non-mirroring
write path — SSH, `docker exec`, or an agent editing its own `.env` — kept
reaching every subsequently-spawned execution until the container restarted.
Invisible to `/proc/<pid>/environ` (an exec-time snapshot; runtime `os.environ`
mutations never appear there) and to `docker exec` (which gets the container
baseline, not this process's mutated env). **Credential revocation silently
failed**, and both standard inspection routes agreed with the file while
disagreeing with reality.

The fix makes the execution environment a pure function of three inspectable
inputs, rebuilt per spawn rather than accumulated in process state:

    INITIAL_ENV          the container baseline captured at import — what
                         `docker exec` shows. Never mutated.
    .env                 parsed fresh at every spawn; authoritative for
                         credentials, so a deleted key is deleted.
    RUNTIME_OVERRIDES    a small named dict for values that are deliberately
                         NOT `.env` credentials — today only the #1089
                         subscription-token rotation.

`RUNTIME_OVERRIDES` is applied AFTER the file, deliberately diverging from the
issue's sketched ordering. `/api/credentials/reload-token` rotates the
subscription token *without* writing `.env` (by design — "the subscription
token is not a .env credential"), so if a stale `CLAUDE_CODE_OAUTH_TOKEN` ever
appears in `.env` — an operator adding it by hand, a credential export that
captured it — file-last would silently serve the pre-rotation token and
re-open #1089 from the other side. An explicit hot-reload is the most recent
and most specific signal available; it wins. A `None` value means *force-unset*
(the `remove_api_key` case), which a plain dict merge cannot express.

No intra-package imports: this module is loadable standalone, so a test can
exercise it without importing the whole agent-server package.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, Mapping, Optional

logger = logging.getLogger(__name__)

AGENT_HOME = Path("/home/developer")
ENV_FILE = AGENT_HOME / ".env"

# The container baseline: Docker `Config.Env` plus whatever `startup.sh`
# exported, captured at import — i.e. BEFORE any credential push can mirror
# into `os.environ`. This is exactly what `docker exec` shows an operator, and
# it is the reason the ghost class cannot recur: a key that is neither in the
# baseline nor in `.env` has no path into a spawned process.
INITIAL_ENV: Dict[str, str] = dict(os.environ)

# Env names `.env` may not set. `.env` is agent-writable (an execution can edit
# its own file), and this module now reads it at every spawn — a wider trigger
# than the old backend-only mirror. These names do not carry credentials; they
# redirect what the child process *executes* or *loads*, so a prompt-injected
# workspace could otherwise repoint the runtime binary or preload a shared
# object. Narrowing from the old mirror's "any key" is intentional.
PROTECTED_KEYS = frozenset({
    "PATH", "HOME", "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT",
    "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "BASH_ENV", "ENV", "IFS",
    # Node equivalents (#2010 review): every runtime here is a Node process, and
    # `NODE_OPTIONS=--require /tmp/x.js` is LD_PRELOAD for Node. NODE_REPL_*
    # and the loader flags are the same mechanism under other names.
    "NODE_OPTIONS", "NODE_PATH", "NODE_REPL_EXTERNAL_MODULE",
    # git executes these as commands, so a .env key would be arbitrary
    # execution on the next fetch/push the agent runs.
    "GIT_SSH", "GIT_SSH_COMMAND", "GIT_EXTERNAL_DIFF", "GIT_PAGER",
    "GIT_EDITOR", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_COUNT",
})

# NOTE (#1999): the parsing below is deliberately BYTE-FAITHFUL to the export
# loop this replaces. That loop's exact quirks are a published contract — the
# ent#127 "is this credential set" predicate
# (`services/credential_requirements_service._env_pairs`) is *defined* as
# agreement with it, and its source is spliced into an in-container probe. This
# issue is about the LIFECYCLE (a removed key must stop applying), not about
# parsing; changing both at once would silently move that predicate under a
# security fix. Improving the parse — one matched quote pair instead of
# peeling every layer, unescaping what the writer escaped, honouring
# `export ` — is a real improvement and a separate change, with ent#127's
# parity test as its gate.

# `.env` is written by the platform but editable in-container; cap the read so
# a runaway file cannot be loaded into memory at every single spawn.
_ENV_FILE_MAX_BYTES = 1024 * 1024

# Values that are deliberately not `.env` credentials. `None` = force-unset.
_RUNTIME_OVERRIDES: Dict[str, Optional[str]] = {}

# Keys this process has mirrored into `os.environ` from `.env`. Tracked so the
# mirror can *remove* what a newly-written `.env` no longer contains without
# ever touching a key that came from the container baseline.
_MIRRORED_KEYS: set = set()


# ---------------------------------------------------------------------------
# .env parsing
# ---------------------------------------------------------------------------

def parse_env_file(path: Path = ENV_FILE) -> Dict[str, str]:
    """Parse a `.env` file into a dict. Never raises.

    Matches the writer in `routers/credentials.py`, which emits
    `KEY="value"` with embedded `"` backslash-escaped. Tolerant of the shapes a
    human or an agent produces: `export KEY=value`, single quotes, no quotes,
    comments, blank lines, CRLF. A malformed line is skipped, not fatal — this
    runs on the spawn path, where refusing to parse would mean refusing to run.
    """
    try:
        if not path.is_file():
            return {}
        size = path.stat().st_size
        if size > _ENV_FILE_MAX_BYTES:
            logger.warning(
                "%s is %d bytes (> %d cap); ignoring it for execution env",
                path, size, _ENV_FILE_MAX_BYTES,
            )
            return {}
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning("could not read %s for execution env: %s", path, e)
        return {}

    parsed: Dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        parsed[key] = value.strip().strip('"').strip("'")
    return parsed


# ---------------------------------------------------------------------------
# Runtime overrides (#1089)
# ---------------------------------------------------------------------------

def set_runtime_override(key: str, value: Optional[str]) -> None:
    """Record a non-`.env` value for spawned processes. `None` = force-unset."""
    _RUNTIME_OVERRIDES[key] = value


def clear_runtime_override(key: str) -> None:
    _RUNTIME_OVERRIDES.pop(key, None)


def runtime_overrides() -> Mapping[str, Optional[str]]:
    """Read-only view — for diagnostics and tests, not for mutation."""
    return dict(_RUNTIME_OVERRIDES)


# ---------------------------------------------------------------------------
# The spawn-path entry point
# ---------------------------------------------------------------------------

def build_execution_env(
    extra: Optional[Mapping[str, str]] = None,
    env_file: Path = ENV_FILE,
) -> Dict[str, str]:
    """Assemble the environment for one spawned runtime subprocess.

    Precedence, lowest to highest: container baseline → `.env` → runtime
    overrides → `extra`. `extra` is last so `EXECUTION_TAG_NAME` (#407 orphan
    sweep) can never be displaced by a `.env` key of the same name.
    """
    env: Dict[str, str] = dict(INITIAL_ENV)

    for key, value in parse_env_file(env_file).items():
        if key in PROTECTED_KEYS:
            logger.warning(
                "ignoring %s from .env: it controls what the runtime executes "
                "or loads and is not settable from credentials", key,
            )
            continue
        env[key] = value

    for key, value in _RUNTIME_OVERRIDES.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value

    if extra:
        env.update(extra)
    return env


# ---------------------------------------------------------------------------
# Process-env mirror (for in-process readers, not for spawning)
# ---------------------------------------------------------------------------

def sync_process_env(env_file: Path = ENV_FILE) -> Dict[str, int]:
    """Re-point this process's `os.environ` at the current `.env`.

    Spawned executions no longer depend on this — `build_execution_env` reads
    the file directly. It still runs because platform code inside the agent
    server reads credentials from `os.environ` (the error classifier's
    `ANTHROPIC_API_KEY`/`CLAUDE_CODE_OAUTH_TOKEN` probes, `AGENT_RUNTIME`,
    `GOOGLE_API_KEY`) and because the credential sanitizer builds its redaction
    set from it.

    Unlike the loop it replaces, this has a **delete phase**: a key this
    process previously mirrored that the newly-written `.env` no longer
    contains is removed — restored to its container-baseline value when it has
    one, popped when it does not. Only keys we ourselves mirrored are eligible,
    so a baseline-only key is never deleted.
    """
    file_env = {
        k: v for k, v in parse_env_file(env_file).items()
        if k not in PROTECTED_KEYS
    }

    stale = sorted(_MIRRORED_KEYS - set(file_env))
    for key in stale:
        baseline = INITIAL_ENV.get(key)
        if baseline is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = baseline
    _MIRRORED_KEYS.difference_update(stale)
    removed = len(stale)

    for key, value in file_env.items():
        os.environ[key] = value
        _MIRRORED_KEYS.add(key)

    if removed:
        # Names only — never values (#1999 is a credential-handling path).
        logger.info(
            "credential sync: %d key(s) present in .env, %d stale key(s) "
            "cleared from the process env", len(file_env), removed,
        )
    return {"applied": len(file_env), "removed": removed}


def env_drift_report(env_file: Path = ENV_FILE) -> list:
    """Per-key drift between `.env` and this process's env — NAMES ONLY.

    The reported bug took hours to root-cause because every inspection route
    (`/proc/<pid>/environ`, `docker exec`) agreed with the file and disagreed
    with reality. This is the missing route: one call that shows where the two
    channels differ. Values are never included — only whether each key is
    present on each side and whether the two agree.
    """
    file_env = parse_env_file(env_file)
    report = []
    for key in sorted(set(file_env) | set(_MIRRORED_KEYS)):
        in_file = key in file_env
        in_proc = key in os.environ
        report.append({
            "key": key,
            "in_file": in_file,
            "in_process_env": in_proc,
            "equal": bool(in_file and in_proc and file_env[key] == os.environ[key]),
        })
    return report
