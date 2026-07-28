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
                self._last_sync = datetime.utcnow()
                self._last_commit_sha = self._get_current_commit()
                self._list_cache = (None, [])
                result["commit_sha"] = self._last_commit_sha
                result["skill_count"] = len(self.list_skills())
                result["last_sync"] = self._last_sync.isoformat()

            return result

        except Exception as e:
            logger.error(f"Failed to sync skills library: {e}")
            return {
                "success": False,
                "error": str(e)
            }

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

        status = {
            "configured": bool(url),
            "url": url,
            "branch": branch,
            "cloned": self.library_path.exists(),
            "last_sync": self._last_sync.isoformat() if self._last_sync else None,
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

            # Append skills section
            content = content.rstrip() + skills_section

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
