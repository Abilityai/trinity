"""
System manifest parsing and deployment service.

Handles YAML parsing, validation, agent naming, and deployment orchestration.
"""
import json
import os
import stat
import yaml

from utils.safe_yaml import (
    AliasPolicy,
    HardenedYamlError,
    load_hardened_yaml,
)
import re
import logging
from pathlib import Path
from typing import Callable, Dict, List, Tuple, Optional

from fastapi import HTTPException, Request

from models import (
    AgentConfig,
    SystemManifest,
    SystemAgentConfig,
    SystemPermissions,
    SystemViewConfig,
    BundledManifestDetail,
    BundledManifestSummary,
    MANIFEST_MAX_BYTES,
    SystemDeployFailure,
    SystemDeployResponse,
    SystemSchedulePreview,
    User,
)
from database import db
from db_models import ScheduleCreate, SystemViewCreate
from utils.credential_sanitizer import redact_url_userinfo, sanitize_text

logger = logging.getLogger(__name__)

# trinity-enterprise#125: cap on a single agent-create failure reason in the
# deploy report (the full text is still in the backend log).
_REASON_MAX_LEN = 500

# ent#126: every top-level manifest key `parse_manifest` reads. Kept next to the
# parser so adding a key to one without the other is visible in review.
_KNOWN_MANIFEST_KEYS = frozenset({
    "name", "description", "prompt", "agents", "permissions",
    "default_tags", "system_view",
})

# #2373: the per-agent equivalent. `parse_manifest` reads exactly these and
# dropped everything else silently.
_KNOWN_AGENT_KEYS = frozenset({
    "template", "resources", "folders", "schedules", "tags",
})


# --- Manifest YAML hardening (#1884, shared since ent#314) ----------------
#
# The guards themselves now live in `utils/safe_yaml.py`, which ent#314 made the
# single implementation for every author-controlled document (this manifest,
# `template.yaml`, skills frontmatter). Three near-copies had accumulated; the
# catalog path had none at all, which is what ent#314 fixes. Everything below is
# the manifest's POLICY — its budgets, its error type, its published codes — not
# a second copy of the mechanism.

# Bounds the INPUT. Deliberately not sufficient on its own: alias expansion is
# bounded by the budget below, because a few hundred bytes can expand to
# hundreds of MB while sitting comfortably under any size cap.
#
# Defined ONCE, in `models.py`, and imported above (ent#126 + #1884 + ent#314
# merge): the same number bounds this parse cap and the bundled-catalog file
# read below. A second local definition here would shadow the import silently
# and let the two drift apart — precisely the failure the shared constant
# exists to prevent.

# Bounds the EXPANSION COST, not the alias count — see utils/safe_yaml.py for
# why a `maxAliasCount` is the wrong shape for a bomb, and for the measurements.
MANIFEST_MAX_EXPANDED_NODES = 100_000


class ManifestError(HardenedYamlError):
    """A manifest the platform refuses to parse.

    A distinct type so the router can answer a NAMED 400 instead of letting a
    bomb surface as a request timeout or an unnamed 500 — which is the
    difference between "your manifest is malformed" and "Trinity is broken".

    ent#314: now a `HardenedYamlError` subclass, so the shared loader can raise
    exactly this type and every existing `except ManifestError` (and the
    router's code-to-400 mapping) keeps working unchanged.
    """


def _load_manifest_yaml(yaml_str: str):
    """Parse manifest YAML with the three guards applied.

    BUDGET rather than REJECT: a manifest may legitimately anchor a repeated
    agent block, and the measured budget already refuses the bomb (level 4 and
    up) while admitting the small honest anchor.
    """
    return load_hardened_yaml(
        yaml_str,
        kind="manifest",
        alias_policy=AliasPolicy.BUDGET,
        max_bytes=MANIFEST_MAX_BYTES,
        max_expanded_nodes=MANIFEST_MAX_EXPANDED_NODES,
        error_cls=ManifestError,
    )


