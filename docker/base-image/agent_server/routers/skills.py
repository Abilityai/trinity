"""
Skills listing endpoint for agent playbooks.

Scans .claude/skills/ directory for SKILL.md files, parses YAML frontmatter,
and returns skill metadata for the Playbooks tab in the Trinity UI.
"""
import re
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List

from fastapi import APIRouter
from pydantic import BaseModel

from ..safe_yaml import AliasPolicy, load_hardened_yaml

logger = logging.getLogger(__name__)
router = APIRouter()


class SkillInfo(BaseModel):
    """Information about a single skill/playbook."""
    name: str
    description: Optional[str] = None
    path: str
    user_invocable: bool = True
    automation: Optional[str] = None  # autonomous, gated, manual, null
    # Display metadata ONLY (#2850). Nothing enforces this list — the
    # `allowed_tools` that actually restricts a run comes from schedule / loop /
    # task config, never from skill frontmatter. Do not wire it into
    # enforcement without a validation step of its own.
    allowed_tools: Optional[List[str]] = None
    argument_hint: Optional[str] = None
    has_schedule: bool = False  # Placeholder for future schedule integration


class SkillsResponse(BaseModel):
    """Response for GET /api/skills endpoint."""
    skills: List[SkillInfo]
    count: int
    skill_paths: List[str]


def parse_yaml_frontmatter(content: str) -> Dict[str, Any]:
    """
    Parse YAML frontmatter from a SKILL.md file.

    Frontmatter is delimited by --- at the start and end:
    ---
    name: my-skill
    description: Does something
    ---
    """
    # Strip BOM (byte order mark) if present - common cause of parse failures
    if content.startswith('\ufeff'):
        content = content[1:]

    # Normalize line endings (CRLF -> LF)
    content = content.replace('\r\n', '\n').replace('\r', '\n')

    # Match YAML frontmatter at the start of the file
    # Allow optional whitespace around --- delimiters
    pattern = r'^---[ \t]*\n(.*?)\n[ \t]*---'
    match = re.match(pattern, content, re.DOTALL)

    if not match:
        logger.debug(f"No frontmatter found. Content starts with: {repr(content[:50])}")
        return {}

    try:
        # #1965: REJECT, matching `skill_packaging`'s policy for the same
        # frontmatter on the backend side. Skill files are agent-authored and
        # every consumer walks the parsed mapping.
        frontmatter = load_hardened_yaml(
            match.group(1),
            kind="frontmatter",
            alias_policy=AliasPolicy.REJECT,
        )
        if not isinstance(frontmatter, dict):
            logger.warning(f"Frontmatter is not a dict: {type(frontmatter)}")
            return {}
        return frontmatter
    except Exception as e:
        logger.warning(f"Failed to parse YAML frontmatter: {e}")
        return {}


class FieldSkipped(ValueError):
    """A frontmatter field holds a value the record cannot represent.

    Raised by the per-field normalizers below; the scanner catches it, drops
    THAT field to ``None`` and warns once — every other parsed field survives.
    Before #2850 the same situation raised out of the ``SkillInfo`` constructor
    and the whole record fell back to a bare directory name.
    """


# Warn-once ledger for skipped fields, keyed (skill path, field, shown value).
# `GET /api/skills` is polled by the Playbooks tab, the `/` typeahead and the
# chat empty state, so a per-scan warning repeated on every request (#2850).
# Keyed on the value too, so a field that is fixed and later re-broken is
# reported again. Bounded by the number of malformed fields in the workspace.
_SKIPPED_FIELD_WARNED: set = set()

# Only the container HOME is stripped for display; a path outside it (a mount,
# or a test tmp dir) falls back to the absolute path instead of raising.
_HOME = Path('/home/developer')

_GROUP_CLOSER = {'(': ')', '[': ']', '{': '}'}
_GROUP_CLOSERS = frozenset(_GROUP_CLOSER.values())


def _display_path(skill_md: Path) -> str:
    try:
        return str(skill_md.relative_to(_HOME))
    except ValueError:
        return str(skill_md)


