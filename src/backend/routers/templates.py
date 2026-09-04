# mcp: agents.ts (list_templates)
"""
Template routes for the Trinity backend.
"""
from fastapi import APIRouter, Depends, HTTPException

from models import User
from dependencies import get_current_user
from services.template_service import (
    get_all_templates,
    get_github_template,
    get_local_template,
)

router = APIRouter(prefix="/api/templates", tags=["templates"])


@router.get("")
async def list_templates(current_user: User = Depends(get_current_user)):
    """List available agent templates.

    Includes both local templates (bundled under `config/agent-templates/`,
    `id` prefix `local:`) and GitHub-configured templates (`id` prefix
    `github:`). Local templates always available without network; GitHub
    metadata is cached 10 min per repo. (#843)
    """
    templates = get_all_templates()
    templates.sort(key=lambda t: (t.get("priority", 100), t.get("display_name", "")))
    return templates


@router.get("/{template_id:path}")
async def get_template(template_id: str, current_user: User = Depends(get_current_user)):
    """Get template details by id.

    Resolves both `github:owner/repo` and `local:<name>` ids. Uses
    `{template_id:path}` to capture slashes in GitHub ids.
    """
    if template_id.startswith("github:"):
        gh_template = get_github_template(template_id)
        if gh_template:
            return gh_template

    if template_id.startswith("local:"):
        local = get_local_template(template_id)
        if local:
            return local

    raise HTTPException(status_code=404, detail="Template not found")
