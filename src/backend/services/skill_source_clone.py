"""One skills-library source's local clone (ent#237).

Extracted from ``skill_service`` when the library went multi-source: that
module now orchestrates N of these, one per ``skill_sources`` row, each in its
own subdirectory of ``/data/skills-library/``. Everything here is scoped to a
single clone and knows nothing about precedence or merging.

The one behavioural addition over the old single-clone path is **tag pinning**
(ent#237 AC#5). Skills carry executable ``scripts/`` (ent#183) that the ent#139
runner executes, and ent#236 makes syncing automatic — so a source that tracks
a branch head puts every merged upstream commit on every install with no human
in the loop. The bundled community source therefore pins to a tag; custom
sources, whose write access the operator controls, keep tracking a branch.

Pinning is only as good as the tag being immutable, and git does not enforce
that. So a tag that resolves to a **different commit than last sync** is
treated as an attack signal and refused (`moved_tag`), not silently adopted —
the whole point of pinning is that the bytes don't change under you. Bumping to
new content is done by pointing the source at a new tag NAME, which is an
explicit admin action.
"""

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Issue #184 (UnderDefense pentest 3.3.1): override git's default User-Agent
# (`git/<version> (libcurl/...)`) on every HTTP-bearing git subcommand so
# outbound requests don't fingerprint the backend stack. The SSRF allowlist
# (#179) already locks the destination to github.com, but defense-in-depth: even
# if the allowlist is ever loosened, the UA stays generic. Deliberately carries
# no version suffix, to avoid another version string drifting against VERSION.
_GIT_HTTP_UA_ARGS = ["-c", "http.useragent=Trinity-Skills-Sync"]

# Source ids are server-minted (`src_<hex>`), never user-supplied — but this is
# a directory name derived from a DB value, so it is validated anyway rather
# than trusted. One regex, applied at construction, is cheaper than auditing
# every path join downstream.
_SOURCE_ID_RE = re.compile(r"^src_[0-9a-f]{8,32}$")

# Git refs reach the command line. The SSRF allowlist covers the URL; this
# covers the ref, so neither a branch nor a tag name can smuggle an option
# (a leading '-') or traverse.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")

_CLONE_TIMEOUT = 120
_FETCH_TIMEOUT = 60
_QUICK_TIMEOUT = 30


def redact(text: str) -> str:
    """Strip any embedded PAT from git output before it is logged or returned."""
    return re.sub(r"https://[^@]+@", "https://***@", text or "")


