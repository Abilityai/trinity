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

from utils.safe_yaml import (
    AliasPolicy,
    HardenedYamlError,
    load_hardened_yaml,
)

# One name guard for get_skill / assign / inject alike. Names originate from
# library directory names, but the guard is defense-in-depth against a
# traversal-shaped name reaching path math or an exec argument.
SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# trinity-enterprise#753: the values `approval:` may take. `recommended` gates a
# library skill by default where it is assigned.
APPROVAL_VALUES = frozenset({"recommended"})

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
# The block runs from a first line of `---` to the next line that starts with
# `---`. Anchored to line starts, like the agent server's parser
# (`agent_server/routers/skills.py::parse_yaml_frontmatter`), so a `---` inside
# a value does not end the block (trinity-enterprise#584: the two parsers must
# agree on what a skill's description is).
_FRONTMATTER_BLOCK_RE = re.compile(r"\A---[ \t]*\n(.*?)^[ \t]*---", re.DOTALL | re.MULTILINE)


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

def parse_frontmatter(text: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Parse the leading ``---`` frontmatter block of a SKILL.md.

    Returns ``(mapping, warning)``. Missing frontmatter → ``(None, None)``;
    oversized / invalid / non-mapping / alias-bearing → ``(None, "frontmatter_invalid")``.
    Never raises.
    """
    # Same framing as the agent server: a byte-order mark and CRLF line ends
    # are an editor's, not the author's (trinity-enterprise#584).
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    m = _FRONTMATTER_BLOCK_RE.match(text)
    if not m:
        return None, None
    block = m.group(1)
    if len(block.encode("utf-8", errors="replace")) > FRONTMATTER_MAX_BYTES:
        return None, "frontmatter_invalid"
    try:
        # ent#314: was a local `_NoAliasSafeLoader`; now the shared loader, which
        # keeps BOTH of that copy's gates (scanner + compose) and adds the
        # duplicate-key guard this file never had. REJECT is preserved
        # deliberately — no legitimate frontmatter needs an alias, and this
        # consumer walks every field.
        data = load_hardened_yaml(
            block,
            kind="frontmatter",
            alias_policy=AliasPolicy.REJECT,
            max_bytes=FRONTMATTER_MAX_BYTES,
        )
    except (HardenedYamlError, yaml.YAMLError):
        # HardenedYamlError is a ValueError, not a YAMLError — catch it
        # explicitly or a refused alias would escape as a 500 instead of the
        # graceful "frontmatter_invalid" this contract promises.
        return None, "frontmatter_invalid"
    if not isinstance(data, dict):
        return None, "frontmatter_invalid" if data is not None else None
    return data, None


def clean_description(value: Any) -> Optional[str]:
    """The one blank rule for a capability description (trinity-enterprise#584).

    A capability is described once, in its skill's SKILL.md frontmatter
    ``description:`` (requirements/skills.md §23). Every surface agrees on
    whether a skill HAS one only if every place a description enters the
    backend applies this rule, so it is called at those chokepoints and
    nowhere else needs one: ``extract_contract`` (the skills library),
    ``connector_service.resolve_exposed_playbooks`` (the MCP connector and the
    Workspace briefing) and ``agent_skills_listing`` (the agent's live listing
    and its kept copy). It lives HERE, not in a module of its own, because the
    community skills library's CI downloads exactly this file and
    ``utils/safe_yaml.py`` to validate against: a new import would break it.

    The text rule mirrors the agent server's ``_coerce_optional_str``: a
    string passes, a YAML 1.1 scalar (bool, number, date) becomes its string
    form, a collection cannot be text. On top of that, blank means absent.
    """
    if value is None or isinstance(value, (list, dict, set, tuple)):
        return None
    text = (value if isinstance(value, str) else str(value)).strip()
    return text or None


def _as_str_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


# The successor is the AUTHOR's text, surfaced verbatim in the UI — so it is
# bounded here, once, rather than at each of the places that render it.
SUPERSEDED_BY_MAX_CHARS = 200
# trinity-enterprise#660: the author's category, shown as a filter option.
CATEGORY_MAX_CHARS = 64


def _first_present(sources: List[Dict[str, Any]], keys: Tuple[str, ...]) -> Any:
    """First non-None value of ``keys`` across ``sources``, source by source.

    Read per source rather than through the merged scope: a ``trinity:`` block
    must win whichever spelling it uses, and a key present but null must not
    mask the other spelling.
    """
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value is not None:
                return value
    return None


def _one_line(value: str, limit: int) -> Optional[str]:
    """``value`` as one printable, bounded line — or None when nothing is left.

    Control and format characters are dropped (a bidi override in a library
    string would otherwise reorder the text around it on screen) and every run
    of whitespace, newlines included, collapses to one space.
    """
    kept = "".join(ch for ch in value if ch.isprintable() or ch.isspace())
    return " ".join(kept.split())[:limit].rstrip() or None


def extract_contract(frontmatter: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[str]]:
    """Extract the skill contract from parsed frontmatter.

    Tolerant by construction: every field is isinstance-guarded, unknown keys
    ignored. Flat keys are the catalog convention (the agent-server Playbooks
    parser already reads flat ``automation:``); a ``trinity:`` mapping, when
    present, takes precedence (forward-compat namespace).

    Returns ``(contract, warnings)`` where contract is::

        {description, automation, user_invocable, allowed_tools,
         requires: {packages, binaries, env}, deprecated, superseded_by, approval,
         category}

    ``deprecated`` / ``superseded-by`` (trinity-enterprise#672) are the two
    lifecycle keys a library uses to retire a skill. ``superseded_by`` is the
    author's text, not a resolved skill name, and is None unless the skill is
    deprecated — see :func:`deprecation_warning` for the machine-usable form.
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

    # Both spellings (#3134): Claude Code and the libraries write the hyphen,
    # which wins inside one source; a `trinity:` block wins either way.
    sources = ([trinity_block] if isinstance(trinity_block, dict) else []) + [fm]
    user_invocable = _first_present(sources, ("user-invocable", "user_invocable"))
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

    # trinity-enterprise#584: the one blank rule, so the library and the agent
    # agree on whether a skill has a description (no new warning code: a
    # library whose CI runs this parser fails on `frontmatter_invalid*`).
    description = clean_description(fm.get("description"))

    # Lifecycle keys (#672). A malformed value is NAMED rather than ignored: a
    # library whose validation runs this parser fails on `frontmatter_invalid*`,
    # which is what keeps such a library and the platform agreeing.
    deprecated = _first_present(sources, ("deprecated",))
    if deprecated is not None and not isinstance(deprecated, bool):
        warnings.append("frontmatter_invalid:deprecated")
    deprecated = deprecated is True

    superseded_raw = _first_present(sources, ("superseded-by", "superseded_by"))
    superseded_by = None
    if isinstance(superseded_raw, str):
        if deprecated:
            superseded_by = _one_line(superseded_raw, SUPERSEDED_BY_MAX_CHARS)
    elif superseded_raw is not None:
        warnings.append("frontmatter_invalid:superseded-by")

    # trinity-enterprise#753: `approval: recommended` gates the skill by default
    # on every agent it is assigned to (the owner can clear it). A closed set; a
    # non-string (YAML hands `yes` as a bool, an unquoted date as a date) or an
    # unknown word is named, read as no recommendation, and never raises.
    approval_raw = _first_present(sources, ("approval",))
    approval = None
    if isinstance(approval_raw, str) and approval_raw.strip().lower() in APPROVAL_VALUES:
        approval = approval_raw.strip().lower()
    elif approval_raw is not None:
        warnings.append("frontmatter_invalid:approval")

    # trinity-enterprise#660: it only narrows a list, so garbage reads as no
    # category and is not named: `frontmatter_invalid:*` would ride every
    # injection of this skill on every agent, for a value nothing else reads.
    category_raw = _first_present(sources, ("category",))
    category = _one_line(category_raw, CATEGORY_MAX_CHARS) if isinstance(category_raw, str) else None

    contract = {
        "description": description,
        "automation": automation,
        "user_invocable": user_invocable,
        "allowed_tools": allowed_tools,
        "requires": requires,
        "deprecated": deprecated,
        "superseded_by": superseded_by,
        "approval": approval,
        "category": category,
    }
    return contract, warnings


def deprecation_warning(info: Dict[str, Any]) -> Optional[str]:
    """The per-skill warning code for a deprecated skill, or None (#672).

    The ONE producer of the grammar, shared by the injection result and the
    assign-time delivery report: ``deprecated:<successor>`` when the successor
    is a valid skill name, bare ``deprecated`` otherwise. The author's text is
    free prose, so it never enters a ``kind:detail`` code that other code
    splits and pattern-matches.
    """
    if info.get("deprecated") is not True:
        return None
    successor = info.get("superseded_by")
    if validate_skill_name(successor):
        return f"deprecated:{successor}"
    return "deprecated"


def is_deprecation_code(warning: Any) -> bool:
    """True for the code `deprecation_warning` produces, in either shape."""
    return warning == "deprecated" or (isinstance(warning, str) and warning.startswith("deprecated:"))


# #2991 / ent#334 — what a per-skill warning may say outside the backend. A
# warning's `kind:detail` detail can be free text (a path, a source label, an
# exception string), so a REST/MCP body carries only bare codes and the three
# kinds whose detail is a name by construction. One rule for every body that
# projects injection warnings: the start endpoint (`public_skills_result`) and
# the assign report (`skill_service._deliver_assigned`, #3136).
PUBLIC_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_PUBLIC_NAMED_WARNING_RE = re.compile(r"^(missing_binary|missing_env|deprecated):[A-Za-z0-9_.+-]{1,128}$")


def public_warnings(warnings: Any) -> List[str]:
    """The warnings safe to return to a caller, in order; anything else is dropped."""
    if not isinstance(warnings, (list, tuple)):
        return []
    return [w for w in warnings if isinstance(w, str)
            and (_PUBLIC_NAMED_WARNING_RE.match(w) or PUBLIC_CODE_RE.match(w))]


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


DEST_SKILLS_ROOT = ".claude/skills"


def filter_skill_archive(
    archive_bytes: bytes, skill_name: str, source_root: str = DEST_SKILLS_ROOT
) -> Tuple[List[Tuple[str, bytes, int]], List[str], int]:
    """Vet ``git archive HEAD -- <source_root>/<name>`` output.

    Returns ``(members, warnings, total_bytes)`` where members are
    ``(arcname, content, mode)`` for REGULAR files only. Symlink/dir/device
    members and litter/protected names are dropped with named warnings —
    an out-of-archive SYMTYPE member would raise KeyError inside the
    agent-server ``restore_from_tar`` mid-extraction, and a followed symlink
    at build time is an exfiltration vector, so links never ship at all.

    ent#332: this is the ONE point where the source repo's layout becomes the
    agent-side destination — every accepted member's arcname is REWRITTEN from
    ``<source_root>/<name>/…`` to ``.claude/skills/<name>/…``, so everything
    downstream (manifest, prune confinement, chmod/finalize paths, restore
    accounting, the legacy-fallback SKILL.md lookup) stays destination-
    canonical with no layout awareness. For the legacy layout the rewrite is
    an identity. ``source_root`` is validated defensively even though the only
    producer (`SkillSourceClone.skills_rel_root`) pre-validates: a traversal-
    shaped root reaching prefix math is a programming error worth failing loud.
    """
    if (
        not isinstance(source_root, str)
        or source_root.startswith(("/", "-"))
        or any(seg in ("", ".", "..") for seg in source_root.split("/"))
    ):
        raise ValueError(f"unsafe source_root: {source_root!r}")
    src_prefix = f"{source_root}/{skill_name}/"
    dest_prefix = f"{DEST_SKILLS_ROOT}/{skill_name}/"
    members: List[Tuple[str, bytes, int]] = []
    warnings: List[str] = []
    total = 0
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r") as tf:
        for member in tf.getmembers():
            name = member.name
            if member.isdir():
                continue
            if not name.startswith(src_prefix) or "/../" in f"/{name}/":
                warnings.append(f"restore_skipped:{name}")
                continue
            rel = name[len(src_prefix):]
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
            members.append((dest_prefix + rel, content, member.mode))
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


def compute_removal(
    previous_manifest: Optional[List[str]], skill_name: str
) -> Tuple[List[str], bool]:
    """Every path a prior injection wrote, for removal-on-unassign (ent#236).

    The unassign counterpart of :func:`compute_prune`, and deliberately built
    ON it rather than beside it: "delete everything the platform wrote" is
    exactly "prune against an empty new manifest", so the confinement,
    ``..``-rejection, and cap live in ONE place and cannot drift apart. The
    caller deletes only these paths, so agent-authored files and runtime
    artifacts inside the skill directory survive by construction — the same
    guarantee prune states.

    The generated ``.trinity-skill.json`` is appended LAST (prune filters it
    out, because a re-injection rewrites it). Ordering matters for the same
    reason ``build_injection_tar`` writes it last: while it exists the package
    is still platform-managed, so an interrupted removal is resumable.

    The meta is included even when the manifest is missing or unusable. Callers
    only reach this function for a package that HAS a meta (an unmanaged
    directory is filtered out one level up), so the meta file provably exists
    and is the marker that makes the directory removable at all — leaving it
    behind would keep the skill in every future reconcile's inventory with
    nothing left to delete, i.e. an unremovable package retried on every agent
    start. The one exception is truncation: there, files definitely remain, and
    dropping the meta would strand them as unmanaged orphans no later prune
    could reach, so the marker stays and the caller warns.

    Returns ``(paths_to_delete, truncated)``.
    """
    paths, truncated = compute_prune(previous_manifest, [], skill_name)
    if not truncated:
        paths = paths + [f".claude/skills/{skill_name}/{META_FILENAME}"]
    return paths, truncated
