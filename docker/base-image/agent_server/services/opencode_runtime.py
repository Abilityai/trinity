"""OpenCode CLI execution service (#2981).

Implements :class:`AgentRuntime` for `opencode`, the fourth Trinity agent
runtime alongside Claude Code, Gemini and Codex. Built on the same per-runtime
primitives as the Codex path — process registry, `subprocess_lifecycle`'s
concurrency-safe `_drain_bounded`, activity tracking, credential sanitizer —
so a timeout here cannot SIGKILL a sibling execution.

Command shape
-------------
``opencode run --format json --auto [-m provider/model] [--session id]
[--dir path] <prompt>``. ``--auto`` is non-interactive; ``--format json``
emits a JSON-lines event stream.

Event stream (measured against the pinned v1.18.32, #2981)
----------------------------------------------------------
* Every line is ``{"type": ..., "sessionID": ..., "part": {...}}`` — the
  payload is NESTED under ``part`` (``part.text``, ``part.tool`` /
  ``part.callID`` / ``part.state``, ``part.tokens`` / ``part.cost`` /
  ``part.reason``). The parser reads ``part`` first and falls back to the
  same keys at the top level, because the lab fork read only the flat shape
  and that is why its ledger showed cost and model as null (#2981).
* ``step_start`` / ``step_finish`` bracket each LLM step. ``step_finish``
  carries ``tokens: {input, output, reasoning, cache: {read, write}}``, a
  **native** ``cost`` — this runtime is ``cost_reporting="native"``, so there
  is deliberately NO pricing table here (the #2207 class: a hand-maintained
  rate card that silently goes stale) — and a ``reason`` (``stop`` /
  ``tool-calls`` / ``length``).
* ``tool_use`` / ``text`` / ``reasoning`` / ``error`` (the error payload is
  ``error.data.message`` or ``part.error``).
* ``sessionID`` appears on EVERY line, so session capture does not depend on
  any one event type arriving.
* There is **no separate result line**. The final response is the
  concatenation of the ``text`` parts belonging to the LAST step.
* Non-JSON lines are dropped, counted and logged ONCE per turn.

Two subset traps, both of which would silently inflate billing:
``tokens.reasoning`` is a SUBSET of ``tokens.output`` and ``tokens.cache.read``
is a subset of ``tokens.input``. Neither is ever added to anything. Cost is
read, not computed.

Empty final text is a FAILED turn (#520)
----------------------------------------
This is live, not theoretical: an open-weight model on an OpenAI-compatible
endpoint returns its prose in a non-standard ``reasoning``
field with ``content: ""`` **while reporting success**. A runtime that reads
only the text and shrugs at an empty one reports that as a good turn. The
policy, decided here and pinned by tests:

====================================  =========================================
final state                           outcome
====================================  =========================================
any ``error`` event in the stream     **FAIL** ``error`` (500; auth-shaped
                                      message → 503, rate-shaped → 429)
last ``step_finish.reason: length``   **FAIL** ``truncated`` (500)
last ``step_finish.reason:
tool-calls`` with no step after it    **FAIL** ``incomplete`` (500)
text non-empty, zero ``tool_use``,
text carries a tool-call shape        **FAIL** ``text_tool_call`` (500)
text non-empty                        success
text empty, reasoning non-empty       **FAIL** ``reasoning_only`` (500)
text empty, no reasoning, tools ran   success, ``"(Task completed)"``
text empty, nothing at all            **FAIL** ``empty_response`` (500)
====================================  =========================================

The ``error`` row outranks the tools-ran row on purpose: a provider 5xx after
the tools ran is a failed turn, never laundered into ``(Task completed)``. The
``text_tool_call`` row is live too: an open-weight model emitted its tool call
as prose (``<tool_call>…``) on a test fork and the ledger recorded success.

Cost (ruling T6): the native ``cost`` is summed for models.dev providers. A
custom provider (the template's ``runtime.provider`` block, carried as
``AGENT_RUNTIME_PROVIDER``) is priced by OpenCode at $0 unless the block
declares ``models.<id>.cost`` — and $0 is "unknown", not "free", so without a
declared cost ``cost_usd`` is ``None`` and one warning is logged per turn.

The reasoning content is deliberately NOT promoted into the response. It is a
model's scratchpad, not an answer; promoting it would launder a failed turn
into a plausible-looking success, which is precisely the failure #520 names. It
IS surfaced — truncated — in the error detail and the logs, so an operator can
see the model produced *something* and diagnose the provider.

The tools-ran row is parity with the Codex path: a turn that did real work and
ended without a closing sentence is a thin result, not a failure. Tightening
that (empty-result-as-failure controls) is a later #2981 change.

Per-turn system prompt
----------------------
``opencode run`` has **no system-prompt flag** — verified against the real
``--help``, not assumed. The carrier is the ``instructions:`` config key. A
single shared config file would race across the parallel ``/api/task`` path, so
each turn gets its own ``XDG_CONFIG_HOME`` (the one relocation mechanism that
was actually measured to work) holding a generated ``opencode.json`` whose
``instructions`` names that turn's prompt file. The pre-seeded provider
``node_modules`` tree is symlinked in from the shared root rather than copied,
so a cold turn still never reaches the npm registry.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException

from ..models import ExecutionLogEntry, ExecutionMetadata
from ..model_context import resolve_context_window
from ..state import agent_state
from ..utils.credential_sanitizer import (
    sanitize_dict,
    sanitize_subprocess_line,
    sanitize_text,
)
from ..utils.subprocess_pgroup import EXECUTION_TAG_NAME
from ._runtime_config import (
    _DEFAULT_EXECUTION_TIMEOUT_SEC,
    _load_guardrails,
    merged_disallowed_tools,
)
from .activity_tracking import complete_tool_execution, start_tool_execution
from .execution_env import build_execution_env
from . import opencode_commands, opencode_mcp, opencode_provider
from .opencode_provider import OpenCodeProviderConfigError
from .process_registry import get_process_registry
from .runtime_adapter import AgentRuntime, RuntimeCapabilities
from .subprocess_lifecycle import (
    _capture_pgid,
    _drain_bounded,
    _safe_close_pipes,
    _terminate_process_group,
)

logger = logging.getLogger(__name__)

_AGENT_HOME = "/home/developer"
_READ_ONLY_CONFIG = Path("/opt/trinity/read-only-config.json")
_LEGACY_READ_ONLY_CONFIG = Path(_AGENT_HOME) / ".trinity" / "read-only-config.json"

# One long-lived reader-thread worker, mirroring claude_code / gemini / codex.
# A fresh ThreadPoolExecutor per call would lean on CPython's non-deterministic
# weakref cleanup of the worker thread under load (#333). Executor SIZING is
# explicitly out of scope here — that is #2436.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="opencode-subproc")

# OpenCode is model-agnostic: the window belongs to whatever `provider/model`
# the agent is pinned to, not to the CLI. Resolved through the shared catalog,
# which falls back to the 200K safe floor for an id it does not know.
DEFAULT_OPENCODE_MODEL = "anthropic/claude-sonnet-5"


# ---------------------------------------------------------------------------
# provider/model shape
# ---------------------------------------------------------------------------

# `-m` takes `provider/model`. A bare model id is not merely rejected by the
# CLI — it is ambiguous, and the CLI's own error arrives one subprocess and one
# failed turn later. Validated at the boundary instead, with a NAMED reason.
# Deliberately permissive about the model half (providers use `:`, `.`, `@`
# and further `/` segments); the property being enforced is only that a
# provider prefix EXISTS and that neither half is empty.
_PROVIDER_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][\w./:@+-]*$")


class OpenCodeModelShapeError(ValueError):
    """A model id that is not `provider/model`. Carries the reason text."""


# ---------------------------------------------------------------------------
# Turn watchdog (#2981)
# ---------------------------------------------------------------------------
#
# `step_start` is the FIRST line `opencode run --format json` writes to stdout
# (measured against v1.18.32: step_start → text → step_finish). Everything
# before it is silent: config load, session create, the cold ripgrep fetch,
# the project/git walk, and the provider's prefill. A turn wedged anywhere in
# that window emits ZERO bytes, so the only honest signal the harness has is
# time — and without a watchdog it burns the whole execution timeout before
# failing, holding the chat lock for the duration.
#
# The default is generous on purpose. Measured with an open-weight model on a
# self-hosted OpenAI-compatible endpoint (cold): the silent pre-`step_start` window ranged 7.2s → 59.3s
# for byte-identical configs, the variance being the provider's own model
# load. A watchdog tighter than that would kill healthy turns, which is the
# worse failure — so 300s, ~5x the worst measured floor.
_DEFAULT_STALL_TIMEOUT_SEC = 300
_STALL_TIMEOUT_ENV = "OPENCODE_STALL_TIMEOUT_SEC"

# How many times a stalled turn is relaunched. Default 2 (3 attempts).
#
# The measurement that sets it: the stall is HOST-DEPENDENT. One host
# completed 16/16; another completed 2/15 (~13% per attempt) on the
# same pinned binary, flags, provider block and endpoint. One retry on a
# 13% host is ~24% — i.e. three turns in four end as a 504, which is not
# shippable unattended. Three attempts is ~34% and costs, at worst, two extra
# silent windows out of the SAME turn budget (never on top of it). An
# operator on a badly affected host raises this without a rebuild; an
# operator on a healthy host pays nothing, because a turn that starts never
# enters this path at all.
_DEFAULT_STALL_RETRIES = 2
_STALL_RETRIES_ENV = "OPENCODE_STALL_RETRIES"
#: Hard ceiling on the knob. A 20th relaunch is not a strategy, it is a way to
#: spend a whole turn timeout discovering the host is broken.
_MAX_STALL_RETRIES = 5
#: Floor of remaining turn budget below which a retry is not attempted. The
#: measured worst-case SILENT pre-`step_start` window on a healthy run was
#: 59.3s, so under ~90s an attempt cannot clear it and still answer — it would
#: only delay the 504 it is already heading for.
_MIN_RETRY_BUDGET_SEC = 90
#: Single greppable token for every retry line and for the give-up detail.
#: Counting these per host is how the stall stops being invisible in the ledger.
STALL_RETRY_MARKER = "[OpenCode][STALL-RETRY]"


class OpenCodeStallError(RuntimeError):
    """No `step_start` within the watchdog window. NEVER a success.

    Distinct from :class:`subprocess.TimeoutExpired` because the two have
    different remedies: a stall is retried once on a fresh session, a genuine
    overrun is not.
    """


def _stall_timeout_seconds(timeout_seconds: int) -> int:
    """Watchdog window, env-overridable, never longer than the turn itself.

    A non-numeric or non-positive override falls back to the default rather
    than disabling the watchdog — "0 means off" would make a typo silently
    remove the protection.
    """
    raw = os.environ.get(_STALL_TIMEOUT_ENV)
    window = _DEFAULT_STALL_TIMEOUT_SEC
    if raw:
        try:
            parsed = int(float(raw.strip()))
            if parsed > 0:
                window = parsed
            else:
                logger.warning(
                    "[OpenCode] %s=%r is not a positive number; using %ss",
                    _STALL_TIMEOUT_ENV, raw, _DEFAULT_STALL_TIMEOUT_SEC,
                )
        except (TypeError, ValueError):
            logger.warning(
                "[OpenCode] %s=%r is not a number; using %ss",
                _STALL_TIMEOUT_ENV, raw, _DEFAULT_STALL_TIMEOUT_SEC,
            )
    return max(1, min(window, max(1, int(timeout_seconds))))


def _stall_retry_limit() -> int:
    """Retries allowed for a stalled turn, env-overridable and bounded.

    A non-numeric override falls back to the default; a negative one clamps to
    0 (no retry) rather than being treated as "unset", because "I set it to
    -1" most plausibly means "stop retrying" and silently retrying anyway
    would be the surprising reading. Clamped to ``_MAX_STALL_RETRIES`` so a
    fat-fingered 500 cannot turn one turn into an all-day relaunch loop.
    """
    raw = os.environ.get(_STALL_RETRIES_ENV)
    if not raw:
        return _DEFAULT_STALL_RETRIES
    try:
        parsed = int(float(raw.strip()))
    except (TypeError, ValueError):
        logger.warning(
            "[OpenCode] %s=%r is not a number; using %s",
            _STALL_RETRIES_ENV, raw, _DEFAULT_STALL_RETRIES,
        )
        return _DEFAULT_STALL_RETRIES
    return max(0, min(parsed, _MAX_STALL_RETRIES))


def validate_provider_model(model: Optional[str]) -> Optional[str]:
    """Return ``model`` unchanged, or raise :class:`OpenCodeModelShapeError`.

    ``None``/empty is valid — it means "no pin", and the CLI's own default
    applies. The two rejections are named separately because they have
    different fixes.
    """
    if not model:
        return model
    candidate = model.strip()
    if "/" not in candidate:
        raise OpenCodeModelShapeError(
            f"OpenCode model {model!r} is missing a provider prefix. "
            "Use 'provider/model' (e.g. 'anthropic/claude-sonnet-5', "
            "'local/example-model')."
        )
    if not _PROVIDER_MODEL_RE.match(candidate):
        raise OpenCodeModelShapeError(
            f"OpenCode model {model!r} is not a valid 'provider/model' "
            "identifier: both halves must be non-empty and the provider may "
            "contain only letters, digits, '.', '_' and '-'."
        )
    return candidate


# ---------------------------------------------------------------------------
# XDG / config layout
# ---------------------------------------------------------------------------

def _opencode_root() -> str:
    """The relocated OpenCode state root.

    OpenCode defaults its XDG dirs under ``$HOME``, which for a Trinity agent is
    the git-tracked workspace — every turn would dirty auto-sync. startup.sh
    relocates all four XDG vars under ``$AGENT_TMPDIR/opencode`` (the
    disk-backed scratch dir that the canonical gitignore already excludes);
    this mirrors that resolution so the runtime and the shell agree even when
    the env is not inherited.
    """
    explicit = os.environ.get("OPENCODE_ROOT")
    if explicit:
        return explicit
    tmpdir = (
        os.environ.get("AGENT_TMPDIR")
        or os.environ.get("TMPDIR")
        or os.path.join(_AGENT_HOME, ".tmp")
    )
    return os.path.join(tmpdir, "opencode")


def _shared_config_dir() -> str:
    """``$XDG_CONFIG_HOME/opencode`` — the image-seeded config + provider tree."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(_opencode_root(), "config")
    return os.path.join(base, "opencode")


