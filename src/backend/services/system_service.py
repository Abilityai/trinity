"""
System manifest parsing and deployment service.

Handles YAML parsing, validation, agent naming, and deployment orchestration.
"""
import json
import yaml
import re
import logging
from typing import Callable, Dict, List, Tuple, Optional

from fastapi import HTTPException, Request

from models import (
    AgentConfig,
    SystemManifest,
    SystemAgentConfig,
    SystemPermissions,
    SystemViewConfig,
    SystemDeployFailure,
    SystemDeployResponse,
    User,
)
from database import db
from db_models import ScheduleCreate, SystemViewCreate
from utils.credential_sanitizer import redact_url_userinfo, sanitize_text

logger = logging.getLogger(__name__)

# trinity-enterprise#125: cap on a single agent-create failure reason in the
# deploy report (the full text is still in the backend log).
_REASON_MAX_LEN = 500


def parse_manifest(yaml_str: str) -> SystemManifest:
    """
    Parse YAML string into SystemManifest.

    Raises:
        ValueError: If YAML is invalid or missing required fields
    """
    try:
        data = yaml.safe_load(yaml_str)
    except yaml.YAMLError as e:
        raise ValueError(f"YAML parse error: {str(e)}")

    if not data:
        raise ValueError("Empty manifest")

    if not isinstance(data, dict):
        raise ValueError("Manifest must be a YAML object")

    # Validate required fields
    if "name" not in data:
        raise ValueError("Missing required field: name")
    if "agents" not in data or not data["agents"]:
        raise ValueError("Missing required field: agents (must have at least 1)")

    # Parse agents
    agents = {}
    for agent_name, agent_config in data["agents"].items():
        if not isinstance(agent_config, dict):
            raise ValueError(f"Agent '{agent_name}' config must be an object")
        if "template" not in agent_config:
            raise ValueError(f"Agent '{agent_name}' missing required field: template")

        agents[agent_name] = SystemAgentConfig(
            template=agent_config["template"],
            resources=agent_config.get("resources"),
            folders=agent_config.get("folders"),
            schedules=agent_config.get("schedules"),
            tags=agent_config.get("tags")  # ORG-001 Phase 4
        )

    # Parse permissions
    permissions = None
    if "permissions" in data:
        perm_data = data["permissions"]
        permissions = SystemPermissions(
            preset=perm_data.get("preset"),
            explicit=perm_data.get("explicit")
        )

    # ORG-001 Phase 4: Parse default_tags
    default_tags = data.get("default_tags")

    # ORG-001 Phase 4: Parse system_view
    system_view = None
    if "system_view" in data:
        sv_data = data["system_view"]
        if isinstance(sv_data, dict) and "name" in sv_data:
            system_view = SystemViewConfig(
                name=sv_data["name"],
                icon=sv_data.get("icon"),
                color=sv_data.get("color"),
                shared=sv_data.get("shared", True)
            )

    return SystemManifest(
        name=data["name"],
        description=data.get("description"),
        prompt=data.get("prompt"),
        agents=agents,
        permissions=permissions,
        default_tags=default_tags,
        system_view=system_view
    )