def parse_manifest(yaml_str: str) -> SystemManifest:
    """
    Parse YAML string into SystemManifest.

    Raises:
        ManifestError: If the manifest is oversized, uses more aliases than the
            expansion budget allows, or carries a duplicate key (#1884). A
            subclass of ValueError, so existing callers catching ValueError are
            unaffected.
        ValueError: If required fields are missing.
    """
    data = _load_manifest_yaml(yaml_str)

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
    # #2373: per-agent unknown keys, collected the way top-level ones already are
    # (ent#126). Five keys are read and everything else was dropped in SILENCE —
    # `credentials:`, `skills:`, `display_label:` all vanished without a word,
    # and those are precisely the fields people try first. A manifest that half
    # works while saying nothing is worse than one that is refused.
    unknown_agent_keys = {}
    for agent_name, agent_config in data["agents"].items():
        if not isinstance(agent_config, dict):
            raise ValueError(f"Agent '{agent_name}' config must be an object")
        if "template" not in agent_config:
            raise ValueError(f"Agent '{agent_name}' missing required field: template")

        extra = sorted(str(k) for k in set(agent_config.keys()) - _KNOWN_AGENT_KEYS)
        if extra:
            unknown_agent_keys[str(agent_name)] = extra

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
        system_view=system_view,
        # ent#126: everything above is every key this parser reads. Anything else
        # is silently dropped, which is how `trinity_prompt:` (a typo for
        # `prompt:`) and `auto_start:` sat in a shipped manifest doing nothing.
        # Recorded, not rejected — `validate_manifest` turns these into warnings,
        # because rejecting would 400 manifests that deploy successfully today.
        # `str(k)` because YAML keys are not necessarily strings: YAML 1.1
        # renders bare `on`/`off`/`yes`/`no` as BOOLEANS and `2:` as an int, so
        # a mixed-type set makes `sorted` raise TypeError -> a raw 500 from a
        # manifest that deployed fine before this key check existed, and a
        # single non-string key fails the List[str] field with a raw Pydantic
        # dump. Both are exactly the unnamed-500 this warning exists to prevent.
        unknown_keys=sorted(str(k) for k in set(data.keys()) - _KNOWN_MANIFEST_KEYS),
        unknown_agent_keys=unknown_agent_keys,
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

    # trinity-enterprise#305: org-overlay namespaces are human-only, and this
    # writer bypasses the tags router's `_guard_org_namespace` (it calls
    # `db.set_agent_tags` directly). `deploy_system` is `require_role("creator")`,
    # which an agent-scoped key satisfies via its owner's role — so without
    # this check a prompt-injected agent could deploy a manifest that hangs a
    # fabricated node under a real manager. Rejected at validation, mirroring
    # the router guard.
    from db.tags import ORG_TAG_PREFIXES

    def _reject_org_tag(tag: str, where: str) -> None:
        normalized = tag.lower().strip()
        if normalized.startswith(ORG_TAG_PREFIXES):
            raise ValueError(
                f"{where} uses reserved org-overlay tag '{tag}': 'dept-*' and "
                "'reports-to-*' carry the org chart and can only be set by a "
                "human operator through the tags API"
            )

    # Validate default_tags
    if manifest.default_tags:
        for tag in manifest.default_tags:
            if not tag_pattern.match(tag.lower().strip()):
                raise ValueError(
                    f"Invalid tag '{tag}': must be lowercase alphanumeric and hyphens"
                )
            _reject_org_tag(tag, "default_tags")

    # Validate per-agent tags
    for agent_name, config in manifest.agents.items():
        if config.tags:
            for tag in config.tags:
                if not tag_pattern.match(tag.lower().strip()):
                    raise ValueError(
                        f"Agent '{agent_name}' has invalid tag '{tag}': "
                        "must be lowercase alphanumeric and hyphens"
                    )
                _reject_org_tag(tag, f"Agent '{agent_name}'")

    # Validate system_view name
    if manifest.system_view:
        if not manifest.system_view.name or not manifest.system_view.name.strip():
            raise ValueError("system_view.name is required")

    # ent#126: surface silently-dropped top-level keys. A warning, not an error:
    # rejecting would break manifests that deploy today. The common case is a
    # near-miss key name (`trinity_prompt` for `prompt`) whose intent is lost
    # with no signal anywhere.
    if manifest.unknown_agent_keys:
        for _agent, _keys in sorted(manifest.unknown_agent_keys.items()):
            warnings.append(
                f"Agent '{_agent}': ignored unknown key(s): {', '.join(_keys)}. "
                f"Recognised per-agent keys are: "
                f"{', '.join(sorted(_KNOWN_AGENT_KEYS))}."
            )

    if manifest.unknown_keys:
        warnings.append(
            "Ignored unknown top-level key(s): "
            f"{', '.join(manifest.unknown_keys)}. "
            "Recognized keys are: "
            f"{', '.join(sorted(_KNOWN_MANIFEST_KEYS))}."
        )

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

def resolve_permission_edges(
    agent_names: Dict[str, str],  # {short_name: final_name}
    permissions: Optional[SystemPermissions],
) -> Tuple[List[Tuple[str, List[str]]], int]:
    """Compute the permission write-set WITHOUT touching the database (ent#126).

    Returns ``(write_set, permissions_count)`` where ``write_set`` is the exact
    ordered sequence of ``db.set_agent_permissions(source, targets)`` calls the
    deploy would make, and ``permissions_count`` is the integer it would report.

    This exists so the dry-run preview and the real deploy compute topology from
    ONE piece of code: only the backend knows the resolved ``_N``-suffixed names,
    and a preview that re-derived the preset rules separately would drift from
    the writer the first time either changed. ``configure_permissions`` below is
    now a thin loop over this.

    An ordered list of pairs, not a dict, so the write SEQUENCE is faithful by
    construction — clearing an agent and then granting to it is observable
    ordering that a dict would silently normalise away.

    Every truthiness guard here is load-bearing and is pinned by
    ``tests/unit/test_ent126_permission_characterization.py``:

    * ``full-mesh``: ``if targets`` — a source with no targets is skipped
      entirely, never written as an empty list.
    * ``orchestrator-workers``: ``if workers`` guards the WHOLE body, so a lone
      orchestrator with zero workers writes nothing and counts 0; the count is
      ``len(workers)`` by assignment, and the worker-clearing writes are not
      counted.
    * ``none``: clears every agent but counts 0 — clearing is not "configuring".
    * ``explicit``: ``{}`` is falsy, so the branch is skipped and NOTHING is
      cleared (that is the ``none`` preset's job). Phase 1 clears every agent
      absent from ``explicit`` as a SOURCE — so a target-only agent is cleared
      first and granted-to second. Unknown targets are silently filtered;
      an unknown source is skipped.

    The unknown-source/target branches are unreachable from a manifest
    (``validate_manifest`` rejects them) but ARE reachable on the partial-deploy
    path, where the caller passes ``created_map`` — a subset of the resolved
    names. The preview is called with the FULL map, so it shows the optimistic
    topology; the UI says so.
    """
    if not permissions:
        return [], 0

    final_names = list(agent_names.values())
    write_set: List[Tuple[str, List[str]]] = []
    permissions_count = 0

    if permissions.preset == "full-mesh":
        # Every agent can communicate with every other agent
        for source in final_names:
            targets = [t for t in final_names if t != source]
            if targets:
                write_set.append((source, targets))
                permissions_count += len(targets)

    elif permissions.preset == "orchestrator-workers":
        # Only orchestrator can call workers
        # Workers cannot call anyone (clear their default permissions)
        orchestrator_short = "orchestrator"
        if orchestrator_short in agent_names:
            orchestrator = agent_names[orchestrator_short]
            workers = [n for n in final_names if n != orchestrator]
            if workers:
                # Set orchestrator permissions to call all workers
                write_set.append((orchestrator, workers))
                permissions_count = len(workers)

                # Clear worker permissions (set to empty list = clear all)
                for worker in workers:
                    write_set.append((worker, []))

    elif permissions.preset == "none":
        # No permissions - clear all default permissions for all system agents
        for agent in final_names:
            write_set.append((agent, []))

    elif permissions.explicit:
        # Apply explicit permission matrix
        # First, clear permissions for all system agents not in explicit config
        for short_name, final_name in agent_names.items():
            if short_name not in permissions.explicit:
                write_set.append((final_name, []))

        # Then set explicit permissions
        for source_short, target_shorts in permissions.explicit.items():
            source = agent_names.get(source_short)
            if not source:
                logger.warning(f"Unknown source agent in permissions: {source_short}")
                continue
            targets = [agent_names[t] for t in target_shorts if t in agent_names]
            # set_agent_permissions does a full replacement
            write_set.append((source, targets))
            permissions_count += len(targets)

    return write_set, permissions_count


