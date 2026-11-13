"""
Trinity backend services.
"""
from . import docker_service as _docker_service
from .docker_service import (
    get_docker_client,
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
    "get_docker_client",
    "get_agent_container",
    "get_agent_status_from_container",
    "list_all_agents",
    "get_agent_by_name",
    "get_next_available_port",
    "get_github_template",
    "extract_agent_credentials",
    "generate_credential_files",
]


def __getattr__(name):
    # #4406: `services.docker_client` is read at access time, so it follows a
    # reconnect instead of keeping the value from when this package was imported.
    if name == "docker_client":
        return _docker_service.get_docker_client()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
