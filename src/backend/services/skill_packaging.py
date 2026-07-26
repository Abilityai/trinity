"""Skill package primitives (trinity-enterprise#183).

Pure functions for the full-directory skill package format:

- ``validate_skill_name`` — the ONE name guard applied at get/assign/inject
- ``parse_frontmatter`` / ``extract_contract`` — tolerant, hardened parse of the
  skill frontmatter contract (deps, env keys, automation, invocability)
- ``filter_skill_archive`` — turn ``git archive`` bytes into the vetted member
  list (regular files only, litter/protected names dropped, caps enforced)
- ``build_injection_tar`` — assemble the final uncompressed tar with the
  generated ``.trinity-skill.json`` meta member appended LAST
- ``compute_prune`` — manifest-based stale-file diff (the platform deletes only
  what it previously wrote)

Everything here is pure so the unit suite can exercise the tar round-trip
against the real agent-server ``restore_from_tar`` without service wiring.
Orchestration (locks, HTTP, docker exec) lives in ``skill_service``.
"""

import io
import json
import os
import re
import tarfile
from typing import Any, Dict, List, Optional, Tuple

import yaml

# One name guard for get_skill / assign / inject alike. Names originate from
# library directory names, but the guard is defense-in-depth against a
# traversal-shaped name reaching path math or an exec argument.
SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Dep names are interpolation-gated BEFORE any probe runs: a name failing these
# is reported as frontmatter_invalid and never reaches the agent container.
BINARY_NAME_RE = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

# VCS/runtime litter never ships in an injection tar.
_EXCLUDE_DIRS = {".git", "__pycache__", "node_modules", ".venv"}
_EXCLUDE_FILES = {".DS_Store"}
_EXCLUDE_SUFFIXES = (".pyc",)

# Never legitimate skill content: restore could WRITE these (it bypasses the
# agent-server edit-protection list) while prune could never DELETE them — an
# asymmetry closed by refusing them at build time.
_PROTECTED_BASENAMES = {
    ".env", "CLAUDE.md", "AGENTS.md", ".mcp.json", ".mcp.json.template",
    ".gitignore", ".credentials.enc",
}

# The generated meta member name; a library file with this name is dropped so
# provenance can't be spoofed from repo content.
META_FILENAME = ".trinity-skill.json"

FRONTMATTER_MAX_BYTES = 64 * 1024


