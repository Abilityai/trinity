"""
Skills Service - Git sync and skill management.

Manages the skills library:
- Sync from GitHub repository
- List available skills (with the frontmatter contract, ent#183)
- Get skill content
- Inject skills into running agents as FULL directory packages

Skills are stored in a GitHub repository with structure:
  .claude/skills/<name>/SKILL.md   (+ scripts/, templates, resources)

The local clone is stored at /data/skills-library/

Injection (ent#183) ships the whole skill directory: a vetted tar built from
`git archive` is POSTed to the EXISTING agent-server restore primitive
(`POST /api/agent-server/restore`, #384/#1169 — allowlist + traversal-guarded),
followed by manifest-based pruning of files a previous injection wrote that the
new package no longer carries. Pure packaging primitives live in
`services/skill_packaging.py`; this module owns orchestration only.
"""

import asyncio
import base64
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from database import db
from services.settings_service import get_skills_library_url, get_skills_library_branch, get_github_pat
from services.agent_client import get_agent_client, AgentClientError
from services import skill_packaging as pkg
from utils.url_validation import validate_skills_library_url

try:  # Redis is optional here: the injection lock fails open without it.
    import redis as _redis_lib
except Exception:  # noqa: BLE001 — import guard
    _redis_lib = None

logger = logging.getLogger(__name__)

# Local path for skills library clone
SKILLS_LIBRARY_PATH = Path("/data/skills-library")

# Route Redis URL resolution through config (#589/#645): a direct
# os.getenv here would bypass config.py's credential gate. Import-guarded so
# the module still loads in host-side unit tests without backend config.
try:
    from config import REDIS_URL as _REDIS_URL
except Exception:  # noqa: BLE001 — no config → lock fails open (see _acquire_inject_lock)
    _REDIS_URL = ""

# Issue #184 (UnderDefense pentest 3.3.1): override git's default
# User-Agent (`git/<version> (libcurl/...)`) on every HTTP-bearing git
# subcommand so outbound requests don't fingerprint the backend stack.
# The SSRF allowlist (#179) already locks the destination to github.com,
# but defense-in-depth: even if the allowlist is ever loosened, the UA
# stays generic. Constant intentionally has no version suffix to avoid
# yet another version string drifting against VERSION / package.json.
_GIT_HTTP_UA_ARGS = ["-c", "http.useragent=Trinity-Skills-Sync"]

# Restore POSTs carry whole skill packages — the 30s client default is for
# single-file writes; #1169 uses 300s for the same endpoint.
_RESTORE_TIMEOUT = 300.0
_EXEC_TIMEOUT = 30
# Must outlive the worst-case injection (N skills × restore + repair retry +
# prune deletes) — an expiring lock evaporates exactly in the slow case.
_INJECT_LOCK_TTL_SECONDS = 1800

# ent#236 — durable sync status. `_last_sync`/`_last_commit_sha` are per-PROCESS
# fields, and the backend runs `--workers 2`: the worker answering
# GET /skills/library/status is usually not the worker that synced, so the panel
# showed a stale timestamp and could NEVER show a sync error. These rows are the
# cross-worker truth; the in-memory fields stay as a fast path.
SKILLS_LAST_SYNC_KEY = "skills_library_last_sync"
SKILLS_LAST_STATUS_KEY = "skills_library_last_status"
SKILLS_LAST_ERROR_KEY = "skills_library_last_error"
SKILLS_LAST_COMMIT_KEY = "skills_library_last_commit"

# A stored error string is rendered in the admin Settings panel; git stderr is
# already PAT-scrubbed by the callers, but cap it so one pathological failure
# can't push a multi-KiB blob into system_settings.
_SYNC_ERROR_MAX_CHARS = 500

# Cross-worker sync lock (ent#236). Before scheduled sync existed, two
# concurrent `sync_library()` calls needed two admins clicking at the same
# instant; now the leader's timer fires on its own and can land on top of a
# manual click. Both run `git fetch` + `git reset --hard` against the SAME clone
# at `/data/skills-library`, and a reset landing between an injection's
# `git ls-tree` (version) and its `git archive` (content) hands an agent a
# package stamped with the wrong tree SHA.
_SYNC_LOCK_KEY = "skills:library:sync"
_SYNC_LOCK_TTL_SECONDS = 300


def _scrub_pat(text: str) -> str:
    """Replace credentials in any `https://<creds>@host` URL. Never raises."""
    try:
        return re.sub(r"https://[^@\s]+@", "https://***@", text)
    except Exception:  # noqa: BLE001
        return ""


# Host for the reconcile alarm's `agent_name` (the column is NOT NULL, no FK).
# Leading underscore is deliberate: `utils.helpers.sanitize_agent_name` strips
# leading non-alphanumerics, so this name is uncreatable through agent creation
# and can never collide with a real agent — whose owner would otherwise inherit
# the item's ACL and whose 5s queue-sync loop would start writing it into that
# agent's queue file (#1644 precedent).
RECONCILE_ALARM_AGENT_NAME = "_skills-sync"


def _reconcile_max_removals() -> int:
    """Blast-radius cap for start-path reconciliation (ent#236).

    A reconcile proposing more than this many removals for ONE agent refuses
    wholesale. A wiped/reset `agent_skills` table is indistinguishable from a
    legitimate mass-unassign, and keeping files is the fail-safe direction
    (#1638: the destructive default is the one that ships green).
    """
    raw = os.getenv("SKILLS_RECONCILE_MAX_REMOVALS", "")
    try:
        value = int(raw)
        return value if value > 0 else 10
    except (TypeError, ValueError):
        return 10


class SkillInjectionBusy(Exception):
    """Another injection is already running for this agent (Redis lock held)."""