# Privacy + determinism defaults, baked in rather than left to the CLI's own
# (network-touching) defaults. `share: disabled` stops a turn from being
# publishable to opencode's share service; autoupdate off keeps the pinned
# binary pinned; no `plugin:` entries, so nothing is fetched at turn time.
_BASE_CONFIG: Dict[str, object] = {
    "$schema": "https://opencode.ai/config.json",
    "share": "disabled",
    "autoupdate": False,
}


def _load_shared_config() -> Dict[str, object]:
    """The image/startup-generated ``opencode.json``, or the baked defaults.

    Read rather than assumed so an operator's custom-provider block (the
    OpenAI-compatible endpoint path) survives into the per-turn config. Falls back to the defaults
    on any read/parse failure — a corrupt config must not take the agent down,
    and the fallback is the SAFE direction (privacy defaults on).
    """
    path = os.path.join(_shared_config_dir(), "opencode.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            merged = dict(_BASE_CONFIG)
            merged.update(loaded)
            # Never let a workspace-writable config re-enable these.
            merged["share"] = "disabled"
            merged["autoupdate"] = False
            return merged
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "[OpenCode] shared config unreadable (%s); using baked defaults", exc
        )
    return dict(_BASE_CONFIG)


_LINK_FROM_SHARED = ("node_modules", "package.json", "package-lock.json", "auth.json")