def resolve_schedule_previews(
    agent_names: Dict[str, str],  # {short_name: final_name}
    agents_config: Dict[str, SystemAgentConfig],
) -> List[SystemSchedulePreview]:
    """Compute the schedules a deploy would create, WITHOUT writing them (ent#126).

    The schedule sibling of `resolve_permission_edges`, and `create_schedules`
    is now a thin loop over it. Mirrors that writer's iteration and its `.get()`
    defaults exactly — `enabled` defaulting to **True** is why a manifest that
    merely lists a schedule begins autonomous executions the moment it deploys,
    which is the thing the preview has to surface.

    Each preview is built through the same `ScheduleCreate` the writer constructs,
    so whatever that model rejects is rejected HERE, in the preview, instead of
    degrading to a post-deploy warning once the fleet already exists.

    **What that does NOT include: the cron expression.** `ScheduleCreate` declares
    `cron_expression: str` with no validator, and `validate_manifest` only checks
    that the key is PRESENT — so neither layer parses it. A syntactically invalid
    cron still previews clean, deploys clean, and surfaces only when the scheduler
    tries to arm it. Validating it here would change a shipped path (manifests with
    a bad cron deploy today), so it is left as a documented gap rather than
    silently half-fixed. What this DOES catch is a schedule entry whose field types
    the model rejects (e.g. a non-string `name`), which `validate_manifest`'s
    presence-only checks let through.

    **Nor does it model ent#89's duplicate-name skip.** `create_agent_internal`
    now materializes the TEMPLATE's own declared `schedules:`, and the writer
    skips any manifest schedule whose name already exists on the agent. This
    resolver is pure over the manifest and never reads a template, so a manifest
    schedule colliding with a template-declared one is previewed as "will be
    created" and is then skipped at deploy. The **armed** end state is still what
    the preview showed — the template created a schedule of that name — so what
    diverges is provenance and the report's `schedules_created` count, not
    whether the fleet ends up running that schedule. Closing it properly means
    resolving templates inside the preview, which is deferred rather than
    half-fixed here.

    Pinned by `tests/unit/test_ent126_schedule_characterization.py`; both the
    caught and the uncaught case are pinned in `test_ent126_dry_run_preview.py`.
    """
    previews: List[SystemSchedulePreview] = []

    for short_name, config in agents_config.items():
        final_name = agent_names.get(short_name)
        if not final_name or not config.schedules:
            continue

        for schedule_data in config.schedules:
            # The writer's own model, so a field type it rejects is a PREVIEW
            # blocker rather than a warning discovered after the fleet exists.
            # NOT a cron-syntax check — see the docstring.
            schedule_create = _build_schedule_create(schedule_data)
            previews.append(SystemSchedulePreview(
                agent=final_name,
                short_name=short_name,
                name=schedule_create.name,
                cron=schedule_create.cron_expression,
                message=schedule_create.message,
                enabled=schedule_create.enabled,
                timezone=schedule_create.timezone,
                description=schedule_create.description,
            ))

    return previews


def _build_schedule_create(schedule_data: dict) -> ScheduleCreate:
    """Map one manifest schedule entry onto `ScheduleCreate` (ent#126).

    The single mapping shared by the preview resolver and the writer, so the
    manifest key names (`cron` -> `cron_expression`) and the three `.get()`
    defaults cannot diverge between what a preview shows and what deploy writes.
    Fields the manifest never populates (timeout_seconds, model, allowed_tools,
    retries) keep their model defaults.
    """
    return ScheduleCreate(
        name=schedule_data["name"],
        cron_expression=schedule_data["cron"],
        message=schedule_data["message"],
        enabled=schedule_data.get("enabled", True),
        timezone=schedule_data.get("timezone", "UTC"),
        description=schedule_data.get("description")
    )


