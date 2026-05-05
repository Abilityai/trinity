"""
Skills Service - Git sync and skill management.

Manages the skills library:
- Sync from GitHub repository
- List available skills
- Get skill content
- Inject skills into running agents

Skills are stored in a GitHub repository with structure:
  .claude/skills/<name>/SKILL.md

The local clone is stored at /data/skills-library/
"""

import os
import re
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

from database import db
from services.settings_service import get_skills_library_url, get_skills_library_branch, get_github_pat
from services.agent_client import get_agent_client, AgentClientError
from utils.url_validation import validate_skills_library_url

logger = logging.getLogger(__name__)

# Local path for skills library clone
SKILLS_LIBRARY_PATH = Path("/data/skills-library")

# Issue #184 (UnderDefense pentest 3.3.1): override git's default
# User-Agent (`git/<version> (libcurl/...)`) on every HTTP-bearing git
# subcommand so outbound requests don't fingerprint the backend stack.
# The SSRF allowlist (#179) already locks the destination to github.com,
# but defense-in-depth: even if the allowlist is ever loosened, the UA
# stays generic. Constant intentionally has no version suffix to avoid
# yet another version string drifting against VERSION / package.json.
_GIT_HTTP_UA_ARGS = ["-c", "http.useragent=Trinity-Skills-Sync"]

class SkillService:
    """
    Service for managing skills library and skill assignments.

    The skills library is a GitHub repository containing SKILL.md files
    organized in .claude/skills/<name>/SKILL.md structure.
    """

    def __init__(self):
        self.library_path = SKILLS_LIBRARY_PATH
        self._last_sync: Optional[datetime] = None
        self._last_commit_sha: Optional[str] = None

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
    # Skill Discovery Operations
    # =========================================================================

    def list_skills(self) -> List[Dict[str, Any]]:
        """
        List all available skills from the library.

        Scans .claude/skills/*/SKILL.md files.

        Returns:
            List of skill info dicts with name, description, path
        """
        skills = []
        skills_dir = self.library_path / ".claude" / "skills"

        if not skills_dir.exists():
            logger.debug(f"Skills directory not found: {skills_dir}")
            return skills

        for skill_path in skills_dir.iterdir():
            if skill_path.is_dir():
                skill_file = skill_path / "SKILL.md"
                if skill_file.exists():
                    skill_info = self._parse_skill_info(skill_path.name, skill_file)
                    skills.append(skill_info)

        return sorted(skills, key=lambda s: s["name"])

    def _parse_skill_info(self, skill_name: str, skill_file: Path) -> Dict[str, Any]:
        """
        Parse skill information from SKILL.md file.

        Extracts description from frontmatter or first paragraph.
        """
        info = {
            "name": skill_name,
            "description": None,
            "path": f".claude/skills/{skill_name}/SKILL.md"
        }

        try:
            content = skill_file.read_text()

            # Try to extract description from frontmatter
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    frontmatter = parts[1]
                    for line in frontmatter.split("\n"):
                        if line.startswith("description:"):
                            info["description"] = line.split(":", 1)[1].strip().strip('"\'')
                            break

            # Fallback: first non-header paragraph
            if not info["description"]:
                lines = content.split("\n")
                for line in lines:
                    line = line.strip()
                    if line and not line.startswith("#") and not line.startswith("---"):
                        info["description"] = line[:200]  # Truncate
                        break

        except Exception as e:
            logger.warning(f"Failed to parse skill info for {skill_name}: {e}")

        return info

    def get_skill(self, skill_name: str) -> Optional[Dict[str, Any]]:
        """
        Get full details for a specific skill.

        Returns:
            Skill info dict with full content, or None if not found
        """
        skill_file = self.library_path / ".claude" / "skills" / skill_name / "SKILL.md"

        if not skill_file.exists():
            return None

        try:
            content = skill_file.read_text()
            info = self._parse_skill_info(skill_name, skill_file)
            info["content"] = content
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
            Dict with configuration status, sync info, and skill count
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
            "skill_count": 0
        }

        if self.library_path.exists():
            status["skill_count"] = len(self.list_skills())

        return status

    # =========================================================================
    # Skill Injection
    # =========================================================================

    async def inject_skills(
        self,
        agent_name: str,
        skill_names: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Inject skills into a running agent.

        Copies SKILL.md files to .claude/skills/<name>/SKILL.md in the agent.

        Args:
            agent_name: Name of the agent
            skill_names: List of skill names to inject, or None to use assigned skills

        Returns:
            Dict with injection results for each skill
        """
        if skill_names is None:
            skill_names = db.get_agent_skill_names(agent_name)

        if not skill_names:
            return {
                "success": True,
                "message": "No skills to inject",
                "skills_injected": 0
            }

        client = get_agent_client(agent_name)
        results = {}
        success_count = 0
        error_count = 0

        for skill_name in skill_names:
            skill = self.get_skill(skill_name)
            if not skill:
                results[skill_name] = {
                    "success": False,
                    "error": "Skill not found in library"
                }
                error_count += 1
                continue

            try:
                # Write skill to agent
                path = f".claude/skills/{skill_name}/SKILL.md"
                result = await client.write_file(path, skill["content"])

                if result.get("success"):
                    results[skill_name] = {"success": True}
                    success_count += 1
                else:
                    results[skill_name] = {
                        "success": False,
                        "error": result.get("error", "Write failed")
                    }
                    error_count += 1

            except AgentClientError as e:
                results[skill_name] = {
                    "success": False,
                    "error": str(e)
                }
                error_count += 1

        # Update CLAUDE.md with skills section so the agent knows what skills it has
        if success_count > 0:
            injected_skills = [name for name, res in results.items() if res.get("success")]
            await self._update_claude_md_skills_section(client, injected_skills)

        return {
            "success": error_count == 0,
            "skills_injected": success_count,
            "skills_failed": error_count,
            "results": results
        }

    async def _update_claude_md_skills_section(
        self,
        client,
        skill_names: List[str]
    ) -> None:
        """
        Update CLAUDE.md with a Platform Skills section.

        This tells the agent what skills it has available so it can
        answer questions like "what skills do you have?"
        """
        try:
            # Read current CLAUDE.md (path is relative to /home/developer)
            result = await client.read_file("CLAUDE.md")

            if not result.get("success"):
                logger.warning(f"Could not read CLAUDE.md: {result.get('error')}")
                return

            content = result.get("content") or ""

            # Build skills section
            skills_list = "\n".join([f"- `/{skill}` - Use with /{skill} command" for skill in sorted(skill_names)])
            skills_section = f"""

## Platform Skills

This agent has the following skills installed in `~/.claude/skills/`:

{skills_list}

Use these skills by invoking their slash commands (e.g., `/{skill_names[0] if skill_names else 'skill-name'}`).
"""

            # Remove existing Platform Skills section if present
            if "## Platform Skills" in content:
                # Find start and end of section
                start_idx = content.index("## Platform Skills")
                # Find next ## heading or end of content
                rest = content[start_idx + len("## Platform Skills"):]
                next_section = rest.find("\n## ")
                if next_section != -1:
                    end_idx = start_idx + len("## Platform Skills") + next_section
                    content = content[:start_idx].rstrip() + content[end_idx:]
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