def _prepare_turn_config(
    execution_id: str,
    system_prompt: Optional[str],
    model: Optional[str] = None,
    env_out: Optional[Dict[str, str]] = None,
    permission: Optional[Dict[str, str]] = None,
    commands: Optional[Dict[str, Dict[str, str]]] = None,
) -> str:
    """Build this turn's private ``XDG_CONFIG_HOME`` and return its path.

    The returned directory IS the ``XDG_CONFIG_HOME`` (OpenCode reads
    ``$XDG_CONFIG_HOME/opencode/opencode.json``) and is also what the caller
    removes in its ``finally``.

    The template's provider block (``opencode_provider``) and the
    ``.mcp.json`` → ``mcp:`` translation (``opencode_mcp``) are rendered
    here, per turn, carrying env-var NAMES only (``{env:VAR}`` references,
    decision 6) — never a literal secret. ``env_out``, when given, collects
    the ``{VAR: default}`` pairs the caller must export into the subprocess
    env: the pinned OpenCode substitutes ``{env:VAR}`` with no default form,
    so a ``${VAR:-default}`` placeholder's default is honoured by the spawn.
    A malformed provider block raises :class:`OpenCodeProviderConfigError`
    BEFORE anything is written, naming the field. ``permission`` is the
    turn's control block from :func:`build_permission_block` (read-only,
    guardrails, platform denials, headless ``allowed_tools``); it is merged
    over the operator's own ``permission:`` with deny sticky
    (:func:`merge_permission_block`) and rendered into the same per-turn
    ``opencode.json`` — checkpoint D of #2981. ``commands`` are the playbook
    ``command:`` entries (:mod:`opencode_commands`, checkpoint E), laid over
    the shared config's own ``command:`` so a same-named skill wins.

    Why per-turn and not one shared file: ``opencode run`` has no
    system-prompt flag (verified against the real ``--help``), so the platform
    prompt has to travel through the ``instructions:`` config key — and a
    single shared config file would have concurrent ``/api/task`` turns
    overwriting each other's identity. Per-turn ``XDG_CONFIG_HOME`` is the one
    relocation mechanism that was actually measured to work.

    The pre-seeded provider ``node_modules`` tree is SYMLINKED in, never
    copied: OpenCode installs provider npm packages at runtime into
    ``$XDG_CONFIG_HOME/opencode/node_modules``, so a turn that could not see
    the seeded tree would reach the live npm registry on every cold container.
    """
    # Validated first so a named failure leaves no half-built turn dir behind.
    provider_block = opencode_provider.provider_block()

    turn_dir = os.path.join(_opencode_root(), "turns", _safe_token(execution_id))
    config_dir = os.path.join(turn_dir, "opencode")
    # 0700/0600: the turn config names the agent's secrets and carries its
    # platform prompt. makedirs' mode is umask-masked, so chmod after.
    os.makedirs(config_dir, mode=0o700, exist_ok=True)
    os.chmod(turn_dir, 0o700)
    os.chmod(config_dir, 0o700)

    shared = _shared_config_dir()
    for name in _LINK_FROM_SHARED:
        source = os.path.join(shared, name)
        target = os.path.join(config_dir, name)
        if not os.path.exists(source) or os.path.lexists(target):
            continue
        try:
            os.symlink(source, target)
        except OSError as exc:  # pragma: no cover — defensive
            logger.warning("[OpenCode] could not link %s into turn config: %s", name, exc)

    config = _load_shared_config()
    if system_prompt:
        prompt_file = os.path.join(config_dir, "trinity-instructions.md")
        fd = os.open(prompt_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(system_prompt)
        os.chmod(prompt_file, 0o600)
        # Additive: keep whatever the operator declared, put Trinity's first.
        existing = config.get("instructions")
        existing = list(existing) if isinstance(existing, list) else []
        config["instructions"] = [prompt_file, *existing]

    # Provider first (the `-m` target must exist before `mcp:` is read), then
    # the `.mcp.json` translation. Both render `{env:NAME}` references; the
    # serialized file never carries a value from the environment.
    opencode_provider.render_provider_config(config, provider_block)
    env_defaults = opencode_mcp.apply_mcp_config(config)
    if env_out is not None:
        env_out.update(env_defaults)
    if permission:
        config["permission"] = merge_permission_block(
            config.get("permission"), permission
        )
    if commands:
        existing_commands = config.get("command")
        config["command"] = {
            **(existing_commands if isinstance(existing_commands, dict) else {}),
            **commands,
        }

    config_path = os.path.join(config_dir, "opencode.json")
    with open(config_path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
    # 0600, the same as the agent's `.mcp.json`. Best-effort: a chmod failure on an
    # exotic filesystem must not fail the turn, and the turn dir itself is
    # already under the 0700 OpenCode root.
    try:
        os.chmod(config_path, 0o600)
    except OSError as exc:  # pragma: no cover — defensive
        logger.warning("[OpenCode] could not chmod turn config: %s", exc)
    return turn_dir


def _safe_token(execution_id: str) -> str:
    """Filesystem-safe token. ``execution_id`` is system-generated today, but a
    path is never built from it unguarded (the Codex ``_safe_result_token``
    precedent): reduce to a basename and a conservative charset so a '/' or
    '..' cannot escape the turn root."""
    token = re.sub(r"[^A-Za-z0-9_.-]", "_", os.path.basename(execution_id or ""))
    return token or "opencode"


def _cleanup_turn_dir(turn_dir: str) -> None:
    """Remove the per-turn config tree. Symlinks are unlinked, never followed —
    ``shutil.rmtree`` does not recurse into a symlinked directory, so the shared
    provider tree is safe."""
    try:
        shutil.rmtree(turn_dir, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
        logger.debug("[OpenCode] turn dir cleanup failed for %s: %s", turn_dir, exc)


# ---------------------------------------------------------------------------
# Read-only mode
# ---------------------------------------------------------------------------

def _is_read_only() -> bool:
    """True when the backend has put this agent in read-only mode.

    Same signal the Claude read-only *hook* consumes
    (``/opt/trinity/read-only-config.json`` → ``enabled``; the home copy
    ``~/.trinity/read-only-config.json`` while the root file is missing — the
    Codex loader's precedence). Fails OPEN and logs
    on an unreadable/corrupt file, matching ``read-only-guard.py`` and the
    Codex loader — diverging one runtime to fail-closed would make read-only
    enforcement inconsistent across runtimes.

    Enforced through the per-turn ``permission:`` config block
    (:func:`build_permission_block` → ``_prepare_turn_config``): read-only
    renders ``edit: deny`` and ``bash: deny``, which the pinned OpenCode
    refuses BEFORE asking (``permission/index.ts:75-79``, v1.18.32) — so
    ``--auto``'s auto-approval never sees them.
    """
    config = _READ_ONLY_CONFIG if _READ_ONLY_CONFIG.exists() else _LEGACY_READ_ONLY_CONFIG
    try:
        raw = config.read_text()
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning(
            "[OpenCode] read-only config unreadable (%s); treating as not read-only", exc
        )
        return False
    try:
        return bool(json.loads(raw).get("enabled"))
    except json.JSONDecodeError as exc:
        logger.warning(
            "[OpenCode] read-only config malformed (%s); treating as not read-only", exc
        )
        return False


# ---------------------------------------------------------------------------
# Controls → `permission:` (checkpoint D, #2981)
# ---------------------------------------------------------------------------
#
# Verified against the pinned OpenCode v1.18.32 source, not assumed:
#
# * Schema — `packages/core/src/v1/config/permission.ts`: `Action` is the
#   literal set `ask | allow | deny` (line 5); a `Rule` is an Action OR a
#   `{pattern: Action}` map (line 11); the known keys are `read`, `edit`,
#   `glob`, `grep`, `list`, `bash`, `task`, `external_directory`,
#   `todowrite`, `question`, `webfetch`, `websearch`, `lsp`, `doom_loop`,
#   `skill` plus any string key (lines 17-36). `bash`/`edit` take a Rule (so a
#   pattern map is accepted); `webfetch`/`websearch` are Action-only scalars.
#   `tools: {write: false}` is folded into the same block, with `write`,
#   `edit` and `patch` all collapsing onto the ONE `edit` key
#   (`packages/opencode/src/config/config.ts:567-578`) — which is why the four
#   Claude edit tools below share a key and cannot be denied separately.
# * What `opencode run` does with `ask` — `packages/opencode/src/cli/cmd/run.ts`:
#   `--auto` is "auto-approve permissions that are not explicitly denied"
#   (lines 242-245); on a `permission.asked` event it replies `once` under
#   `--auto` and `reject` otherwise (lines 801-819). Either way the turn never
#   blocks on a prompt — so under the `--auto` this runtime always passes,
#   `ask` is an approval, and `deny` is the ONLY value that withholds a
#   capability: `permission/index.ts:75-79` raises `PermissionDenied` before
#   the ask is ever published.
# * The binary default left in place for every key this map does not decide —
#   `packages/opencode/src/agent/agent.ts:119-135`, the `build` agent:
#   `"*": "allow"`, `doom_loop: ask`, `external_directory: {"*": ask}` (+ the
#   tmp/skill dirs allowed), `read: {"*": allow, "*.env": ask, "*.env.*": ask,
#   "*.env.example": allow}`; `question`/`plan_enter` are re-allowed for
#   `build` (lines 146-150). Under `--auto` every one of those `ask`s is
#   approved, i.e. the effective default is "everything allowed".
#
# The map is deliberately the plan's set plus `websearch` (which the pinned
# schema has — the plan conditioned it on exactly that). Claude names with
# no row here are NOT silently dropped: `build_permission_block` returns them
# and `_execute_opencode` writes one execution-log entry per name.

# Claude tool name → OpenCode permission key. The ONE vocabulary table.
PERMISSION_KEY_FOR_TOOL: Dict[str, str] = {
    "Write": "edit",
    "Edit": "edit",
    "MultiEdit": "edit",
    "NotebookEdit": "edit",
    "Bash": "bash",
    "WebFetch": "webfetch",
    "WebSearch": "websearch",
}

# Read-only mode: no writes, no shell; fetching stays allowed (the plan's
# set). The `allow` is the lowest-precedence value in the block — a deny from
# any other source over `webfetch` survives it (`merge`-sticky below).
READ_ONLY_PERMISSION: Dict[str, str] = {
    "edit": "deny",
    "bash": "deny",
    "webfetch": "allow",
}

# Origins a surfaced (unmapped) control name can carry, and the ledger text.
CONTROL_GUARDRAIL = "guardrail"
CONTROL_PLATFORM = "platform"
CONTROL_ALLOWED_TOOLS = "allowed_tools"
_UNMAPPED_TEXT = {
    CONTROL_GUARDRAIL: "guardrail not mapped: {tool}",
    CONTROL_PLATFORM: "platform denial not mapped: {tool}",
    CONTROL_ALLOWED_TOOLS: "allowed_tools not mapped: {tool}",
}


def build_permission_block(
    *,
    read_only: bool,
    guardrails: Optional[dict],
    allowed_tools: Optional[List[str]],
) -> Tuple[Dict[str, str], List[Tuple[str, str]]]:
    """Fold every Trinity control onto OpenCode's ``permission:`` block.

    Pure: ``(read_only, guardrails, allowed_tools) → (block, unmapped)``, so
    the tests and the mutation check hit one place. ``block`` holds ONLY the
    keys a control decides; everything else stays at the binary's default
    (stated in the module comment above). ``unmapped`` is every control name
    with no row in :data:`PERMISSION_KEY_FOR_TOOL`, as ``(origin, name)``
    pairs, in first-seen order and never deduplicated across origins — the
    caller surfaces each one in the execution log rather than dropping it.

    Precedence, pinned by test: read-only deny > guardrail / platform deny >
    ``allowed_tools`` > the binary default. Nothing turns a deny back into
    allow — read-only's ``webfetch: allow`` yields to a guardrail that denies
    ``WebFetch``. An empty or ``None`` ``allowed_tools`` restricts nothing,
    exactly as the Claude runtime treats it; a non-empty one denies every
    mapped key NONE of whose Claude names is listed (the four edit tools share
    one key, so listing any of them keeps ``edit`` at its default).
    """
    block: Dict[str, str] = {}
    unmapped: List[Tuple[str, str]] = []

    # Lowest explicit layer: the headless allowlist (deny what it omits).
    if allowed_tools:
        listed = {name.strip() for name in allowed_tools if isinstance(name, str)}
        allowed_keys = {
            PERMISSION_KEY_FOR_TOOL[name] for name in listed if name in PERMISSION_KEY_FOR_TOOL
        }
        for key in set(PERMISSION_KEY_FOR_TOOL.values()) - allowed_keys:
            block[key] = "deny"
        for name in allowed_tools:
            if isinstance(name, str) and name.strip() and name.strip() not in PERMISSION_KEY_FOR_TOOL:
                unmapped.append((CONTROL_ALLOWED_TOOLS, name.strip()))

    # Guardrails (operator `disallowed_tools`) ∪ platform denials, through the
    # one builder every Claude spawn uses — never a second reader (#2454).
    guardrails = guardrails if isinstance(guardrails, dict) else {}
    operator_names = {
        n.strip() for n in (guardrails.get("disallowed_tools") or ())
        if isinstance(n, str) and n.strip()
    }
    for name in merged_disallowed_tools(guardrails):
        key = PERMISSION_KEY_FOR_TOOL.get(name)
        if key:
            block[key] = "deny"
        else:
            origin = CONTROL_GUARDRAIL if name in operator_names else CONTROL_PLATFORM
            unmapped.append((origin, name))

    # Highest: read-only. Its denies win outright; its one allow only fills a
    # key nothing above denied.
    if read_only:
        for key, action in READ_ONLY_PERMISSION.items():
            if action == "deny" or block.get(key) != "deny":
                block[key] = action
    return block, unmapped


def merge_permission_block(existing: object, computed: Dict[str, str]) -> Dict[str, object]:
    """Overlay Trinity's block on the operator's own ``permission:``.

    The schema accepts a bare Action for the whole block (normalised to
    ``{"*": action}``, ``permission.ts:40-41``) or a key map whose values may
    be pattern maps. Deny is sticky in BOTH directions: an operator ``deny``
    is never replaced by Trinity's ``allow``, and Trinity's ``deny`` replaces
    whatever the operator wrote for that key (a pattern map included — a
    scalar ``deny`` is the stricter rule).
    """
    if isinstance(existing, str):
        merged: Dict[str, object] = {"*": existing}
    elif isinstance(existing, dict):
        merged = dict(existing)
    else:
        merged = {}
    for key, action in computed.items():
        if action != "deny" and merged.get(key) == "deny":
            continue
        merged[key] = action
    return merged


def _control_log_entries(unmapped: List[Tuple[str, str]]) -> List[ExecutionLogEntry]:
    """One ledger entry per surfaced control — the operator sees it in the
    execution log, not only in the container logger (the Codex path, which
    only logs, is the pattern being improved on)."""
    entries: List[ExecutionLogEntry] = []
    for origin, name in unmapped:
        text = _UNMAPPED_TEXT[origin].format(tool=name)
        entries.append(
            ExecutionLogEntry(
                id=f"control-{origin}-{_safe_token(name)}",
                type="tool_result",
                tool="trinity-control",
                output=text,
                success=None,
                timestamp=datetime.now().isoformat(),
            )
        )
    return entries


# ---------------------------------------------------------------------------
# JSON-lines event parsing
# ---------------------------------------------------------------------------

@dataclass
class _OpenCodeParseState:
    """Mutable accumulators threaded through per-event parsing.

    ``step_texts`` holds one list of text fragments per step, because the final
    response is the LAST step's text — not every text the turn ever emitted.
    An earlier step's narration is transcript, not answer.
    """

    execution_log: List[ExecutionLogEntry]
    metadata: ExecutionMetadata
    step_texts: List[List[str]] = field(default_factory=list)
    reasoning_parts: List[str] = field(default_factory=list)
    session_id: Optional[str] = None
    model: Optional[str] = None
    execution_id: Optional[str] = None
    seen_tool_ids: set = field(default_factory=set)
    reasoning_tokens: int = 0
    cache_write_tokens: int = 0
    # Reason on the most recent `step_finish`, cleared by the next
    # `step_start` — so at the end of the stream it says whether the last
    # step promised a continuation (`tool-calls`) or was cut (`length`).
    last_finish_reason: Optional[str] = None
    error_seen: bool = False
    dropped_lines: int = 0
    first_dropped: Optional[str] = None

    def current_step(self) -> List[str]:
        if not self.step_texts:
            self.step_texts.append([])
        return self.step_texts[-1]


def _part(event: dict) -> dict:
    """The nested ``part`` payload, or ``{}`` for a flat event."""
    part = event.get("part")
    return part if isinstance(part, dict) else {}


def _first_str(*sources: dict, keys: Tuple[str, ...]) -> Optional[str]:
    """First non-empty string under any of ``keys`` across ``sources`` in order
    (``part`` first, then the top level — the fallback direction)."""
    for source in sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _event_text(event: dict) -> str:
    """The prose carried by a ``text``/``reasoning`` event.

    Three spellings are accepted because providers disagree and the wrapper
    passes them through: ``text``, ``content``, ``part.text``. An event whose
    payload is a dict under ``part`` is the shape OpenCode uses for streamed
    parts.
    """
    for key in ("text", "content", "delta"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    part = event.get("part")
    if isinstance(part, dict):
        for key in ("text", "content"):
            value = part.get(key)
            if isinstance(value, str) and value:
                return value
    return ""


def _tool_name(event: dict) -> str:
    return _first_str(_part(event), event, keys=("tool", "name", "toolName")) or "Tool"


def _tool_id(event: dict) -> str:
    # `part.callID` is the tool CALL id; `part.id` is the part's own id and is
    # only a fallback. Flat events keep the older spellings.
    return (
        _first_str(_part(event), keys=("callID", "toolCallID", "tool_call_id", "id"))
        or _first_str(event, keys=("id", "toolCallID", "callID", "tool_call_id"))
        or str(uuid.uuid4())
    )


def _tool_state(event: dict) -> dict:
    for source in (_part(event), event):
        blob = source.get("state")
        if isinstance(blob, dict):
            return blob
    return {}


def _record_tool(state: _OpenCodeParseState, event: dict) -> None:
    tool_id = _tool_id(event)
    name = _tool_name(event)
    part = _part(event)
    state_blob = _tool_state(event)
    tool_input = None
    for source in (state_blob, part, event):
        for key in ("input", "args", "arguments"):
            if source.get(key) is not None:
                tool_input = source[key]
                break
        if tool_input is not None:
            break
    if tool_input is None:
        tool_input = {}
    if not isinstance(tool_input, dict):
        tool_input = {"value": tool_input}

    if tool_id not in state.seen_tool_ids:
        state.seen_tool_ids.add(tool_id)
        state.execution_log.append(
            ExecutionLogEntry(
                id=tool_id,
                type="tool_use",
                tool=name,
                input=tool_input,
                timestamp=datetime.now().isoformat(),
            )
        )
        try:
            start_tool_execution(
                tool_id, name, tool_input, execution_id=state.execution_id
            )
        except Exception:  # noqa: BLE001 — activity tracking is best-effort
            logger.debug("[OpenCode] start_tool_execution failed for %s", tool_id, exc_info=True)

    # OpenCode reports a tool_use with its outcome attached once it settles;
    # an event carrying neither an output nor a status is the start half only.
    status = state_blob.get("status")
    if status is None:
        status = part.get("status", event.get("status"))
    output = state_blob.get("output")
    if output is None:
        output = part.get("output") or event.get("output") or event.get("result")
    if output is None and status is None:
        return
    if not isinstance(output, str):
        output = json.dumps(output, default=str) if output is not None else ""
    is_error = (
        status in ("error", "failed")
        or bool(state_blob.get("error"))
        or bool(part.get("error"))
        or bool(event.get("error"))
    )
    state.execution_log.append(
        ExecutionLogEntry(
            id=tool_id,
            type="tool_result",
            tool=name,
            output=output or None,
            success=not is_error,
            timestamp=datetime.now().isoformat(),
        )
    )
    try:
        complete_tool_execution(
            tool_id, not is_error, output, execution_id=state.execution_id
        )
    except Exception:  # noqa: BLE001
        logger.debug("[OpenCode] complete_tool_execution failed for %s", tool_id, exc_info=True)


def _accumulate_usage(state: _OpenCodeParseState, event: dict) -> None:
    """Fold one ``step_finish`` payload into the running totals.

    Tokens and cost are SUMMED across steps: each ``step_finish`` reports its
    own step. If a future OpenCode makes them cumulative this over-reports —
    chosen deliberately, because cost feeds `max_cost_usd` loop budgets and a
    budget is a spend control, so the safe failure direction is over- not
    under-reporting.

    ``tokens.reasoning`` ⊂ ``tokens.output`` and ``tokens.cache.read`` ⊂
    ``tokens.input``. Both are recorded separately and NEITHER is added to any
    total — adding them is the double-count this runtime must not commit.
    """
    part = _part(event)
    tokens: dict = {}
    for source in (part, event):
        for key in ("tokens", "usage"):
            if isinstance(source.get(key), dict):
                tokens = source[key]
                break
        if tokens:
            break
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}

    state.metadata.input_tokens += _as_int(tokens.get("input"))
    state.metadata.output_tokens += _as_int(tokens.get("output"))
    state.metadata.cache_read_tokens += _as_int(cache.get("read"))
    state.cache_write_tokens += _as_int(cache.get("write"))
    state.reasoning_tokens += _as_int(tokens.get("reasoning"))

    cost = part.get("cost", event.get("cost"))
    if cost is None and isinstance(tokens.get("cost"), (int, float)):
        cost = tokens["cost"]
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        state.metadata.cost_usd = (state.metadata.cost_usd or 0.0) + float(cost)

    reason = _first_str(part, event, keys=("reason", "finishReason", "finish_reason"))
    state.last_finish_reason = reason.lower() if reason else None


def _as_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _process_opencode_event(event: dict, state: _OpenCodeParseState) -> None:
    """Update ``state`` from one parsed JSON line.

    Tolerant of unknown event types and missing fields by design: an unknown
    line must cost a turn nothing. The one thing it is NOT tolerant about is a
    missing final text — see ``finalize_response``.
    """
    # sessionID rides on EVERY line, so capture it unconditionally rather than
    # from one blessed event type that a provider might not emit.
    session = _first_str(event, _part(event), keys=("sessionID", "sessionId", "session_id"))
    if session:
        state.session_id = session

    event_type = event.get("type") or event.get("event")

    if event_type == "step_start":
        state.step_texts.append([])
        state.last_finish_reason = None
    elif event_type == "step_finish":
        _accumulate_usage(state, event)
    elif event_type == "tool_use":
        _record_tool(state, event)
    elif event_type == "text":
        text = _event_text(event)
        if text:
            state.current_step().append(text)
    elif event_type == "reasoning":
        text = _event_text(event)
        if text:
            state.reasoning_parts.append(text)
    elif event_type == "error":
        state.error_seen = True
        state.metadata.error_type = "error"
        state.metadata.error_message = _error_message(event)


def _error_message(event: dict) -> str:
    """The human-readable reason carried by an ``error`` event.

    Measured shape: ``{"type": "error", "error": {"name": ..., "data":
    {"message": ...}}}``; the flat ``message`` / ``error: "<str>"`` spellings
    and a ``part.error`` nest are accepted as fallbacks. An unreadable payload
    is serialized rather than replaced, so the operator sees what arrived.
    """
    candidates = [event.get("error"), _part(event).get("error"), event.get("data"), event]
    for blob in candidates:
        if isinstance(blob, str) and blob:
            return blob
        if not isinstance(blob, dict):
            continue
        data = blob.get("data") if isinstance(blob.get("data"), dict) else {}
        for source in (data, blob):
            message = source.get("message")
            if isinstance(message, str) and message:
                return message
    raw = event.get("error") or _part(event).get("error")
    if raw:
        return json.dumps(raw, default=str)[:500]
    return "OpenCode error"


def _parse_line(line: str, state: _OpenCodeParseState) -> Optional[dict]:
    """One stdout line → one event dict, or ``None`` for a non-JSON line.

    Shared by the subprocess reader and the unit entrypoint so both count the
    same things: a line that is not a JSON object is dropped and COUNTED (the
    turn logs the tally once at the end), never raised on.
    """
    stripped = line.strip()
    if not stripped:
        return None
    try:
        event = json.loads(stripped)
    except json.JSONDecodeError:
        event = None
    if isinstance(event, dict):
        return event
    state.dropped_lines += 1
    if state.first_dropped is None:
        state.first_dropped = stripped[:200]
    return None


def _log_dropped_lines(state: _OpenCodeParseState) -> None:
    if state.dropped_lines:
        logger.warning(
            "[OpenCode] dropped %s non-JSON stdout line(s) this turn "
            "(execution_id=%s); first: %r",
            state.dropped_lines, state.execution_id, state.first_dropped,
        )


# Outcomes of `finalize_response`, so callers branch on a name rather than on
# "is the string empty" — the distinction the #520 class turns on.
FINAL_OK = "ok"
FINAL_TOOLS_ONLY = "tools_only"
FINAL_REASONING_ONLY = "reasoning_only"
FINAL_EMPTY = "empty_response"
FINAL_ERROR = "error"
FINAL_TRUNCATED = "truncated"
FINAL_INCOMPLETE = "incomplete"
FINAL_TEXT_TOOL_CALL = "text_tool_call"

_FAILED_FINALS = frozenset({
    FINAL_REASONING_ONLY, FINAL_EMPTY, FINAL_ERROR, FINAL_TRUNCATED,
    FINAL_INCOMPLETE, FINAL_TEXT_TOOL_CALL,
})

# The four text-shaped tool-call spellings open-weight models fall back to when
# they fail to use the provider's native tool channel. Checked only when the
# turn produced ZERO `tool_use` events: a model that ran a real tool and then
# quoted JSON in its answer is answering, not calling.
_TEXT_TOOL_CALL_SHAPES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("tool_call_tag", re.compile(r"<tool_call\b", re.IGNORECASE)),
    ("function_call_tag", re.compile(r"<function_call\b", re.IGNORECASE)),
    ("name_arguments_object", re.compile(
        r"\{\s*\"name\"\s*:\s*\"[^\"]+\"\s*,\s*\"arguments\"\s*:"
        r"|\{\s*\"arguments\"\s*:\s*\{.*?\}\s*,\s*\"name\"\s*:", re.DOTALL)),
    ("tool_calls_array", re.compile(r"\"tool_calls\"\s*:\s*\[")),
)


def text_tool_call_shape(text: str) -> Optional[str]:
    """Name the tool-call shape ``text`` carries, or ``None`` for prose."""
    for name, pattern in _TEXT_TOOL_CALL_SHAPES:
        if pattern.search(text):
            return name
    return None


def finalize_response(state: _OpenCodeParseState) -> Tuple[str, str]:
    """Return ``(response_text, outcome)`` for a completed stream.

    The final text is the LAST step's concatenated ``text`` parts — there is no
    separate result line to fall back on. See the module docstring for the
    outcome table (row order here IS the precedence) and why ``reasoning`` is
    never promoted into the response.
    """
    if state.error_seen:
        return "", FINAL_ERROR
    if state.last_finish_reason == "length":
        return "", FINAL_TRUNCATED
    if state.last_finish_reason == "tool-calls":
        return "", FINAL_INCOMPLETE
    last_step = state.step_texts[-1] if state.step_texts else []
    text = "".join(last_step).strip()
    if text:
        ran_tools = any(entry.type == "tool_use" for entry in state.execution_log)
        if not ran_tools and text_tool_call_shape(text):
            return "", FINAL_TEXT_TOOL_CALL
        return text, FINAL_OK
    if state.reasoning_parts and "".join(state.reasoning_parts).strip():
        return "", FINAL_REASONING_ONLY
    if any(entry.type == "tool_use" for entry in state.execution_log):
        return "(Task completed)", FINAL_TOOLS_ONLY
    return "", FINAL_EMPTY


_PROVIDER_ENV = opencode_provider.ENV_PROVIDER


def _custom_provider_block() -> Optional[dict]:
    """The template's ``runtime.provider`` block, as serialized at create
    (ruling T5), through the ONE reader in ``opencode_provider``. Malformed →
    ``None`` with a warning: the turn boundary fails such a block named, and
    the cost policy — which can run on a parsed stream alone — only needs
    the ``models.<id>.cost`` shape."""
    try:
        return opencode_provider.load_provider_block()
    except OpenCodeProviderConfigError as exc:
        logger.warning("[OpenCode] %s; cost policy ignores it", exc)
        return None


def _apply_cost_policy(metadata: ExecutionMetadata, model: Optional[str]) -> None:
    """Ruling T6: a custom provider's native cost is $0 unless the block
    declares ``models.<id>.cost``, and $0 is "unknown", not "free". With no
    declared cost ``cost_usd`` becomes ``None`` (loop budgets see unknown) and
    ONE warning is logged per turn. A models.dev provider keeps the native sum.
    """
    block = _custom_provider_block()
    if not block or not model or "/" not in model:
        return
    provider_id, _, model_id = model.partition("/")
    if str(block.get("id") or "") != provider_id:
        return
    models = block.get("models") if isinstance(block.get("models"), dict) else {}
    declared = models.get(model_id) if isinstance(models.get(model_id), dict) else {}
    if isinstance(declared.get("cost"), dict) and declared["cost"]:
        return
    logger.warning(
        "[OpenCode] custom provider %r declares no cost for model %r — recording "
        "cost_usd=None (unknown), not the $%s OpenCode reported. Declare "
        "runtime.provider.models.%s.cost in template.yaml to price it.",
        provider_id, model_id, metadata.cost_usd or 0, model_id,
    )
    metadata.cost_usd = None


def _finish_parse(state: _OpenCodeParseState) -> Tuple[str, str]:
    """Close out a fully-read stream: ledger counts, the dropped-line tally,
    the cost policy, then the outcome. One function so the subprocess reader
    and the unit entrypoint cannot drift."""
    metadata = state.metadata
    metadata.tool_count = len([e for e in state.execution_log if e.type == "tool_use"])
    metadata.session_id = state.session_id
    _log_dropped_lines(state)
    _apply_cost_policy(metadata, state.model)
    return finalize_response(state)


def parse_opencode_jsonl(
    lines: List[str], model: Optional[str] = None
) -> Tuple[str, List[ExecutionLogEntry], ExecutionMetadata, List[Dict], str]:
    """Parse a full ``--format json`` line stream (unit-test entrypoint).

    Returns ``(response_text, execution_log, metadata, raw_messages, outcome)``.
    """
    metadata = ExecutionMetadata()
    metadata.context_window = resolve_context_window(model)
    state = _OpenCodeParseState(execution_log=[], metadata=metadata, model=model)
    raw_messages: List[Dict] = []
    for line in lines:
        event = _parse_line(line, state)
        if event is not None:
            raw_messages.append(event)
            _process_opencode_event(event, state)
    response_text, outcome = _finish_parse(state)
    return response_text, state.execution_log, metadata, raw_messages, outcome


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

# Anchored, not bare-substring (the #1187 review-I1 lesson): a non-auth failure
# whose output merely CONTAINS "401" — an upstream MCP tool, say — must not be
# read as auth, because 503 is the backend's AUTH signal and the dispatch
# breaker counts AUTH only.
_AUTH_PATTERNS = (
    re.compile(r"\bunauthorized\b", re.IGNORECASE),
    re.compile(r"\b401\s+unauthorized\b", re.IGNORECASE),
    re.compile(r"\b(?:invalid|incorrect|missing|no)[ _]api[ _]key\b", re.IGNORECASE),
    re.compile(r"\bnot\s+authenticated\b", re.IGNORECASE),
    re.compile(r"\bauthentication\s+(?:failed|error)\b", re.IGNORECASE),
    re.compile(r"\bno\s+credentials?\s+found\b", re.IGNORECASE),
)
# Rate markers are anchored for the same reason: "quota" inside
# `disk_quota_report` or "429" inside a port number in tool output is not a
# provider rate limit.
_RATE_MARKERS = (
    re.compile(r"\b429\b"),
    re.compile(r"\brate[ _]limit", re.IGNORECASE),
    re.compile(r"\bquota\b", re.IGNORECASE),
    re.compile(r"\binsufficient_quota\b", re.IGNORECASE),
    re.compile(r"\btoo\s+many\s+requests\b", re.IGNORECASE),
)


def classify_opencode_failure(
    return_code: int, stderr: str, metadata: ExecutionMetadata
) -> Tuple[int, str]:
    """Map a non-zero exit (+ stderr + parsed error) to an HTTP status.

    auth → 503, rate-limit → 429, everything else → 500. A generic runtime
    failure is deliberately 500 and NOT 503: 503 is the backend's AUTH signal
    and the dispatch breaker counts AUTH only, so a 503 for an unrelated crash
    would open the breaker on a healthy credential.
    """
    haystack = " ".join(s for s in (stderr or "", metadata.error_message or "") if s)
    shaped = _classify_message(haystack)
    if shaped == 429:
        return 429, f"OpenCode rate limit: {(stderr or metadata.error_message or '')[:300]}"
    if shaped == 503:
        return 503, (
            f"OpenCode authentication failure: "
            f"{(stderr or metadata.error_message or '')[:300]}. "
            "Check the provider credentials injected into this agent."
        )
    detail = (stderr or "").strip() or metadata.error_message or "see agent logs"
    return 500, f"OpenCode execution failed (exit code {return_code}): {detail[:300]}"


def _classify_message(haystack: str) -> Optional[int]:
    """429 for a rate-shaped message, 503 for an auth-shaped one, else None.
    Shared by the exit-code path and the stream `error`-event path so one
    registry decides what counts as AUTH."""
    if any(marker.search(haystack) for marker in _RATE_MARKERS):
        return 429
    if any(pattern.search(haystack) for pattern in _AUTH_PATTERNS):
        return 503
    return None


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

class OpenCodeRuntime(AgentRuntime):
    """OpenCode CLI implementation of :class:`AgentRuntime`."""

    def __init__(self) -> None:
        # Session id for the interactive chat session (continuity). The
        # singleton persists across /api/chat calls within a container.
        self._chat_session_id: Optional[str] = None
        # `opencode --version` answered once per process: the binary is baked
        # into the image, so it cannot appear or vanish without a recreate,
        # and probing it on every turn costs a subprocess spawn per turn.
        self._available: Optional[bool] = None

    @classmethod
    def capabilities(cls) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            chat_continuity=True,       # opencode run --session <id>
            session_tab_resume=False,   # no cached-UUID --resume machinery
            mcp_support=True,           # MCP servers via opencode.json
            cost_reporting="native",    # step_finish carries a real cost
            hooks=False,                # not a guardrail-hook runtime (ent#945)
            images=False,               # execute_headless ignores images
            plugins=False,              # plugins_reinstall skips non-Claude
            subscription_auth=False,    # no auth_override (#1187 decision 7)
        )

    def is_available(self) -> bool:
        if self._available is None:
            try:
                result = subprocess.run(
                    ["opencode", "--version"], capture_output=True, text=True, timeout=5
                )
                self._available = result.returncode == 0
            except Exception:
                self._available = False
        return self._available

    def get_default_model(self) -> str:
        return os.environ.get("AGENT_RUNTIME_MODEL") or DEFAULT_OPENCODE_MODEL

    def get_context_window(self, model: Optional[str] = None) -> int:
        """OpenCode is model-agnostic, so the window belongs to the pinned
        model. The provider prefix is stripped before the catalog lookup — the
        catalog is keyed on model ids, and `anthropic/claude-sonnet-5` would
        otherwise miss and silently return the 200K floor for a 1M model."""
        candidate = model or self.get_default_model()
        if candidate and "/" in candidate:
            candidate = candidate.split("/", 1)[1]
        return resolve_context_window(candidate)

    def configure_mcp(self, mcp_servers: Dict) -> bool:
        """Persist template-declared MCP servers (#2981).

        The store is the agent's ``.mcp.json`` — the same file Claude Code
        uses and the same file ``trinity_mcp.inject_trinity_mcp_if_configured``
        writes the Trinity server into on startup. OpenCode never reads it
        directly; the per-turn config seam (``_prepare_turn_config``)
        translates it into each turn's ``mcp:`` block. Keeping one store is the point: a
        second persistent copy would let a re-injected Trinity key reach one
        file and not the other.
        """
        from .trinity_mcp import _configure_claude_mcp_servers

        ok = _configure_claude_mcp_servers(mcp_servers)
        if ok:
            logger.info(
                "[OpenCode] recorded %d MCP server(s) in .mcp.json; they are "
                "translated into each turn's opencode.json `mcp:` block",
                len(mcp_servers or {}),
            )
        return ok

    # -- command construction ------------------------------------------------
    def _build_command(
        self,
        *,
        model: Optional[str],
        session_id: Optional[str],
        directory: Optional[str],
        command: Optional[str] = None,
    ) -> List[str]:
        cmd = ["opencode", "run", "--format", "json", "--auto"]
        if model:
            cmd += ["-m", model]
        if session_id:
            cmd += ["--session", session_id]
        if directory:
            cmd += ["--dir", directory]
        if command:
            # A scanned skill name only (`opencode_commands.split_playbook_prefix`).
            cmd += ["--command", command]
        return cmd

    # -- core subprocess execution (stubbed in unit tests) -------------------
    async def _execute_opencode(
        self,
        *,
        prompt: str,
        model: Optional[str],
        system_prompt: Optional[str],
        session_id: Optional[str],
        timeout_seconds: int,
        execution_id: Optional[str],
        concurrent_reader: bool = False,
        allowed_tools: Optional[List[str]] = None,
    ) -> Tuple[str, List[ExecutionLogEntry], ExecutionMetadata, List[Dict], Optional[str]]:
        execution_id = execution_id or str(uuid.uuid4())
        # Always an explicit `-m`: with no pin the CLI would choose a model
        # Trinity cannot name, and the ledger's `model_name` would be a guess.
        model = validate_provider_model(model or self.get_default_model())

        # Ruling T4: the key for the provider this turn runs on must be
        # PRESENT before the subprocess starts — a missing one fails 503
        # naming the variable, never a silent fallback. The wording goes
        # through the same auth registry as the exit-code path, so one
        # message means one cause ("missing api key" is an `_AUTH_PATTERNS`
        # row); a malformed provider block raises named, mapped to 400.
        provider_block = opencode_provider.provider_block()
        missing_var = opencode_provider.missing_key_env(model, provider_block)
        if missing_var:
            provider_id = model.partition("/")[0]
            status_code, detail = classify_opencode_failure(
                0,
                f"missing API key: environment variable {missing_var} is unset "
                f"or empty, and provider {provider_id!r} (model {model}) needs it",
                ExecutionMetadata(),
            )
            logger.error("[OpenCode] %s", detail)
            raise HTTPException(status_code=status_code, detail=detail)

        # Controls → `permission:` (checkpoint D). Read-only, the operator's
        # guardrails ∪ platform denials, and the headless allowlist all land
        # in the same per-turn config; what has no OpenCode equivalent is
        # surfaced below, never dropped.
        read_only = _is_read_only()
        permission, unmapped = build_permission_block(
            read_only=read_only,
            guardrails=_load_guardrails(),
            allowed_tools=allowed_tools,
        )
        control_entries = _control_log_entries(
            [(origin, name) for origin, name in unmapped if origin != CONTROL_PLATFORM]
        )
        if unmapped:
            # Operator guardrails and the allowlist go to the ledger (entries
            # above) AND the logger. Platform denials (#2454/#2468) are Claude
            # harness built-ins OpenCode does not offer at all, so they are
            # satisfied vacuously: logged once per turn, not written to the
            # ledger eleven times per turn.
            logger.warning(
                "[OpenCode] controls not mapped onto permission (surfaced, not "
                "enforced): %s",
                ", ".join(_UNMAPPED_TEXT[o].format(tool=n) for o, n in unmapped),
            )

        # Playbooks → per-turn `command:` entries (checkpoint E, ruling T2). The
        # one entry both chat and headless reach, so `/name args` dispatches
        # the same way from either. Discovery never fails a turn: with no
        # commands every prompt is prose, which is what it was before.
        try:
            commands = opencode_commands.discover_playbook_commands(Path(_AGENT_HOME))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[OpenCode] playbook discovery failed (%s); no /commands", exc)
            commands = {}
        command, message = opencode_commands.split_playbook_prefix(prompt, commands)

        env_defaults: Dict[str, str] = {}
        turn_dir = _prepare_turn_config(
            execution_id, system_prompt, model, env_out=env_defaults,
            permission=permission, commands=commands,
        )
        cmd = self._build_command(
            model=model, session_id=session_id, directory=_AGENT_HOME,
            command=command,
        )
        # End-of-options separator, VERIFIED against v1.18.32 rather than
        # assumed: `opencode run -- "Reply with exactly the word ZEBRA."`
        # returned `ZEBRA`, so the text after `--` reaches the message and is
        # not diverted into yargs' `argv['--']`. Without it a prompt that
        # happens to begin with '-' ("-v, what changed?") is parsed as a flag
        # and the turn either errors or silently runs on an empty message.
        # A bare `/name` sends no message at all (`run.ts:420`, v1.18.32:
        # `--command` alone is a valid turn).
        if command is None:
            cmd += ["--", prompt]
        elif message:
            cmd += ["--", message]

        root = _opencode_root()
        env = build_execution_env({
            EXECUTION_TAG_NAME: execution_id,
            # Per-turn config carries this turn's `instructions:`; the other
            # three stay on the shared root so the model cache and the session
            # database survive across turns.
            "XDG_CONFIG_HOME": turn_dir,
            "XDG_DATA_HOME": os.path.join(root, "data"),
            "XDG_CACHE_HOME": os.path.join(root, "cache"),
            "XDG_STATE_HOME": os.path.join(root, "state"),
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
        })
        # `${VAR:-default}` placeholders from `.mcp.json` were rendered as
        # `{env:VAR}` (OpenCode has no default form — variable.ts, v1.18.32),
        # so the default is applied HERE, to the child env, only where the
        # var is unset or empty. `TRINITY_EXECUTION_ID` is already the
        # execution tag set above, so the Trinity header resolves without one.
        for name, default in env_defaults.items():
            if not env.get(name):
                env[name] = default

        metadata = ExecutionMetadata()
        metadata.context_window = self.get_context_window(model)
        metadata.execution_id = execution_id
        # The resolved `-m` IS the model that ran — OpenCode's stream does not
        # name it on every provider, so it is set here rather than parsed.
        metadata.model_name = model
        state = _OpenCodeParseState(
            execution_log=list(control_entries), metadata=metadata, model=model,
            execution_id=execution_id,
        )
        raw_messages: List[Dict] = []
        stderr_lines: List[str] = []

        registry = get_process_registry()
        logger.info(
            "[OpenCode] run model=%s session=%s read_only=%s permission=%s "
            "command=%s execution_id=%s",
            model or "(default)", bool(session_id), read_only,
            json.dumps(permission, sort_keys=True), command or "-", execution_id,
        )

        # stdin=DEVNULL: the prompt is a positional arg, so OpenCode must never
        # block on stdin. start_new_session isolates the process group so
        # cleanup signals only our descendants, never a sibling execution.
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
            env=env,
        )
        process_pgid = _capture_pgid(process)
        registry.register(
            execution_id, process, metadata={"type": "opencode", "pgid": process_pgid}
        )

        import threading

        # Set by the stdout reader on the first `step_start`; read by the
        # watchdog below. An Event, not a flag, because the two live on
        # different threads.
        step_seen = threading.Event()

        def read_stdout() -> None:
            try:
                for line in iter(process.stdout.readline, ""):
                    if not line:
                        break
                    try:
                        sanitized = sanitize_subprocess_line(line)
                        event = _parse_line(sanitized, state)
                        if event is not None:
                            event = sanitize_dict(event)
                            if event.get("type") == "step_start":
                                step_seen.set()
                            raw_messages.append(event)
                            try:
                                registry.publish_log_entry(execution_id, event)
                            except Exception as pub_err:  # noqa: BLE001
                                logger.debug(
                                    "[OpenCode] publish_log_entry failed (continuing): %s",
                                    pub_err,
                                )
                            _process_opencode_event(event, state)
                    except Exception as line_err:  # noqa: BLE001
                        logger.debug(
                            "[OpenCode] per-line processing error (continuing): %s",
                            line_err,
                        )
            except Exception as exc:  # noqa: BLE001
                logger.error("[OpenCode] error reading stdout: %s", exc)

        def read_stderr() -> None:
            try:
                for line in iter(process.stderr.readline, ""):
                    if not line:
                        break
                    stderr_lines.append(line)
            except Exception as exc:  # noqa: BLE001
                logger.error("[OpenCode] error reading stderr: %s", exc)

        stall_after = _stall_timeout_seconds(timeout_seconds)

        def wait_with_watchdog() -> int:
            """``process.wait``, plus: no ``step_start`` within the window is a
            stall, not a slow turn. Raises :class:`OpenCodeStallError`."""
            started = time.monotonic()
            while True:
                elapsed = time.monotonic() - started
                remaining = timeout_seconds - elapsed
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd, timeout_seconds)
                if step_seen.is_set():
                    # Progress proven — the ordinary turn timeout governs the
                    # rest, exactly as before.
                    return process.wait(timeout=remaining)
                try:
                    return process.wait(timeout=min(1.0, remaining))
                except subprocess.TimeoutExpired:
                    pass
                if not step_seen.is_set() and (time.monotonic() - started) > stall_after:
                    raise OpenCodeStallError(
                        f"OpenCode produced no output within {stall_after}s "
                        f"(no `step_start` event). Turn abandoned."
                    )

        def read_subprocess_output() -> Tuple[str, int]:
            stdout_thread = threading.Thread(target=read_stdout, daemon=True)
            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stdout_thread.start()
            stderr_thread.start()
            try:
                return_code = wait_with_watchdog()
            except OpenCodeStallError:
                logger.error(
                    "[OpenCode] execution %s stalled — no step_start within %ss; "
                    "killing group", execution_id, stall_after,
                )
                _terminate_process_group(
                    process, graceful_timeout=5, pgid=process_pgid,
                    execution_tag=execution_id,
                )
                _drain_bounded(
                    process, stdout_thread, stderr_thread, grace=3,
                    pgid=process_pgid, execution_tag=execution_id,
                )
                raise
            except subprocess.TimeoutExpired:
                logger.error(
                    "[OpenCode] execution %s timed out after %ss — killing group",
                    execution_id, timeout_seconds,
                )
                _terminate_process_group(
                    process, graceful_timeout=5, pgid=process_pgid,
                    execution_tag=execution_id,
                )
                _drain_bounded(
                    process, stdout_thread, stderr_thread, grace=3,
                    pgid=process_pgid, execution_tag=execution_id,
                )
                raise
            _drain_bounded(
                process, stdout_thread, stderr_thread, grace=5,
                pgid=process_pgid, execution_tag=execution_id,
            )
            stderr = "".join(stderr_lines)
            return (sanitize_text(stderr) if stderr else stderr), return_code

        # Chat is lock-serialized → the bounded single worker. /api/task runs
        # concurrently → the loop's default executor, so parallel readers don't
        # serialize behind one thread (parity with Claude/Codex).
        reader_executor = None if concurrent_reader else _executor

        loop = asyncio.get_event_loop()
        try:
            try:
                stderr_output, return_code = await asyncio.wait_for(
                    loop.run_in_executor(reader_executor, read_subprocess_output),
                    timeout=timeout_seconds + 60,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "[OpenCode] outer timeout on %s — killing group as last resort",
                    execution_id,
                )
                await loop.run_in_executor(
                    None,
                    lambda: _terminate_process_group(
                        process, graceful_timeout=2, pgid=process_pgid,
                        execution_tag=execution_id,
                    ),
                )
                await loop.run_in_executor(None, _safe_close_pipes, process)
                raise HTTPException(
                    status_code=504,
                    detail=f"OpenCode execution timed out after {timeout_seconds} seconds",
                )
            except OpenCodeStallError:
                # Deliberately NOT converted to an HTTPException here:
                # `_execute_with_stall_retry` retries a stall once on a fresh
                # session, and a 504 raised at this depth would skip that.
                raise
            except subprocess.TimeoutExpired:
                raise HTTPException(
                    status_code=504,
                    detail=f"OpenCode execution timed out after {timeout_seconds} seconds",
                )

            if return_code != 0:
                status_code, detail = classify_opencode_failure(
                    return_code, stderr_output, metadata
                )
                logger.error("[OpenCode] %s", detail)
                raise HTTPException(status_code=status_code, detail=detail)

            response_text, outcome = _finish_parse(state)

            if outcome in _FAILED_FINALS:
                # Exit code 0 is NOT proof of a completed turn (#520). A
                # provider that answers in `reasoning` with `content: ""` —
                # measured live with an open-weight model on an OpenAI-compatible
                # endpoint — reports success;
                # so does a stream that carried an `error` event, a step cut
                # at `length`, or a tool call emitted as prose.
                status_code = 500
                if outcome == FINAL_ERROR:
                    status_code = _classify_message(metadata.error_message or "") or 500
                detail = _failed_turn_detail(outcome, state, model)
                logger.error("[OpenCode] %s", detail)
                raise HTTPException(status_code=status_code, detail=detail)

            response_text = sanitize_text(response_text)
            metadata.status = "success"
            logger.info(
                "[OpenCode] done execution_id=%s outcome=%s cost=$%s tokens=%s/%s "
                "(reasoning=%s ⊂ output, cache r/w=%s/%s) tools=%s",
                execution_id, outcome, metadata.cost_usd, metadata.input_tokens,
                metadata.output_tokens, state.reasoning_tokens,
                metadata.cache_read_tokens, state.cache_write_tokens,
                metadata.tool_count,
            )
            return response_text, state.execution_log, metadata, raw_messages, state.session_id
        finally:
            _cleanup_turn_dir(turn_dir)
            registry.unregister(execution_id)

    async def _execute_with_stall_retry(self, **kwargs):
        """Relaunch a turn that never started, on a FRESH session, within the
        turn's own deadline.

        A stall is not the same failure as an overrun. The measured shape
        (#2981) is a process that reaches its provider and then
        produces nothing at all, while a later launch of the identical config
        completes.

        **Why more than one retry.** One host measured 16/16 completions,
        which suggested one retry was enough. A second host then measured the
        opposite on the SAME binary, flags, provider block and
        endpoint: 2 completions in 15 runs (~13% per attempt). Both stand —
        the variable is the host, and the cause is unknown. At 13%, one retry
        yields ~24%, i.e. an affected host 504s three turns in four, which is
        not shippable for unattended work; three attempts reach ~34% and the
        knob lets an affected host go higher without a rebuild. The honest
        framing: this raises the odds a turn survives a host that is already
        broken. It is mitigation, not a fix, and the empty-stdout-is-failure
        rule is NOT loosened to compensate — a turn that produced nothing is
        still a failure, however many times it was attempted.

        **The ceiling is the turn's own timeout**, not a retry budget bolted
        on top: each attempt is launched with the time actually remaining, and
        a retry is not started at all once less than ``_MIN_RETRY_BUDGET_SEC``
        is left — below the measured 59.3s worst-case silent window there is
        no room for an attempt to do anything but delay the 504.

        ``session_id`` is dropped on every retry deliberately: resuming into
        the session that wedged would reproduce the wedge, and a stalled turn
        contributed no messages worth keeping. The ``execution_id`` is REUSED
        — each attempt's ``finally`` already unregistered it — so the ledger
        still shows one execution, not a phantom pair.
        """
        execution_id = kwargs.get("execution_id")
        total_timeout = int(kwargs.get("timeout_seconds") or _DEFAULT_EXECUTION_TIMEOUT_SEC)
        max_retries = _stall_retry_limit()
        deadline = time.monotonic() + total_timeout

        attempt = 0
        while True:
            remaining = int(deadline - time.monotonic())
            call = dict(kwargs, timeout_seconds=max(1, remaining))
            if attempt:
                call["session_id"] = None
            try:
                result = await self._execute_opencode(**call)
            except OpenCodeStallError as stalled:
                attempt += 1
                remaining = int(deadline - time.monotonic())
                if attempt > max_retries or remaining < _MIN_RETRY_BUDGET_SEC:
                    reason = (
                        f"gave up after {attempt} stalled attempt(s)"
                        if attempt > max_retries
                        else f"gave up after {attempt} stalled attempt(s): only "
                             f"{max(0, remaining)}s of the {total_timeout}s turn "
                             f"budget left, below the {_MIN_RETRY_BUDGET_SEC}s floor "
                             f"a retry needs"
                    )
                    # Never a success: an empty-stdout exit is a failed turn.
                    raise HTTPException(
                        status_code=504,
                        detail=f"{stalled} {STALL_RETRY_MARKER} {reason}.",
                    )
                # One greppable marker, so an affected host is countable in the
                # logs instead of looking like slow-but-fine.
                logger.error(
                    "%s execution_id=%s attempt=%s/%s window=%ss remaining=%ss — "
                    "relaunching on a fresh session. %s",
                    STALL_RETRY_MARKER, execution_id, attempt + 1, max_retries + 1,
                    _stall_timeout_seconds(total_timeout), remaining, stalled,
                )
                continue

            if attempt:
                # Carried to the backend so the ledger distinguishes "fine" from
                # "recovered after N zero-byte launches".
                metadata = result[2]
                metadata.stall_retries = attempt
                logger.warning(
                    "%s execution_id=%s recovered after %s stalled attempt(s)",
                    STALL_RETRY_MARKER, execution_id, attempt,
                )
            return result

    # -- public interface ----------------------------------------------------
    async def execute(
        self,
        prompt: str,
        model: Optional[str] = None,
        continue_session: bool = False,
        stream: bool = False,
        system_prompt: Optional[str] = None,
        execution_id: Optional[str] = None,
    ) -> Tuple[str, List[ExecutionLogEntry], ExecutionMetadata, List[Dict]]:
        if not self.is_available():
            raise HTTPException(
                status_code=503,
                detail="OpenCode CLI is not available in this container",
            )

        resume_session_id: Optional[str] = None
        if continue_session and agent_state.session_started and self._chat_session_id:
            resume_session_id = self._chat_session_id
        else:
            agent_state.session_started = True
            self._chat_session_id = None

        guardrails = _load_guardrails()
        timeout_seconds = int(
            guardrails.get("execution_timeout_sec") or _DEFAULT_EXECUTION_TIMEOUT_SEC
        )

        try:
            response, log, metadata, raw, session_id = await self._execute_with_stall_retry(
                prompt=prompt,
                model=model,
                system_prompt=system_prompt,
                session_id=resume_session_id,
                timeout_seconds=timeout_seconds,
                execution_id=execution_id,
                concurrent_reader=False,
            )
        except (OpenCodeModelShapeError, OpenCodeProviderConfigError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except HTTPException:
            raise
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc))
        except (BrokenPipeError, ConnectionResetError) as pipe_err:
            logger.info("[OpenCode] subprocess pipe closed before completion: %s", pipe_err)
            raise HTTPException(
                status_code=502,
                detail="Agent subprocess closed before the chat could complete",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[OpenCode] execution error: %s", exc)
            raise HTTPException(status_code=500, detail=f"Execution error: {exc}")

        if session_id:
            self._chat_session_id = session_id
            agent_state.session_started = True

        if metadata.cost_usd:
            agent_state.session_total_cost += metadata.cost_usd
        agent_state.session_total_output_tokens += metadata.output_tokens
        if metadata.input_tokens > agent_state.session_context_tokens:
            agent_state.session_context_tokens = metadata.input_tokens
        agent_state.session_context_window = metadata.context_window
        return response, log, metadata, raw

    async def execute_headless(
        self,
        prompt: str,
        model: Optional[str] = None,
        allowed_tools: Optional[List[str]] = None,
        system_prompt: Optional[str] = None,
        timeout_seconds: int = 900,
        max_turns: Optional[int] = None,
        execution_id: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        persist_session: bool = False,
        images: Optional[List[Dict]] = None,
    ) -> Tuple[str, List[ExecutionLogEntry], ExecutionMetadata, Optional[str]]:
        logger.info(
            "[Headless Task] Using model: %s",
            model or f"{self.get_default_model()} (runtime default)",
        )
        if not self.is_available():
            raise HTTPException(
                status_code=503,
                detail="OpenCode CLI is not available in this container",
            )
        if images:
            logger.warning("[OpenCode] images are not supported — ignoring")
        if max_turns is not None:
            logger.info(
                "[OpenCode] max_turns=%s requested; `opencode run` has no turn-cap "
                "flag — relying on the %ss wall-clock timeout.",
                max_turns, timeout_seconds,
            )

        try:
            response, log, metadata, _raw, session_id = await self._execute_with_stall_retry(
                prompt=prompt,
                model=model,
                system_prompt=system_prompt,
                session_id=resume_session_id,
                timeout_seconds=timeout_seconds,
                execution_id=execution_id,
                concurrent_reader=True,
                allowed_tools=allowed_tools,
            )
        except (OpenCodeModelShapeError, OpenCodeProviderConfigError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except HTTPException:
            raise
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc))
        except (BrokenPipeError, ConnectionResetError) as pipe_err:
            # 502 (not 503) so an early child exit doesn't trip the SUB-003
            # auth-switch — parity with the Claude/Gemini/Codex headless paths.
            logger.info("[OpenCode] subprocess pipe closed before completion: %s", pipe_err)
            raise HTTPException(
                status_code=502,
                detail="Agent subprocess closed before task could complete",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[OpenCode] task execution error: %s", exc)
            raise HTTPException(status_code=500, detail=f"Task execution error: {exc}")

        return response, log, metadata, session_id


def _failed_turn_detail(
    outcome: str, state: _OpenCodeParseState, model: Optional[str]
) -> str:
    """The operator-facing reason an exit-0 turn is being failed.

    Names the outcome and the model, because every case here is a PROVIDER
    property (a model that answers in the wrong field, calls tools in prose,
    or a backend that 5xx'd mid-turn), not a Trinity fault — and the fix is to
    change the model or the provider block, which nobody can do from "the
    turn failed".
    """
    model_label = model or "<default>"
    last_text = "".join(state.step_texts[-1] if state.step_texts else []).strip()
    if outcome == FINAL_ERROR:
        message = state.metadata.error_message or "OpenCode error"
        shaped = _classify_message(message)
        if shaped == 503:
            # Same wording as the exit-code path: 503 is the backend's AUTH
            # signal, so the operator reads one message for one cause.
            return (
                f"OpenCode authentication failure (outcome={FINAL_ERROR}, "
                f"model={model_label}): {message[:300]}. "
                "Check the provider credentials injected into this agent."
            )
        if shaped == 429:
            return (
                f"OpenCode rate limit (outcome={FINAL_ERROR}, "
                f"model={model_label}): {message[:300]}"
            )
        return (
            f"OpenCode stream reported an error (outcome={FINAL_ERROR}, "
            f"model={model_label}): {message[:300]}"
        )
    if outcome == FINAL_TRUNCATED:
        return (
            f"OpenCode turn was cut off by the model's output limit "
            f"(outcome={FINAL_TRUNCATED}, step_finish.reason=length, "
            f"model={model_label}). The partial text is not an answer. "
            f"First 300 chars: {last_text[:300]!r}"
        )
    if outcome == FINAL_INCOMPLETE:
        return (
            f"OpenCode turn ended after a tool-calls step with no following "
            f"step (outcome={FINAL_INCOMPLETE}, model={model_label}): the "
            "tools ran but the model never produced the answer they were "
            "fetched for."
        )
    if outcome == FINAL_TEXT_TOOL_CALL:
        shape = text_tool_call_shape(last_text) or "tool-call"
        return (
            f"OpenCode returned a tool call as TEXT instead of calling the tool "
            f"(outcome={FINAL_TEXT_TOOL_CALL}, shape={shape}, model={model_label}). "
            "No tool ran, so this is a failed turn, not an answer: the model "
            "did not use the provider's native tool channel. First 300 chars: "
            f"{last_text[:300]!r}"
        )
    if outcome == FINAL_REASONING_ONLY:
        preview = "".join(state.reasoning_parts).strip()[:300]
        return (
            f"OpenCode returned reasoning but no assistant text "
            f"(model={model or '<default>'}). The turn is FAILED, not empty-but-ok: "
            "some models answer in a non-standard `reasoning` field while "
            "reporting success, and their reasoning is a scratchpad, not an "
            f"answer. First 300 chars of what was returned: {preview!r}"
        )
    return (
        f"OpenCode produced no output at all (model={model or '<default>'}): no "
        "assistant text, no reasoning and no tool calls, despite exiting 0."
    )


# Global singleton, mirroring claude/gemini/codex.
_opencode_runtime: Optional[OpenCodeRuntime] = None


def get_opencode_runtime() -> OpenCodeRuntime:
    global _opencode_runtime
    if _opencode_runtime is None:
        _opencode_runtime = OpenCodeRuntime()
    return _opencode_runtime