async def configure_permissions(
    agent_names: Dict[str, str],  # {short_name: final_name}
    permissions: Optional[SystemPermissions],
    created_by: str
) -> int:
    """
    Apply permission configuration based on preset or explicit rules.

    Thin writer over `resolve_permission_edges` (ent#126): the topology decision
    lives in the pure resolver the dry-run preview also uses, and this function
    only performs the writes and the logging. Behaviour is unchanged and pinned
    by `tests/unit/test_ent126_permission_characterization.py`.

    Args:
        agent_names: Mapping of short names to final agent names
        permissions: Permission configuration from manifest
        created_by: Username for audit trail

    Returns:
        Number of permissions configured
    """
    write_set, permissions_count = resolve_permission_edges(agent_names, permissions)

    for source, targets in write_set:
        db.set_agent_permissions(source, targets, created_by)
        if targets:
            logger.info(f"Granted {source} permissions to call: {targets}")
        else:
            logger.info(f"Cleared permissions for {source}")

    if permissions:
        mode = permissions.preset or ("explicit" if permissions.explicit else "none-specified")
        logger.info(
            f"Permission mode '{mode}': {len(write_set)} write(s), "
            f"{permissions_count} permission(s) granted"
        )

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

        # trinity-enterprise#89: this runs AFTER `create_agent_internal`, which
        # now materializes the TEMPLATE's declared `schedules:`. Without a
        # name-match skip, a manifest declaring `daily-briefing` on a template
        # that also declares it yields TWO rows — there is no
        # UNIQUE(agent_name, name) index (and adding one is a dual-track schema
        # change that would fail on installs already holding duplicates), so
        # idempotency has to be an explicit read-then-skip in the caller that
        # runs second. Failing open on a read error preserves the pre-#89
        # behaviour (create everything) rather than silently dropping a
        # manifest's schedules.
        try:
            existing_names = {
                s.name for s in db.list_agent_schedules(final_name)
            }
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"Could not read existing schedules for {final_name} "
                f"({e}); manifest schedules will be created unfiltered"
            )
            existing_names = set()

        for schedule_data in config.schedules:
            if schedule_data["name"] in existing_names:
                logger.info(
                    f"Skipping schedule '{schedule_data['name']}' for "
                    f"{final_name}: a schedule of that name already exists"
                )
                continue

            # ent#126: the manifest -> ScheduleCreate mapping is shared with
            # `resolve_schedule_previews`, so the dry-run preview shows exactly
            # the schedules (and `enabled` states) this writer will create.
            # NOTE (ent#89 merge): the skip above is a DEPLOY-side check the
            # preview cannot make — see `resolve_schedule_previews`.
            schedule_create = _build_schedule_create(schedule_data)

            # Create schedule in database
            schedule = db.create_schedule(
                agent_name=final_name,
                username=owner_username,
                schedule_data=schedule_create
            )

            if schedule:
                schedules_count += 1
                existing_names.add(schedule_data["name"])
                logger.info(f"Created schedule '{schedule_data['name']}' for {final_name}")
                # Dedicated scheduler syncs from database automatically
            else:
                logger.warning(f"Failed to create schedule '{schedule_data['name']}' for {final_name}")

    return schedules_count


# ============================================================================
# ORG-001 Phase 4: Tags and System View Functions
# ============================================================================

def _manifest_default_resources() -> dict:
    """The default resources a manifest agent gets when it declares none (#2373).

    Deploy hardcoded `{"cpu": "2", "memory": "4g"}` while `_preflight_template`
    validated against `settings_service.get_agent_default_resources()`, so
    preview and deploy disagreed the moment an admin moved the fleet default.
    Both now resolve through the create path's own `_get_default_resource`.

    Imported lazily for the same reason `_default_create_agent_fn` is:
    `agent_service.crud` imports back into this module's neighbourhood.
    """
    from services.agent_service.crud import _get_default_resource
    return {"cpu": _get_default_resource("cpu"),
            "memory": _get_default_resource("memory")}


