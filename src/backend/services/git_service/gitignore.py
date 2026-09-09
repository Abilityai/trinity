"""The fleet gitignore machinery — pattern list, merge/append/rm-cached builders, the #2069 creation-time merge, and git-dir detection.

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

# #1028/#2529: the sweep REPORT lives next door; reached through the module
# object so a test patching it reaches every caller here.
from . import gitignore_sweep

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Conflict classification (S5 — operator-readable diagnosis, issue #386)
# ----------------------------------------------------------------------------


_TRINITY_AUTHORED_PATHS: Tuple[str, ...] = (
    ".trinity/pre-check",       # SCHED-COND-001 conditional-schedule hook (#454)
    # Template-authored output-contract validator. No platform executor runs it
    # today (compat check I-005 was retired in #2137 as gating on a fiction), but
    # the path stays: #2070 derives the `!` re-includes from this tuple, and 14
    # bundled templates already ship `!.trinity/post-check`, so removing it would
    # untrack an authored hook on the next push — the exact #2070 regression.
    ".trinity/post-check",
    ".trinity/pre-snapshot",    # data-snapshot quiesce hook (#1169)
    ".trinity/setup.sh",        # startup setup convention (trinity-enterprise#76)
    ".trinity/persistent-processes.allow",  # orphan-sweep allowlist patterns (#1501)
    ".trinity/brain-orb/",      # brain-orb convention hooks (#58/#60)
    ".trinity/pipelines/",      # agent-defined pipeline DEFINITIONS (#919);
                                # instance STATE lives in pipeline-state/
    # #1704: declared Claude Code plugin selection (marketplaces + installed).
    # COMMITTED — unlike persistent-state.yaml / data-paths.yaml (volume-local,
    # re-materialized at creation), this must survive a git-based reconstitution
    # onto a fresh volume or a new host, the gap #1704 closes. This entry alone
    # yields both the `!` re-include and the `git rm --cached` exemption, so the
    # manifest is committable while `.claude.json` and `.claude/plugins/` (#1705)
    # stay gitignored.
    ".trinity/plugins.yaml",
)

_GITIGNORE_PATTERNS: Tuple[str, ...] = (
    # Shell init / history (instance-specific)
    ".bash_logout",
    ".bashrc",
    ".profile",
    ".bash_history",
    ".sudo_as_admin_successful",
    # Credentials — NEVER COMMIT
    ".env",
    ".env.*",
    ".mcp.json",
    "credentials.json",
    "*.pem",
    "*.key",
    # Instance-specific directories
    ".cache/",
    ".local/",
    ".npm/",
    ".ssh/",
    # #2070: contents-only, so the authored paths below can be re-included.
    ".trinity/*",
    *(f"!{path}" for path in _TRINITY_AUTHORED_PATHS),
    ".tmp/",  # #1098 disk-backed scratch (TMPDIR); #1187 relocated CODEX_HOME
    ".trinity-clone-tmp/",  # #1439 transient full-history clone staging dir (removed post-merge; ignored so a crash-orphaned copy — incl. its PAT-bearing .git/config — is never committed)
    # Large generated content
    "content/",
    # #1596: bulk data / dependency / cache / index dirs that churn on every
    # run and bloat `.git` unboundedly under auto-sync. Git sync is for code +
    # state, not datasets/indexes/deps — those belong in `data_paths` (#1169) or
    # stay local. Merged into existing agents on sync, which also untracks any
    # already-committed matches (stops future churn; doesn't shrink history).
    # An agent that genuinely needs one committed can negate it in its own
    # `.gitignore` (e.g. `!keep.db`).
    "node_modules/",
    ".venv/",
    "venv/",
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".ipynb_checkpoints/",
    "*.sqlite",
    "*.sqlite3",
    "*.db",
    # Claude Code runtime — commit commands/skills/agents, exclude runtime data
    ".claude.json",
    ".claude.json.backup",
    ".claude/projects/",
    ".claude/statsig/",
    ".claude/todos/",
    ".claude/debug/",
    ".claude/sessions/",
    ".claude/shell-snapshots/",
    # Marketplace plugin caches (#1702): Claude Code copies each installed
    # plugin (skills/agents/hooks) into ~/.claude/plugins/cache/<plugin>@<ver>/.
    # Since HOME == the agent's repo root, that lands in the working tree and
    # the 15-min sync loop commits it (and every plugin update commits another
    # copy) — repo bloat, same class as #1596. Re-installable, so never in git.
    ".claude/plugins/",
    # #2036: container-only Claude Code config. The base image bakes
    # `~/.claude/settings.json` (docker/base-image/hooks/claude-settings.json)
    # registering the platform guardrail hooks by ABSOLUTE container path
    # (`/opt/trinity/hooks/*.py`). HOME == the repo root, so `git add -A` swept
    # it into the agent's GitHub repo — and any clone made outside the container
    # is then hard-bricked: a PreToolUse hook whose script is missing exits 2,
    # which is precisely Claude Code's "block this tool call" signal, so every
    # Bash/Edit/Write fails on a machine that has no `/opt/trinity`. Worse blast
    # radius than #462/#1596/#1702 — the leak breaks foreign clones rather than
    # merely bloating them. The rest are runtime state observed leaking in the
    # same commit (`backups/` alone was ~3,000 lines).
    #
    # ent#345 UPDATE: the platform no longer bakes this file — the guardrail
    # registration moved to root-owned `/etc/claude-code/managed-settings.json`,
    # out of the agent's write reach and out of the synced tree. The rule STAYS
    # load-bearing, for the two copies that can still exist: a legacy one on a
    # volume that predates ent#345 (removed by `startup.sh` only on an exact
    # content match, so an agent that never restarts still has it) and an
    # agent-authored one. Either still registers absolute `/opt/trinity` paths, so
    # committing either still bricks a foreign clone — the damage above, unchanged.
    #
    # Trade-off, stated: `.claude/settings.json` doubles as Claude Code's
    # PROJECT-level settings file, so a template can no longer commit one. The
    # original justification ("the baked file always exists and would collide") no
    # longer holds — nothing bakes it — but the rule survives on the leak argument
    # alone, and an agent that genuinely needs it keeps the #1596 escape hatch:
    # negate in its own `.gitignore` (`!.claude/settings.json`).
    # `settings.local.json` is already covered by the `*.local.json` rule below.
    ".claude/settings.json",
    ".claude/remote-settings.json",
    ".claude/policy-limits.json",
    ".claude/backups/",
    ".claude/.last-cleanup",
    # Temporary files
    "*.log",
    "*.tmp",
    ".DS_Store",
    # Local overrides
    "*.local.md",
    "*.local.json",
    # #2529: the two negations the agent guide's canonical fence has carried for
    # months while this constant did not — the direction
    # `test_doc_and_constant_in_sync` could not catch, because it asserted
    # `constant ⊆ doc` only. Both live in the PROTECTED floor below, so an agent
    # cannot lose `.env.example` (compat check F-004 requires it, and
    # `credential_requirements_service` reads it to tell users which credentials
    # to supply) to its own `.env.*`-shaped rule. Appended at the TAIL so this
    # constant's order is now EXACTLY the guide fence's order.
    "!.env.example",
    # Belt-and-braces: `.mcp.json` is an exact-name pattern, so this negation is
    # a no-op today. It is here because the guide promises it and because a
    # future `.mcp.json*` would silently take the template with it.
    "!.mcp.json.template",
)

_GITIGNORE_SUPERSEDED_LINES: Tuple[str, ...] = (
    ".trinity/",
    ".trinity",
)

AGENT_HOME_DIR = "/home/developer"

LEGACY_WORKSPACE_DIR = "/home/developer/workspace"

def _build_gitignore_append_command(git_dir: str, patterns) -> str:
    """Build a bash command that appends any missing ``patterns`` to
    ``{git_dir}/.gitignore`` without clobbering user-supplied rules.
    Idempotent — each pattern is gated by an exact-line ``grep -qxF`` check,
    so a second run is a no-op. Used by the per-agent data_paths append (#1169);
    the fleet-wide merge no longer appends at all (see
    ``_build_gitignore_merge_command``).

    #2529 interaction, stated because it looks wrong at first glance: appending
    to the END now lands the declared data paths BELOW the protected floor.
    Harmless — they are positive ignores over agent-declared data directories,
    so they shadow nothing the floor protects — and self-correcting, because
    they are not managed lines and the next merge carries them into the user
    region with everything else. Both writers dedupe by exact line, so neither
    order duplicates them (pinned by
    ``test_2529_gitignore_precedence.py::test_data_paths_append_lands_in_the_user_region_and_does_not_churn``).
    """
    parts = [f"cd {shlex.quote(git_dir)}", "touch .gitignore"]
    for p in patterns:
        q = shlex.quote(p)
        parts.append(f"(grep -qxF -- {q} .gitignore || echo {q} >> .gitignore)")
    script = " && ".join(parts)
    return f"bash -c {shlex.quote(script)}"


# ---------------------------------------------------------------------------
# #2529: TWO managed regions, not one.
# ---------------------------------------------------------------------------
#
# Both writers of an agent's `.gitignore` used to APPEND, and git is
# last-match-wins, so the canonical block landed BELOW every `!negation` the
# agent had written and silently reversed it — after which
# `_build_rm_cached_ignored_command` untracked the files those negations were
# protecting, inside an unrelated sync commit that named none of them (the
# 2026-07-30 `47efd80` incident; corbin repeated it on 2026-09-02 and left the
# comment "Negation must stay LAST in this file.").
#
# The merge is therefore a NORMALIZE-AND-REBUILD, not an append: every managed
# line is stripped wherever it appears, then the file is rewritten as
#
#     [DEFAULTS block]  <- markers below; the agent may override these
#     [user region]     <- the agent's own rules, ORIGINAL ORDER
#     [PROTECTED floor] <- markers below; the agent may NOT override these
#
# Idempotent by construction: a second run computes byte-identical output, and
# the file is written only when the computed content differs — so the 15-minute
# auto-sync loop has nothing to re-commit.
#
# WHY TWO REGIONS AND NOT ONE. A single hoisted block cannot carry both
# "defaults the agent may override" and "guarantees the agent may not", and both
# halves of that were measured, not assumed:
#
#   1. CREDENTIALS. Today a user's `!.env` with no `.env` line of its own is
#      INERT — the old merge appended `.env` BELOW the negation, so `.env` was
#      ignored. Hoisting a single block reverses that for every such agent in the
#      fleet at once, and the unattended `git add -A` commits the newly-tracked
#      credential file to the user's own GitHub repo. That is the #458 class on
#      the path nobody watches, so the six credential patterns and their two
#      canonical negations sit BELOW the user region.
#   2. PLATFORM-AUTHORED `.trinity/` PATHS. `_GITIGNORE_PATTERNS` derives eight
#      `!.trinity/...` re-includes from `_TRINITY_AUTHORED_PATHS` (#2070). Under a
#      single hoisted block a user `*.sh` below it beats `!.trinity/setup.sh` —
#      trinity-enterprise#76 / #1704 reintroduced, and QUIETLY: the rm-cached
#      pathspec still exempts the path, so nothing is untracked; the hook is
#      simply never `git add`-ed again and the absence surfaces one
#      reconstitution later.
#
# `_GITIGNORE_PROTECTED` is a MEMBERSHIP SET, and the region split is derived
# from it inside `_build_gitignore_merge_command` — not a second hand-written
# copy of the list — so adding an authored path stays one edit and the two
# cannot drift (#2070's principle).
#
# CHANGING A MARKER STRING is a fleet migration: the old text must be added to
# `_GITIGNORE_SUPERSEDED_LINES` first, or every existing agent keeps an orphaned
# marker line in its user region forever. Same discipline as the wholesale
# `.trinity/` line above.
_GITIGNORE_BLOCK_BEGIN = (
    "# >>> Trinity default ignore rules — managed; "
    "your own rules go BELOW and win (#2529) >>>"
)
_GITIGNORE_BLOCK_END = "# <<< Trinity default ignore rules <<<"
_GITIGNORE_FLOOR_BEGIN = (
    "# >>> Trinity protected rules — managed; NOT overridable "
    "(credentials + platform-authored paths, #2529) >>>"
)
_GITIGNORE_FLOOR_END = "# <<< Trinity protected rules <<<"

_GITIGNORE_MARKERS: Tuple[str, ...] = (
    _GITIGNORE_BLOCK_BEGIN,
    _GITIGNORE_BLOCK_END,
    _GITIGNORE_FLOOR_BEGIN,
    _GITIGNORE_FLOOR_END,
)

# Invariant #12 — credentials are never committed. These keep their canonical
# negations with them: `!.env.example` is REQUIRED by compat check F-004 and read
# by `credential_requirements_service`, so it must be un-ignorable by the agent
# AND un-ignorable by the platform's own `.env.*`.
_GITIGNORE_CREDENTIAL_PATTERNS: Tuple[str, ...] = (
    ".env",
    ".env.*",
    ".mcp.json",
    "credentials.json",
    "*.pem",
    "*.key",
    # `.ssh/` is here for the SAME reason `.env` is, and it is here because
    # review caught it MISSING. The hoist above is only safe for a default the
    # agent may genuinely override; `credential_paths.py` classifies
    # `.ssh/id_*` as secret material, so this is not one. Reproduced on the
    # live fleet shape (`!.ssh` above an appended canonical block): pre-merge
    # `.ssh/id_rsa` is ignored, post-merge it is NOT, and the unattended
    # 15-minute `git add -A` then commits a private key to the owner's GitHub
    # repo — Decision 1's own hazard, one directory over. Note this yields no
    # derived negation: nothing in `_TRINITY_AUTHORED_PATHS` lives under
    # `.ssh/`, and a `!.ssh/config` under a dir-form rule is inert anyway
    # (reported via `shadowed_negations`, not silently dropped).
    ".ssh/",
    "!.env.example",
    "!.mcp.json.template",
)

# NOTE `.trinity/operator-queue.json` is deliberately NOT here and deliberately
# NOT in `_TRINITY_AUTHORED_PATHS`: it is a live approval queue rewritten
# continuously, so committing it would put a diff in every 15-minute auto-sync
# cycle and push approval payloads into the user's repo. Same call #919 /
# Invariant #8 made for `.trinity/pipelines/` (definitions committed) vs
# `pipeline-state/` (state not). Reviewed in #2529, not forgotten.
_GITIGNORE_PROTECTED: frozenset = frozenset(
    (
        *_GITIGNORE_CREDENTIAL_PATTERNS,
        ".trinity/*",
        *(f"!{path}" for path in _TRINITY_AUTHORED_PATHS),
    )
)

# Every line this platform owns: stripped from the user region by the merge, and
# the oracle for "was this negation shadowed by US or by the agent's own rule?".
_GITIGNORE_MANAGED_LINES: Tuple[str, ...] = (
    *_GITIGNORE_PATTERNS,
    *_GITIGNORE_SUPERSEDED_LINES,
    *_GITIGNORE_MARKERS,
)

if not _GITIGNORE_PROTECTED <= set(_GITIGNORE_PATTERNS):
    raise RuntimeError(
        "_GITIGNORE_PROTECTED is not a subset of _GITIGNORE_PATTERNS: "
        f"{sorted(_GITIGNORE_PROTECTED - set(_GITIGNORE_PATTERNS))}. The floor is "
        "a PARTITION of the canonical list, not a second list — a pattern here "
        "that is not there would be written to the floor and never stripped."
    )

for _managed_line in _GITIGNORE_MANAGED_LINES:
    # `grep -vxF -f` is the strip mechanism. An EMPTY entry in that pattern file
    # matches every blank line with `-x` (silently deleting the user's spacing)
    # and every line without it (wiping the file); a newline-bearing entry
    # smuggles an extra pattern in. Fail at import, not in a container.
    if not _managed_line or "\n" in _managed_line or "\r" in _managed_line:
        raise RuntimeError(
            "managed .gitignore line is empty or carries a newline: "
            f"{_managed_line!r}"
        )
del _managed_line






def _build_gitignore_merge_command(git_dir: str) -> str:
    """Build a bash command that REBUILDS ``{git_dir}/.gitignore`` as

        [defaults block][user region, original order][protected floor]

    stripping every managed line wherever it currently appears (#2529). The two
    regions and the reason there are two of them are documented at
    ``_GITIGNORE_PROTECTED`` above; this is the only function that partitions
    ``_GITIGNORE_PATTERNS`` into them, and the only reader of that constant
    (pinned by ``test_2069_gitignore_merge_caller_guard.py``).

    Idempotent by CONTENT, not by absence of work: a second run computes
    byte-identical output and the ``cmp`` gate then leaves the file — and its
    mtime — untouched, so the 15-minute auto-sync loop has nothing to re-commit.

    Four shell details are load-bearing, each reproduced before it was fixed:

    * **The strip ``grep`` is a real command in the ``&&`` chain**, with its
      status inspected (``0`` = lines kept, ``1`` = the legitimate "file held
      only managed lines", ``>=2`` = a real error that aborts the chain and
      leaves the original file alone). The obvious spelling —
      ``cat <(grep ...) > tmp`` — takes only ``cat``'s status, so an unreadable
      ``.gitignore`` produced a block-only temp file and the ``mv`` then
      DESTROYED the user's rules. Reproduced on the shipped base image with a
      mode-000 ``.gitignore``: ``grep`` says "Permission denied", the chain
      exits 0, and ``mv`` succeeds because it needs DIRECTORY write permission,
      not file. ``|| true`` is exactly the wrong tool here — it masks the one
      status that matters.
    * **``LC_ALL=C grep -a``**. Without ``-a``, a ``.gitignore`` carrying a NUL
      byte (a crash-truncated file) makes grep print ``binary file matches`` and
      emit ZERO lines while exiting ZERO — so even the status check above passes
      and the whole user region is dropped.
    * **The strip list carries every line twice, bare and CR-suffixed**, so a
      CRLF copy of a canonical line is stripped instead of surviving below the
      block and still overriding it. Targeted, unlike ``tr -d '\r'``, which
      would rewrite the user's line endings wholesale.
    * **``[ -e .gitignore ] || : > .gitignore``**, not ``touch`` — ``touch``
      bumps mtime on every Push even when nothing changes.

    ``grep -vxF`` is whole-line and fixed-string, so no metacharacter in a user
    rule can be caught by accident — the same guarantee the #2070
    superseded-line removal already relied on. Empty and newline-bearing entries
    are rejected at import (see ``_GITIGNORE_MANAGED_LINES``), because an empty
    entry in a ``-f`` pattern file matches every blank line.

    ``.gitignore.tmp`` is written on every Push now rather than only when a
    superseded line exists. Benign: canonical ``*.tmp`` already matches it. The
    one residual is the very first merge on a pre-canonical repo, where ``*.tmp``
    is not yet in the file — one-time and milliseconds wide.

    The leading probe records the untracked-and-not-ignored set BEFORE the
    rebuild, so ``gitignore_sweep.GitignoreSweep.unignored`` can be computed without a fifth
    ``docker exec``. It is wrapped in a group ending in ``:`` so a repo-less
    directory (the init path calls this builder too) reports nothing instead of
    aborting the merge.
    """
    q = shlex.quote
    top = tuple(p for p in _GITIGNORE_PATTERNS if p not in _GITIGNORE_PROTECTED)
    floor = tuple(p for p in _GITIGNORE_PATTERNS if p in _GITIGNORE_PROTECTED)

    top_args = " ".join(
        q(line) for line in (_GITIGNORE_BLOCK_BEGIN, *top, _GITIGNORE_BLOCK_END)
    )
    floor_args = " ".join(
        q(line) for line in (_GITIGNORE_FLOOR_BEGIN, *floor, _GITIGNORE_FLOOR_END)
    )
    strip_args = " ".join(q(line) for line in _GITIGNORE_MANAGED_LINES)

    script = (
        f"cd {q(git_dir)} && "
        "{ git ls-files --others --exclude-standard 2>/dev/null | "
        f"head -n {gitignore_sweep._SWEEP_PROBE_LINE_CAP + 1} | "
        f"sed 's#^#{gitignore_sweep._SWEEP_TAG_BEFORE}#'; :; }} && "
        "{ [ -e .gitignore ] || : > .gitignore; } && "
        f"printf '%s\\n' {top_args} > .gitignore.tmp && "
        "{ LC_ALL=C grep -a -vxF -f "
        f"<(printf '%s\\n' {strip_args}; printf '%s\\r\\n' {strip_args}) "
        ".gitignore >> .gitignore.tmp || [ $? -eq 1 ]; } && "
        f"printf '%s\\n' {floor_args} >> .gitignore.tmp && "
        "if cmp -s .gitignore.tmp .gitignore; then rm -f .gitignore.tmp; "
        "else mv .gitignore.tmp .gitignore; fi"
    )
    return f"bash -c {shlex.quote(script)}"


def _build_rm_cached_ignored_command(git_dir: str) -> str:
    """Build a bash command that ``git rm --cached``s any tracked files that
    NOW match an ignore rule, and REPORTS what it did (#2529).

    Idempotent — `git ls-files -ci` returns the empty set after the first
    successful run.

    Two-pass: a non-NUL `git ls-files` to check emptiness via shell variable
    (bash can't hold NUL bytes), then a NUL-delimited pipe to xargs so paths
    with spaces or unicode survive the round-trip. Working-tree files are
    left alone; only the index is touched.

    Authored ``.trinity/`` content is exempt, and the exemption list is DERIVED
    from ``_TRINITY_AUTHORED_PATHS`` rather than written out here (#2070). It
    used to be two hardcoded strings, and each of the three incidents in this
    area was one more forgotten string: brain-orb hooks
    (trinity-enterprise#76), ``setup.sh`` (swept live before the second
    exemption was added), ``pre-check`` (#2070 — the SCHED-COND-001 hook the
    platform's own docs tell template authors to commit). Deriving it makes
    adding an authored path one edit instead of two, and the two cannot drift.

    Belt-and-braces: since #2070 the ignore rules themselves no longer match
    these paths, so ``git ls-files -ci`` should not list them at all. The
    pathspec stays for the agent whose ``.gitignore`` still carries the
    superseded wholesale ``.trinity/`` line — otherwise its hooks would be
    swept by the very push that repairs the file.

    THE THREE REPORT PROBES (#2529) ride on this same exec rather than costing
    two more `docker exec`s on the hot path of every Push:

    * ``removed`` — the sweep's own ``$ignored``, echoed before the ``xargs``.
      Nothing downstream could previously tell "5 files pushed" from "5 files
      pushed, 4 silent deletions", which is why two field incidents surfaced two
      months late.
    * ``untracked-after`` — differenced against the merge command's
      ``untracked-before`` to find paths this Push newly UN-ignored (an inverted
      user duplicate) and that the same Push's ``git add -A`` will now commit.
    * ``shadow`` — for every ``!`` line in the file, ask GIT which rule actually
      decides, via ``git check-ignore -v --no-index --stdin``. Never reimplement
      gitignore matching; a hand-rolled matcher would diverge from git and be a
      bug farm. The pipeline is assembled IN-CONTAINER by the shell (``grep |
      sed | git check-ignore``) and never interpolated from Python, so
      agent-controlled ``.gitignore`` content never reaches a ``bash -c`` payload
      backend-side — keep it that way if this is ever refactored.

    The three probe groups end in ``:`` so a report failure can never fail a
    Push; the SWEEP itself stays in the ``&&`` chain, so a real ``git rm``
    failure still aborts before the probes run.
    """
    q = shlex.quote
    exempt = " ".join(
        q(f":!{path.rstrip('/')}") for path in _TRINITY_AUTHORED_PATHS
    )
    script = (
        f"cd {q(git_dir)} && "
        f"ignored=$(git ls-files -ci --exclude-standard -- . {exempt}) && "
        'if [ -n "$ignored" ]; then '
        f"git ls-files -ci -z --exclude-standard -- . {exempt} | "
        "xargs -0 git rm --cached --quiet -r --; "
        "fi && "
        # AFTER the rm, never before: a failed `git rm` aborts the `&&` chain
        # here, so nothing is reported. Silence on a failed sweep is what the
        # code did before #2529 anyway; a `removed_paths` list — and an
        # operator-queue entry — naming files that are in fact still tracked
        # would be a false alarm on the one surface whose whole job is to be
        # trusted.
        '{ [ -z "$ignored" ] || { printf \'%s\\n\' "$ignored" | wc -l | tr -d " " | '
        f"sed 's#^#{gitignore_sweep._SWEEP_TAG_REMOVED_COUNT}#'; "
        'printf \'%s\\n\' "$ignored" | '
        f"head -n {gitignore_sweep._SWEEP_PROBE_LINE_CAP} | "
        f"sed 's#^#{gitignore_sweep._SWEEP_TAG_REMOVED}#'; }}; }} && "
        "{ git ls-files --others --exclude-standard | "
        f"head -n {gitignore_sweep._SWEEP_PROBE_LINE_CAP + 1} | "
        f"sed 's#^#{gitignore_sweep._SWEEP_TAG_AFTER}#'; :; }} && "
        # `^!.` (not `^!`) — a bare `!` line would reach check-ignore as an
        # empty pathspec and make it fatal.
        "{ LC_ALL=C grep -a -h '^!.' .gitignore | sed 's/^!//' | "
        "git check-ignore -v --no-index --stdin | "
        f"sed 's#^#{gitignore_sweep._SWEEP_TAG_SHADOW}#'; :; }}"
    )
    return f"bash -c {shlex.quote(script)}"










async def _git_toplevel(container_name: str) -> Optional[str]:
    """Ask git where this agent's repository is rooted (#2075).

    The probe starts at ``workspace/`` when that directory exists and at the
    home directory otherwise. ``rev-parse --show-toplevel`` walks **up**, so
    the nearest enclosing repository wins: a genuinely workspace-rooted legacy
    repo still answers ``/home/developer/workspace``, while a standard agent
    that merely keeps a populated non-git ``workspace/`` data directory answers
    ``/home/developer`` — the case the old content heuristic got wrong.

    ``safe.directory`` is relaxed for this read-only query only: the exec runs
    as ``developer``, but a volume restored with foreign ownership would
    otherwise make git refuse to answer and silently drop the caller onto the
    fallback heuristic that this function exists to replace.

    ``GIT_DISCOVERY_ACROSS_FILESYSTEM=1`` because the walk up has a second way to
    stop that has nothing to do with repositories (#2245): git halts discovery at
    a filesystem boundary by default, so on an agent whose ``workspace/`` is its
    own mount — a bind mount, a distinct volume, an overlay — a probe started
    inside it never reaches a repository rooted at the home directory. Git says so
    in as many words: *"Stopping at filesystem boundary
    (GIT_DISCOVERY_ACROSS_FILESYSTEM not set)"*, exit 128. This function would then
    return None and the caller would fall through to the content heuristic, which
    answers ``/home/developer/workspace`` for exactly that topology — the
    misclassification #2075 exists to eliminate, reintroduced by a mount.

    Crossing the boundary is safe here specifically because the containment check
    below is unchanged: discovery may walk out of the mount, but an answer outside
    the agent home is still refused before it is trusted. (Carried over from #2076,
    a competing #2075 fix closed as superseded — this flag was the one thing it had
    that #2077 did not.)

    Returns None when there is no repository at or above the probe point, or
    when git answers with a path outside the agent home (never trusted).
    """
    script = (
        f"start={shlex.quote(LEGACY_WORKSPACE_DIR)}; "
        f'[ -d "$start" ] || start={shlex.quote(AGENT_HOME_DIR)}; '
        "GIT_DISCOVERY_ACROSS_FILESYSTEM=1 "
        "git -c safe.directory='*' -C \"$start\" rev-parse --show-toplevel "
        "2>/dev/null"
    )
    result = await execute_command_in_container(
        container_name=container_name,
        command=f"bash -c {shlex.quote(script)}",
        timeout=5,
    )
    if result.get("exit_code") != 0:
        return None
    top = (result.get("output") or "").strip().splitlines()
    top = top[-1].strip() if top else ""
    if top == AGENT_HOME_DIR or top.startswith(f"{AGENT_HOME_DIR}/"):
        return top
    return None


async def _detect_git_dir_fallback(container_name: str) -> str:
    """Where to *create* a repo when the container has none yet.

    Verbatim the pre-#2075 content heuristic: any non-empty ``workspace/``
    means the repo goes there. ``initialize_git_in_container`` uses this to
    place a brand-new repo, so fresh-agent placement stays byte-compatible.
    """
    check_workspace = await execute_command_in_container(
        container_name=container_name,
        command=(
            'bash -c "[ -d /home/developer/workspace ] && '
            'find /home/developer/workspace -mindepth 1 -maxdepth 1 | '
            'head -1 | wc -l"'
        ),
        timeout=5,
    )
    workspace_has_content = (
        check_workspace.get("exit_code") == 0
        and "1" in check_workspace.get("output", "")
    )
    return "/home/developer/workspace" if workspace_has_content else "/home/developer"


async def _detect_git_dir(container_name: str) -> str:
    """Pick the directory git operations should run in for an agent container.

    Git's own answer wins (``_git_toplevel``). Only when the container has no
    repository at all does the legacy content heuristic decide — that path is
    reached by ``initialize_git_in_container``, which needs a placement for a
    repo that does not exist yet.
    """
    top = await _git_toplevel(container_name)
    if top:
        return top
    return await _detect_git_dir_fallback(container_name)


async def _migrate_workspace_gitignore(agent_name: str) -> gitignore_sweep.GitignoreSweep:
    """Idempotently rebuild an existing agent's `.gitignore` around the current
    `_GITIGNORE_PATTERNS` and untrack any files that NOW match a rule.

    Runs on every Push (#462) so existing agents adopt new patterns without
    requiring a re-init or container rebuild. Errors are logged and swallowed
    — a transient migration failure must not break an operator's Push.

    No-op if the container has no `.git` directory (agent not initialized for
    git sync).

    Returns what the sweep actually did (#2529). Two field incidents went two
    months unnoticed because this returned `None` and nothing downstream could
    distinguish "5 files pushed" from "5 files pushed, 4 silent deletions".
    EVERY failure path returns the empty sweep — reporting must never be able to
    break a Push, so a caller can read the fields unconditionally.

    Still exactly TWO execs on the happy path (plus the two pre-flight probes
    this function already ran): both report probes are folded into the merge and
    rm-cached commands rather than costing two more round-trips on the shared
    4-thread Docker executor.
    """
    container_name = f"agent-{agent_name}"
    try:
        git_dir = await _detect_git_dir(container_name)
        # Bail if not git-initialized — the agent's /api/git/sync will
        # return its own 400 in that case.
        check_git = await execute_command_in_container(
            container_name=container_name,
            command=f'bash -c "[ -d {shlex.quote(git_dir)}/.git ]"',
            timeout=5,
        )
        if check_git.get("exit_code") != 0:
            return gitignore_sweep.GitignoreSweep()
        # 1. Rebuild the file around the canonical regions (idempotent), and
        #    carry the untracked-BEFORE probe.
        merge = await execute_command_in_container(
            container_name=container_name,
            command=_build_gitignore_merge_command(git_dir),
            timeout=10,
        )
        # 2. Untrack any indexed files that now match an ignore rule, and carry
        #    the removed / untracked-AFTER / shadowed-negation probes.
        sweep = await execute_command_in_container(
            container_name=container_name,
            command=_build_rm_cached_ignored_command(git_dir),
            timeout=30,
        )
        return gitignore_sweep._parse_gitignore_sweep(merge.get("output"), sweep.get("output"))
    except Exception as exc:
        logger.warning(
            f"_migrate_workspace_gitignore failed for {agent_name}: {exc}. "
            "Push will proceed against the existing .gitignore."
        )
        return gitignore_sweep.GitignoreSweep()
