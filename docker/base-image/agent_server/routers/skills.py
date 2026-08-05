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


def scan_skills_directory(skills_dir: Path) -> List[SkillInfo]:
    """
    Scan a skills directory for subdirectories containing SKILL.md files.

    Returns list of SkillInfo objects sorted by name.
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

            # Extract skill info from frontmatter only
            name = frontmatter.get('name', entry.name)
            description = frontmatter.get('description')

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
                path=str(skill_md.relative_to(Path('/home/developer'))),
                user_invocable=user_invocable,
                automation=frontmatter.get('automation'),
                allowed_tools=frontmatter.get('allowed-tools'),
                argument_hint=frontmatter.get('argument-hint'),
                has_schedule=False  # TODO: Check if schedule exists for this skill
            )
            skills.append(skill)

        except Exception as e:
            logger.warning(f"Failed to parse skill at {skill_md}: {e}")
            # Still include the skill with minimal info
            skills.append(SkillInfo(
                name=entry.name,
                description=None,
                path=str(skill_md.relative_to(Path('/home/developer'))),
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
