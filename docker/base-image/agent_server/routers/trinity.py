"""
Trinity injection API endpoints.
"""
import os
import json
import shutil
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException

from ..models import TrinityInjectRequest, TrinityInjectResponse, TrinityStatusResponse
from ..config import TRINITY_META_PROMPT_DIR, WORKSPACE_DIR

logger = logging.getLogger(__name__)
router = APIRouter()


def check_trinity_injection_status() -> dict:
    """Check if Trinity has been injected"""
    workspace = WORKSPACE_DIR

    files = {
        ".trinity/prompt.md": (workspace / ".trinity" / "prompt.md").exists(),
    }

    directories = {
        ".trinity": (workspace / ".trinity").exists(),
    }

    # Check if CLAUDE.md has Trinity section
    claude_md_path = workspace / "CLAUDE.md"
    claude_md_has_trinity = False
    if claude_md_path.exists():
        content = claude_md_path.read_text()
        claude_md_has_trinity = "## Trinity Agent System" in content

    return {
        "meta_prompt_mounted": TRINITY_META_PROMPT_DIR.exists(),
        "files": files,
        "directories": directories,
        "claude_md_has_trinity_section": claude_md_has_trinity,
        "injected": all(files.values()) and all(directories.values()) and claude_md_has_trinity
    }


@router.get("/api/trinity/status", response_model=TrinityStatusResponse)
async def get_trinity_status():
    """Check Trinity injection status"""
    status = check_trinity_injection_status()
    return TrinityStatusResponse(**status)


@router.post("/api/trinity/inject", response_model=TrinityInjectResponse)
async def inject_trinity(request: TrinityInjectRequest = TrinityInjectRequest()):
    """
    Inject Trinity meta-prompt and planning infrastructure.

    This endpoint:
    1. Copies prompt.md to .trinity/prompt.md
    2. Copies commands to .claude/commands/trinity/
    3. Creates plans/active and plans/archive directories
    4. Updates CLAUDE.md with Trinity section
    """
    workspace = WORKSPACE_DIR

    # Check if meta-prompt is mounted
    if not TRINITY_META_PROMPT_DIR.exists():
        return TrinityInjectResponse(
            status="error",
            error="Trinity meta-prompt not mounted at /trinity-meta-prompt"
        )

    # Check if already injected
    current_status = check_trinity_injection_status()
    if current_status["injected"] and not request.force:
        return TrinityInjectResponse(
            status="already_injected",
            already_injected=True
        )

    files_created = []
    directories_created = []

    try:
        # 1. Create .trinity directory and copy prompt.md
        trinity_dir = workspace / ".trinity"
        trinity_dir.mkdir(parents=True, exist_ok=True)
        directories_created.append(".trinity")

        prompt_src = TRINITY_META_PROMPT_DIR / "prompt.md"
        prompt_dst = trinity_dir / "prompt.md"
        if prompt_src.exists():
            shutil.copy2(prompt_src, prompt_dst)
            files_created.append(".trinity/prompt.md")
            logger.info(f"Copied {prompt_src} to {prompt_dst}")

        # 2. Update CLAUDE.md with Trinity section
        claude_md_updated = False
        claude_md_path = workspace / "CLAUDE.md"
        trinity_section = """

## Trinity Agent System

This agent is part of the Trinity Deep Agent Orchestration Platform.

### Agent Collaboration

You can collaborate with other agents using the Trinity MCP tools:

- `mcp__trinity__list_agents()` - See agents you can communicate with
- `mcp__trinity__chat_with_agent(agent_name, message)` - Delegate tasks to other agents

**Note**: You can only communicate with agents you have been granted permission to access.
Use `list_agents` to discover your available collaborators.

### Trinity System Prompt

Additional platform instructions are available in `.trinity/prompt.md`.
"""

        # Build the custom instructions section if provided
        custom_section = ""
        if request.custom_prompt and request.custom_prompt.strip():
            custom_section = f"""

## Custom Instructions

{request.custom_prompt.strip()}
"""
            logger.info("Custom prompt provided, will inject into CLAUDE.md")

        if claude_md_path.exists():
            content = claude_md_path.read_text()
            original_content = content

            # Remove existing Custom Instructions section if present (to update it)
            had_custom_instructions = False
            if "## Custom Instructions" in content:
                parts = content.split("## Custom Instructions")
                # Keep content before Custom Instructions
                content = parts[0].rstrip()
                # If there's content after (another section), this is tricky
                # For now, assume Custom Instructions is the last section
                had_custom_instructions = True
                logger.info("Removed existing Custom Instructions section")

            if "## Trinity Agent System" not in content:
                with open(claude_md_path, "a") as f:
                    f.write(trinity_section)
                    f.write(custom_section)
                claude_md_updated = True
                logger.info("Appended Trinity section to CLAUDE.md")
            elif custom_section or had_custom_instructions:
                # Trinity section exists, update file if custom section changed
                with open(claude_md_path, "w") as f:
                    f.write(content)
                    f.write(custom_section)
                claude_md_updated = True
                if custom_section:
                    logger.info("Updated Custom Instructions in CLAUDE.md")
                else:
                    logger.info("Removed Custom Instructions from CLAUDE.md")
        else:
            # Create minimal CLAUDE.md
            agent_name = os.getenv("AGENT_NAME", "Agent")
            with open(claude_md_path, "w") as f:
                f.write(f"# {agent_name}\n\nThis agent is managed by Trinity.\n")
                f.write(trinity_section)
                f.write(custom_section)
            claude_md_updated = True
            logger.info("Created CLAUDE.md with Trinity section")

        return TrinityInjectResponse(
            status="injected",
            already_injected=False,
            files_created=files_created,
            directories_created=directories_created,
            claude_md_updated=claude_md_updated
        )

    except Exception as e:
        logger.error(f"Trinity injection failed: {e}")
        return TrinityInjectResponse(
            status="error",
            error=str(e)
        )


@router.post("/api/trinity/reset")
async def reset_trinity():
    """
    Reset Trinity injection - remove all injected files and directories.
    """
    workspace = WORKSPACE_DIR
    files_removed = []
    directories_removed = []

    try:
        # Remove .trinity directory
        trinity_dir = workspace / ".trinity"
        if trinity_dir.exists():
            shutil.rmtree(trinity_dir)
            directories_removed.append(".trinity")

        # Remove .claude/commands/trinity directory
        commands_dir = workspace / ".claude/commands/trinity"
        if commands_dir.exists():
            shutil.rmtree(commands_dir)
            directories_removed.append(".claude/commands/trinity")

        # Note: We don't remove plans/ as that contains user data

        # Remove Trinity section from CLAUDE.md
        claude_md_path = workspace / "CLAUDE.md"
        if claude_md_path.exists():
            content = claude_md_path.read_text()
            if "## Trinity Agent System" in content:
                # Remove the Trinity section
                parts = content.split("## Trinity Agent System")
                if len(parts) > 1:
                    # Keep only the part before Trinity section
                    new_content = parts[0].rstrip()
                    with open(claude_md_path, "w") as f:
                        f.write(new_content)
                    files_removed.append("CLAUDE.md (Trinity section)")

        return {
            "status": "reset",
            "files_removed": files_removed,
            "directories_removed": directories_removed
        }

    except Exception as e:
        logger.error(f"Trinity reset failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