class SkillService:
    """
    Service for managing skills library and skill assignments.

    The skills library is a GitHub repository containing skill directory
    packages in .claude/skills/<name>/ structure.
    """

    def __init__(self):
        self.library_path = SKILLS_LIBRARY_PATH
        self._last_sync: Optional[datetime] = None
        self._last_commit_sha: Optional[str] = None
        # list_skills metadata cache keyed by library commit SHA — walking every
        # skill dir + parsing frontmatter on each GET is sync filesystem work.
        self._list_cache: Tuple[Optional[str], List[Dict[str, Any]]] = (None, [])

    # =========================================================================
    # Library Sync Operations
    # =========================================================================

    def sync_library(self) -> Dict[str, Any]:
        """
        Sync the skills library from GitHub.

        Clones the repository if it doesn't exist, or pulls latest changes.
        Uses GitHub PAT for private repository access.

        Returns:
            Dict with sync status, commit info, and skill count
        """
        url = get_skills_library_url()
        if not url:
            return {
                "success": False,
                "error": "Skills library URL not configured",
                "hint": "Configure skills_library_url in Settings"
            }

        # Validate URL to prevent SSRF (SEC-179)
        try:
            url = validate_skills_library_url(url)
        except ValueError as e:
            logger.warning(f"Skills library URL validation failed: {e}")
            return {
                "success": False,
                "error": f"Invalid skills library URL: {e}"
            }

        # ent#236: serialize clone/pull across workers. Returned as `busy`, NOT
        # as a failure — a contended manual click must not overwrite the panel's
        # status with "Last sync failed" when nothing actually failed.
        sync_token = self._acquire_sync_lock()
        if sync_token is False:
            return {
                "success": False,
                "busy": True,
                "error": "A skills library sync is already running",
            }
        try:
            return self._sync_library_locked(url)
        finally:
            self._release_sync_lock(sync_token)

    def _acquire_sync_lock(self):
        """SETNX lock around the shared library clone.

        Returns a token when held, ``None`` when Redis is unavailable
        (fail-open — a single-worker dev box must still be able to sync), or
        ``False`` when another sync holds it.
        """
        if _redis_lib is None or not _REDIS_URL:
            return None
        import secrets
        token = secrets.token_hex(16)
        try:
            client = self._redis_client()
            if client.set(_SYNC_LOCK_KEY, token, nx=True, ex=_SYNC_LOCK_TTL_SECONDS):
                return token
            return False
        except Exception as e:  # noqa: BLE001 — redis down → proceed best-effort
            logger.warning(f"skills sync lock unavailable: {e}")
            return None

    def _release_sync_lock(self, token) -> None:
        """Check-and-delete: only the holder's token releases the lock."""
        if not token:
            return
        try:
            client = self._redis_client()
            if client.get(_SYNC_LOCK_KEY) == token:
                client.delete(_SYNC_LOCK_KEY)
        except Exception:  # noqa: BLE001
            pass

    def _sync_library_locked(self, url: str) -> Dict[str, Any]:
        """The git half of `sync_library`, run under the cross-worker lock.

        `url` arrives already SSRF-validated by the caller — re-resolving it
        here would re-run the check against settings that may have changed
        after the lock was taken.
        """
        branch = get_skills_library_branch()
        github_pat = get_github_pat()

        # Construct authenticated URL for private repos
        if github_pat and "github.com" in url:
            # Handle various URL formats
            if url.startswith("https://"):
                auth_url = url.replace("https://", f"https://{github_pat}@")
            elif url.startswith("github.com"):
                auth_url = f"https://{github_pat}@{url}"
            else:
                auth_url = f"https://{github_pat}@github.com/{url}"
        else:
            # Public repo or no PAT
            if not url.startswith("https://"):
                auth_url = f"https://github.com/{url}"
            else:
                auth_url = url

        # Log without exposing PAT
        safe_url = re.sub(r'https://[^@]+@', 'https://***@', auth_url)
        logger.info(f"Syncing skills library from {safe_url} (branch: {branch})")

        try:
            if self.library_path.exists():
                # Pull latest changes
                result = self._git_pull(branch)
            else:
                # Clone repository
                result = self._git_clone(auth_url, branch)

            if result["success"]:
                previous_commit = self._persisted_commit()
                self._last_sync = datetime.utcnow()
                self._last_commit_sha = self._get_current_commit()
                self._list_cache = (None, [])
                result["commit_sha"] = self._last_commit_sha
                result["skill_count"] = len(self.list_skills())
                result["last_sync"] = self._last_sync.isoformat()
                # ent#236: the fleet sweep fires only on a REAL update — a no-op
                # pull must not re-inject across every running agent.
                result["commit_changed"] = bool(
                    self._last_commit_sha and self._last_commit_sha != previous_commit
                )
                result["previous_commit_sha"] = previous_commit
                self._persist_sync_status(True, None, self._last_commit_sha)
            else:
                self._persist_sync_status(False, result.get("error"), None)

            return result

        except Exception as e:
            logger.error(f"Failed to sync skills library: {e}")
            self._persist_sync_status(False, str(e), None)
            return {
                "success": False,
                "error": str(e)
            }

    # -------------------------------------------------------------------------
    # Durable sync status (ent#236)
    # -------------------------------------------------------------------------

    def _persisted_commit(self) -> Optional[str]:
        """Last commit SHA this install synced, per the durable row.

        Read from the DB rather than `self._last_commit_sha` because the
        commit-changed comparison must hold across workers AND restarts — an
        in-memory None on a fresh process would read as "changed" and sweep the
        whole fleet on every backend restart.
        """
        try:
            return db.get_setting_value(SKILLS_LAST_COMMIT_KEY, None)
        except Exception:  # noqa: BLE001 — status is advisory, never blocks sync
            return None

    def _persist_sync_status(
        self, success: bool, error: Optional[str], commit_sha: Optional[str]
    ) -> None:
        """Record the outcome of one sync. Never raises.

        Written on BOTH branches: the AC is that a failure surfaces honestly in
        Settings, and a failure path that skips the write would leave the last
        SUCCESS on screen — the silent-failure mode this exists to remove.
        """
        try:
            db.set_setting(
                SKILLS_LAST_SYNC_KEY,
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
            db.set_setting(SKILLS_LAST_STATUS_KEY, "success" if success else "failed")
            # PAT scrub before this string becomes DURABLE, admin-rendered state.
            # `_git_clone`/`_git_pull` scrub their own stderr, but `sync_library`'s
            # outer `except Exception as e` passes a raw `str(e)` — and the
            # authenticated remote URL is an argument in the `subprocess.run`
            # command list, so an unexpected OSError can stringify the token
            # straight into `system_settings` and the Settings panel. Same idiom
            # the git helpers already use; canary G-04's lesson is that durable
            # operator-visible state is exactly where secrets must not land.
            db.set_setting(
                SKILLS_LAST_ERROR_KEY,
                "" if success else _scrub_pat(str(error or ""))[:_SYNC_ERROR_MAX_CHARS],
            )
            if commit_sha:
                db.set_setting(SKILLS_LAST_COMMIT_KEY, commit_sha)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"could not persist skills library sync status: {e}")

    def _git_clone(self, url: str, branch: str) -> Dict[str, Any]:
        """Clone the skills library repository."""
        self.library_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            "git", *_GIT_HTTP_UA_ARGS,
            "clone", "--branch", branch, "--depth", "1", url, str(self.library_path),
        ]

        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)
            logger.info(f"Cloned skills library to {self.library_path}")
            return {"success": True, "action": "cloned"}
        except subprocess.CalledProcessError as e:
            # Sanitize error output to remove PAT
            error_msg = re.sub(r'https://[^@]+@', 'https://***@', e.stderr or str(e))
            logger.error(f"Git clone failed: {error_msg}")
            return {"success": False, "error": f"Clone failed: {error_msg}"}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Clone timed out after 120 seconds"}

    def _git_pull(self, branch: str) -> Dict[str, Any]:
        """Pull latest changes from the skills library."""
        try:
            # Fetch and reset to remote branch
            subprocess.run(
                ["git", *_GIT_HTTP_UA_ARGS, "fetch", "origin", branch],
                cwd=self.library_path,
                check=True,
                capture_output=True,
                text=True,
                timeout=60
            )
            subprocess.run(
                ["git", "reset", "--hard", f"origin/{branch}"],
                cwd=self.library_path,
                check=True,
                capture_output=True,
                text=True,
                timeout=30
            )
            logger.info(f"Pulled latest skills library changes")
            return {"success": True, "action": "pulled"}
        except subprocess.CalledProcessError as e:
            error_msg = re.sub(r'https://[^@]+@', 'https://***@', e.stderr or str(e))
            logger.error(f"Git pull failed: {error_msg}")
            return {"success": False, "error": f"Pull failed: {error_msg}"}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Pull timed out"}

    def _get_current_commit(self) -> Optional[str]:
        """Get the current commit SHA."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.library_path,
                check=True,
                capture_output=True,
                text=True,
                timeout=10
            )
            return result.stdout.strip()[:12]  # Short SHA
        except Exception:
            return None

    # =========================================================================
    # Git package helpers (ent#183)
    # =========================================================================

    def _git_tree_shas(self) -> Dict[str, str]:
        """Per-skill git tree SHA — the package VERSION.

        Deterministic across clones/mtimes by construction (a content hash of
        the tar bytes would embed checkout mtimes and never match twice).
        One `git ls-tree -z` covers every skill; NUL-delimited so names are
        never shell-quoted by git.
        """
        try:
            result = subprocess.run(
                ["git", "ls-tree", "-z", "HEAD", ".claude/skills/"],
                cwd=self.library_path,
                check=True,
                capture_output=True,
                timeout=15,
            )
        except Exception as e:  # noqa: BLE001 — no git/no HEAD → no versions
            logger.warning(f"skills ls-tree failed: {e}")
            return {}
        shas: Dict[str, str] = {}
        for entry in result.stdout.decode("utf-8", errors="replace").split("\0"):
            if not entry.strip():
                continue
            # "<mode> <type> <sha>\t<path>"
            head, _, path = entry.partition("\t")
            fields = head.split()
            if len(fields) != 3 or fields[1] != "tree":
                continue
            shas[path.rsplit("/", 1)[-1]] = fields[2]
        return shas

    def _git_archive_skill(self, skill_name: str) -> Optional[bytes]:
        """`git archive HEAD -- .claude/skills/<name>` — the tar SOURCE.

        Reading from HEAD (not the working tree) is atomic against a
        concurrent admin sync's `git reset --hard`, so a mid-sync injection
        gets one consistent version instead of a mixed-tree tar.
        """
        try:
            result = subprocess.run(
                ["git", "archive", "--format=tar", "HEAD", "--",
                 f".claude/skills/{skill_name}"],
                cwd=self.library_path,
                check=True,
                capture_output=True,
                timeout=30,
            )
            return result.stdout
        except Exception as e:  # noqa: BLE001
            logger.warning(f"git archive failed for skill {skill_name}: {e}")
            return None

    # =========================================================================
    # Skill Discovery Operations
    # =========================================================================

    def list_skills(self) -> List[Dict[str, Any]]:
        """
        List all available skills from the library.

        Scans .claude/skills/*/SKILL.md files. Each entry carries the parsed
        frontmatter contract (ent#183): automation, user_invocable, requires,
        plus package metadata (multi_file, file_count, size_bytes, version).
        Cached per library commit SHA.
        """
        # Cache key is the CLONE's current commit, never the worker-local
        # _last_commit_sha: under --workers 2 another worker's sync moves the
        # shared clone while this worker's remembered SHA still matches its
        # own cache — it would serve a stale list indefinitely. One cheap
        # `git rev-parse` per call; the cache exists to skip the walk+parse.
        commit = self._get_current_commit()
        cached_commit, cached = self._list_cache
        if commit is not None and cached_commit == commit and cached:
            return cached

        skills = []
        skills_dir = self.library_path / ".claude" / "skills"

        if not skills_dir.exists():
            logger.debug(f"Skills directory not found: {skills_dir}")
            return skills

        tree_shas = self._git_tree_shas()
        for skill_path in sorted(skills_dir.iterdir()):
            if not skill_path.is_dir():
                continue
            skill_file = skill_path / "SKILL.md"
            if not skill_file.exists():
                continue
            info = self._parse_skill_info(skill_path.name, skill_file)
            info["version"] = tree_shas.get(skill_path.name)
            skills.append(info)

        skills = sorted(skills, key=lambda s: s["name"])
        if commit is not None:
            self._list_cache = (commit, skills)
        return skills

    def _skills_root(self) -> str:
        return os.path.realpath(str(self.library_path / ".claude" / "skills"))

    def _skill_dir(self, skill_name: str) -> Optional[Path]:
        """Resolve a library skill directory, or None if the name is unsafe.

        TWO independent guards, because skill names arrive from URL paths and
        from persisted assignments: the name regex (`validate_skill_name`) and
        a realpath containment check against the skills root. The containment
        check is the one that still holds if the regex is ever loosened — and
        it is the single chokepoint every path in this module derives from.
        """
        if not pkg.validate_skill_name(skill_name):
            return None
        root = self._skills_root()
        target = os.path.realpath(os.path.join(root, skill_name))
        if target != root and not target.startswith(root + os.sep):
            return None
        return Path(target)

    def _skill_files(self, skill_name: str) -> List[Dict[str, Any]]:
        """Walk a skill dir, litter-excluded — [{path, size, executable}]."""
        skill_dir = self._skill_dir(skill_name)
        files: List[Dict[str, Any]] = []
        if skill_dir is None or not skill_dir.is_dir():
            return files
        for candidate in sorted(skill_dir.rglob("*")):
            # lstat-order guards: never follow symlinks out of the skill dir.
            if candidate.is_symlink() or not candidate.is_file():
                continue
            rel = candidate.relative_to(skill_dir).as_posix()
            parts = tuple(rel.split("/"))
            if pkg._is_excluded(parts):
                continue
            stat = candidate.stat()
            files.append({
                "path": rel,
                "size": stat.st_size,
                "executable": bool(stat.st_mode & 0o111),
            })
        return files

    def _parse_skill_info(self, skill_name: str, skill_file: Path) -> Dict[str, Any]:
        """
        Parse skill information from SKILL.md + directory metadata.

        Extracts the frontmatter contract (hardened, tolerant — see
        skill_packaging) with a first-paragraph description fallback.
        """
        info: Dict[str, Any] = {
            "name": skill_name,
            "description": None,
            "path": f".claude/skills/{skill_name}/SKILL.md",
            "automation": None,
            "user_invocable": True,
            "allowed_tools": None,
            "requires": {"packages": [], "binaries": [], "env": []},
            "multi_file": False,
            "file_count": 0,
            "size_bytes": 0,
            "contract_warnings": [],
        }

        try:
            content = skill_file.read_text(errors="replace")
            frontmatter, fm_warning = pkg.parse_frontmatter(content)
            contract, warnings = pkg.extract_contract(frontmatter)
            if fm_warning:
                warnings = [fm_warning] + warnings
            info.update({
                "description": contract["description"],
                "automation": contract["automation"],
                "user_invocable": contract["user_invocable"],
                "allowed_tools": contract["allowed_tools"],
                "requires": contract["requires"],
                "contract_warnings": warnings,
            })

            # Fallback: first non-header paragraph
            if not info["description"]:
                for line in content.split("\n"):
                    line = line.strip()
                    if line and not line.startswith("#") and not line.startswith("---"):
                        info["description"] = line[:200]  # Truncate
                        break

            files = self._skill_files(skill_name)
            info["file_count"] = len(files)
            info["size_bytes"] = sum(f["size"] for f in files)
            info["multi_file"] = len(files) > 1

        except Exception as e:
            logger.warning(f"Failed to parse skill info for {skill_name}: {e}")

        return info

    def get_skill(self, skill_name: str) -> Optional[Dict[str, Any]]:
        """
        Get full details for a specific skill.

        Returns:
            Skill info dict with full content and file list, or None if not found
        """
        skill_dir = self._skill_dir(skill_name)
        if skill_dir is None:
            return None
        skill_file = skill_dir / "SKILL.md"

        if not skill_file.exists():
            return None

        try:
            content = skill_file.read_text(errors="replace")
            info = self._parse_skill_info(skill_name, skill_file)
            info["version"] = self._git_tree_shas().get(skill_name)
            info["content"] = content
            info["files"] = self._skill_files(skill_name)
            return info
        except Exception as e:
            logger.error(f"Failed to read skill {skill_name}: {e}")
            return None

    # =========================================================================
    # Library Status
    # =========================================================================

    def get_library_status(self) -> Dict[str, Any]:
        """
        Get the current status of the skills library.

        Returns:
            Dict with configuration status, sync info, and skill counts
        """
        url = get_skills_library_url()
        branch = get_skills_library_branch()

        # ent#236: the DURABLE row wins over the per-process field. Under
        # `--workers 2` the worker answering this request is usually not the one
        # that synced, and the auto-sync loop runs on a single leader worker —
        # reading memory first would report "never synced" from every follower.
        try:
            stored_sync = db.get_setting_value(SKILLS_LAST_SYNC_KEY, None)
            last_status = db.get_setting_value(SKILLS_LAST_STATUS_KEY, None)
            last_error = db.get_setting_value(SKILLS_LAST_ERROR_KEY, None) or None
        except Exception:  # noqa: BLE001 — status must never 500 the panel
            stored_sync = last_status = last_error = None

        status = {
            "configured": bool(url),
            "url": url,
            "branch": branch,
            "cloned": self.library_path.exists(),
            "last_sync": stored_sync
            or (self._last_sync.isoformat() if self._last_sync else None),
            "last_sync_status": last_status,
            "last_sync_error": last_error,
            "commit_sha": self._last_commit_sha or self._get_current_commit(),
            "skill_count": 0,
            "multi_file_count": 0,
        }

        if self.library_path.exists():
            skills = self.list_skills()
            status["skill_count"] = len(skills)
            status["multi_file_count"] = sum(1 for s in skills if s.get("multi_file"))

        return status

    # =========================================================================
    # Injection lock (cross-worker, fail-open — the compat_fix pattern)
    # =========================================================================

    def _redis_client(self):
        """One cached client per service instance — acquire runs per injection
        and must not churn a fresh connection pool each time."""
        if getattr(self, "_redis", None) is None:
            self._redis = _redis_lib.from_url(_REDIS_URL, decode_responses=True)
        return self._redis

    def _acquire_inject_lock(self, agent_name: str):
        """SETNX lock serialising injections (start × manual sync race) and the
        CLAUDE.md read-modify-write. Deliberately OUTSIDE the agent:* namespace
        (compat_fix precedent) — a short-TTL lock has no lifecycle to register
        in the #1560 keyspace registry. Redis down → fail open (None).

        TTL must exceed the worst-case injection (one restore attempt alone is
        up to _RESTORE_TIMEOUT, ×2 with the repair retry, × N skills) — a lock
        that expires mid-injection evaporates exactly in the slow case it
        exists for. Value is a random token so release can never delete a
        successor's lock after an expiry.
        """
        if _redis_lib is None or not _REDIS_URL:
            return None
        import secrets
        token = secrets.token_hex(16)
        try:
            client = self._redis_client()
            if client.set(
                f"skill_inject:{agent_name}", token, nx=True,
                ex=_INJECT_LOCK_TTL_SECONDS,
            ):
                return token
            raise SkillInjectionBusy(
                f"another skill injection is already running for {agent_name}"
            )
        except SkillInjectionBusy:
            raise
        except Exception as e:  # noqa: BLE001 — redis down → proceed best-effort
            logger.warning(f"skill inject lock unavailable for {agent_name}: {e}")
            return None

    def _release_inject_lock(self, token, agent_name: str) -> None:
        """Check-and-delete: only the holder's token releases the lock."""
        if token is None:
            return
        try:
            client = self._redis_client()
            key = f"skill_inject:{agent_name}"
            if client.get(key) == token:
                client.delete(key)
        except Exception:  # noqa: BLE001
            pass

    # =========================================================================
    # In-container helpers (injected-python — zero shell interpolation of
    # library-derived names; the compatibility-collector idiom)
    # =========================================================================

    async def _run_agent_python(self, agent_name: str, script: str) -> Optional[Any]:
        """Run a python3 script inside the agent via base64 injection and parse
        its stdout as JSON. Returns None on any failure (callers fail open)."""
        from services.docker_service import execute_command_in_container

        b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
        # b64 charset is [A-Za-z0-9+/=] — shell-safe inside the double quotes.
        command = f'bash -c "echo {b64} | base64 -d | python3 - 2>/dev/null"'
        try:
            result = await execute_command_in_container(
                container_name=f"agent-{agent_name}",
                command=command,
                timeout=_EXEC_TIMEOUT,
            )
            raw = (result.get("output") or "").strip()
            if result.get("exit_code") != 0 or not raw:
                return None
            return json.loads(raw)
        except Exception as e:  # noqa: BLE001 — probes are advisory
            logger.warning(f"skill exec helper failed for {agent_name}: {e}")
            return None

    async def _read_agent_skill_metas(
        self, agent_name: str, skill_names: List[str]
    ) -> Dict[str, Dict[str, Any]]:
        """Batch-read every .trinity-skill.json (+ dir existence) in ONE exec.

        Returns {name: {"exists": bool, "meta": dict|None}}; {} on failure
        (treated as no-meta → full inject, no prune — safe direction).
        """
        script = f"""
import json, os
names = {json.dumps(skill_names)}
out = {{}}
base = os.path.expanduser("~/.claude/skills")
for name in names:
    d = os.path.join(base, name)
    entry = {{"exists": os.path.isdir(d), "meta": None}}
    try:
        with open(os.path.join(d, {json.dumps(pkg.META_FILENAME)})) as fh:
            meta = json.load(fh)
        if isinstance(meta, dict):
            entry["meta"] = meta
    except Exception:
        pass
    out[name] = entry
print(json.dumps(out))
"""
        result = await self._run_agent_python(agent_name, script)
        return result if isinstance(result, dict) else {}

    async def _list_managed_skills(self, agent_name: str) -> Optional[List[str]]:
        """Names of skill dirs the PLATFORM manages, in ONE exec (ent#236).

        "Managed" == the directory carries a generated ``.trinity-skill.json``.
        That marker is the whole reconciliation model: it distinguishes a
        package this platform injected (and may therefore remove) from an
        agent-authored Playbook in the same directory (which it must never
        touch). Agent-authored skills have no meta and are invisible here.

        Returns None on ANY failure — the caller treats that as "unknown", and
        an unknown inventory must never be read as "nothing is assigned, remove
        everything". Names are re-validated backend-side before use: this list
        comes from the agent's own filesystem, i.e. untrusted input that later
        reaches path math.
        """
        script = f"""
import json, os
base = os.path.expanduser("~/.claude/skills")
meta_name = {json.dumps(pkg.META_FILENAME)}
out = []
try:
    for name in os.listdir(base):
        d = os.path.join(base, name)
        if os.path.isdir(d) and os.path.isfile(os.path.join(d, meta_name)):
            out.append(name)
except Exception:
    pass
print(json.dumps(sorted(out)))
"""
        result = await self._run_agent_python(agent_name, script)
        if not isinstance(result, list):
            return None
        return [n for n in result if isinstance(n, str) and pkg.validate_skill_name(n)]

    async def _probe_dependencies(
        self, agent_name: str, binaries: List[str], env_keys: List[str]
    ) -> Optional[Dict[str, Dict[str, bool]]]:
        """Declaration-only dep check (v1): binaries via shutil.which, env keys
        by NAME against process env ∪ .env key names (values never read).
        Names were regex-gated at contract extraction. None → probe unavailable.
        """
        if not binaries and not env_keys:
            return {"binaries": {}, "env": {}}
        script = f"""
import json, os, shutil
binaries = {json.dumps(sorted(set(binaries)))}
env_keys = {json.dumps(sorted(set(env_keys)))}
env_names = set(os.environ.keys())
try:
    with open(os.path.expanduser("~/.env")) as fh:
        for line in fh:
            key = line.split("=", 1)[0].strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            if key and not key.startswith("#"):
                env_names.add(key)
except Exception:
    pass
print(json.dumps({{
    "binaries": {{b: shutil.which(b) is not None for b in binaries}},
    "env": {{k: k in env_names for k in env_keys}},
}}))
"""
        result = await self._run_agent_python(agent_name, script)
        if not isinstance(result, dict):
            return None
        return result

    async def _finalize_injected_dirs(
        self,
        agent_name: str,
        skill_names: List[str],
        exec_paths: List[str],
        pruned_paths: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Post-restore finalization in ONE exec: chmod +x (git modes are
        dropped by restore's write_bytes), per-skill .gitignore lines,
        untracking already-committed matches, and reaping directories the prune
        just emptied (#1842).

        The gitignore step is load-bearing (strategy review F1): the 15-min git
        auto-sync deliberately COMMITS .claude/, so without per-name ignore
        lines every injected package lands in the agent's GitHub repo — the
        #1595/#1596 fleet bloat class. Only platform-injected names are
        ignored; agent-authored Playbooks keep committing.

        The empty-dir reap rides here rather than next to the per-file DELETEs
        because it needs to ASK the filesystem whether a directory is now empty
        — one in-agent exec answers that for every pruned path at once, where
        the remote file API would need a round trip per directory.
        """
        script = f"""
import json, os, stat, subprocess
names = {json.dumps(sorted(set(skill_names)))}
exec_paths = {json.dumps(sorted(set(exec_paths)))}
pruned_paths = {json.dumps(sorted(set(pruned_paths or [])))}
home = os.path.expanduser("~")
out = {{"chmod": 0, "gitignore": False, "untracked": False, "rmdir": 0, "errors": []}}
for rel in exec_paths:
    p = os.path.join(home, rel)
    try:
        os.chmod(p, stat.S_IMODE(os.stat(p).st_mode) | 0o111)
        out["chmod"] += 1
    except Exception:
        out["errors"].append("chmod:" + rel)
# #1842: compute_prune diffs MANIFESTS, and a manifest holds files, so dropping
# a package subdirectory deleted its files and left the directory standing. Climb
# from each pruned file toward the skill root, removing only what is already
# empty. `os.rmdir` IS the safety property: it refuses a directory holding
# anything, so runtime artifacts a skill's own scripts wrote (__pycache__,
# downloaded models) and agent-authored files keep the directory alive — the same
# "only remove what the platform wrote" guarantee compute_prune states. It also
# refuses a symlink (NotADirectoryError), so a doctored tree cannot redirect it.
for rel in pruned_paths:
    parts = rel.split("/")
    if len(parts) < 4 or parts[0] != ".claude" or parts[1] != "skills":
        continue  # prefix confinement, mirroring compute_prune's own guard
    if ".." in parts:
        # compute_prune already drops these, but the startswith() confinement
        # below is satisfied by `.claude/skills/x/../../..` — so without this the
        # LAST line of defense would be a prefix check that a traversal segment
        # walks straight through. Reject rather than rely on the caller.
        continue
    root = "/".join(parts[:3])
    d = os.path.dirname(rel)
    while d.startswith(root + "/"):
        try:
            os.rmdir(os.path.join(home, d))
            out["rmdir"] += 1
        except OSError:
            break  # non-empty, already gone, or not a directory — stop climbing
        d = os.path.dirname(d)
lines = [".claude/skills/%s/" % n for n in names]
gi = os.path.join(home, ".gitignore")
try:
    text = ""
    if os.path.exists(gi):
        with open(gi) as fh:
            text = fh.read()
    existing = set(text.splitlines())
    missing = [l for l in lines if l not in existing]
    if missing:
        with open(gi, "a") as fh:
            if text and not text.endswith("\\n"):
                fh.write("\\n")
            fh.write("\\n".join(missing) + "\\n")
    out["gitignore"] = True
except Exception:
    out["errors"].append("gitignore")
if os.path.isdir(os.path.join(home, ".git")) and names:
    try:
        proc = subprocess.run(
            ["git", "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--"]
            + [".claude/skills/%s" % n for n in names],
            cwd=home, capture_output=True, timeout=20,
        )
        if proc.returncode == 0:
            out["untracked"] = True
        else:
            out["errors"].append("untrack")
    except Exception:
        out["errors"].append("untrack")
print(json.dumps(out))
"""
        return await self._run_agent_python(agent_name, script)

    async def _finalize_removed_dirs(
        self, agent_name: str, skill_names: List[str], deleted_paths: List[str]
    ) -> Optional[Dict[str, Any]]:
        """Post-removal finalization in ONE exec (ent#236): reap the directories
        the removal emptied, then drop the skills' `.gitignore` lines.

        The mirror of `_finalize_injected_dirs`, and it must undo BOTH of that
        method's durable side effects. Leaving the ignore line behind means the
        agent's `.gitignore` accumulates one dead line per skill ever injected,
        and — worse — a *re-created* agent-authored skill of the same name would
        stay silently ignored by the 15-min auto-sync, so the agent's own work
        would stop being committed. `git rm --cached` is deliberately NOT undone:
        the files are gone, so there is nothing to re-track.

        `os.rmdir` is again the safety property (#1842): it refuses a non-empty
        directory, so runtime artifacts and agent-authored files inside a removed
        skill's directory keep that directory alive rather than being swept with
        it — the "platform removes only what it wrote" guarantee, applied to
        directories.
        """
        script = f"""
import json, os
names = {json.dumps(sorted(set(skill_names)))}
deleted = {json.dumps(sorted(set(deleted_paths)))}
home = os.path.expanduser("~")
out = {{"rmdir": 0, "gitignore": False, "errors": []}}
roots = set()
for rel in deleted:
    parts = rel.split("/")
    if len(parts) < 4 or parts[0] != ".claude" or parts[1] != "skills":
        continue  # prefix confinement, mirroring compute_prune's own guard
    if ".." in parts:
        continue  # a traversal segment walks straight through a prefix check
    root = "/".join(parts[:3])
    roots.add(root)
    d = os.path.dirname(rel)
    while d.startswith(root + "/"):
        try:
            os.rmdir(os.path.join(home, d))
            out["rmdir"] += 1
        except OSError:
            break  # non-empty, already gone, or not a directory — stop climbing
        d = os.path.dirname(d)
# The skill root itself, which the climb above deliberately stops short of.
for root in sorted(roots):
    try:
        os.rmdir(os.path.join(home, root))
        out["rmdir"] += 1
    except OSError:
        pass
gi = os.path.join(home, ".gitignore")
drop = set(".claude/skills/%s/" % n for n in names)
try:
    if os.path.exists(gi):
        with open(gi) as fh:
            lines = fh.read().splitlines()
        kept = [l for l in lines if l not in drop]
        if len(kept) != len(lines):
            with open(gi, "w") as fh:
                fh.write("\\n".join(kept) + ("\\n" if kept else ""))
    out["gitignore"] = True
except Exception:
    out["errors"].append("gitignore")
print(json.dumps(out))
"""
        return await self._run_agent_python(agent_name, script)

    # =========================================================================
    # Skill Injection (ent#183 — full directory packages)
    # =========================================================================

    async def inject_skills(
        self,
        agent_name: str,
        skill_names: Optional[List[str]] = None,
        force: bool = True,
    ) -> Dict[str, Any]:
        """
        Inject skills into a running agent as full directory packages.

        Args:
            agent_name: Name of the agent
            skill_names: Skill names to inject, or None to use assigned skills
            force: False (agent start) skips skills whose agent-side version
                   matches the library tree SHA; True (manual sync / REST)
                   re-injects unconditionally as a repair action.

        Returns:
            Dict with per-skill results:
            {success, skills_injected, skills_unchanged, skills_failed,
             results: {name: {success, status, files_written, error?, warnings}}}

        Raises:
            SkillInjectionBusy: another injection holds the per-agent lock.
        """
        if skill_names is None:
            skill_names = db.get_agent_skill_names(agent_name)

        if not skill_names:
            return {
                "success": True,
                "message": "No skills to inject",
                "skills_injected": 0,
                "skills_unchanged": 0,
                "skills_failed": 0,
                "results": {},
            }

        lock_token = self._acquire_inject_lock(agent_name)
        try:
            return await self._inject_skills_locked(agent_name, skill_names, force)
        finally:
            self._release_inject_lock(lock_token, agent_name)

    async def _inject_skills_locked(
        self, agent_name: str, skill_names: List[str], force: bool
    ) -> Dict[str, Any]:
        client = get_agent_client(agent_name)
        results: Dict[str, Dict[str, Any]] = {}
        commit_sha = self._last_commit_sha or self._get_current_commit()
        tree_shas = await asyncio.to_thread(self._git_tree_shas)
        # The bulk-assign PUT historically persisted arbitrary strings, so the
        # ONE name guard must run before any name reaches an in-container exec.
        valid_names = [n for n in skill_names if pkg.validate_skill_name(n)]
        agent_metas = await self._read_agent_skill_metas(agent_name, valid_names)

        total_cap = pkg.skills_total_max_bytes()
        per_skill_cap = pkg.skill_max_bytes()
        running_total = 0
        exec_paths: List[str] = []
        injected_names: List[str] = []
        # #1842: paths the prune deleted, so finalize can reap any directory
        # they leave empty.
        pruned_paths: List[str] = []
        contracts: Dict[str, Dict[str, Any]] = {}

        for skill_name in skill_names:
            warnings: List[str] = []

            if not pkg.validate_skill_name(skill_name):
                results[skill_name] = {
                    "success": False, "status": "failed", "files_written": 0,
                    "error": "invalid_skill_name", "warnings": [],
                }
                continue

            # Direct parse, not get_skill(): the batch tree_shas above already
            # covers versions — get_skill would fork one git subprocess per
            # skill per injection. Path still derives from the one sanitized
            # chokepoint.
            skill_dir = self._skill_dir(skill_name)
            skill_file = None if skill_dir is None else skill_dir / "SKILL.md"
            if skill_file is None or not skill_file.exists():
                results[skill_name] = {
                    "success": False, "status": "failed", "files_written": 0,
                    "error": "Skill not found in library", "warnings": [],
                }
                continue
            skill = self._parse_skill_info(skill_name, skill_file)
            contracts[skill_name] = skill
            warnings.extend(skill.get("contract_warnings") or [])

            tree_sha = tree_shas.get(skill_name)
            agent_entry = agent_metas.get(skill_name) or {}
            agent_meta = agent_entry.get("meta") if isinstance(agent_entry, dict) else None

            if (
                not force
                and tree_sha
                and isinstance(agent_meta, dict)
                and agent_meta.get("version") == tree_sha
            ):
                results[skill_name] = {
                    "success": True, "status": "unchanged",
                    "files_written": 0, "warnings": warnings,
                }
                continue

            archive = await asyncio.to_thread(self._git_archive_skill, skill_name)
            if archive:
                members, filter_warnings, total = pkg.filter_skill_archive(
                    archive, skill_name
                )
            else:
                members, filter_warnings, total = [], [], 0
            warnings.extend(filter_warnings)

            if not members:
                results[skill_name] = {
                    "success": False, "status": "failed", "files_written": 0,
                    "error": "skill package empty (no committed regular files)",
                    "warnings": warnings,
                }
                continue

            if total > per_skill_cap:
                results[skill_name] = {
                    "success": False, "status": "failed", "files_written": 0,
                    "error": f"skill_too_large: {total} bytes exceeds cap {per_skill_cap}",
                    "warnings": warnings,
                }
                continue
            if running_total + total > total_cap:
                results[skill_name] = {
                    "success": False, "status": "failed", "files_written": 0,
                    "error": f"skills_total_cap_exceeded: cap {total_cap} bytes",
                    "warnings": warnings,
                }
                continue
            running_total += total

            manifest = [arcname for arcname, _c, _m in members]
            meta = {
                "version": tree_sha,
                "commit": commit_sha,
                "manifest": manifest,
                "injected_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            tar_bytes = await asyncio.to_thread(
                pkg.build_injection_tar, skill_name, members, meta
            )

            outcome = await self._restore_skill(
                agent_name, client, skill_name, tar_bytes, members, warnings
            )
            if outcome["status"] in ("injected", "fallback"):
                injected_names.append(skill_name)
                if outcome["status"] == "injected":
                    # fallback wrote ONLY SKILL.md — queuing its executables
                    # would chmod nonexistent files and stamp spurious
                    # finalize_partial warnings on every old-image start.
                    exec_paths.extend(pkg.executable_paths(members))
                    # Prune only what a PREVIOUS injection wrote (manifest diff)
                    # — never runtime artifacts or agent-authored files.
                    if agent_entry.get("exists") and not isinstance(agent_meta, dict):
                        warnings.append("unmanaged_dir_overwritten")
                    else:
                        prev = (agent_meta or {}).get("manifest")
                        stale, truncated = pkg.compute_prune(prev, manifest, skill_name)
                        if truncated:
                            warnings.append("prune_truncated")
                        for path in stale:
                            deleted = await self._delete_agent_file(client, path)
                            if deleted:
                                pruned_paths.append(path)
                            else:
                                warnings.append(f"stale_delete_failed:{path}")
            results[skill_name] = outcome

        # Post-restore finalization: chmod + gitignore + untrack, one exec.
        if injected_names:
            finalize = await self._finalize_injected_dirs(
                agent_name, injected_names, exec_paths, pruned_paths
            )
            if not isinstance(finalize, dict):
                for name in injected_names:
                    results[name]["warnings"].append("gitignore_update_failed")
            elif finalize.get("errors"):
                for name in injected_names:
                    results[name]["warnings"].append(
                        "finalize_partial:" + ",".join(finalize["errors"])[:120]
                    )

        # Declaration-only dep check — runs for unchanged skills too, so
        # CLAUDE.md annotations stay fresh when the environment changes.
        await self._apply_dep_warnings(agent_name, contracts, results)

        success_statuses = {"injected", "fallback", "unchanged"}
        success_count = sum(
            1 for r in results.values() if r["status"] in ("injected", "fallback")
        )
        unchanged_count = sum(1 for r in results.values() if r["status"] == "unchanged")
        error_count = sum(1 for r in results.values() if r["status"] == "failed")

        # CLAUDE.md lists ALL assigned skills present on the agent — a
        # 1-of-19-changed start must not shrink the section to this pass's
        # writes (eng review #5).
        present = [n for n, r in results.items() if r["status"] in success_statuses]
        if present:
            await self._update_claude_md_skills_section(client, present, results)

        return {
            "success": error_count == 0,
            "skills_injected": success_count,
            "skills_unchanged": unchanged_count,
            "skills_failed": error_count,
            "results": results,
        }

    # =========================================================================
    # Skill Removal + start-path reconciliation (ent#236)
    # =========================================================================

    async def remove_skills(
        self, agent_name: str, skill_names: List[str]
    ) -> Dict[str, Any]:
        """Remove injected skill packages from a running agent (ent#236).

        The counterpart of :meth:`inject_skills`, and the reason unassigning
        finally means something: before this, an unassigned skill stayed on the
        agent forever.

        Removal deletes ONLY what a previous injection wrote — the paths in that
        skill's own `.trinity-skill.json` manifest — so agent-authored files and
        runtime artifacts inside the directory survive. A directory with no meta
        was never injected by the platform and is left completely alone.

        Takes the SAME per-agent lock as injection: both mutate
        `~/.claude/skills/` and both read-modify-write CLAUDE.md, so they must
        never interleave.

        Raises:
            SkillInjectionBusy: another injection/removal holds the lock.
        """
        names = sorted({n for n in skill_names if pkg.validate_skill_name(n)})
        if not names:
            return {
                "success": True, "skills_removed": 0, "skills_failed": 0, "results": {},
            }

        lock_token = self._acquire_inject_lock(agent_name)
        try:
            return await self._remove_skills_locked(agent_name, names)
        finally:
            self._release_inject_lock(lock_token, agent_name)

    async def _remove_skills_locked(
        self, agent_name: str, names: List[str]
    ) -> Dict[str, Any]:
        # Re-read assignments INSIDE the lock and refuse to remove anything
        # still assigned (#1445's check-then-write pattern). The bulk PUT
        # computes `previous - new` before it takes this lock, so two concurrent
        # PUTs can each hand us a name the other just re-assigned — without this
        # the agent silently loses a skill it is supposed to have until its next
        # start. It also makes the API safe against a caller that simply asks
        # for the wrong thing: an assigned skill is never removable.
        try:
            still_assigned = set(db.get_agent_skill_names(agent_name))
        except Exception:  # noqa: BLE001 — unreadable ⇒ trust the caller's list
            still_assigned = set()

        results: Dict[str, Dict[str, Any]] = {}
        skipped = [n for n in names if n in still_assigned]
        for name in skipped:
            results[name] = {
                "success": True, "status": "still_assigned",
                "files_deleted": 0, "warnings": ["removal_skipped:still_assigned"],
            }
        names = [n for n in names if n not in still_assigned]
        if not names:
            return {
                "success": True, "skills_removed": 0, "skills_failed": 0,
                "results": results,
            }

        client = get_agent_client(agent_name)
        metas = await self._read_agent_skill_metas(agent_name, names)

        if not metas:
            # The batch meta read returns {} on ANY exec failure, and the script
            # always emits one entry per requested name — so an empty dict means
            # "could not look", never "nothing is there". Reporting `not_present`
            # here would claim a removal that never happened; defer instead and
            # let the start-path reconcile finish the job.
            results.update({
                n: {
                    "success": False, "status": "deferred", "files_deleted": 0,
                    "warnings": ["removal_deferred:probe_unavailable"],
                }
                for n in names
            })
            return {
                "success": False,
                "skills_removed": 0,
                "skills_failed": len(names),
                "results": results,
            }

        deleted_paths: List[str] = []
        touched_names: List[str] = []
        # Only fully-removed skills get their `.gitignore` line stripped — see
        # the finalize call below.
        cleared_names: List[str] = []

        for name in names:
            entry = metas.get(name) or {}
            meta = entry.get("meta") if isinstance(entry, dict) else None

            if not entry.get("exists"):
                results[name] = {
                    "success": True, "status": "not_present",
                    "files_deleted": 0, "warnings": [],
                }
                continue

            if not isinstance(meta, dict):
                # No meta ⇒ the platform never wrote this directory (an
                # agent-authored Playbook of the same name, or the pre-#183
                # single-file era). `unmanaged_dir_overwritten` is injection's
                # matching concession: overwrite is recoverable, deletion is not.
                results[name] = {
                    "success": True, "status": "not_managed",
                    "files_deleted": 0, "warnings": ["unmanaged_dir_kept"],
                }
                continue

            paths, truncated = pkg.compute_removal(meta.get("manifest"), name)
            warnings: List[str] = ["removal_truncated"] if truncated else []
            meta_path = f".claude/skills/{name}/{pkg.META_FILENAME}"
            deleted = 0
            failed = 0
            for path in paths:
                # compute_removal puts the meta LAST precisely so this check is
                # possible: if any file delete failed, keeping the meta keeps the
                # package platform-managed and retryable. Dropping it here would
                # leave the survivors as unmanaged orphans no prune could reach —
                # the same reasoning that keeps it on truncation.
                if path == meta_path and failed:
                    warnings.append("removal_incomplete_meta_kept")
                    continue
                if await self._delete_agent_file(client, path):
                    deleted += 1
                    deleted_paths.append(path)
                else:
                    failed += 1
                    warnings.append(f"stale_delete_failed:{path}")

            touched_names.append(name)
            if failed == 0 and not truncated:
                cleared_names.append(name)
            results[name] = {
                "success": failed == 0,
                "status": "removed" if failed == 0 else "partial",
                "files_deleted": deleted,
                "warnings": warnings,
            }

        if deleted_paths or cleared_names:
            # The `.gitignore` line is stripped ONLY for skills whose package is
            # provably gone. Stripping it while injected files survive would let
            # the 15-min git auto-sync commit them into the agent's repo — the
            # #1595/#1596 bloat class the ignore line exists to prevent.
            finalize = await self._finalize_removed_dirs(
                agent_name, cleared_names, deleted_paths
            )
            if not isinstance(finalize, dict) or finalize.get("errors"):
                for name in cleared_names:
                    results[name]["warnings"].append("gitignore_cleanup_failed")

        # Rebuild the CLAUDE.md section from what's still assigned AND present.
        # Missing-dep annotations are not recomputed here (no injection ran, so
        # there is no fresh probe) — they come back on the next inject/start.
        if touched_names:
            await self._rebuild_claude_md_after_removal(agent_name, client, set(names))

        removed_count = sum(1 for r in results.values() if r["status"] == "removed")
        failed_count = sum(
            1 for r in results.values() if r["status"] in ("partial", "deferred")
        )
        return {
            "success": failed_count == 0,
            "skills_removed": removed_count,
            "skills_failed": failed_count,
            "results": results,
        }

    async def _rebuild_claude_md_after_removal(
        self, agent_name: str, client, just_removed: set
    ) -> None:
        """Re-render the Platform Skills section without the removed skills."""
        try:
            remaining = [
                n for n in db.get_agent_skill_names(agent_name)
                if n not in just_removed and pkg.validate_skill_name(n)
            ]
            present: List[str] = []
            if remaining:
                metas = await self._read_agent_skill_metas(agent_name, remaining)
                # An unreadable inventory must not shrink the section to nothing;
                # keep every still-assigned skill listed in that case.
                present = (
                    [n for n in remaining if (metas.get(n) or {}).get("exists")]
                    if metas else remaining
                )
            await self._update_claude_md_skills_section(client, present)
        except Exception as e:  # noqa: BLE001 — cosmetic, never fails a removal
            logger.warning(f"CLAUDE.md rebuild after removal failed for {agent_name}: {e}")

    async def reconcile_agent_skills(
        self, agent_name: str, assigned_names: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Remove platform-managed skills the agent is no longer assigned (ent#236).

        This is how a removal reaches an agent that was STOPPED when the
        unassign happened. The assignment row is already gone by then, so there
        is nothing to replay — the agent's own filesystem is diffed against the
        current assignment set instead. Reconciliation (not a tombstone table)
        also means every removal route converges here: single DELETE, bulk-PUT
        shrink, even a direct DB edit.

        Runs on the start path AFTER injection, and deliberately also when ZERO
        skills are assigned — that is exactly the "unassigned the last skill"
        case, which an early return would strand forever.

        Never raises: reconciliation is best-effort cleanup and must not be able
        to fail an agent start.
        """
        try:
            if assigned_names is None:
                assigned_names = db.get_agent_skill_names(agent_name)
            assigned = {n for n in assigned_names if pkg.validate_skill_name(n)}

            managed = await self._list_managed_skills(agent_name)
            if managed is None:
                return {"status": "skipped", "reason": "inventory_unavailable"}

            orphans = sorted(set(managed) - assigned)
            if not orphans:
                return {"status": "clean", "removed": 0}

            cap = _reconcile_max_removals()
            if len(orphans) > cap:
                # Blast-radius refusal (#1638/#1644 discipline). A wiped or
                # reset `agent_skills` table looks EXACTLY like a legitimate
                # mass-unassign from here, and one of those two readings deletes
                # every skill on every agent in the fleet on the next restart.
                # Keeping files is the recoverable direction; the operator can
                # unassign in smaller batches or raise the cap deliberately.
                self._announce_reconcile_refusal(agent_name, len(orphans), cap)
                return {
                    "status": "refused", "removed": 0,
                    "orphans": len(orphans), "cap": cap,
                }

            result = await self.remove_skills(agent_name, orphans)
            await self._audit_removal(
                agent_name, orphans, result, trigger="reconcile"
            )
            return {
                "status": "reconciled",
                "removed": result.get("skills_removed", 0),
                "failed": result.get("skills_failed", 0),
                "skills": orphans,
                "results": result.get("results", {}),
            }
        except SkillInjectionBusy:
            return {"status": "skipped", "reason": "injection_already_running"}
        except Exception as e:  # noqa: BLE001 — never fail an agent start
            logger.warning(f"skill reconcile failed for {agent_name}: {e}")
            return {"status": "skipped", "reason": "error"}

    def _announce_reconcile_refusal(
        self, agent_name: str, orphan_count: int, cap: int
    ) -> None:
        """ERROR + operator alarm for a refused reconcile. Never raises.

        Carries COUNTS ONLY — never the skill names. The alarm lands in durable,
        operator-visible state, and G-04's lesson is that the "show them what
        would be deleted" instinct is how that state starts carrying content it
        shouldn't. The names are already available via the skills API.
        """
        message = (
            f"[Skills] REFUSED reconcile for agent '{agent_name}': {orphan_count} "
            f"orphaned skill package(s) exceeds the per-agent cap of {cap}. Nothing "
            f"was removed. Verify the agent's assignments are intact, then unassign "
            f"in smaller batches or raise SKILLS_RECONCILE_MAX_REMOVALS (ent#236)."
        )
        logger.error(message)
        try:
            db.create_operator_queue_item(
                RECONCILE_ALARM_AGENT_NAME,
                {
                    "id": f"skills-reconcile-{agent_name}-{orphan_count}",
                    "type": "alert",
                    "priority": "high",
                    "title": f"Skill reconcile refused: {agent_name}",
                    "question": message,
                    "context": {
                        "alert_type": "skills_reconcile_blast_radius",
                        "agent_name": agent_name,
                        "orphan_count": orphan_count,
                        "cap": cap,
                    },
                    # Must stay NULL: `mark_operator_queue_expired` flips any
                    # pending row past `expires_at` to `expired` every 5s.
                    "expires_at": None,
                },
            )
        except Exception as e:  # noqa: BLE001 — the alarm is decorative
            logger.warning(f"could not raise skills reconcile alarm: {e}")

    @staticmethod
    async def _audit_removal(
        agent_name: str, skill_names: List[str], result: Dict[str, Any], trigger: str
    ) -> None:
        """SEC-001 audit for a skill removal. Never raises."""
        try:
            from services.platform_audit_service import (
                platform_audit_service, AuditEventType,
            )
            await platform_audit_service.log(
                event_type=AuditEventType.CONFIGURATION,
                event_action="skill_removed",
                source="system" if trigger == "reconcile" else "api",
                target_type="agent",
                target_id=agent_name,
                details={
                    "trigger": trigger,
                    "skills": sorted(skill_names)[:50],
                    "removed": result.get("skills_removed", 0),
                    "failed": result.get("skills_failed", 0),
                },
            )
        except Exception:  # noqa: BLE001
            pass

    async def _restore_skill(
        self,
        agent_name: str,
        client,
        skill_name: str,
        tar_bytes: bytes,
        members: List[Tuple[str, bytes, int]],
        warnings: List[str],
    ) -> Dict[str, Any]:
        """POST one skill package to the agent restore primitive.

        404 (pre-#384 image) → legacy single-file SKILL.md fallback.
        Other failure → ONE repair retry as delete-dir + re-restore (a
        dir→file type transition wedges restore forever otherwise, because
        restore always fails before prune could remove the dir).
        """
        resp = await self._post_restore(agent_name, skill_name, tar_bytes)

        if resp is not None and resp.status_code == 404:
            return await self._legacy_fallback(client, skill_name, members, warnings)

        if resp is None:
            # Transport failure (timeout / connection drop) — the agent may be
            # slow-but-alive with a WORKING skill install. Deleting the dir
            # here could destroy state the retry then fails to replace; only
            # an HTTP-level error (the dir→file wedge class) warrants repair.
            return {
                "success": False, "status": "failed", "files_written": 0,
                "error": "restore failed (transport)", "warnings": warnings,
            }

        if resp.status_code != 200:
            logger.warning(
                f"skill restore failed for {skill_name} on {agent_name} "
                f"({resp.status_code}); attempting delete-dir repair"
            )
            await self._delete_agent_file(client, f".claude/skills/{skill_name}")
            resp = await self._post_restore(agent_name, skill_name, tar_bytes)
            if resp is None or resp.status_code != 200:
                error = "restore failed" if resp is None else (
                    f"restore failed ({resp.status_code}): {resp.text[:300]}"
                )
                return {
                    "success": False, "status": "failed", "files_written": 0,
                    "error": error, "warnings": warnings,
                }
            warnings.append("repair_reinjected")

        try:
            body = resp.json()
        except ValueError:
            body = {}
        restored = body.get("restored") or []
        # Honest write accounting: files_written = what the agent CONFIRMED
        # restoring; anything we sent that didn't land is a named warning.
        sent = {arcname for arcname, _c, _m in members}
        sent.add(f".claude/skills/{skill_name}/{pkg.META_FILENAME}")
        for missing in sorted(sent - set(restored)):
            warnings.append(f"restore_skipped:{missing}")
        return {
            "success": True, "status": "injected",
            "files_written": len(restored), "warnings": warnings,
        }

    async def _post_restore(self, agent_name: str, skill_name: str, tar_bytes: bytes):
        """One multipart POST to /api/agent-server/restore (the #1169 shape)."""
        from services.agent_service.helpers import agent_http_request

        try:
            return await agent_http_request(
                agent_name,
                "POST",
                "/api/agent-server/restore",
                timeout=_RESTORE_TIMEOUT,
                files={"tarball": ("skill.tar", tar_bytes, "application/x-tar")},
                data={"paths": json.dumps([f".claude/skills/{skill_name}/**"])},
            )
        except Exception as e:  # noqa: BLE001 — transport errors → caller retries/repairs
            logger.warning(f"skill restore POST failed for {agent_name}: {e}")
            return None

    async def _legacy_fallback(
        self, client, skill_name: str, members: List[Tuple[str, bytes, int]],
        warnings: List[str],
    ) -> Dict[str, Any]:
        """Pre-#384 image: write SKILL.md only, exactly today's behavior."""
        skill_md = next(
            (
                content for arcname, content, _m in members
                if arcname == f".claude/skills/{skill_name}/SKILL.md"
            ),
            None,
        )
        if skill_md is None:
            return {
                "success": False, "status": "failed", "files_written": 0,
                "error": "SKILL.md missing from package", "warnings": warnings,
            }
        if len(members) > 1:
            warnings.append("multi_file_dropped_old_image")
        try:
            result = await client.write_file(
                f".claude/skills/{skill_name}/SKILL.md",
                skill_md.decode("utf-8", errors="replace"),
            )
        except AgentClientError as e:
            return {
                "success": False, "status": "failed", "files_written": 0,
                "error": str(e), "warnings": warnings,
            }
        if not result.get("success"):
            return {
                "success": False, "status": "failed", "files_written": 0,
                "error": result.get("error", "Write failed"), "warnings": warnings,
            }
        return {
            "success": True, "status": "fallback", "files_written": 1,
            "warnings": warnings,
        }

    @staticmethod
    async def _delete_agent_file(client, path: str) -> bool:
        """DELETE one path via the agent server (protected-path guard applies)."""
        try:
            import urllib.parse
            encoded = urllib.parse.quote(path, safe='')
            resp = await client.delete(f"/api/files?path={encoded}")
            return resp.status_code in (200, 404)
        except Exception:  # noqa: BLE001
            return False

    async def _apply_dep_warnings(
        self,
        agent_name: str,
        contracts: Dict[str, Dict[str, Any]],
        results: Dict[str, Dict[str, Any]],
    ) -> None:
        """Fold declaration-only dep-check outcomes into per-skill warnings."""
        binaries: List[str] = []
        env_keys: List[str] = []
        for skill in contracts.values():
            requires = skill.get("requires") or {}
            binaries.extend(requires.get("binaries") or [])
            env_keys.extend(requires.get("env") or [])

        probe = None
        if binaries or env_keys:
            probe = await self._probe_dependencies(agent_name, binaries, env_keys)

        for name, skill in contracts.items():
            result = results.get(name)
            if result is None or result["status"] == "failed":
                continue
            requires = skill.get("requires") or {}
            if requires.get("packages"):
                result["warnings"].append("packages_not_checked")
            if not (requires.get("binaries") or requires.get("env")):
                continue
            if probe is None:
                result["warnings"].append("dep_check_skipped")
                continue
            for binary in requires.get("binaries") or []:
                if probe.get("binaries", {}).get(binary) is False:
                    result["warnings"].append(f"missing_binary:{binary}")
            for key in requires.get("env") or []:
                if probe.get("env", {}).get(key) is False:
                    result["warnings"].append(f"missing_env:{key}")

    async def _update_claude_md_skills_section(
        self,
        client,
        skill_names: List[str],
        results: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        """
        Update CLAUDE.md with a Platform Skills section.

        This tells the agent what skills it has available so it can
        answer questions like "what skills do you have?" Missing-dep warnings
        are annotated so the agent knows a skill may be degraded.

        An EMPTY `skill_names` removes the section outright (ent#236): after the
        last skill is unassigned the agent must stop advertising slash commands
        it no longer has — rendering an empty bullet list would leave it telling
        Claude to invoke `/skill-name`.
        """
        try:
            # Read current CLAUDE.md (path is relative to /home/developer)
            result = await client.read_file("CLAUDE.md")

            if not result.get("success"):
                logger.warning(f"Could not read CLAUDE.md: {result.get('error')}")
                return

            content = result.get("content") or ""

            lines = []
            for skill in sorted(skill_names):
                entry = f"- `/{skill}` - Use with /{skill} command"
                skill_result = (results or {}).get(skill) or {}
                missing = [
                    w.split(":", 1)[1]
                    for w in skill_result.get("warnings", [])
                    if w.startswith(("missing_binary:", "missing_env:"))
                ]
                if missing:
                    entry += f" — ⚠ missing: {', '.join(sorted(set(missing)))}"
                lines.append(entry)
            skills_list = "\n".join(lines)
            skills_section = f"""

## Platform Skills

This agent has the following skills installed in `~/.claude/skills/`:

{skills_list}

Use these skills by invoking their slash commands (e.g., `/{sorted(skill_names)[0] if skill_names else 'skill-name'}`).
"""

            # Remove existing Platform Skills section if present — anchored on
            # a line boundary so `### Platform Skills` never false-matches.
            marker = re.search(r"(?m)^## Platform Skills\s*$", content)
            if marker:
                start_idx = marker.start()
                rest = content[marker.end():]
                next_section = re.search(r"(?m)^## ", rest)
                if next_section:
                    content = content[:start_idx].rstrip() + "\n\n" + rest[next_section.start():]
                else:
                    content = content[:start_idx].rstrip()

            # Append skills section — unless there is nothing left to advertise,
            # in which case the strip above IS the whole update (ent#236).
            if skill_names:
                content = content.rstrip() + skills_section
            else:
                content = content.rstrip() + "\n"

            # Write back (path is relative to /home/developer)
            write_result = await client.write_file("CLAUDE.md", content)
            if write_result.get("success"):
                logger.info(f"Updated CLAUDE.md with {len(skill_names)} skills")
            else:
                logger.warning(f"Failed to update CLAUDE.md: {write_result.get('error')}")

        except Exception as e:
            logger.warning(f"Failed to update CLAUDE.md with skills: {e}")


# Global service instance
skill_service = SkillService()