def _report_skipped_field(skill_md: Path, field: str, value: Any, reason: str) -> None:
    shown = repr(value)[:120]
    key = (str(skill_md), field, shown)
    if key in _SKIPPED_FIELD_WARNED:
        logger.debug("Skipping frontmatter field %r in %s again (%s)", field, skill_md, reason)
        return
    _SKIPPED_FIELD_WARNED.add(key)
    logger.warning(
        "Skipping frontmatter field %r in %s (%s): %s — the other fields are kept",
        field, skill_md, reason, shown,
    )


def _split_tool_list(text: str) -> List[str]:
    """Split a comma-separated tool spec on the commas at nesting depth 0.

    Linear single-pass scan (no regex). `Bash(git *)` keeps its spaces and
    `Bash(npm run lint, npm test)` stays one entry. Only groups are tracked,
    not quotes: every comma that is part of a tool spec sits inside a group,
    and tracking quotes made an apostrophe in shell text (`Bash(echo it's)`)
    drop the whole field. An unbalanced group raises ``FieldSkipped`` rather
    than silently merging the tail into one entry.
    """
    entries: List[str] = []
    buf: List[str] = []
    open_groups: List[str] = []  # expected closers, innermost last

    for ch in text:
        if ch in _GROUP_CLOSER:
            open_groups.append(_GROUP_CLOSER[ch])
            buf.append(ch)
            continue
        if ch in _GROUP_CLOSERS:
            if not open_groups or open_groups[-1] != ch:
                raise FieldSkipped(f"unbalanced {ch!r}")
            open_groups.pop()
            buf.append(ch)
            continue
        if ch == ',' and not open_groups:
            entries.append(''.join(buf))
            buf = []
            continue
        buf.append(ch)

    if open_groups:
        raise FieldSkipped(f"unbalanced group, expected {open_groups[-1]!r}")
    entries.append(''.join(buf))
    return [entry.strip() for entry in entries if entry.strip()]


def normalize_allowed_tools(value: Any) -> Optional[List[str]]:
    """Normalize the two accepted `allowed-tools` spellings to one list.

    Claude Code's canonical form is the comma-separated string
    (`allowed-tools: Read, Bash, Bash(git:*)`); a YAML list
    (`allowed-tools: [Read, Bash]`) is accepted too. Both yield the same list.
    Absent / empty → ``None`` (unrestricted). Anything else — a bool from YAML
    1.1's `yes`, a mapping, a number — raises ``FieldSkipped``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        entries = _split_tool_list(value)
    elif isinstance(value, list):
        entries = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, (list, dict, bool)):
                # `[yes]` is a YAML 1.1 bool, not a tool named "True"
                raise FieldSkipped(f"list entry is a {type(item).__name__}, not a tool name")
            text = str(item).strip()
            if text:
                entries.append(text)
    else:
        raise FieldSkipped(
            f"expected a comma-separated string or a list, got {type(value).__name__}"
        )
    return entries or None


def _coerce_optional_str(value: Any) -> Optional[str]:
    """Text field: a string passes; a YAML 1.1 scalar (bool, int, float, date)
    becomes its string form; a collection cannot be text and is skipped."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (list, dict, set, tuple)):
        raise FieldSkipped(f"expected text, got {type(value).__name__}")
    return str(value)


def _coerce_argument_hint(value: Any) -> Optional[str]:
    """`argument-hint: [file]` is Claude Code's documented idiom, and unquoted
    it parses as a YAML flow sequence. Restore the bracketed string rather
    than dropping the hint."""
    if isinstance(value, list):
        if any(isinstance(item, (list, dict)) for item in value):
            raise FieldSkipped("nested collection in argument hint")
        items = [str(item) for item in value if item is not None]
        if not items:
            return None
        return '[' + ', '.join(items) + ']'
    return _coerce_optional_str(value)


def _field(skill_md: Path, field: str, value: Any, normalize) -> Any:
    try:
        return normalize(value)
    except FieldSkipped as e:
        _report_skipped_field(skill_md, field, value, str(e))
        return None