def validate_manifest(manifest: SystemManifest) -> List[str]:
    """
    Validate manifest and return warnings.

    Returns:
        List of warning messages (empty if all valid)

    Raises:
        ValueError: If validation fails
    """
    warnings = []

    # Validate system name
    if not re.match(r'^[a-z0-9][a-z0-9-]{0,48}[a-z0-9]$|^[a-z0-9]{1,2}$', manifest.name):
        raise ValueError(
            f"Invalid system name '{manifest.name}': must be 1-50 chars, "
            "lowercase alphanumeric and hyphens, start/end with alphanumeric"
        )

    # Validate agent names
    for agent_name in manifest.agents.keys():
        if not re.match(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$', agent_name):
            raise ValueError(
                f"Invalid agent name '{agent_name}': must be lowercase alphanumeric and hyphens"
            )

    # Validate template references
    for agent_name, config in manifest.agents.items():
        template = config.template
        if not (template.startswith("github:") or template.startswith("local:")):
            raise ValueError(
                f"Agent '{agent_name}': template must start with 'github:' or 'local:'"
            )

    # Validate permissions
    if manifest.permissions:
        if manifest.permissions.preset and manifest.permissions.explicit:
            raise ValueError("Cannot specify both preset and explicit permissions")

        if manifest.permissions.preset:
            valid_presets = ["full-mesh", "orchestrator-workers", "none"]
            if manifest.permissions.preset not in valid_presets:
                raise ValueError(
                    f"Invalid permission preset '{manifest.permissions.preset}': "
                    f"must be one of {valid_presets}"
                )

            # Warn if orchestrator-workers but no orchestrator agent
            if manifest.permissions.preset == "orchestrator-workers":
                if "orchestrator" not in manifest.agents:
                    warnings.append(
                        "Permission preset 'orchestrator-workers' specified but no "
                        "'orchestrator' agent defined. No permissions will be granted."
                    )

        if manifest.permissions.explicit:
            agent_names = set(manifest.agents.keys())
            for source, targets in manifest.permissions.explicit.items():
                if source not in agent_names:
                    raise ValueError(f"Unknown agent in permissions: {source}")
                for target in targets:
                    if target not in agent_names:
                        raise ValueError(f"Unknown agent in permissions: {target}")

    # Validate schedules (if any)
    for agent_name, config in manifest.agents.items():
        if config.schedules:
            for i, schedule in enumerate(config.schedules):
                if "name" not in schedule:
                    raise ValueError(f"Agent '{agent_name}' schedule {i}: missing 'name'")
                if "cron" not in schedule:
                    raise ValueError(f"Agent '{agent_name}' schedule {i}: missing 'cron'")
                if "message" not in schedule:
                    raise ValueError(f"Agent '{agent_name}' schedule {i}: missing 'message'")

    # ORG-001 Phase 4: Validate tags
    tag_pattern = re.compile(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$')

    # Validate default_tags
    if manifest.default_tags:
        for tag in manifest.default_tags:
            if not tag_pattern.match(tag.lower().strip()):
                raise ValueError(
                    f"Invalid tag '{tag}': must be lowercase alphanumeric and hyphens"
                )

    # Validate per-agent tags
    for agent_name, config in manifest.agents.items():
        if config.tags:
            for tag in config.tags:
                if not tag_pattern.match(tag.lower().strip()):
                    raise ValueError(
                        f"Agent '{agent_name}' has invalid tag '{tag}': "
                        "must be lowercase alphanumeric and hyphens"
                    )

    # Validate system_view name
    if manifest.system_view:
        if not manifest.system_view.name or not manifest.system_view.name.strip():
            raise ValueError("system_view.name is required")

    return warnings


def agent_exists(name: str) -> bool:
    """Check if an agent with this name already exists."""
    # Check database for ownership record
    owner = db.get_agent_owner(name)
    return owner is not None


def get_next_agent_name(base_name: str) -> str:
    """
    Find next available name with _N suffix if base name exists.

    Examples:
        "my-agent" -> "my-agent" (if doesn't exist)
        "my-agent" -> "my-agent_2" (if my-agent exists)
        "my-agent" -> "my-agent_3" (if my-agent and my-agent_2 exist)
    """
    if not agent_exists(base_name):
        return base_name

    n = 2
    while agent_exists(f"{base_name}_{n}"):
        n += 1
    return f"{base_name}_{n}"


def resolve_agent_names(
    system_name: str,
    agents: Dict[str, SystemAgentConfig]
) -> Tuple[Dict[str, str], List[str]]:
    """
    Resolve short agent names to final names, handling conflicts.

    Args:
        system_name: The system name prefix
        agents: Dict of short_name -> agent config

    Returns:
        Tuple of (name_mapping, warnings)
        - name_mapping: {short_name: final_name}
        - warnings: List of conflict warnings
    """
    name_mapping = {}
    warnings = []

    for short_name in agents.keys():
        base_name = f"{system_name}-{short_name}"
        final_name = get_next_agent_name(base_name)
        name_mapping[short_name] = final_name

        if final_name != base_name:
            warnings.append(
                f"Agent '{base_name}' already exists, will create '{final_name}'"
            )

    return name_mapping, warnings


# ============================================================================
# Phase 2: Configuration Functions
# ============================================================================

async def configure_permissions(
    agent_names: Dict[str, str],  # {short_name: final_name}
    permissions: Optional[SystemPermissions],
    created_by: str
) -> int:
    """
    Apply permission configuration based on preset or explicit rules.

    Args:
        agent_names: Mapping of short names to final agent names
        permissions: Permission configuration from manifest
        created_by: Username for audit trail

    Returns:
        Number of permissions configured
    """
    if not permissions:
        return 0

    final_names = list(agent_names.values())
    permissions_count = 0

    if permissions.preset == "full-mesh":
        # Every agent can communicate with every other agent
        for source in final_names:
            targets = [t for t in final_names if t != source]
            if targets:
                db.set_agent_permissions(source, targets, created_by)
                permissions_count += len(targets)
                logger.info(f"Granted {source} permissions to call: {targets}")

    elif permissions.preset == "orchestrator-workers":
        # Only orchestrator can call workers
        # Workers cannot call anyone (clear their default permissions)
        orchestrator_short = "orchestrator"
        if orchestrator_short in agent_names:
            orchestrator = agent_names[orchestrator_short]
            workers = [n for n in final_names if n != orchestrator]
            if workers:
                # Set orchestrator permissions to call all workers
                db.set_agent_permissions(orchestrator, workers, created_by)
                permissions_count = len(workers)
                logger.info(f"Granted {orchestrator} permissions to call workers: {workers}")

                # Clear worker permissions (set to empty list = clear all)
                for worker in workers:
                    db.set_agent_permissions(worker, [], created_by)
                    logger.info(f"Cleared permissions for worker {worker}")

    elif permissions.preset == "none":
        # No permissions - clear all default permissions for all system agents
        for agent in final_names:
            db.set_agent_permissions(agent, [], created_by)
        logger.info(f"Cleared all permissions for {len(final_names)} agents (preset: none)")

    elif permissions.explicit:
        # Apply explicit permission matrix
        # First, clear permissions for all system agents not in explicit config
        for short_name, final_name in agent_names.items():
            if short_name not in permissions.explicit:
                db.set_agent_permissions(final_name, [], created_by)
                logger.info(f"Cleared default permissions for {final_name} (not in explicit config)")

        # Then set explicit permissions
        for source_short, target_shorts in permissions.explicit.items():
            source = agent_names.get(source_short)
            if not source:
                logger.warning(f"Unknown source agent in permissions: {source_short}")
                continue
            targets = [agent_names[t] for t in target_shorts if t in agent_names]
            # set_agent_permissions does a full replacement
            db.set_agent_permissions(source, targets, created_by)
            permissions_count += len(targets)
            if targets:
                logger.info(f"Granted {source} permissions to call: {targets}")
            else:
                logger.info(f"Cleared permissions for {source} (empty target list)")

    return permissions_count


def configure_folders(
    agent_names: Dict[str, str],  # {short_name: final_name}
    agents_config: Dict[str, SystemAgentConfig]
) -> int:
    """
    Configure shared folder settings for all agents.

    Args:
        agent_names: Mapping of short names to final agent names
        agents_config: Agent configurations from manifest

    Returns:
        Number of folder configs created
    """
    folders_configured = 0

    for short_name, config in agents_config.items():
        final_name = agent_names.get(short_name)
        if not final_name:
            continue

        if config.folders:
            expose = config.folders.get("expose", False)
            consume = config.folders.get("consume", False)

            db.upsert_shared_folder_config(
                agent_name=final_name,
                expose_enabled=expose,
                consume_enabled=consume
            )
            folders_configured += 1
            logger.info(f"Configured folders for {final_name}: expose={expose}, consume={consume}")

    return folders_configured


def create_schedules(
    agent_names: Dict[str, str],  # {short_name: final_name}
    agents_config: Dict[str, SystemAgentConfig],
    owner_username: str
) -> int:
    """
    Create schedules for all agents.

    Args:
        agent_names: Mapping of short names to final agent names
        agents_config: Agent configurations from manifest
        owner_username: Username for schedule ownership

    Returns:
        Number of schedules created
    """
    schedules_count = 0

    for short_name, config in agents_config.items():
        final_name = agent_names.get(short_name)
        if not final_name or not config.schedules:
            continue

        for schedule_data in config.schedules:
            schedule_create = ScheduleCreate(
                name=schedule_data["name"],
                cron_expression=schedule_data["cron"],
                message=schedule_data["message"],
                enabled=schedule_data.get("enabled", True),
                timezone=schedule_data.get("timezone", "UTC"),
                description=schedule_data.get("description")
            )

            # Create schedule in database
            schedule = db.create_schedule(
                agent_name=final_name,
                username=owner_username,
                schedule_data=schedule_create
            )

            if schedule:
                schedules_count += 1
                logger.info(f"Created schedule '{schedule_data['name']}' for {final_name}")
                # Dedicated scheduler syncs from database automatically
            else:
                logger.warning(f"Failed to create schedule '{schedule_data['name']}' for {final_name}")

    return schedules_count


# ============================================================================
# ORG-001 Phase 4: Tags and System View Functions
# ============================================================================

def configure_tags(
    system_name: str,
    agent_names: Dict[str, str],  # {short_name: final_name}
    agents_config: Dict[str, SystemAgentConfig],
    default_tags: Optional[List[str]] = None
) -> int:
    """
    Configure tags for all agents.

    - system_prefix is automatically added as a tag to all agents
    - default_tags (from manifest) are added to all agents
    - per-agent tags (from agent config) are added to specific agents

    Args:
        system_name: The system name prefix (auto-applied as tag)
        agent_names: Mapping of short names to final agent names
        agents_config: Agent configurations from manifest
        default_tags: Default tags to apply to all agents

    Returns:
        Total number of tags configured
    """
    tags_count = 0

    for short_name, config in agents_config.items():
        final_name = agent_names.get(short_name)
        if not final_name:
            continue

        # Build combined tag list
        combined_tags = []

        # 1. Auto-apply system_prefix as tag (always first)
        combined_tags.append(system_name)

        # 2. Add default_tags (from manifest root)
        if default_tags:
            combined_tags.extend(default_tags)

        # 3. Add per-agent tags
        if config.tags:
            combined_tags.extend(config.tags)

        # Normalize and dedupe tags
        normalized_tags = list(set(t.lower().strip() for t in combined_tags if t.strip()))

        if normalized_tags:
            db.set_agent_tags(final_name, normalized_tags)
            tags_count += len(normalized_tags)
            logger.info(f"Configured {len(normalized_tags)} tags for {final_name}: {normalized_tags}")

    return tags_count


def create_system_view(
    system_name: str,
    system_view: SystemViewConfig,
    default_tags: Optional[List[str]],
    owner_id: str
) -> Optional[str]:
    """
    Create a System View for the deployed system.

    The view filters by:
    - system_prefix tag (always included)
    - default_tags (if specified)

    Args:
        system_name: The system name prefix
        system_view: System view configuration from manifest
        default_tags: Default tags to include in filter
        owner_id: User ID for view ownership

    Returns:
        View ID if created, None if failed
    """
    try:
        # Build filter tags
        filter_tags = [system_name]  # Always include system prefix
        if default_tags:
            filter_tags.extend(default_tags)

        # Normalize and dedupe
        filter_tags = list(set(t.lower().strip() for t in filter_tags if t.strip()))

        # Create the view
        view_data = SystemViewCreate(
            name=system_view.name,
            description=f"Auto-created for {system_name} system deployment",
            icon=system_view.icon,
            color=system_view.color,
            filter_tags=filter_tags,
            is_shared=system_view.shared
        )

        view = db.create_system_view(owner_id, view_data)
        if view:
            logger.info(f"Created System View '{system_view.name}' with filter tags: {filter_tags}")
            return view.id
        else:
            logger.warning(f"Failed to create System View '{system_view.name}'")
            return None

    except Exception as e:
        logger.error(f"Error creating System View: {e}")
        return None


async def start_all_agents(agent_names: List[str]) -> Dict[str, str]:
    """
    Start all created agents.

    This triggers Trinity meta-prompt injection with the updated trinity_prompt.

    Args:
        agent_names: List of agent names to start

    Returns:
        Dict of {agent_name: status} where status is 'started' or error message
    """
    from routers.agents import start_agent_internal

    results = {}
    for agent_name in agent_names:
        try:
            result = await start_agent_internal(agent_name)
            results[agent_name] = "started"
            logger.info(f"Started agent '{agent_name}': {result}")
        except Exception as e:
            results[agent_name] = f"error: {str(e)}"
            logger.warning(f"Failed to start agent '{agent_name}': {e}")
            # Continue starting other agents even if one fails

    return results


def export_manifest(system_name: str, agents: List[Dict]) -> str:
    """
    Export a system as a YAML manifest.

    Args:
        system_name: The system prefix
        agents: List of agent dictionaries from Docker

    Returns:
        YAML string representing the system configuration
    """
    # Extract short names (remove system prefix)
    agent_configs = {}
    # Agents with no template label, whose manifest entry is inferred (#1759).
    templateless: List[str] = []
    for agent in agents:
        full_name = agent['name']
        # Remove system prefix and hyphen
        short_name = full_name[len(system_name) + 1:]

        # #1759: `or`, NOT a `.get` default. Every Blank Agent's dict carries
        # `"template": None` (routers/agents.py builds the label as
        # `config.template or ''` then `or None`), and `dict.get(key, default)`
        # returns the default only when the key is ABSENT — so the old
        # `local:business-assistant` fallback was unreachable dead code, and
        # blank agents have always exported `template: null`. Since
        # `SystemAgentConfig.template` is a non-Optional `str`, redeploying
        # such a manifest already failed Pydantic validation; and
        # `config/agent-templates/business-assistant` has never existed. This
        # is a pre-existing broken round-trip on the platform's most common
        # agent type, not a regression introduced by the create-time gate.
        # `local:default` is the truthful representation of a template-less
        # agent (a real, minimal template) — a product template like
        # `local:scout` would fabricate provenance.
        template = agent.get('template') or None
        if template is None:
            templateless.append(full_name)
            template = 'local:default'

        # Get agent details
        config = {
            "template": template
        }

        # Get resources (if available from labels)
        if agent.get('resources'):
            config["resources"] = agent['resources']

        # Get folders config from database
        try:
            folder_config = db.get_agent_folder_config(full_name)
            if folder_config and (folder_config['expose_enabled'] or folder_config['consume_enabled']):
                config["folders"] = {
                    "expose": bool(folder_config['expose_enabled']),
                    "consume": bool(folder_config['consume_enabled'])
                }
        except Exception as e:
            logger.warning(f"Failed to get folder config for {full_name}: {e}")

        # Get schedules from database
        try:
            schedules = db.list_agent_schedules(full_name)
            if schedules:
                config["schedules"] = [
                    {
                        "name": s.name,
                        "cron": s.cron_expression,
                        "message": s.message,
                        "enabled": bool(s.enabled),
                        "timezone": s.timezone
                    }
                    for s in schedules
                ]
        except Exception as e:
            logger.warning(f"Failed to get schedules for {full_name}: {e}")

        # ORG-001 Phase 4: Get tags from database
        try:
            agent_tags = db.get_agent_tags(full_name)
            # Filter out the system prefix tag (auto-applied on import)
            # so export shows only explicitly configured tags
            non_prefix_tags = [t for t in agent_tags if t != system_name]
            if non_prefix_tags:
                config["tags"] = non_prefix_tags
        except Exception as e:
            logger.warning(f"Failed to get tags for {full_name}: {e}")

        agent_configs[short_name] = config

    if templateless:
        # `export_manifest` returns a bare YAML string (routers/systems.py), so
        # there is no structured field to carry this — a log line is the only
        # non-contract-breaking channel (#1759).
        logger.warning(
            "Exported system '%s': %d agent(s) have no template label; their "
            "manifest entry was inferred as 'local:default': %s",
            system_name, len(templateless), ", ".join(sorted(templateless)),
        )

    # Build manifest dict
    manifest_dict = {
        "name": system_name,
        "description": f"Exported system configuration for {system_name}",
        "agents": agent_configs
    }

    # Get permissions (try to detect preset pattern)
    try:
        # Check first agent's permissions to infer pattern
        if agents:
            first_agent = agents[0]['name']
            perms = db.get_agent_permissions(first_agent)

            if perms:
                # Try to detect full-mesh pattern
                all_agents_names = [a['name'] for a in agents]
                permitted_targets = [p["target_agent"] for p in perms]

                # Full-mesh: agent can call all other agents
                expected_targets = [n for n in all_agents_names if n != first_agent]
                if set(permitted_targets) == set(expected_targets):
                    # Verify other agents also have full-mesh
                    is_full_mesh = True
                    for agent in agents:
                        agent_perms = db.get_agent_permissions(agent['name'])
                        expected = [n for n in all_agents_names if n != agent['name']]
                        actual = [p["target_agent"] for p in agent_perms]
                        if set(actual) != set(expected):
                            is_full_mesh = False
                            break

                    if is_full_mesh:
                        manifest_dict["permissions"] = {"preset": "full-mesh"}
                    else:
                        # Export explicit permissions
                        explicit_perms = {}
                        for agent in agents:
                            short_name = agent['name'][len(system_name) + 1:]
                            agent_perms = db.get_agent_permissions(agent['name'])
                            targets = [
                                p["target_agent"][len(system_name) + 1:]
                                for p in agent_perms
                            ]
                            if targets:
                                explicit_perms[short_name] = targets

                        if explicit_perms:
                            manifest_dict["permissions"] = {"explicit": explicit_perms}
                else:
                    # Export explicit permissions
                    explicit_perms = {}
                    for agent in agents:
                        short_name = agent['name'][len(system_name) + 1:]
                        agent_perms = db.get_agent_permissions(agent['name'])
                        targets = [
                            p["target_agent"][len(system_name) + 1:]
                            for p in agent_perms
                            if p["target_agent"].startswith(f"{system_name}-")
                        ]
                        if targets:
                            explicit_perms[short_name] = targets

                    if explicit_perms:
                        manifest_dict["permissions"] = {"explicit": explicit_perms}

    except Exception as e:
        logger.warning(f"Failed to export permissions for system {system_name}: {e}")

    # Get global trinity_prompt if it exists
    try:
        trinity_prompt = db.get_setting_value("trinity_prompt")
        if trinity_prompt:
            manifest_dict["prompt"] = trinity_prompt
    except Exception as e:
        logger.warning(f"Failed to get trinity_prompt: {e}")

    # Convert to YAML
    yaml_output = yaml.dump(manifest_dict, default_flow_style=False, sort_keys=False, allow_unicode=True)

    return yaml_output


# ============================================================================
# Deploy orchestration (moved verbatim from routers/systems.py, ent#124 —
# Invariant #1: a service (the first-run seeder) must reuse the deploy pipeline
# without importing a router; #1578 precedent)
# ============================================================================

def _default_create_agent_fn():
    """Resolve the agent-create function at call time.

    Deliberately the `routers/agents.py` FACADE — not `services.agent_service
    .crud.create_agent_internal` — because the facade injects `ws_manager`, so
    `agent_created` WebSocket broadcasts keep firing on every deploy path
    (HTTP and first-run seed alike). Lazy call-time import: an import-time edge
    back into a router would be a cycle (same pattern as `start_all_agents`).
    Unit seam: tests patch this function (or pass `create_agent_fn=`).
    """
    from routers.agents import create_agent_internal
    return create_agent_internal


def _preflight_template(
    final_name: str, short_name: str, template: Optional[str]
) -> Optional[SystemDeployFailure]:
    """Can this agent's template resolve? Returns a failure, or None if it can.

    Reuses the CREATE path's own resolver rather than re-deriving "does this
    template exist", so the preview cannot drift from the deploy: the reason
    string and status code a caller sees here are produced by the same code that
    will produce them for real (#1841).

    Scope, deliberately:
      * ``local:`` — resolved (a filesystem read, no side effects). This is
        where the cheap typo lives, and where #1793/#1759 made an unresolvable
        id a hard 404 instead of a silent blank agent.
      * ``github:`` — NOT probed. Validating it means a network call to GitHub
        with the platform PAT on a preview endpoint; slow, rate-limited, and a
        new outbound call on a path that had none. A dry run therefore still
        cannot promise a github-template manifest deploys.
      * no template — valid by construction (creates a bare agent by design).
    """
    if not template or not template.startswith("local:"):
        return None

    # Lazy import: crud imports service-layer modules, so a module-level import
    # here would close a cycle (same reason `_default_create_agent_fn` is lazy).
    from models import AgentConfig
    from services.agent_service.crud import _resolve_local_template

    try:
        # A throwaway config: `_resolve_local_template` mutates the object it is
        # given (type/resources/tools/runtime from template.yaml), which is why
        # the manifest's own config is never handed to it.
        _resolve_local_template(AgentConfig(name=final_name, template=template))
    except Exception as e:  # noqa: BLE001 — mirrors the create loop's catch
        reason, status_code = _failure_reason(e)
        return SystemDeployFailure(
            name=final_name,
            short_name=short_name,
            template=template,
            reason=reason,
            status_code=status_code,
        )
    return None


def _failure_reason(exc: Exception) -> Tuple[str, Optional[int]]:
    """Normalize an agent-create exception into a (reason, status_code) pair.

    HTTPException details may be dicts (e.g. QUOTA_EXCEEDED) — prefer their
    'error' field. Reasons are credential-sanitized and URL-userinfo-redacted
    at this exit point because git/GitHub errors can embed PAT-bearing remote
    URLs (learnings 2026-07-14) and the deploy report is a durable response
    surface (trinity-enterprise#125).
    """
    status_code: Optional[int] = None
    if isinstance(exc, HTTPException):
        status_code = exc.status_code
        detail = exc.detail
        if isinstance(detail, dict):
            reason = detail.get("error")
            if not isinstance(reason, str) or not reason:
                reason = json.dumps(detail, default=str)
        else:
            reason = str(detail)
    else:
        reason = str(exc)
    reason = sanitize_text(redact_url_userinfo(reason))
    return reason[:_REASON_MAX_LEN], status_code


async def deploy_manifest(
    manifest_yaml: str,
    current_user: User,
    request: Optional[Request] = None,
    *,
    dry_run: bool = False,
    strict: bool = False,
    create_agent_fn: Optional[Callable] = None,
) -> SystemDeployResponse:
    """
    Deploy a multi-agent system from a YAML manifest.

    This is a "recipe" deployment - agents become independent after creation.

    Best-effort by default (trinity-enterprise#125): a per-agent create failure
    is reported in `failed[]` and the remaining agents still deploy; callers
    must check `status`, not just exceptions. `status` is "deployed" (all
    created), "partial" (some failed), "failed" (none created — RETURNED, not
    raised; the HTTP router maps it to a 500 JSONResponse), or "valid"
    (dry_run). `strict=True` aborts on the first agent-create failure (legacy
    behavior), preserving the failure's original status code by raising
    HTTPException. Parse/validation errors raise HTTPException(400); any other
    unexpected error raises HTTPException(500) — callers see HTTPException
    uniformly (ent#124 verbatim-delta: the catch-all lives here, the
    failed→500 wrap lives in the router).
    """
    try:
        # 1. Parse YAML
        try:
            manifest = parse_manifest(manifest_yaml)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # 2. Validate manifest
        try:
            validation_warnings = validate_manifest(manifest)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # 3. Resolve agent names (handle conflicts)
        agent_names, name_warnings = resolve_agent_names(
            manifest.name,
            manifest.agents
        )
        all_warnings = validation_warnings + name_warnings

        # 4. If dry_run, return preview
        if dry_run:
            agents_to_create = [
                {
                    "name": final_name,
                    "short_name": short_name,
                    "template": manifest.agents[short_name].template
                }
                for short_name, final_name in agent_names.items()
            ]

            # #1841: a preview that only checks manifest SHAPE clears manifests
            # the real deploy then 404s on — a typo'd or renamed template id is
            # the cheapest mistake to make and precisely what a preview is for.
            # It matters more than it sounds: a partial deploy is expensive to
            # undo, because re-running the same manifest creates suffixed
            # duplicates of whatever already succeeded, so recovery is manual
            # and per-agent.
            preview_failed = [
                failure for failure in (
                    _preflight_template(
                        final_name, short_name, manifest.agents[short_name].template
                    )
                    for short_name, final_name in agent_names.items()
                ) if failure is not None
            ]

            return SystemDeployResponse(
                # "valid" keeps its meaning — a manifest that will deploy. A
                # preview that found blockers reports `invalid`, matching the
                # deploy path's own vocabulary (`partial` / `failed`) instead of
                # claiming success next to a populated failure list.
                status="invalid" if preview_failed else "valid",
                system_name=manifest.name,
                agents_created=[],
                agents_to_create=agents_to_create,
                prompt_updated=bool(manifest.prompt),
                warnings=all_warnings,
                failed=preview_failed,
            )

        # 5. Create all agents — best-effort by default (trinity-enterprise#125):
        # a per-agent failure is collected and the remaining agents still
        # deploy. `strict=True` restores abort-on-first-error, preserving the
        # failing agent's original status code. Each failed create self-cleans
        # via create_agent_internal's own rollback (#1484 _RollbackHandles).
        create_agent = create_agent_fn or _default_create_agent_fn()
        created_agents = []
        failed: list[SystemDeployFailure] = []
        for short_name, config in manifest.agents.items():
            final_name = agent_names[short_name]

            try:
                # Build AgentConfig for existing create_agent logic
                agent_config = AgentConfig(
                    name=final_name,
                    template=config.template,
                    resources=config.resources or {"cpu": "2", "memory": "4g"}
                )

                # Create agent using existing internal function
                await create_agent(
                    config=agent_config,
                    current_user=current_user,
                    request=request,
                    skip_name_sanitization=True  # Name already validated
                )

                created_agents.append(final_name)
                logger.info(f"Created agent '{final_name}' for system '{manifest.name}'")

            except Exception as e:
                reason, status_code = _failure_reason(e)
                logger.error(f"Failed to create agent '{final_name}': {reason}")

                if strict:
                    raise HTTPException(
                        status_code=status_code or 500,
                        detail={
                            "error": "Deployment failed",
                            "failed_at": final_name,
                            "created": created_agents,
                            "reason": reason
                        }
                    )

                failed.append(SystemDeployFailure(
                    name=final_name,
                    short_name=short_name,
                    template=config.template,
                    reason=reason,
                    status_code=status_code
                ))

        # 6. Total failure: nothing was created — return the full report; the
        # HTTP router wraps it in a non-2xx response so code-only callers
        # (curl -f) don't read it as success.
        if not created_agents:
            logger.error(
                f"System '{manifest.name}' deploy failed: 0/{len(agent_names)} agents created"
            )
            return SystemDeployResponse(
                status="failed",
                system_name=manifest.name,
                agents_created=[],
                prompt_updated=False,
                warnings=all_warnings,
                failed=failed
            )

        # 6b. Update trinity_prompt (if provided) — moved after the create loop
        # (trinity-enterprise#125) so a totally-failed deploy never mutates the
        # platform-wide prompt; injection happens at agent start (step 12).
        prompt_updated = False
        if manifest.prompt:
            db.set_setting("trinity_prompt", manifest.prompt)
            prompt_updated = True
            logger.info(f"Updated trinity_prompt for system '{manifest.name}'")

        # 6c. Scope post-create configuration to the agents that were actually
        # created (the config functions skip short_names missing from the map).
        created_set = set(created_agents)
        created_map = {s: f for s, f in agent_names.items() if f in created_set}

        if failed:
            failed_names = [f.name for f in failed]
            all_warnings.append(
                f"Partial deploy: {len(failed)} agent(s) failed to create: {failed_names}. "
                "Re-deploying this manifest will create suffixed duplicates of the "
                "already-created agents — fix the cause and create the missing agents "
                "individually."
            )
            # An orchestrator-workers system without its orchestrator is a
            # headless fleet: survivors keep zero inter-agent permissions.
            if (
                manifest.permissions
                and manifest.permissions.preset == "orchestrator-workers"
                and "orchestrator" in agent_names
                and agent_names["orchestrator"] not in created_set
            ):
                all_warnings.append(
                    "Orchestrator agent failed to create; the deployed workers have "
                    "no inter-agent permissions — the system may be non-functional."
                )

        # 7–11. Post-create configuration — each phase is best-effort
        # (trinity-enterprise#125): once agents exist, a config failure must
        # degrade to a warning, never abort the report the caller was promised.

        # 7. Configure shared folders (Phase 2)
        folders_configured = 0
        try:
            folders_configured = configure_folders(
                agent_names=created_map,
                agents_config=manifest.agents
            )
            logger.info(f"Configured {folders_configured} folder configs for system '{manifest.name}'")
        except Exception as e:
            reason, _ = _failure_reason(e)
            logger.exception(f"Folder configuration failed for system '{manifest.name}'")
            all_warnings.append(f"Failed to configure shared folders: {reason}")

        # 8. Configure permissions (Phase 2)
        permissions_count = 0
        if manifest.permissions:
            try:
                permissions_count = await configure_permissions(
                    agent_names=created_map,
                    permissions=manifest.permissions,
                    created_by=current_user.username
                )
                logger.info(f"Configured {permissions_count} permissions for system '{manifest.name}'")
            except Exception as e:
                reason, _ = _failure_reason(e)
                logger.exception(f"Permission configuration failed for system '{manifest.name}'")
                all_warnings.append(f"Failed to configure permissions: {reason}")

        # 9. Create schedules (Phase 2)
        schedules_count = 0
        try:
            schedules_count = create_schedules(
                agent_names=created_map,
                agents_config=manifest.agents,
                owner_username=current_user.username
            )
            logger.info(f"Created {schedules_count} schedules for system '{manifest.name}'")
        except Exception as e:
            reason, _ = _failure_reason(e)
            logger.exception(f"Schedule creation failed for system '{manifest.name}'")
            all_warnings.append(f"Failed to create schedules: {reason}")

        # 10. Configure tags (ORG-001 Phase 4)
        tags_count = 0
        try:
            tags_count = configure_tags(
                system_name=manifest.name,
                agent_names=created_map,
                agents_config=manifest.agents,
                default_tags=manifest.default_tags
            )
            logger.info(f"Configured {tags_count} tags for system '{manifest.name}'")
        except Exception as e:
            reason, _ = _failure_reason(e)
            logger.exception(f"Tag configuration failed for system '{manifest.name}'")
            all_warnings.append(f"Failed to configure tags: {reason}")

        # 11. Create System View (ORG-001 Phase 4, optional)
        # create_system_view self-guards (returns None on error).
        system_view_id = None
        if manifest.system_view:
            system_view_id = create_system_view(
                system_name=manifest.name,
                system_view=manifest.system_view,
                default_tags=manifest.default_tags,
                owner_id=str(current_user.id)
            )
            if system_view_id:
                logger.info(f"Created System View '{manifest.system_view.name}' (ID: {system_view_id}) for system '{manifest.name}'")

        # 12. Start all agents (triggers Trinity injection with updated prompt)
        start_results = await start_all_agents(created_agents)
        agents_started = sum(1 for status in start_results.values() if status == "started")
        agents_failed = len(created_agents) - agents_started

        if agents_failed > 0:
            failed_agents = [name for name, status in start_results.items() if status != "started"]
            all_warnings.append(f"Failed to start {agents_failed} agents: {failed_agents}")

        logger.info(f"Started {agents_started}/{len(created_agents)} agents for system '{manifest.name}'")

        return SystemDeployResponse(
            status="partial" if failed else "deployed",
            system_name=manifest.name,
            agents_created=created_agents,
            prompt_updated=prompt_updated,
            permissions_configured=permissions_count,
            schedules_created=schedules_count,
            tags_configured=tags_count,
            system_view_created=system_view_id,
            warnings=all_warnings,
            failed=failed
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"System deployment failed: {e}")
        raise HTTPException(status_code=500, detail=f"Deployment failed: {str(e)}")
