"""
Trinity backend services.
"""
from .docker_service import (
    docker_client,
    get_agent_container,
    get_agent_status_from_container,
    list_all_agents,
    get_agent_by_name,
    get_next_available_port,
)
from .template_service import (
    get_github_template,
    extract_agent_credentials,
    generate_credential_files,
)

__all__ = [
    "docker_client",
    "get_agent_container",
    "get_agent_status_from_container",
    "list_all_agents",
    "get_agent_by_name",
    "get_next_available_port",
    "get_github_template",
    "extract_agent_credentials",
    "generate_credential_files",
]
