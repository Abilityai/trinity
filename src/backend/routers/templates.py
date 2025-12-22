"""
Template routes for the Trinity backend.
"""
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
import yaml

from models import User
from config import ALL_GITHUB_TEMPLATES
from dependencies import get_current_user
from services.template_service import (
    get_github_template,
    extract_agent_credentials,
)

router = APIRouter(prefix="/api/templates", tags=["templates"])


@router.get("")
async def list_templates(current_user: User = Depends(get_current_user)):
    """List available agent templates (both local and GitHub-based)."""
    templates_dir = Path("/agent-configs/templates")
    if not templates_dir.exists():
        templates_dir = Path("./config/agent-templates")

    templates = []

    # Add GitHub-native templates first (production + testing)
    for gh_template in ALL_GITHUB_TEMPLATES:
        templates.append(gh_template)

    # Add local templates
    if templates_dir.exists():
        for template_path in templates_dir.iterdir():
            if template_path.is_dir():
                template_yaml = template_path / "template.yaml"
                if template_yaml.exists():
                    try:
                        with open(template_yaml) as f:
                            template_data = yaml.safe_load(f)

                        creds_info = extract_agent_credentials(template_path)

                        templates.append({
                            "id": f"local:{template_path.name}",
                            "display_name": template_data.get("display_name", template_path.name),
                            "description": template_data.get("description", ""),
                            "mcp_servers": template_data.get("mcp_servers", []),
                            "resources": template_data.get("resources", {"cpu": "2", "memory": "4g"}),
                            "source": "local",
                            "required_credentials": creds_info.get("required_credentials", [])
                        })
                    except Exception as e:
                        print(f"Error loading template {template_path}: {e}")
    return templates


@router.get("/env-template")
async def get_template_env_template(
    template_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get a .env template file with all required credential keys for a template.
    Returns text content that can be copied/downloaded and filled in.
    """
    try:
        required_credentials = []

        if template_id.startswith("github:"):
            gh_template = get_github_template(template_id)
            if not gh_template:
                raise HTTPException(status_code=404, detail="GitHub template not found")
            required_credentials = gh_template.get("required_credentials", [])
            template_name = gh_template.get("display_name", template_id)
        else:
            templates_dir = Path("/agent-configs/templates")
            if not templates_dir.exists():
                templates_dir = Path("./config/agent-templates")

            # Remove "local:" prefix if present
            if template_id.startswith("local:"):
                template_id = template_id[6:]

            template_path = templates_dir / template_id
            if not template_path.exists():
                raise HTTPException(status_code=404, detail="Template not found")

            creds_info = extract_agent_credentials(template_path)
            required_credentials = creds_info.get("required_credentials", [])

            template_yaml = template_path / "template.yaml"
            if template_yaml.exists():
                with open(template_yaml) as f:
                    template_data = yaml.safe_load(f)
                    template_name = template_data.get("display_name", template_id)
            else:
                template_name = template_id

        if not required_credentials:
            return {
                "template_id": template_id,
                "template_name": template_name,
                "content": "# No credentials required for this template\n",
                "credential_count": 0
            }

        # Group credentials by source/service
        grouped = {}
        for cred in required_credentials:
            # Handle both dict and string credentials
            if isinstance(cred, str):
                # If credential is just a string name
                group = "OTHER"
                cred_name = cred
            elif isinstance(cred, dict):
                # Normal dict credential with source and name
                source = cred.get("source", "other")
                if source.startswith("mcp:"):
                    group = source[4:].upper()
                elif source.startswith("template:mcp:"):
                    group = source[13:].upper()
                elif source == "env_file":
                    group = "ENV_FILE"
                else:
                    group = source.upper()
                cred_name = cred.get("name", str(cred))
            else:
                # Unknown type, skip
                continue

            if group not in grouped:
                grouped[group] = []
            grouped[group].append(cred_name)

        # Build the .env template content
        lines = [
            f"# Credentials template for: {template_name}",
            f"# Template ID: {template_id}",
            f"# Total credentials: {len(required_credentials)}",
            "#",
            "# Fill in the values and paste into Trinity's bulk import",
            "#",
            ""
        ]

        for group in sorted(grouped.keys()):
            lines.append(f"# === {group} ===")
            for cred_name in sorted(grouped[group]):
                lines.append(f"{cred_name}=")
            lines.append("")

        content = "\n".join(lines)

        return {
            "template_id": template_id,
            "template_name": template_name,
            "content": content,
            "credential_count": len(required_credentials)
        }
    except HTTPException:
        raise
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.exception(f"Error getting env template for {template_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate env template: {str(e)}")


@router.get("/{template_id:path}")
async def get_template(template_id: str, current_user: User = Depends(get_current_user)):
    """Get template details including required credentials.

    Note: Uses {template_id:path} to capture full path including slashes,
    which is needed for GitHub template IDs like 'github:org/repo'.
    """
    try:
        if template_id.startswith("github:"):
            gh_template = get_github_template(template_id)
            if gh_template:
                return gh_template
            raise HTTPException(status_code=404, detail="GitHub template not found")

        templates_dir = Path("/agent-configs/templates")
        if not templates_dir.exists():
            templates_dir = Path("./config/agent-templates")

        # Remove "local:" prefix if present
        if template_id.startswith("local:"):
            template_id = template_id[6:]

        template_path = templates_dir / template_id
        if not template_path.exists():
            raise HTTPException(status_code=404, detail="Template not found")

        template_yaml = template_path / "template.yaml"
        if not template_yaml.exists():
            raise HTTPException(status_code=404, detail="Template configuration not found")

        with open(template_yaml) as f:
            template_data = yaml.safe_load(f)

        creds_info = extract_agent_credentials(template_path)
        template_data["required_credentials"] = creds_info.get("required_credentials", [])
        template_data["mcp_servers_credentials"] = creds_info.get("mcp_servers", {})
        template_data["env_file_vars"] = creds_info.get("env_file_vars", [])

        return template_data
    except HTTPException:
        raise
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.exception(f"Error getting template {template_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to load template: {str(e)}")
