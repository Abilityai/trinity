"""`schedules:` block — tolerant reader for template.yaml (trinity-enterprise#89).

A `template.yaml` is untrusted input: bundled ones are hand-authored, `github:`
ones come from arbitrary repos, and `local:` ones can be uploaded by any
authenticated user via `deploy_local_agent_logic`. `yaml.safe_load(...) or {}`
can yield a scalar, a list, or a mapping at any level, so every function here is
**total** — it degrades to a safe empty value and collects named errors, and it
NEVER raises. Three consumers depend on that:

  * `template_service._build_template` / `_build_local_template` — the catalog
    read path, where one raise empties the whole template list (#1835);
  * `agent_service.crud.reconcile_declared_schedules` — creation, where a raise
    would enter the destructive rollback fence;
  * the T-018 compatibility check, whose entire purpose is malformed-input
    tolerance.

This is a **leaf**: stdlib + `services.schedule_validation` only. It must stay
that way — `template_service` imports *this* module, so an import back would
close a cycle (see `_safe_echo` below).

Two public functions over one private `_parse`, mirroring the sibling
`credential_shape_errors` / `credential_mcp_server_names` convention
(trinity-enterprise#128). Sharing `_parse` is what makes the reported errors and
the accepted entries structurally unable to disagree.
"""

from typing import Any, Dict, List, Optional, Tuple

from services.schedule_validation import validate_cron_expression, validate_timezone

# A template declaring hundreds of schedules would mint hundreds of recurring
# autonomous LLM turns on one agent. The cap lives HERE rather than in the
# materializer so the catalog surface and creation inherit the same bound.
# (Verified: the platform has no other per-agent schedule cap.)
MAX_DECLARED_SCHEDULES = 20

# `agent_schedules.name` / `.description` / `.message` are unbounded TEXT and
# `ScheduleCreate` carries no `Field` constraints, so without these a template
# could push a 10 MB `message:` into the DB, the catalog response, and the logs.
MAX_NAME_LEN = 200
MAX_DESCRIPTION_LEN = 1000
MAX_MESSAGE_LEN = 10_000

DEFAULT_TIMEZONE = "UTC"

# YAML-flavoured type names ("mapping", not "dict"). Duplicated from
# `template_service._type_name` deliberately — importing it would close the
# cycle described in the module docstring.
_TYPE_NAMES = {
    type(None): "null",
    bool: "boolean",
    int: "number",
    float: "number",
    str: "string",
    list: "list",
    dict: "mapping",
}


def _type_name(value: Any) -> str:
    """YAML-flavoured type name for an error message."""
    return _TYPE_NAMES.get(type(value), type(value).__name__)


def _safe_echo(text: Any, max_len: int = 80) -> str:
    """Make an author-supplied string safe to echo in an error message.

    Twin of `template_service._sanitize_for_warning` (#950 L1) — strip
    non-printable characters (ANSI escapes, newlines, C0/C1 control bytes) so a
    crafted value cannot hijack a terminal rendering the error, and bound the
    length so it cannot flood the output.

    Deliberately duplicated rather than imported: `template_service` imports
    this module, so importing it back would close an import cycle. If these two
    are ever consolidated, move BOTH to a shared leaf.
    """
    cleaned = "".join(ch for ch in str(text) if ch.isprintable())
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len] + "..."
    return cleaned


def _required_str(entry: dict, key: str, index: int, errors: List[str],
                  max_len: Optional[int] = None) -> Optional[str]:
    """A required non-empty string field, or `None` with a named error.

    Errors name the **index, the key, and the type** — never the value. `name`
    and `message` are author-controlled, unbounded and prompt-injection-shaped,
    and this error list is persisted into `agent_compatibility_results.
    checks_json`, rendered in the UI, and returned in the catalog response.
    """
    value = entry.get(key)
    if value is None:
        errors.append(f"schedules[{index}]: missing required key '{key}'")
        return None
    if not isinstance(value, str):
        errors.append(
            f"schedules[{index}].{key}: expected a string, got {_type_name(value)}"
        )
        return None
    if not value.strip():
        errors.append(f"schedules[{index}].{key}: must not be empty")
        return None
    if max_len is not None and len(value) > max_len:
        errors.append(
            f"schedules[{index}].{key}: exceeds the {max_len}-character limit "
            f"({len(value)} characters)"
        )
        return None
    return value