def system_member_names(system_name: str, agent_names: List[str]) -> List[str]:
    """Which agents belong to `system_name` (#2373).

    THE ONE membership predicate. `get_system`, `restart_system` and
    `export_manifest` each matched `startswith(f"{system_name}-")`, so an
    operation on `acme` also captured every agent of a system named
    `acme-extra` — including `restart`, which stops and starts containers. Three
    copies of a wrong rule; this is one rule, and it is also the prerequisite
    for the system teardown verb, where the same collision would delete.

    Tags first, because `deploy_manifest` already applies the system name as a
    tag to every member (`configure_tags`) — that is a RECORD of membership,
    where a name prefix is an inference from a naming convention.

    The prefix is kept only as a fallback for systems deployed before tagging,
    and it is narrowed: an agent is excluded when it carries some other tag `T`
    whose own prefix claims it (`name.startswith(f"{T}-")`). So an `acme-extra`
    agent, tagged by its own deploy, is never captured by an operation on
    `acme` even on the fallback path.

    Residual, stated rather than hidden: two systems deployed BEFORE tagging,
    where one name is a prefix of the other, remain ambiguous — neither carries
    a tag to distinguish them. Tagging is what removes the ambiguity, and every
    system deployed since ent#124 has it.
    """
    if not system_name or not agent_names:
        return []
    try:
        tags_by_agent = db.get_tags_for_agents(list(agent_names)) or {}
    except Exception as e:  # noqa: BLE001 — membership must not 500 on a tag read
        logger.warning("system membership: tag read failed (%s); using prefix", e)
        tags_by_agent = {}

    tagged = [n for n in agent_names if system_name in (tags_by_agent.get(n) or [])]
    if tagged:
        return tagged

    prefix = f"{system_name}-"
    out = []
    for name in agent_names:
        if not name.startswith(prefix):
            continue
        claimed_elsewhere = any(
            t != system_name and name.startswith(f"{t}-")
            for t in (tags_by_agent.get(name) or [])
        )
        if not claimed_elsewhere:
            out.append(name)
    return out


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
    # #2373: the members, resolved ONCE through the shared predicate. The
    # permission branches below decide what is "inside the system", and they
    # must agree with `get_system`/`restart_system` about that — the caller
    # already filtered `agents`, so this is the same set, named so the
    # comprehensions can test membership instead of re-deriving it from a prefix.
    member_names = set(system_member_names(system_name, [a['name'] for a in agents]))

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
                        # #2373: the sibling branch below filters on membership
                        # and this one did not — a permission edge pointing
                        # OUTSIDE the system was blind-sliced into a garbage
                        # short name, which then failed validate_manifest's
                        # "unknown agent in permissions" check on re-deploy. The
                        # round trip was broken by the export, not the import.
                        explicit_perms = {}
                        _member_set = set(member_names)
                        for agent in agents:
                            short_name = agent['name'][len(system_name) + 1:]
                            agent_perms = db.get_agent_permissions(agent['name'])
                            targets = [
                                p["target_agent"][len(system_name) + 1:]
                                for p in agent_perms
                                if p["target_agent"] in _member_set
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
                        # #2373: membership, not the prefix — `acme` must not
                        # export an edge to an `acme-extra` agent as though it
                        # were its own member.
                        targets = [
                            p["target_agent"][len(system_name) + 1:]
                            for p in agent_perms
                            if p["target_agent"] in set(member_names)
                        ]
                        if targets:
                            explicit_perms[short_name] = targets

                    if explicit_perms:
                        manifest_dict["permissions"] = {"explicit": explicit_perms}

    except Exception as e:
        logger.warning(f"Failed to export permissions for system {system_name}: {e}")

    # #2373: the instance-global `trinity_prompt` is NOT exported.
    #
    # It was injected as the manifest's `prompt:`, so deploying an exported
    # manifest on another instance overwrote THAT instance's platform-wide
    # prompt with this one's — a fleet-wide side effect from what reads like a
    # copy of one system. Nothing records whether the source system ever set a
    # prompt, so there is no honest way to tell "this system's prompt" from
    # "whatever this instance happens to have configured"; the only correct
    # export of an unknown is to omit it.

    # Convert to YAML
    yaml_output = yaml.dump(manifest_dict, default_flow_style=False, sort_keys=False, allow_unicode=True)

    return yaml_output


# ============================================================================
# Deploy orchestration (moved verbatim from routers/systems.py, ent#124 —
# Invariant #1: a service (the first-run seeder) must reuse the deploy pipeline
# without importing a router; #1578 precedent)
# ============================================================================

# ============================================================================
# ent#126: bundled-manifest catalog (read-only)
#
# `config/manifests/` is bind-mounted read-only into the backend at
# /app/config/manifests by BOTH compose files, and WORKDIR is /app. Until now
# the only code reading that directory was the first-run seeder, against one
# hard-coded filename. These two functions turn it into a catalog the UI can
# offer as "pick a system to install".
# ============================================================================

# Env-overridable because the bare relative default is correct at runtime
# (WORKDIR /app + the :ro mount) but CWD-dependent under pytest — and a catalog
# that silently returns [] because the CWD differs is a silent failure. Read at
# CALL time, not import, so a test (or an operator) can point it elsewhere
# without reloading the module.
MANIFESTS_DIR_ENV = "TRINITY_MANIFESTS_DIR"
_DEFAULT_MANIFESTS_DIR = "config/manifests"

_MANIFEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_MANIFEST_SUFFIXES = (".yaml", ".yml")
# Generous for a real manifest name, and short enough that `{id}.yaml` can never
# reach the filesystem's own NAME_MAX. Without this an over-long id escapes as
# OSError(ENAMETOOLONG) from `os.open` and surfaces as a bare 500 instead of the
# 400 that a malformed id deserves.
_MANIFEST_ID_MAX_LEN = 128


def _manifests_dir() -> Path:
    return Path(os.getenv(MANIFESTS_DIR_ENV) or _DEFAULT_MANIFESTS_DIR)


def _resolve_manifest_path(manifest_id: str) -> Path:
    """Map a manifest id onto a confined path under the manifests dir.

    Raises ValueError on anything that is not a plain, in-directory id — the
    router maps that to 400.

    Layered on purpose, because no single check here is sufficient:

    1. Character allowlist. Rejects `/`, backslashes, NUL and (being ASCII-only)
       Unicode homoglyph tricks. FastAPI has already percent-decoded the path
       parameter, so `%2e%2e%2f` arrives as `../` and is caught here.
    2. EXPLICIT rejection of "", ".", ".." and any id containing "..". The regex
       above does NOT do this — `.` is inside the character class, so `..` and
       `...` match it happily. Relying on the regex for traversal is the mistake
       #1759 taught (a guard tested against 9 leak shapes missed 8).
    3. The suffix is ours by construction: the id is a STEM and we append
       `.yaml`/`.yml` ourselves, so a caller cannot steer the extension at all.
       A trailing `.yaml`/`.yml` in the id is tolerated and stripped, so both
       `default-system` and `default-system.yaml` address the same file.
    4. `.resolve()` both sides, then `is_relative_to`. This is what actually
       defeats a symlink inside the directory pointing outside it.
    """
    raw = (manifest_id or "").strip()
    if not raw or not _MANIFEST_ID_RE.match(raw):
        raise ValueError(
            "Invalid manifest id: must be letters, digits, dot, underscore or hyphen"
        )
    if len(raw) > _MANIFEST_ID_MAX_LEN:
        raise ValueError(
            f"Invalid manifest id: longer than {_MANIFEST_ID_MAX_LEN} characters"
        )
    # Tolerate (and normalise away) an explicit extension before the dot checks.
    lowered = raw.lower()
    for suffix in _MANIFEST_SUFFIXES:
        if lowered.endswith(suffix):
            raw = raw[: -len(suffix)]
            break
    if raw in ("", ".", "..") or ".." in raw:
        raise ValueError("Invalid manifest id: path traversal is not allowed")

    base = _manifests_dir().resolve()
    for suffix in _MANIFEST_SUFFIXES:
        entry = base / f"{raw}{suffix}"
        candidate = entry.resolve()
        if not candidate.is_relative_to(base):
            # A symlink inside the directory pointing outside it.
            raise ValueError("Invalid manifest id: resolves outside the manifests directory")
        # Parity with `list_bundled_manifests`, which skips symlinks outright.
        # Checked on the UNRESOLVED entry, because `.resolve()` has already erased
        # the link. Without this, a symlink whose target happens to be inside the
        # directory is invisible in the catalog yet readable by id — the two routes
        # would disagree about what the catalog contains, and "not listed" would
        # stop meaning "not served".
        if entry.is_symlink():
            raise ValueError("Invalid manifest id: symlinked manifests are not served")
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"No bundled manifest '{manifest_id}'")


