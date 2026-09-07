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
explicit admin action — and one that works: `db.update_source` clears the
recorded SHA whenever `url`/`ref`/`ref_type` change, because a pin baseline is
only meaningful for the ref it was recorded against. (It did not, once: the
bump was compared against the *old* tag's SHA and refused as a move, with the
error telling the operator to do what they had just done.)

Repointing a source's `url` re-clones rather than fetching: `origin` is written
at clone time, so without that check every later sync silently pulled the OLD
repo — `success`, moving commit, fleet re-inject and all. See `_origin_matches`.

**Per-source skills root (ent#332).** The layout inside a source repo is no
longer assumed to be `.claude/skills/`: each clone resolves its own root —
a root `catalog.yaml` `skills_root:` declaration wins, else a `skills/`
directory carrying SKILL.md evidence, else the legacy `.claude/skills/`
fallback (existing sources keep working with zero config). The resolved root is
the SOURCE layout only; packaging rewrites arcnames to the canonical
agent-side `.claude/skills/` destination (see `skill_packaging`).
"""

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from utils.safe_yaml import (
    AliasPolicy,
    HardenedYamlError,
    load_hardened_yaml,
)
from utils.url_validation import scrub_url_credentials_in_text

logger = logging.getLogger(__name__)

# Issue #184 (UnderDefense pentest 3.3.1): override git's default User-Agent
# (`git/<version> (libcurl/...)`) on every HTTP-bearing git subcommand so
# outbound requests don't fingerprint the backend stack. The SSRF allowlist
# (#179) already locks the destination to github.com, but defense-in-depth: even
# if the allowlist is ever loosened, the UA stays generic. Deliberately carries
# no version suffix, to avoid another version string drifting against VERSION.
_GIT_HTTP_UA_ARGS = ["-c", "http.useragent=Trinity-Skills-Sync"]

# The sibling directory `_quarantine_non_repo_dir` parks a non-repository
# checkout in. Shared rather than spelled inline, because it is written by this
# module and read by `skill_service`'s two reclamation paths — a literal drifting
# between the writer and the reclaimer leaves quarantines nothing can collect.
QUARANTINE_SUFFIX = ".broken"

# Source ids are server-minted (`src_<hex>`), never user-supplied — but this is
# a directory name derived from a DB value, so it is validated anyway rather
# than trusted. One regex, applied at construction, is cheaper than auditing
# every path join downstream. Public because `skill_service` reclaims orphaned
# checkouts by directory name and must apply the SAME shape test — a second
# copy of this pattern is a second thing to get wrong when it drifts.
SOURCE_ID_RE = re.compile(r"^src_[0-9a-f]{8,32}$")

# Git refs reach the command line. The SSRF allowlist covers the URL; this
# covers the ref, so neither a branch nor a tag name can smuggle an option
# (a leading '-') or traverse.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")

_CLONE_TIMEOUT = 120
_FETCH_TIMEOUT = 60
_QUICK_TIMEOUT = 30

# ent#332 — per-source skills root.
_DEFAULT_SKILLS_ROOT = ".claude/skills"
_PROBE_SKILLS_ROOT = "skills"
# catalog.yaml is author-controlled repo content (same trust tier as SKILL.md
# frontmatter, hardened by ent#314). The cap is enforced by a BOUNDED read
# (`read(cap + 1)`) — a post-read length check is defeated by a symlink at
# `catalog.yaml` pointing at /dev/zero, and git materializes author symlinks.
_CATALOG_MAX_BYTES = 64 * 1024
# Per-segment charset for a declared skills_root. Applied segment-wise: the
# whole-string regex family admits `.`, `./skills` and `skills//x`, each of
# which breaks archive-prefix math into per-skill "empty package" failures.
_ROOT_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def validate_declared_root(value: Any) -> Optional[str]:
    """Validate a catalog-declared ``skills_root``; normalized root or None.

    Segment-wise by design: strip ONE trailing ``/``, split on ``/``, and
    reject any segment that is empty, ``.``, or ``..`` — so ``.``, ``./x``,
    ``a//b`` and ``a/../b`` are all refused, not just substring-``..`` shapes.
    A leading ``-`` is refused so the value can never read as a git option
    (the ``--`` separators downstream are the belt to this suspenders).
    """
    if not isinstance(value, str):
        return None
    root = value[:-1] if value.endswith("/") else value
    if not root or len(root) > 200 or root.startswith(("/", "-")):
        return None
    segments = root.split("/")
    if any(
        seg in ("", ".", "..") or not _ROOT_SEGMENT_RE.match(seg)
        for seg in segments
    ):
        return None
    return root


def redact(text: str) -> str:
    """Strip any embedded PAT from git output before it is logged or returned.

    THE free-text scrub for this module — `skill_service._scrub_pat` delegates
    here rather than carrying a second pattern (ent#347: the bug was two
    hand-written patterns that had drifted apart and were each wrong
    differently). Handles the double-`@` shape `_authenticated_url` produces.
    Never raises.

    The pattern itself now lives with the single-URL parser in
    `utils/url_validation`, because ent#347's own argument turned out to apply
    one level up (#2052): this pattern and `strip_url_credentials` were the
    *next* pair to drift. Anchored on a literal `https://`, it never fired on
    the protocol-relative, scheme-less and alternate-scheme URLs the parser
    handles — all shapes a stored source row may legitimately carry, since
    `_adopt_legacy_clone` writes with no validation. Both now derive from one
    authority grammar; see `scrub_url_credentials_in_text` for why the free-text
    side stays regex-oriented and what it deliberately over-matches.
    """
    return scrub_url_credentials_in_text(text)


def canonical_remote(url: str) -> str:
    """Comparable form of a git remote: no credentials, no `.git`, no trailing /.

    Used only to answer "is the checkout's `origin` still the repo this source
    is configured for". Userinfo is dropped because the stored URL never carries
    a PAT while the on-disk `origin` always does for a private source, so a raw
    comparison would report a repoint on every sync. Scheme and host are
    case-folded; the PATH is not, since a local-filesystem remote (the shape the
    tests use) is case-sensitive on Linux.
    """
    text = (url or "").strip()
    if not text:
        return ""
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*://)([^/]*)(.*)$", text)
    if m:
        scheme, netloc, rest = m.groups()
        netloc = netloc.rsplit("@", 1)[-1]  # drop `user:pat@`
        text = f"{scheme.lower()}{netloc.lower()}{rest}"
    if text.endswith(".git"):
        text = text[:-4]
    return text.rstrip("/")


class SkillSourceClone:
    """The local git clone backing one skill source."""

    def __init__(self, source_id: str, url: str, ref: str, ref_type: str, root: Path):
        if not SOURCE_ID_RE.match(source_id or ""):
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
        # ent#332: lazily-resolved per-source skills root. Instances are
        # per-operation (skill_service._clones constructs fresh ones per call),
        # so the cache has no staleness window across syncs.
        self._rel_root: Optional[str] = None
        # True when the probe found SKILL.md evidence under BOTH skills/ and
        # .claude/skills/ with no catalog.yaml to decide — surfaced in status.
        self.dual_layout = False

    @property
    def quarantine_path(self) -> Path:
        """Where `_quarantine_non_repo_dir` parks a non-repository checkout."""
        return self.path.with_name(self.path.name + QUARANTINE_SUFFIX)

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
            repointed = False
            if (self.path / ".git").exists() and not self._origin_matches(auth_url):
                self._discard_repointed_checkout()
                repointed = True

            if (self.path / ".git").exists():
                result = self._update(expected_sha)
            else:
                result = self._clone(auth_url)
                # A repoint is a DIFFERENT repo, so `expected_sha` — recorded
                # against the old one — is not a pin for this tag and must not
                # refuse the very content the admin just asked for. The DB
                # clears the SHA on a url/ref change (`update_source`) so this
                # is normally already None; the guard keeps this class correct
                # for a caller that doesn't. The genuine bypass case
                # `_refuse_moved_pin_after_clone` exists for — a LOST checkout
                # while upstream moved the tag — is untouched: there is no
                # `origin` to mismatch when there is no checkout.
                if result.get("success") and not repointed:
                    refused = self._refuse_moved_pin_after_clone(expected_sha)
                    if refused is not None:
                        result = refused
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "git operation timed out"}
        except Exception as exc:  # noqa: BLE001 — one bad source must not
            # abort a fleet-wide sync of the others.
            logger.error("skill source %s sync failed: %s", self.source_id, exc)
            return {"success": False, "error": redact(str(exc))}

        if result["success"]:
            result["commit_sha"] = self.current_commit()
        return result

    def _refuse_moved_pin_after_clone(
        self, expected_sha: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Apply the tag pin to a FRESH clone. Returns a refusal, or None if OK.

        `_update_tag` enforces the pin two ways, and BOTH are properties of an
        existing checkout: the no-`--force` fetch needs a local tag ref to
        refuse to clobber, and its SHA comparison only runs on the update path.
        A clone resolves the tag name upstream with nothing local to conflict,
        so without this the pin is bypassed in exactly the case it matters —
        the checkout was lost (this class's own quarantine rename, a restored
        `/data` backup, a recreated volume) while upstream moved the tag.

        That is not a theoretical ordering: the sync then reports success with
        a changed commit, so ent#236's fleet re-inject pushes the moved tag's
        executables to every running agent with no human in the loop, which is
        the entire scenario AC#5's pinning exists to prevent.

        No `expected_sha` means this source has never synced — the genuine
        first clone, where there is nothing to compare and any tag is the pin.
        """
        if self.ref_type != "tag" or not expected_sha:
            return None

        resolved = self._resolve("HEAD")
        if resolved and resolved.startswith(expected_sha):
            return None

        # Delete the checkout, don't just report failure: `list_skills` reads
        # the working tree, so leaving it would serve the moved tag's content
        # to the merged listing (and to injection) even though sync "failed".
        shutil.rmtree(self.path, ignore_errors=True)
        return {
            "success": False,
            "error": (
                f"refusing tag {self.ref!r}: a fresh clone resolves to "
                f"{(resolved or 'unknown')[:12]} but {expected_sha} was recorded "
                "at the last sync. A pinned tag must not move — verify upstream, "
                "then point this source at a new tag."
            ),
            "moved_tag": True,
        }

    def _origin_matches(self, auth_url: str) -> bool:
        """Is this checkout's `origin` still the configured repo?

        `_update_branch`/`_update_tag` fetch **`origin`**, which git wrote at
        clone time — so without this check, repointing a source's `url` changed
        nothing on disk and every later sync silently pulled the OLD repo,
        reporting `success` with a moving commit that feeds ent#236's fleet
        re-inject. Silent-wrong: the admin sees the new URL in Settings while
        the fleet keeps receiving the previous repo's executables.

        An unreadable or absent `origin` counts as a MATCH — i.e. no discard.
        The answer is genuinely unknown there, and the action gated on it
        deletes a directory: the fail-safe direction is to leave it alone and
        let `_update`'s `fetch origin` fail honestly, rather than to widen an
        unattended `rmtree` to cover an ambiguity (#1638/#1644). It costs
        nothing for the case this exists for — a repointed source always has a
        readable `origin`, that being the whole problem.
        """
        proc = self._git(["config", "--get", "remote.origin.url"], timeout=10)
        if proc.returncode != 0:
            return True
        return canonical_remote(proc.stdout.strip()) == canonical_remote(auth_url)

    def _discard_repointed_checkout(self) -> None:
        """Drop a checkout whose source now points at a different repo.

        Deleted rather than `git remote set-url`-ed, which would be the smaller
        edit but leaves the OLD repo's refs in place — and a tag name present in
        both repos at different commits then reads to `_update_tag` as a moved
        pin and is refused forever. A skills clone is derived state, fully
        reconstructible from the URL the admin just supplied, and this path only
        runs on an explicit admin repoint (#1638's caution is about unattended
        deletion of data with no other copy; neither clause holds here).

        The quarantine goes with it. It holds the PREVIOUS repo's content, so a
        repoint makes it permanently irrelevant — and it is invisible to both
        reclamation paths while the source still exists:
        `discard_source_checkout` runs only on delete, and
        `_reclaim_orphan_checkouts` considers only ids with no row. Left behind
        it persists for the life of the source.
        """
        logger.warning(
            "skill source %s now points at a different repository — discarding "
            "the stale checkout at %s and re-cloning",
            self.source_id, self.path,
        )
        for path in (self.path, self.quarantine_path):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)

    def _quarantine_non_repo_dir(self) -> None:
        """Move a non-repository source directory aside so a clone can proceed.

        Ported from ent#236, which fixed this for the single shared library and
        whose reasoning applies unchanged per source. `sync` decides clone-vs-
        update on `.git` alone, so a path that exists but holds no repository —
        a clone interrupted by a full disk or a crash, a stray `mkdir`, a
        restored backup — takes the clone branch; but `git clone` refuses a
        destination that exists and is non-empty, so without this the source
        fails forever and the only recovery is shell access. ent#236 put sync on
        an unattended timer, where "forever" means every scheduled sync fails and
        the fleet re-inject silently never runs.

        Renamed, never deleted. The platform owns this path exclusively, so
        removal would probably be safe — but "probably safe" is not the standard
        for an unattended timer deleting a directory (#1638/#1644). Only one
        quarantine is kept per source, so this cannot grow without bound.
        """
        quarantine = self.quarantine_path
        try:
            if quarantine.exists():
                shutil.rmtree(quarantine, ignore_errors=True)
            self.path.rename(quarantine)
            logger.warning(
                "skill source %s at %s was not a git repository — moved to %s "
                "and re-cloning.", self.source_id, self.path, quarantine,
            )
        except OSError as e:  # noqa: BLE001 — fall through and let clone report
            logger.error(
                "could not move aside non-repo skill source at %s: %s",
                self.path, e,
            )

    def _clone(self, auth_url: str) -> Dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            # Reached only via the `.git`-absent branch in `sync`.
            self._quarantine_non_repo_dir()
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

        The comparison resolves the tag PEELED (`^{commit}`), never bare. The
        recorded SHA is `current_commit()` — HEAD, i.e. the commit — while a
        bare `rev-parse refs/tags/<ref>` on an ANNOTATED tag yields the tag
        object, so an unmoved tag compared unequal and was refused as moved on
        every sync after the first (#2550). Every `trinity-skills` release tag
        is annotated, so the bundled source hit this on its second sync. Peeling
        is the identity for a lightweight tag, and a tag that really moved still
        resolves to a different commit — the refusal itself is unchanged.
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

        resolved = self._resolve(f"refs/tags/{self.ref}^{{commit}}")
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

    def skills_rel_root(self) -> str:
        """This source's skills root, RELATIVE to the clone (ent#332).

        Resolution order: catalog.yaml ``skills_root:`` declaration → the
        ``skills/`` probe → the legacy ``.claude/skills`` fallback. Never
        raises — every invalid tier logs and falls through to the next, so a
        broken catalog can never blind a source a probe can still read.
        """
        if self._rel_root is None:
            self._rel_root = self._resolve_rel_root()
        return self._rel_root

    def _resolve_rel_root(self) -> str:
        declared = self._declared_root()
        if declared is not None:
            if self._declared_root_usable(declared):
                return declared
            # Containment/symlink failure at the DECLARED tier falls through —
            # blanking the whole source over a tier it doesn't need would
            # regress a repo that serves skills from a probeable root today.
        if self._probe_skills_layout():
            return _PROBE_SKILLS_ROOT
        return _DEFAULT_SKILLS_ROOT

    def _declared_root(self) -> Optional[str]:
        """The catalog.yaml ``skills_root`` declaration, validated, or None.

        A missing catalog.yaml is silent; a PRESENT-but-unusable one warns.
        Never raises: ``HardenedYamlError`` is a ValueError, not a YAMLError,
        so it is caught EXPLICITLY — missing it would escape through
        ``list_skills`` and 500 the merged listing (the skill_packaging trap).
        """
        catalog = self.path / "catalog.yaml"
        try:
            # lstat-order guard: never open a symlinked catalog.yaml — git
            # materializes author-controlled symlinks (the _skill_files idiom).
            if catalog.is_symlink() or not catalog.is_file():
                return None
        except OSError:
            return None
        try:
            with open(catalog, "rb") as fh:
                raw = fh.read(_CATALOG_MAX_BYTES + 1)
        except OSError as e:
            logger.warning(
                "could not read catalog.yaml for skill source %s: %s",
                self.source_id, e,
            )
            return None
        if len(raw) > _CATALOG_MAX_BYTES:
            logger.warning(
                "catalog.yaml too large for skill source %s; ignoring",
                self.source_id,
            )
            return None
        try:
            data = load_hardened_yaml(
                raw.decode("utf-8", errors="replace"),
                kind="catalog",
                alias_policy=AliasPolicy.REJECT,
                max_bytes=_CATALOG_MAX_BYTES,
            )
        except (HardenedYamlError, yaml.YAMLError):
            logger.warning(
                "unparseable catalog.yaml for skill source %s; ignoring",
                self.source_id,
            )
            return None
        if not isinstance(data, dict):
            logger.warning(
                "catalog.yaml for skill source %s is not a mapping; ignoring",
                self.source_id,
            )
            return None
        # Unknown schema_version ⇒ the whole catalog is unusable (semantics may
        # have changed under us) — degrade to the probe, which still resolves a
        # conventional layout correctly. Tolerates int 1 AND string "1": YAML
        # yields either depending on how the author quoted it.
        schema = data.get("schema_version", 1)
        if str(schema).strip() != "1":
            logger.warning(
                "unknown catalog.yaml schema_version %r for skill source %s; "
                "ignoring the catalog", schema, self.source_id,
            )
            return None
        value = data.get("skills_root")
        if value is None:
            return None
        root = validate_declared_root(value)
        if root is None:
            logger.warning(
                "invalid catalog.yaml skills_root %r for skill source %s; "
                "falling back to layout probe", value, self.source_id,
            )
        return root

    def _declared_root_usable(self, rel_root: str) -> bool:
        """Containment + symlink gate for a DECLARED root.

        The declared directory may be absent (declared-but-empty is honest);
        what it may not be is a symlink or a realpath escape from the clone.
        """
        candidate = self.path / rel_root
        try:
            if candidate.is_symlink():
                logger.warning(
                    "declared skills_root %r for skill source %s is a symlink; "
                    "falling back to layout probe", rel_root, self.source_id,
                )
                return False
        except OSError:
            return False
        base = os.path.realpath(str(self.path))
        target = os.path.realpath(os.path.join(base, rel_root))
        if not target.startswith(base + os.sep):
            logger.warning(
                "declared skills_root %r for skill source %s resolves outside "
                "the clone; falling back to layout probe", rel_root, self.source_id,
            )
            return False
        return True

    def _probe_skills_layout(self) -> bool:
        """True when ``skills/`` is the evidence-bearing root.

        Evidence-gated twice: ``skills/`` must be a REAL directory (lstat — a
        git-authored ``skills -> .claude/skills`` symlink would list fine but
        yield empty ``git archive`` output, failing every injection) holding at
        least one real ``<dir>/SKILL.md``; and when ``.claude/skills/`` ALSO
        carries evidence with no catalog to decide, the legacy root is kept —
        a pre-existing dual-layout source must never silently flip which
        executable content the ent#236 auto-sync injects fleet-wide.
        """
        candidate = self.path / _PROBE_SKILLS_ROOT
        if not self._is_real_dir(candidate) or not self._has_skill_evidence(candidate):
            return False
        legacy = self.path / _DEFAULT_SKILLS_ROOT
        if self._is_real_dir(legacy) and self._has_skill_evidence(legacy):
            self.dual_layout = True
            logger.warning(
                "skill source %s carries SKILL.md evidence under BOTH skills/ "
                "and .claude/skills/ with no catalog.yaml skills_root to "
                "decide; keeping .claude/skills/ — declare skills_root in "
                "catalog.yaml to switch", self.source_id,
            )
            return False
        return True

    @staticmethod
    def _is_real_dir(p: Path) -> bool:
        try:
            return not p.is_symlink() and p.is_dir()
        except OSError:
            return False

    @classmethod
    def _has_skill_evidence(cls, root: Path) -> bool:
        try:
            for child in root.iterdir():
                if cls._is_real_dir(child) and (child / "SKILL.md").is_file():
                    return True
        except OSError:
            pass
        return False

    def skills_root(self) -> Optional[str]:
        """Absolute, realpath-resolved skills root — or None when refused.

        The containment base is ``realpath(self.path)``, not ``self.path``:
        ``/data/skills-library`` may itself be a symlink to a larger volume,
        and comparing a resolved target against the unresolved base would
        refuse every source spuriously. None (a symlinked final root escaping
        the clone) propagates as an empty listing via ``skill_dir``/
        ``skill_names`` — never a TypeError inside ``list_skills``.
        """
        rel = self.skills_rel_root()
        base = os.path.realpath(str(self.path))
        target = os.path.realpath(os.path.join(base, rel))
        if not target.startswith(base + os.sep):
            logger.warning(
                "skills root for source %s resolves outside the clone; "
                "treating as absent", self.source_id,
            )
            return None
        return target

    def skill_dir(self, skill_name: str) -> Optional[Path]:
        """Resolve a skill directory inside THIS source, or None if unsafe.

        The realpath containment check is per-clone, so a symlink in one
        source's tree cannot resolve into another source's checkout — a new
        escape route that did not exist while there was only one clone.
        Callers must still apply the skill-name regex; this is the second,
        independent guard (see skill_service._skill_dir for the pairing).
        """
        root = self.skills_root()
        if root is None:
            return None
        target = os.path.realpath(os.path.join(root, skill_name))
        if target != root and not target.startswith(root + os.sep):
            return None
        return Path(target)

    def skill_names(self) -> List[str]:
        """Directory names under the resolved root that carry a SKILL.md."""
        root = self.skills_root()
        if root is None:
            return []
        skills_dir = Path(root)
        if not skills_dir.is_dir():
            return []
        return sorted(
            p.name for p in skills_dir.iterdir()
            if p.is_dir() and (p / "SKILL.md").exists()
        )

    def tree_shas(self) -> Dict[str, str]:
        """Per-skill git tree SHA — the package version (ent#183)."""
        rel = self.skills_rel_root()
        proc = self._git(["ls-tree", "-z", "HEAD", "--", f"{rel}/"], text=False, timeout=15)
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
        """`git archive HEAD -- <root>/<name>` — the injection source.

        Reads from HEAD rather than the working tree so a concurrent sync's
        `reset --hard` cannot yield a mixed-tree tar. Member names carry the
        SOURCE layout prefix; `skill_packaging.filter_skill_archive` rewrites
        them to the canonical agent-side `.claude/skills/` destination.
        """
        rel = self.skills_rel_root()
        proc = self._git(
            ["archive", "--format=tar", "HEAD", "--", f"{rel}/{skill_name}"],
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