def _parse(block: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The single implementation behind both public functions.

    Returns `(normalized_entries, errors)`. Total: any input shape yields a
    value, never an exception.
    """
    if block is None or block == []:
        return [], []

    if not isinstance(block, list):
        return [], [
            f"schedules: expected a list of {{name, cron, message}} entries, "
            f"got {_type_name(block)}"
        ]

    errors: List[str] = []
    entries = block
    if len(entries) > MAX_DECLARED_SCHEDULES:
        errors.append(
            f"schedules: {len(entries)} entries declared, only the first "
            f"{MAX_DECLARED_SCHEDULES} are materialized"
        )
        entries = entries[:MAX_DECLARED_SCHEDULES]

    out: List[Dict[str, Any]] = []
    seen_names = set()

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(
                f"schedules[{index}]: expected a mapping with 'name', 'cron' "
                f"and 'message', got {_type_name(entry)}"
            )
            continue

        # --- fatal shape/bounds checks: any failure drops the entry ---------
        name = _required_str(entry, "name", index, errors, MAX_NAME_LEN)
        cron = _required_str(entry, "cron", index, errors)
        message = _required_str(entry, "message", index, errors)
        if name is None or cron is None or message is None:
            continue

        if name in seen_names:
            errors.append(
                f"schedules[{index}].name: duplicate schedule name "
                f"'{_safe_echo(name)}' — only the first is materialized"
            )
            continue

        timezone = entry.get("timezone", DEFAULT_TIMEZONE)
        if timezone is None:
            timezone = DEFAULT_TIMEZONE
        if not isinstance(timezone, str):
            # `pytz.timezone(5)` raises AttributeError (it calls `.upper()`),
            # which `validate_timezone` does not catch — gate before calling.
            errors.append(
                f"schedules[{index}].timezone: expected a string, got "
                f"{_type_name(timezone)}"
            )
            continue

        try:
            validate_timezone(timezone)
        except Exception as e:  # noqa: BLE001 — see the cron handler below
            errors.append(
                f"schedules[{index}].timezone: unknown timezone "
                f"'{_safe_echo(timezone)}' ({type(e).__name__})"
            )
            continue

        # Strict cron, validated with the SAME parser the dedicated scheduler
        # registers with (#1472). This is a MATERIALIZATION gate, not a report
        # verdict: `_calculate_next_run_at` swallows a bad cron and returns
        # None, and `set_schedule_enabled` never re-validates — so an
        # unvalidated entry becomes a zombie schedule (a row that exists, shows
        # no next run, and can never fire).
        try:
            validate_cron_expression(cron, timezone)
        except Exception as e:  # noqa: BLE001
            # Broader than ScheduleValidationError on purpose: this module's
            # contract is totality, and pytz/apscheduler raise their own types
            # on shapes the str-gate above cannot anticipate. The problem is
            # still REPORTED, never swallowed.
            errors.append(
                f"schedules[{index}].cron: invalid cron expression "
                f"'{_safe_echo(cron)}' for timezone '{_safe_echo(timezone)}' "
                f"({type(e).__name__})"
            )
            continue

        # --- non-fatal normalizations: the entry survives ------------------
        # `purpose` is the abilities-plugin spelling of `description`.
        description = entry.get("description")
        if description is None:
            description = entry.get("purpose")
        if description is not None and not isinstance(description, str):
            errors.append(
                f"schedules[{index}].description: expected a string, got "
                f"{_type_name(description)}"
            )
            description = None
        elif isinstance(description, str) and len(description) > MAX_DESCRIPTION_LEN:
            errors.append(
                f"schedules[{index}].description: exceeds the "
                f"{MAX_DESCRIPTION_LEN}-character limit ({len(description)} "
                f"characters) — dropped"
            )
            description = None

        if len(message) > MAX_MESSAGE_LEN:
            errors.append(
                f"schedules[{index}].message: exceeds the {MAX_MESSAGE_LEN}-"
                f"character limit ({len(message)} characters) — truncated"
            )
            message = message[:MAX_MESSAGE_LEN]

        # Fail-safe: only a real bool enables a schedule. `enabled: "no"` is
        # truthy in Python, so a loose read would ARM a schedule its author
        # meant to leave off. Unspecified defaults to False (AC #3).
        enabled_raw = entry.get("enabled", False)
        if isinstance(enabled_raw, bool):
            enabled = enabled_raw
        else:
            errors.append(
                f"schedules[{index}].enabled: expected true or false, got "
                f"{_type_name(enabled_raw)} — treated as false"
            )
            enabled = False

        seen_names.add(name)
        out.append({
            "name": name,
            "cron": cron,
            "message": message,
            "enabled": enabled,
            "timezone": timezone,
            "description": description,
        })

    return out, errors


def schedule_shape_errors(block: Any) -> List[str]:
    """Named, operator-readable errors for a malformed `schedules:` block.

    An absent, null, or empty block all mean "this agent declares no
    schedules" and are NOT errors, so a template that comments its block out
    does not acquire a spurious warning. Never raises.
    """
    return _parse(block)[1]


def normalize_declared_schedules(block: Any) -> List[Dict[str, Any]]:
    """Well-formed declared schedules, tolerant of any input shape.

    Each entry is `{name, cron, message, enabled, timezone, description}` with
    every field already type-checked, bounded, and cron/timezone-validated —
    safe to hand straight to `ScheduleCreate`. Malformed entries are dropped
    (see `schedule_shape_errors` for why). Never raises.
    """
    return _parse(block)[0]