def _env_cap(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    try:
        value = int(raw)
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def skill_max_bytes() -> int:
    """Per-skill package cap (env-tunable, default 10 MiB)."""
    return _env_cap("SKILL_MAX_BYTES", 10 * 1024 * 1024)


def skills_total_max_bytes() -> int:
    """Per-injection total cap (env-tunable, default 50 MiB)."""
    return _env_cap("SKILLS_TOTAL_MAX_BYTES", 50 * 1024 * 1024)


def validate_skill_name(name: str) -> bool:
    """True when the name is safe for path math and exec arguments."""
    if not isinstance(name, str) or name in (".", ".."):
        return False
    return bool(SKILL_NAME_RE.match(name))


# =========================================================================
# Frontmatter contract
# =========================================================================

class _NoAliasSafeLoader(yaml.SafeLoader):
    """SafeLoader that refuses aliases: safe_load still EXPANDS anchors, so a
    sub-KiB billion-laughs frontmatter would OOM the backend regardless of the
    input size cap (#919 hardened-parse convention)."""

    def fetch_alias(self):  # noqa: D102 — yaml internal hook
        raise yaml.YAMLError("aliases are not allowed in skill frontmatter")

    def compose_node(self, parent, index):  # noqa: D102
        if self.check_event(yaml.events.AliasEvent):
            raise yaml.YAMLError("aliases are not allowed in skill frontmatter")
        return super().compose_node(parent, index)


def parse_frontmatter(text: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Parse the leading ``---`` frontmatter block of a SKILL.md.

    Returns ``(mapping, warning)``. Missing frontmatter → ``(None, None)``;
    oversized / invalid / non-mapping / alias-bearing → ``(None, "frontmatter_invalid")``.
    Never raises.
    """
    if not text.startswith("---"):
        return None, None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None, None
    block = parts[1]
    if len(block.encode("utf-8", errors="replace")) > FRONTMATTER_MAX_BYTES:
        return None, "frontmatter_invalid"
    try:
        data = yaml.load(block, Loader=_NoAliasSafeLoader)
    except yaml.YAMLError:
        return None, "frontmatter_invalid"
    if not isinstance(data, dict):
        return None, "frontmatter_invalid" if data is not None else None
    return data, None


def _as_str_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def extract_contract(frontmatter: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[str]]:
    """Extract the skill contract from parsed frontmatter.

    Tolerant by construction: every field is isinstance-guarded, unknown keys
    ignored. Flat keys are the catalog convention (the agent-server Playbooks
    parser already reads flat ``automation:``); a ``trinity:`` mapping, when
    present, takes precedence (forward-compat namespace).

    Returns ``(contract, warnings)`` where contract is::

        {description, automation, user_invocable, allowed_tools,
         requires: {packages, binaries, env}}
    """
    warnings: List[str] = []
    fm = frontmatter or {}
    scope: Dict[str, Any] = dict(fm)
    trinity_block = fm.get("trinity")
    if isinstance(trinity_block, dict):
        scope.update(trinity_block)

    automation = scope.get("automation")
    if automation is not None and not isinstance(automation, str):
        automation = None

    user_invocable = scope.get("user_invocable")
    if not isinstance(user_invocable, bool):
        user_invocable = True

    allowed_tools = scope.get("allowed-tools", scope.get("allowed_tools"))
    if not isinstance(allowed_tools, (str, list)):
        allowed_tools = None

    requires_raw = scope.get("requires")
    requires: Dict[str, List[str]] = {"packages": [], "binaries": [], "env": []}
    if isinstance(requires_raw, dict):
        requires["packages"] = _as_str_list(requires_raw.get("packages"))
        binaries, env_keys = [], []
        for name in _as_str_list(requires_raw.get("binaries")):
            if BINARY_NAME_RE.match(name):
                binaries.append(name)
            else:
                warnings.append(f"frontmatter_invalid:binary:{name[:40]!r}")
        for key in _as_str_list(requires_raw.get("env")):
            if ENV_KEY_RE.match(key):
                env_keys.append(key)
            else:
                warnings.append(f"frontmatter_invalid:env:{key[:40]!r}")
        requires["binaries"] = binaries
        requires["env"] = env_keys
    elif requires_raw is not None:
        warnings.append("frontmatter_invalid:requires")

    description = fm.get("description")
    if not isinstance(description, str):
        description = None

    contract = {
        "description": description,
        "automation": automation,
        "user_invocable": user_invocable,
        "allowed_tools": allowed_tools,
        "requires": requires,
    }
    return contract, warnings


# =========================================================================
# Tar packaging
# =========================================================================

def _is_excluded(rel_parts: Tuple[str, ...]) -> bool:
    if any(part in _EXCLUDE_DIRS for part in rel_parts[:-1]):
        return True
    basename = rel_parts[-1]
    if basename in _EXCLUDE_FILES or basename.startswith(".git"):
        return True
    return basename.endswith(_EXCLUDE_SUFFIXES)


def filter_skill_archive(
    archive_bytes: bytes, skill_name: str
) -> Tuple[List[Tuple[str, bytes, int]], List[str], int]:
    """Vet ``git archive HEAD -- .claude/skills/<name>`` output.

    Returns ``(members, warnings, total_bytes)`` where members are
    ``(arcname, content, mode)`` for REGULAR files only. Symlink/dir/device
    members and litter/protected names are dropped with named warnings —
    an out-of-archive SYMTYPE member would raise KeyError inside the
    agent-server ``restore_from_tar`` mid-extraction, and a followed symlink
    at build time is an exfiltration vector, so links never ship at all.
    """
    prefix = f".claude/skills/{skill_name}/"
    members: List[Tuple[str, bytes, int]] = []
    warnings: List[str] = []
    total = 0
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r") as tf:
        for member in tf.getmembers():
            name = member.name
            if member.isdir():
                continue
            if not name.startswith(prefix) or "/../" in f"/{name}/":
                warnings.append(f"restore_skipped:{name}")
                continue
            rel = name[len(prefix):]
            if not rel:
                continue
            parts = tuple(rel.split("/"))
            if not member.isreg():
                warnings.append(f"symlink_skipped:{rel}")
                continue
            if _is_excluded(parts):
                continue
            basename = parts[-1]
            if basename in _PROTECTED_BASENAMES or basename == META_FILENAME:
                warnings.append(f"protected_name_skipped:{rel}")
                continue
            extracted = tf.extractfile(member)
            if extracted is None:
                warnings.append(f"non_regular_skipped:{rel}")
                continue
            content = extracted.read()
            total += len(content)
            members.append((name, content, member.mode))
    return members, warnings, total


def build_injection_tar(
    skill_name: str, members: List[Tuple[str, bytes, int]], meta: Dict[str, Any]
) -> bytes:
    """Assemble the final UNCOMPRESSED injection tar.

    The generated meta member goes LAST: ``restore_from_tar`` extracts in
    member order, so a partial extraction must leave the OLD version marker on
    the agent — meta-first would pin new-version-over-old-files and defeat
    skip-if-unchanged's repair loop.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for arcname, content, mode in members:
            info = tarfile.TarInfo(name=arcname)
            info.size = len(content)
            info.mode = mode & 0o777
            tf.addfile(info, io.BytesIO(content))
        meta_bytes = json.dumps(meta, sort_keys=True).encode("utf-8")
        meta_info = tarfile.TarInfo(name=f".claude/skills/{skill_name}/{META_FILENAME}")
        meta_info.size = len(meta_bytes)
        meta_info.mode = 0o644
        tf.addfile(meta_info, io.BytesIO(meta_bytes))
    return buf.getvalue()


def executable_paths(members: List[Tuple[str, bytes, int]]) -> List[str]:
    """Arcnames whose git mode carries any exec bit (restore drops modes)."""
    return [arcname for arcname, _content, mode in members if mode & 0o111]


PRUNE_CAP_PER_SKILL = 200


def compute_prune(
    previous_manifest: Optional[List[str]], new_manifest: List[str], skill_name: str
) -> Tuple[List[str], bool]:
    """Manifest-based stale-file diff: previous − new, capped, dir-confined.

    The platform only ever deletes files IT wrote on a prior injection —
    runtime artifacts a skill's own scripts generate (``__pycache__``,
    downloaded models) and agent-authored files are structurally untouchable.
    The previous manifest is read back from AGENT-side state
    (``.trinity-skill.json``), so it is untrusted: every candidate is
    prefix-confined to ``.claude/skills/<skill_name>/`` — a doctored manifest
    must never steer a backend-driven delete anywhere else in the agent home.
    Returns ``(paths_to_delete, truncated)``; no previous manifest → nothing.
    """
    if not previous_manifest or not isinstance(previous_manifest, list):
        return [], False
    prefix = f".claude/skills/{skill_name}/"
    prev = [
        p for p in previous_manifest
        if isinstance(p, str) and p.startswith(prefix) and ".." not in p.split("/")
    ]
    stale = sorted(set(prev) - set(new_manifest))
    stale = [p for p in stale if not p.endswith("/" + META_FILENAME)]
    if len(stale) > PRUNE_CAP_PER_SKILL:
        return stale[:PRUNE_CAP_PER_SKILL], True
    return stale, False