def _read_manifest_text(path: Path) -> str:
    """Read a confined manifest path safely.

    Opened ONCE and fstat'd through that same descriptor, rather than
    stat-then-read: `config/manifests` is a host bind mount in both compose
    files, so a swap or a growth between a separate stat and a later read is a
    real window, and the file that was checked must be the file that is read.

    `O_NOFOLLOW` refuses a symlinked final component (the confinement check in
    `_resolve_manifest_path` covers where a symlink POINTS; this covers the
    TOCTOU race on the link itself), and at most `cap + 1` bytes are read so an
    oversized file is detected without being slurped into memory first.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("Not a regular file")
        if st.st_size > MANIFEST_MAX_BYTES:
            raise ValueError(
                f"Manifest is larger than {MANIFEST_MAX_BYTES} bytes"
            )
        with os.fdopen(fd, "rb") as fh:
            fd = -1  # ownership transferred to the file object
            raw = fh.read(MANIFEST_MAX_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)

    if len(raw) > MANIFEST_MAX_BYTES:
        raise ValueError(f"Manifest is larger than {MANIFEST_MAX_BYTES} bytes")
    return raw.decode("utf-8")


def _assess_manifest(yaml_text: str) -> Tuple[Optional[SystemManifest], bool, Optional[str]]:
    """Run the FULL three-stage check a deploy would run. -> (manifest, valid, reason)

    `parse_manifest` alone is not a validity check: it accepts invalid system and
    agent names, unsupported template prefixes and bogus permission presets, and
    on a non-mapping `agents:` value it raises AttributeError rather than
    ValueError. So a card marked "valid" on parsing alone would still fail to
    deploy. All three stages run — parse, validate, and the same side-effect-free
    template/resource preflight the dry-run uses — and the field is called `valid`
    only because all three ran. If this is ever reduced to parsing, rename it
    `parseable`.

    Every `reason` leaves here through `_failure_reason`, the same exit point the
    deploy report's `failed[].reason` uses — credential-sanitized, URL-userinfo
    redacted and length-capped. A raw `str(e)` would be neither: PyYAML's parse
    errors ECHO the offending source line (verified), and `validate_manifest`
    interpolates manifest-supplied values, so an uncapped reason grows with the
    file. Nothing a `creator` cannot already read via `GET /manifests/{id}`, which
    returns the raw YAML — but the catalog is the surface most likely to be widened
    to a looser role later, and this is the field a reader would trust.
    """
    try:
        manifest = parse_manifest(yaml_text)
    except Exception as e:  # noqa: BLE001 — incl. the AttributeError shape above
        return None, False, _failure_reason(e)[0]

    try:
        validate_manifest(manifest)
    except Exception as e:  # noqa: BLE001
        return manifest, False, _failure_reason(e)[0]

    # Base names, not `_N`-resolved ones: suffixing is a deploy-time concern and
    # resolving it here would mean a DB round trip per agent per card.
    blockers = [
        f"{short}: {failure.reason}"
        for short, failure in (
            (
                short,
                _preflight_template(
                    f"{manifest.name}-{short}", short, cfg.template, cfg.resources
                ),
            )
            for short, cfg in manifest.agents.items()
        )
        if failure is not None
    ]
    if blockers:
        # Each `failure.reason` is already capped; the JOIN of N of them is not.
        return manifest, False, "; ".join(blockers)[:_REASON_MAX_LEN]

    return manifest, True, None


def _summarize_manifest(path: Path, yaml_text: str) -> BundledManifestSummary:
    """Build one catalog entry. Never raises."""
    manifest, valid, reason = _assess_manifest(yaml_text)
    summary = BundledManifestSummary(
        id=path.stem,
        filename=path.name,
        valid=valid,
        reason=reason,
    )
    if manifest is None:
        return summary

    summary.name = manifest.name
    summary.description = manifest.description
    summary.agent_count = len(manifest.agents)
    summary.templates = sorted({c.template for c in manifest.agents.values() if c.template})
    summary.schedule_count = sum(
        len(c.schedules or []) for c in manifest.agents.values()
    )
    # A top-level `prompt:` OVERWRITES the platform-wide trinity_prompt for every
    # agent, so the UI needs it up front to gate deploy behind an acknowledgement.
    summary.sets_prompt = bool(manifest.prompt)
    summary.permissions_preset = (
        manifest.permissions.preset if manifest.permissions else None
    )
    try:
        summary.already_deployed = any(
            agent_exists(f"{manifest.name}-{short}") for short in manifest.agents
        )
    except Exception:  # noqa: BLE001 — a DB hiccup must not blank the catalog
        logger.warning(
            f"Could not determine deployed state for manifest '{path.name}'",
            exc_info=True,
        )
    return summary


def list_bundled_manifests() -> List[BundledManifestSummary]:
    """List the manifests shipped in `config/manifests/` (ent#126).

    Fail-soft per file: an unparseable, invalid or oversized manifest is listed
    with `valid: false` and a short reason rather than 500-ing the request. One
    bad file must not hide the others — that is precisely how a broken bundled
    manifest stays invisible.
    """
    directory = _manifests_dir()
    try:
        entries = sorted(
            child for child in directory.iterdir()
            if child.suffix.lower() in _MANIFEST_SUFFIXES
        )
    except (OSError, FileNotFoundError) as e:
        # A missing directory is an empty catalog, not an error — but say so,
        # because the alternative reading is a silently misconfigured CWD.
        logger.warning(f"Manifest directory '{directory}' is unreadable: {e}")
        return []

    summaries: List[BundledManifestSummary] = []
    seen_ids: set = set()
    for path in entries:
        if path.stem in seen_ids:
            # Both `x.yaml` and `x.yml` present: the id is the stem, so only the
            # first (.yaml, by sort order) is addressable. Say so rather than
            # listing an entry that resolves to a different file.
            logger.warning(
                f"Skipping '{path.name}': manifest id '{path.stem}' is already taken"
            )
            continue
        seen_ids.add(path.stem)
        # Checked BEFORE the read: `_read_manifest_text` opens with O_NOFOLLOW and
        # would fail on a symlink anyway, but as an unexplained ELOOP listed as a
        # broken manifest. A symlink is not a broken file, it is a file we decline
        # to serve, and the reason should say that.
        if path.is_symlink():
            logger.warning(f"Skipping '{path.name}': symlinked manifests are not served")
            continue
        try:
            text = _read_manifest_text(path)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not read bundled manifest '{path.name}': {e}")
            summaries.append(BundledManifestSummary(
                id=path.stem, filename=path.name, valid=False,
                reason=_failure_reason(e)[0],
            ))
            continue
        summaries.append(_summarize_manifest(path, text))
    return summaries


def read_bundled_manifest(manifest_id: str) -> BundledManifestDetail:
    """Read one bundled manifest plus its summary (ent#126).

    Raises ValueError (-> 400) on a malformed id and FileNotFoundError (-> 404)
    on an unknown one.
    """
    path = _resolve_manifest_path(manifest_id)
    text = _read_manifest_text(path)
    summary = _summarize_manifest(path, text)
    return BundledManifestDetail(**summary.model_dump(), manifest=text)


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
    final_name: str,
    short_name: str,
    template: Optional[str],
    resources: Optional[dict] = None,
) -> Optional[SystemDeployFailure]:
    """Can this agent's template resolve, with usable resources? (failure or None)

    Reuses the CREATE path's own resolver and its own validators rather than
    re-deriving "does this template exist" / "is this cpu usable", so the preview
    cannot drift from the deploy: the reason string and status code a caller sees
    here are produced by the same code that will produce them for real (#1841).

    Template scope, deliberately:
      * ``local:`` — resolved (a filesystem read, no side effects). This is
        where the cheap typo lives, and where #1793/#1759 made an unresolvable
        id a hard 404 instead of a silent blank agent.
      * ``github:`` — NOT probed. Validating it means a network call to GitHub
        with the platform PAT on a preview endpoint; slow, rate-limited, and a
        new outbound call on a path that had none. A dry run therefore still
        cannot promise a github-template manifest deploys.
      * no template — valid by construction (creates a bare agent by design).

    Resource validation (ent#126) closes a hole this function used to have. It
    checked template SHAPE only, so a manifest carrying an unusable resource
    value previewed as `valid` and then failed 100% of its agents at create —
    the exact class of defect #1841 exists to prevent, through a different door.
    A shipped bundled manifest had `cpu: 1.0` (an unquoted YAML float, which
    `normalize_cpu` rejects because it compares against the string set
    ``("1","2","4","8","16")``) and every deploy of it returned
    ``status: "failed"`` / HTTP 500 with a clean preview beforehand.

    The values checked are the MERGED ones, mirroring the create path's own
    precedence: `_resolve_local_template` overwrites `config.resources` with the
    template's block when the template declares one (`crud.py`
    ``config.resources = template_data.get("resources", config.resources)``), so
    a manifest value only survives when the template is silent — which is exactly
    the case the bundled manifest hit.

    For a ``github:`` template the merge cannot be computed without the network
    call this function refuses to make, so the manifest's DECLARED values are
    validated instead. That can over-report: if the remote template declares its
    own resources, an invalid manifest value would have been discarded and the
    deploy would have succeeded. Accepted deliberately — the alternative is
    staying silent about a value that is either fatal or dead config, and the fix
    (quote it, or use a supported value) is harmless in both cases.
    """
    # Lazy imports: crud imports service-layer modules, so a module-level import
    # here would close a cycle (same reason `_default_create_agent_fn` is lazy).
    from models import AgentConfig
    from services.agent_service.capabilities import normalize_cpu, normalize_memory
    from services.agent_service.crud import _get_default_resource, _resolve_local_template

    # A throwaway config: `_resolve_local_template` mutates the object it is
    # given (resources/tools/runtime from template.yaml). The manifest's
    # resources are COPIED in rather than handed over, so neither the resolver
    # nor the normalizers below (which write canonical values back) can mutate
    # the parsed manifest during what must stay a read-only preview.
    config = AgentConfig(
        name=final_name, template=template, resources=dict(resources or {})
    )

    try:
        if template and template.startswith("local:"):
            _resolve_local_template(config)

        # Same validators, same defaults, same actionable messages as create.
        normalize_cpu(config.resources.get("cpu"), _get_default_resource("cpu"))
        normalize_memory(config.resources.get("memory"), _get_default_resource("memory"))
    except Exception as e:  # noqa: BLE001 — mirrors the create loop's catch
        reason, status_code = _failure_reason(e)
        return SystemDeployFailure(
            name=final_name,
            short_name=short_name,
            template=template or "",
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
        except ManifestError as e:
            # #1884: a NAMED 400. The refusal must be distinguishable from a
            # generic parse error — "manifest_alias_budget_exceeded" tells an
            # operator what to change; a bare stack trace or a timeout does not.
            raise HTTPException(status_code=400, detail=f"{e.code}: {e}")
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
                        final_name,
                        short_name,
                        manifest.agents[short_name].template,
                        manifest.agents[short_name].resources,
                    )
                    for short_name, final_name in agent_names.items()
                ) if failure is not None
            ]

            # ent#126 (AC #2): the preview must show permission topology and
            # schedules, not just agent names. Both come from the pure resolvers
            # the real writers consume, so what is previewed is what deploys.
            #
            # `permission_edges` collapses the resolver's ordered write-set to
            # {source: targets} for display. Lossless for the set of writes —
            # each branch writes any given agent at most once (explicit's clear
            # and set phases are disjoint) — and write order carries no meaning
            # for a reader.
            #
            # Topology is OPTIMISTIC: it is resolved against the full agent map,
            # whereas a partial deploy configures permissions against
            # `created_map` (a subset). The UI states this.
            edges, _preview_permission_count = resolve_permission_edges(
                agent_names, manifest.permissions
            )
            try:
                schedules_preview = resolve_schedule_previews(
                    agent_names, manifest.agents
                )
            except Exception as e:  # noqa: BLE001
                # A schedule the writer could not construct (e.g. a field
                # ScheduleCreate rejects). Surfacing it as a preview BLOCKER is
                # the point: post-deploy it degrades to a warning once the fleet
                # already exists (deploy step 9).
                reason, status_code = _failure_reason(e)
                schedules_preview = []
                preview_failed.append(SystemDeployFailure(
                    name=manifest.name,
                    short_name="(schedules)",
                    template="",
                    reason=f"Invalid schedule definition: {reason}",
                    status_code=status_code,
                ))

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
                permission_edges=dict(edges),
                schedules_preview=schedules_preview,
                system_view_requested=bool(manifest.system_view),
                # `permissions_configured` / `schedules_created` deliberately stay
                # at their 0 defaults on this branch: they mean "written", and
                # changing a shipped field's meaning would mislead any existing
                # consumer. Callers count the new arrays instead.
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
                    # #2373: the ADMIN-CONFIGURABLE default, same resolver the
                    # preflight validates against. This was hardcoded
                    # `{"cpu": "2", "memory": "4g"}`, so preview validated one
                    # value and deploy created another the moment an admin moved
                    # the fleet default through
                    # `PUT /api/settings/agent-defaults/resources` — the one spot
                    # that escaped ent#126's pure-resolver no-drift pattern.
                    resources=config.resources or _manifest_default_resources()
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
            else:
                # ent#126: create_system_view swallows its exception and returns
                # None, so without this the only trace of a lost view was a log
                # line — the response looked identical to "no view requested"
                # and a caller would navigate to an unfiltered dashboard.
                all_warnings.append(
                    f"System View '{manifest.system_view.name}' was requested but "
                    "could not be created; the agents are still tagged with the "
                    "system name."
                )

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
            system_view_requested=bool(manifest.system_view),
            warnings=all_warnings,
            failed=failed
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"System deployment failed: {e}")
        raise HTTPException(status_code=500, detail=f"Deployment failed: {str(e)}")