class SkillSourceClone:
    """The local git clone backing one skill source."""

    def __init__(self, source_id: str, url: str, ref: str, ref_type: str, root: Path):
        if not _SOURCE_ID_RE.match(source_id or ""):
            raise ValueError(f"unsafe skill source id: {source_id!r}")
        if not _REF_RE.match(ref or ""):
            raise ValueError(f"unsafe git ref: {ref!r}")
        if ref_type not in ("branch", "tag"):
            raise ValueError(f"unknown ref_type: {ref_type!r}")
        self.source_id = source_id
        self.url = url
        self.ref = ref
        self.ref_type = ref_type
        self.path = root / source_id

    # =========================================================================
    # Sync
    # =========================================================================

    def sync(self, auth_url: str, expected_sha: Optional[str] = None) -> Dict[str, Any]:
        """Clone or update this source's checkout.

        `expected_sha` is the SHA recorded at the previous successful sync. For
        a **tag** source it is a pin check: same tag name resolving to a
        different commit means the tag was moved, which is refused. It is
        ignored for branch sources, where movement is the point.
        """
        try:
            if (self.path / ".git").exists():
                result = self._update(expected_sha)
            else:
                result = self._clone(auth_url)
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "git operation timed out"}
        except Exception as exc:  # noqa: BLE001 — one bad source must not
            # abort a fleet-wide sync of the others.
            logger.error("skill source %s sync failed: %s", self.source_id, exc)
            return {"success": False, "error": redact(str(exc))}

        if result["success"]:
            result["commit_sha"] = self.current_commit()
        return result

    def _clone(self, auth_url: str) -> Dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "git", *_GIT_HTTP_UA_ARGS, "clone",
            "--branch", self.ref,   # accepts a tag name too, yielding a detached HEAD
            "--depth", "1",
            "--", auth_url, str(self.path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_CLONE_TIMEOUT)
        if proc.returncode != 0:
            return {"success": False, "error": f"clone failed: {redact(proc.stderr)}"}
        logger.info("cloned skill source %s (%s %s)", self.source_id, self.ref_type, self.ref)
        return {"success": True, "action": "cloned"}

    def _update(self, expected_sha: Optional[str]) -> Dict[str, Any]:
        if self.ref_type == "tag":
            return self._update_tag(expected_sha)
        return self._update_branch()

    def _update_branch(self) -> Dict[str, Any]:
        fetch = self._git(
            [*_GIT_HTTP_UA_ARGS, "fetch", "origin", self.ref], timeout=_FETCH_TIMEOUT
        )
        if fetch.returncode != 0:
            return {"success": False, "error": f"fetch failed: {redact(fetch.stderr)}"}
        reset = self._git(["reset", "--hard", f"origin/{self.ref}"])
        if reset.returncode != 0:
            return {"success": False, "error": f"reset failed: {redact(reset.stderr)}"}
        return {"success": True, "action": "pulled"}

    def _update_tag(self, expected_sha: Optional[str]) -> Dict[str, Any]:
        """Re-resolve a pinned tag, refusing it if it moved.

        Deliberately NOT `fetch --force`: without it git refuses to overwrite an
        existing tag ref that now points elsewhere, so the moved-tag case shows
        up as a fetch failure rather than a silent adoption. The explicit SHA
        comparison below is the belt to that suspenders — it catches the same
        condition on a fresh clone where no local tag ref exists yet to conflict.
        """
        fetch = self._git(
            [*_GIT_HTTP_UA_ARGS, "fetch", "origin", "tag", self.ref],
            timeout=_FETCH_TIMEOUT,
        )
        if fetch.returncode != 0:
            stderr = redact(fetch.stderr)
            if "would clobber existing tag" in stderr or "[rejected]" in stderr:
                return {
                    "success": False,
                    "error": (
                        f"refusing tag {self.ref!r}: it now points at a different "
                        "commit than when it was first synced. A pinned tag must "
                        "not move — verify upstream, then point this source at a "
                        "new tag."
                    ),
                    "moved_tag": True,
                }
            return {"success": False, "error": f"fetch failed: {stderr}"}

        resolved = self._resolve(f"refs/tags/{self.ref}")
        if resolved is None:
            return {"success": False, "error": f"tag {self.ref!r} not found upstream"}
        if expected_sha and not resolved.startswith(expected_sha):
            return {
                "success": False,
                "error": (
                    f"refusing tag {self.ref!r}: resolves to {resolved[:12]} but "
                    f"{expected_sha} was recorded at the last sync. A pinned tag "
                    "must not move."
                ),
                "moved_tag": True,
            }

        checkout = self._git(["checkout", "--force", f"refs/tags/{self.ref}"])
        if checkout.returncode != 0:
            return {"success": False, "error": f"checkout failed: {redact(checkout.stderr)}"}
        return {"success": True, "action": "pinned"}

    # =========================================================================
    # Read
    # =========================================================================

    def current_commit(self) -> Optional[str]:
        resolved = self._resolve("HEAD")
        return resolved[:12] if resolved else None

    def _resolve(self, rev: str) -> Optional[str]:
        proc = self._git(["rev-parse", rev], timeout=10)
        return proc.stdout.strip() if proc.returncode == 0 else None

    def skills_root(self) -> str:
        return os.path.realpath(str(self.path / ".claude" / "skills"))

    def skill_dir(self, skill_name: str) -> Optional[Path]:
        """Resolve a skill directory inside THIS source, or None if unsafe.

        The realpath containment check is per-clone, so a symlink in one
        source's tree cannot resolve into another source's checkout — a new
        escape route that did not exist while there was only one clone.
        Callers must still apply the skill-name regex; this is the second,
        independent guard (see skill_service._skill_dir for the pairing).
        """
        root = self.skills_root()
        target = os.path.realpath(os.path.join(root, skill_name))
        if target != root and not target.startswith(root + os.sep):
            return None
        return Path(target)

    def skill_names(self) -> List[str]:
        """Directory names under .claude/skills/ that carry a SKILL.md."""
        skills_dir = self.path / ".claude" / "skills"
        if not skills_dir.is_dir():
            return []
        return sorted(
            p.name for p in skills_dir.iterdir()
            if p.is_dir() and (p / "SKILL.md").exists()
        )

    def tree_shas(self) -> Dict[str, str]:
        """Per-skill git tree SHA — the package version (ent#183)."""
        proc = self._git(["ls-tree", "-z", "HEAD", ".claude/skills/"], text=False, timeout=15)
        if proc.returncode != 0:
            logger.warning("ls-tree failed for skill source %s", self.source_id)
            return {}
        shas: Dict[str, str] = {}
        for entry in proc.stdout.decode("utf-8", errors="replace").split("\0"):
            if not entry.strip():
                continue
            head, _, path = entry.partition("\t")   # "<mode> <type> <sha>\t<path>"
            fields = head.split()
            if len(fields) != 3 or fields[1] != "tree":
                continue
            shas[path.rsplit("/", 1)[-1]] = fields[2]
        return shas

    def archive_skill(self, skill_name: str) -> Optional[bytes]:
        """`git archive HEAD -- .claude/skills/<name>` — the injection source.

        Reads from HEAD rather than the working tree so a concurrent sync's
        `reset --hard` cannot yield a mixed-tree tar.
        """
        proc = self._git(
            ["archive", "--format=tar", "HEAD", "--", f".claude/skills/{skill_name}"],
            text=False, timeout=_QUICK_TIMEOUT,
        )
        if proc.returncode != 0:
            logger.warning("archive failed: %s/%s", self.source_id, skill_name)
            return None
        return proc.stdout

    # =========================================================================

    def _git(self, args: List[str], *, text: bool = True,
             timeout: int = _QUICK_TIMEOUT) -> subprocess.CompletedProcess:
        """Run git in this clone, tolerating a clone that isn't there.

        A source whose very first clone failed has no directory, and
        `subprocess.run(cwd=<missing>)` raises FileNotFoundError — which would
        escape through every read path (`current_commit` → the list cache
        fingerprint) and take down the whole merged listing over ONE unreachable
        repo. Multi-source makes that likely rather than theoretical: with
        several sources configured, some will be broken at any given moment.
        Returning a synthetic failure keeps every caller's existing
        returncode check as the single way this is handled.
        """
        if not self.path.is_dir():
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=128,
                stdout="" if text else b"",
                stderr="clone directory does not exist" if text else b"",
            )
        return subprocess.run(
            ["git", *args], cwd=self.path,
            capture_output=True, text=text, timeout=timeout,
        )