def scan_skills_directory(skills_dir: Path) -> List[SkillInfo]:
    """
    Scan a skills directory for subdirectories containing SKILL.md files.

    Returns list of SkillInfo objects sorted by name.

    SCOPE, stated because a caller cannot tell an empty result from an unscanned
    one (#2213): this walks ONE level and requires `SKILL.md` in each immediate
    subdirectory. Two things the agent can run are therefore invisible here —
    skills nested deeper, and skills provided by an installed PLUGIN (which live
    under the plugin's own cache, not under `.claude/skills/`).

    Enumerating plugin skills is deliberately OUT OF SCOPE for #2213 and needs its
    own change: it means reading the plugin cache layout, and it ships in the agent
    base image, so an instance only gets it after a base-image rebuild — old images
    would silently keep the old surface, which is the release-note trap ent#123
    already paid for once. The Workspace consequence is bounded and now honest: the
    `/` popup offers what this endpoint reports and says how many further playbooks
    exist without listing them, rather than implying the list is complete.
    """
    skills = []

    if not skills_dir.exists():
        return skills

    # Scan for subdirectories with SKILL.md
    for entry in skills_dir.iterdir():
        if not entry.is_dir():
            continue

        skill_md = entry / "SKILL.md"
        if not skill_md.exists():
            continue

        try:
            content = skill_md.read_text(encoding='utf-8')
            frontmatter = parse_yaml_frontmatter(content)

            # Each field is normalized on its own (#2850): a value the record
            # cannot represent drops THAT field to None and is warned once,
            # so the constructor below cannot raise on frontmatter content.
            name = _field(skill_md, 'name', frontmatter.get('name'), _coerce_optional_str) or entry.name
            description = _field(skill_md, 'description', frontmatter.get('description'), _coerce_optional_str)

            # Log if description is missing for debugging
            if not description:
                logger.debug(f"Skill '{name}' has no description in frontmatter")

            # Parse boolean fields properly
            user_invocable_raw = frontmatter.get('user-invocable', True)
            if isinstance(user_invocable_raw, str):
                user_invocable = user_invocable_raw.lower() in ('true', 'yes', '1')
            else:
                user_invocable = bool(user_invocable_raw)

            skill = SkillInfo(
                name=name,
                description=description,
                path=_display_path(skill_md),
                user_invocable=user_invocable,
                automation=_field(skill_md, 'automation', frontmatter.get('automation'), _coerce_optional_str),
                allowed_tools=_field(skill_md, 'allowed-tools', frontmatter.get('allowed-tools'), normalize_allowed_tools),
                argument_hint=_field(skill_md, 'argument-hint', frontmatter.get('argument-hint'), _coerce_argument_hint),
                has_schedule=False  # TODO: Check if schedule exists for this skill
            )
            skills.append(skill)

        except Exception as e:
            # Last resort — an unreadable file, not a bad field (those are
            # handled per field above and never reach here).
            logger.warning(f"Failed to parse skill at {skill_md}: {e}")
            # Still include the skill with minimal info
            skills.append(SkillInfo(
                name=entry.name,
                description=None,
                path=_display_path(skill_md),
            ))

    return skills


@router.get("/api/skills", response_model=SkillsResponse)
async def list_skills():
    """
    List all available skills (playbooks) from the agent's skills directories.

    Scans:
    - .claude/skills/ (project skills)
    - ~/.claude/skills/ (personal skills)

    Returns skill metadata parsed from SKILL.md YAML frontmatter.
    """
    home_dir = Path('/home/developer')

    # Skills directories to scan
    skill_paths = [
        home_dir / '.claude' / 'skills',
        Path.home() / '.claude' / 'skills'  # Personal skills
    ]

    all_skills: List[SkillInfo] = []
    scanned_paths: List[str] = []

    for skills_dir in skill_paths:
        # Convert to relative path for display
        if skills_dir.is_relative_to(home_dir):
            display_path = str(skills_dir.relative_to(home_dir))
        else:
            display_path = str(skills_dir).replace(str(Path.home()), '~')

        scanned_paths.append(display_path)

        skills = scan_skills_directory(skills_dir)
        all_skills.extend(skills)

    # Remove duplicates (by name), keeping project skills over personal
    seen_names = set()
    unique_skills = []
    for skill in all_skills:
        if skill.name not in seen_names:
            seen_names.add(skill.name)
            unique_skills.append(skill)

    # Sort by name
    unique_skills.sort(key=lambda s: s.name.lower())

    return SkillsResponse(
        skills=unique_skills,
        count=len(unique_skills),
        skill_paths=scanned_paths
    )
