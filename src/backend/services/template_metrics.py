"""`metrics:` block — tolerant reader for template.yaml (trinity-enterprise#477).

`template.yaml metrics:` has been a documented block with **no backend reader**
since it was written: the only code that ever looked at it lives inside the
agent container (`agent_server/routers/info.py`), which parses it afresh on
every read and validates nothing. ent#477 gives it a reader on the backend side
and a per-agent **registry** (`db/metric_definitions.py`), so ent#478's
`record_metrics` has a schema to validate points against and ent#479 has
definitions to serve.

A `template.yaml` is untrusted input: bundled ones are hand-authored, `github:`
ones come from arbitrary repos, and `local:` ones can be uploaded by any
authenticated user. `yaml.safe_load(...) or {}` can yield a scalar, a list, or a
mapping at any level, so every function here is **total** — it degrades to a
safe empty value and collects named errors, and it NEVER raises. Three consumers
depend on that (identical to the ent#89 `schedules:` and #1704 `plugins:`
readers):

  * `agent_service.crud._materialize_agent_files` — creation, where a raise
    would enter the destructive rollback fence;
  * `services/metric_registry.py` — the pull/start/refresh reconcile hooks,
    where a raise would have to be caught by every call site;
  * the D-009 compatibility check, whose entire purpose is malformed-input
    tolerance.

This is a **leaf**: stdlib only. `template_service` imports the sibling leaves,
so an import back would close a cycle — hence the duplicated
`_type_name` / `_safe_echo` / `_did_you_mean` below, mirroring
`template_schedules.py` and `template_plugins.py`. (Consolidating all three into
one shared leaf is registered debt D1; it is a refactor of three modules, not
part of this feature's diff.)

Two public functions over one private `_parse`, mirroring the sibling
`schedule_shape_errors` / `normalize_declared_schedules` convention (ent#89).
Sharing `_parse` is what makes the reported errors and the accepted entries
structurally unable to disagree.

## Drop policy

An entry with ANY error is **dropped** from the normalized list and named in the
errors list. The registry never holds a half-valid definition: ent#478 validates
incoming points against these rows, so a row assembled from the half of a
declaration that happened to parse would accept points its author never
declared. The report (D-009) is where a dropped entry becomes visible.

## Security

Error strings echo **indices, key names, type names and bounded closed-enum
values only** — never `name`, `label`, `description` or a status label. This
list is persisted into `agent_compatibility_results.checks_json` and rendered in
the UI (the ent#89 `_safe_echo` rule).
"""

import difflib
import hashlib
import json
import math
import re
from typing import Any, Dict, List, Optional, Tuple

# A template declaring hundreds of metrics would mint hundreds of registry rows
# per agent and hundreds of validated series in ent#478. The caps live HERE so
# the reader, the registry and the compatibility report inherit one bound.
MAX_DECLARED_METRICS = 50
MAX_STATUS_VALUES = 50
MAX_DIMENSIONS = 10
MAX_EXTENSION_KEYS = 20
MAX_EXTENSIONS_BYTES = 1024

# Every string here is persisted, returned by the definitions API and rendered.
MAX_LABEL_LEN = 200
MAX_DESCRIPTION_LEN = 1000
MAX_UNIT_LEN = 32
MAX_STATUS_VALUE_LEN = 64

# `name` is the JOIN KEY: ent#478 keys `metric_points` by `(agent_name, name)`
# and ent#479 renders it. Restricted to a charset that is safe as a JSON object
# key, a URL query value and a dimension key, and whose case cannot fork one
# metric into two rows (`Foo` vs `foo` are both invalid, not two keys).
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

METRIC_TYPES = ("counter", "gauge", "percentage", "status", "duration", "bytes")
DIRECTIONS = ("up_good", "down_good", "neutral")
AGGREGATIONS = ("last", "sum", "avg")

DEFAULT_DIRECTION = "neutral"
DEFAULT_AGGREGATION = "last"

# Same palette the dashboard status widget uses (`static_checks._WIDGET_COLORS`).
# Duplicated rather than imported: this is a leaf, and `static_checks` imports
# THIS module (function-locally, like T-018 does).
STATUS_COLORS = frozenset(
    {"green", "red", "yellow", "gray", "blue", "orange", "purple"}
)

# A cadence is the expected interval between points; ent#479's stale rule is
# "no point within 2x cadence". Below a minute it is not a cadence but a stream,
# and above a year it cannot say anything useful about freshness.
CADENCE_MIN_SECONDS = 60
CADENCE_MAX_SECONDS = 366 * 24 * 3600

# The keys a metric entry may carry. `x-` prefixed keys are the documented
# escape hatch and are NOT listed here (see `_split_extensions`).
_KNOWN_KEYS = frozenset({
    "name", "type", "label", "description", "unit",
    "warning_threshold", "critical_threshold", "values",
    "cadence", "direction", "aggregation", "dimensions",
})
_STATUS_VALUE_KEYS = frozenset({"value", "color", "label"})

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

    Used ONLY for closed-enum-shaped fields (`type`, `direction`,
    `aggregation`, `cadence`, `color`, and key NAMES) — the values that make an
    error actionable and are bounded by construction. `name` / `label` /
    `description` are never echoed: they are unbounded, prompt-injection-shaped,
    and this list is persisted into `agent_compatibility_results.checks_json`
    and rendered in the UI.

    Twin of `template_schedules._safe_echo`; duplicated rather than imported to
    keep this a leaf (see the module docstring, debt D1).
    """
    cleaned = "".join(ch for ch in str(text) if ch.isprintable())
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len] + "..."
    return cleaned


def _did_you_mean(key: str, allowed) -> str:
    """Nearest allowed key, for an unknown-key error.

    `difflib` rather than prefix matching: the typos worth catching are
    transpositions and near-misses (`labl`, `descriptoin`), and a prefix test
    catches none of them. Also normalizes `-` to `_` so a kebab-case author is
    pointed at the snake_case key rather than told nothing.

    Duplicated from `template_service._did_you_mean` for the leaf reason above.
    """
    lowered = key.lower().replace("-", "_")
    matches = difflib.get_close_matches(lowered, sorted(allowed), n=1, cutoff=0.6)
    return f" — did you mean '{matches[0]}'?" if matches else ""


# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------

_SHORT_CADENCE_RE = re.compile(r"^(\d+)([smhdw])$")
_ISO_WEEKS_RE = re.compile(r"^P(\d+)W$")
_ISO_CADENCE_RE = re.compile(
    r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$"
)

_SHORT_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_cadence(value: Any) -> Optional[int]:
    """Cadence → seconds, or `None` if it is not a well-formed fixed duration.

    Two accepted spellings, ONE parser (learning 2026-07-06: ent#479's stale
    rule reads the stored `cadence_seconds` and must never re-parse the raw
    string, or the two can disagree about the same template):

      * `<int><unit>` with unit ∈ `s m h d w` — `15m`, `1h`, `1d`, `2w`;
      * ISO 8601 `PnW`, or `P[nD][T[nH][nM][nS]]` — `PT15M`, `P1D`, `P1DT12H`.

    **Years and months are rejected on purpose.** `P1Y` / `P1M` are not fixed
    durations (a month is 28–31 days), so they cannot be normalized to a
    seconds count that a freshness rule can compare against. Floats,
    negatives, zero and out-of-range values are rejected too: bounds are
    `CADENCE_MIN_SECONDS` … `CADENCE_MAX_SECONDS`.

    Never raises — a non-string, `None`, or nonsense all yield `None`.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None

    seconds: Optional[int] = None
    short = _SHORT_CADENCE_RE.match(text)
    if short:
        seconds = int(short.group(1)) * _SHORT_UNIT_SECONDS[short.group(2)]
    else:
        weeks = _ISO_WEEKS_RE.match(text)
        if weeks:
            seconds = int(weeks.group(1)) * 604800
        elif text not in ("P", "PT"):
            iso = _ISO_CADENCE_RE.match(text)
            if iso and any(iso.groups()):
                days, hours, minutes, secs = (int(g or 0) for g in iso.groups())
                seconds = days * 86400 + hours * 3600 + minutes * 60 + secs

    if seconds is None:
        return None
    if not (CADENCE_MIN_SECONDS <= seconds <= CADENCE_MAX_SECONDS):
        return None
    return seconds


# ---------------------------------------------------------------------------
# Field helpers — each returns `(value, ok)`; `ok is False` drops the entry
# ---------------------------------------------------------------------------


def _optional_str(
    entry: dict, key: str, index: int, errors: List[str], max_len: int
) -> Tuple[Optional[str], bool]:
    """An optional string field. Absent/null is fine; a wrong type is not."""
    value = entry.get(key)
    if value is None:
        return None, True
    if not isinstance(value, str):
        # Never `str()` an untrusted YAML value: a shared alias expands during
        # the walk (443 B → 52 MB). Type-guard first, always.
        errors.append(
            f"metrics[{index}].{key}: expected a string, got {_type_name(value)}"
        )
        return None, False
    if len(value) > max_len:
        errors.append(
            f"metrics[{index}].{key}: exceeds the {max_len}-character limit "
            f"({len(value)} characters)"
        )
        return None, False
    return value, True


def _optional_number(
    entry: dict, key: str, index: int, errors: List[str]
) -> Tuple[Optional[float], bool]:
    """An optional finite numeric field. `bool` is excluded (it IS an `int`)."""
    value = entry.get(key)
    if value is None:
        return None, True
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(
            f"metrics[{index}].{key}: expected a number, got {_type_name(value)}"
        )
        return None, False
    if not math.isfinite(float(value)):
        # `.inf` / `.nan` are YAML natives. They survive a JSON round-trip only
        # as non-standard literals and mean nothing as a threshold.
        errors.append(
            f"metrics[{index}].{key}: expected a finite number"
        )
        return None, False
    return float(value), True


def _enum(
    entry: dict, key: str, index: int, errors: List[str],
    allowed: Tuple[str, ...], default: str,
) -> Tuple[str, bool]:
    """A closed-enum field with a default. The value IS echoed — it is bounded
    and is the only thing that makes the error actionable."""
    value = entry.get(key)
    if value is None:
        return default, True
    if not isinstance(value, str):
        errors.append(
            f"metrics[{index}].{key}: expected one of {list(allowed)}, got "
            f"{_type_name(value)}"
        )
        return default, False
    if value not in allowed:
        errors.append(
            f"metrics[{index}].{key}: expected one of {list(allowed)}, got "
            f"'{_safe_echo(value, 32)}'"
        )
        return default, False
    return value, True


def _status_values(
    entry: dict, index: int, errors: List[str], metric_type: str
) -> Tuple[Optional[List[Dict[str, Any]]], bool]:
    """The `values:` list, required iff `type: status`.

    A `status` metric without `values` has no domain to validate against, and a
    non-status metric WITH `values` is an author who believes a constraint is
    being enforced that is not — both are named, both drop the entry.
    """
    raw = entry.get("values")
    if metric_type != "status":
        if raw is not None:
            errors.append(
                f"metrics[{index}].values: only a 'status' metric declares "
                f"values, this one is '{_safe_echo(metric_type, 32)}'"
            )
            return None, False
        return None, True

    if raw is None:
        errors.append(
            f"metrics[{index}]: a 'status' metric must declare its 'values'"
        )
        return None, False
    if not isinstance(raw, list) or not raw:
        errors.append(
            f"metrics[{index}].values: expected a non-empty list of "
            f"{{value, color, label}} mappings, got {_type_name(raw)}"
        )
        return None, False
    if len(raw) > MAX_STATUS_VALUES:
        errors.append(
            f"metrics[{index}].values: {len(raw)} values declared, limit is "
            f"{MAX_STATUS_VALUES}"
        )
        return None, False

    out: List[Dict[str, Any]] = []
    seen = set()
    for vindex, item in enumerate(raw):
        where = f"metrics[{index}].values[{vindex}]"
        if not isinstance(item, dict):
            errors.append(
                f"{where}: expected a mapping with 'value', got "
                f"{_type_name(item)}"
            )
            return None, False
        # `key=str`: a YAML mapping key need not be a string (`1: x`, `~: x`,
        # `true: x` all parse), and a bare `sorted` over mixed types raises
        # TypeError — which this module's contract forbids (C2).
        unknown = sorted(set(item) - _STATUS_VALUE_KEYS, key=str)
        if unknown:
            errors.append(
                f"{where}: unknown key '{_safe_echo(unknown[0], 40)}'"
                f"{_did_you_mean(str(unknown[0]), _STATUS_VALUE_KEYS)}"
            )
            return None, False
        value = item.get("value")
        if not isinstance(value, str) or not value.strip():
            errors.append(
                f"{where}.value: expected a non-empty string, got "
                f"{_type_name(value)}"
            )
            return None, False
        if len(value) > MAX_STATUS_VALUE_LEN:
            errors.append(
                f"{where}.value: exceeds the {MAX_STATUS_VALUE_LEN}-character "
                f"limit ({len(value)} characters)"
            )
            return None, False
        if value in seen:
            errors.append(f"{where}.value: duplicate of an earlier value")
            return None, False
        color = item.get("color")
        if color is not None and not isinstance(color, str):
            errors.append(
                f"{where}.color: expected one of {sorted(STATUS_COLORS)}, got "
                f"{_type_name(color)}"
            )
            return None, False
        if isinstance(color, str) and color not in STATUS_COLORS:
            errors.append(
                f"{where}.color: expected one of {sorted(STATUS_COLORS)}, got "
                f"'{_safe_echo(color, 32)}'"
            )
            return None, False
        label = item.get("label")
        if label is not None and not isinstance(label, str):
            errors.append(
                f"{where}.label: expected a string, got {_type_name(label)}"
            )
            return None, False
        if isinstance(label, str) and len(label) > MAX_LABEL_LEN:
            errors.append(
                f"{where}.label: exceeds the {MAX_LABEL_LEN}-character limit "
                f"({len(label)} characters)"
            )
            return None, False
        seen.add(value)
        out.append({"value": value, "color": color, "label": label})
    return out, True


def _dimensions(
    entry: dict, index: int, errors: List[str]
) -> Tuple[List[str], bool]:
    """The allowed dimension keys for this metric (ent#478 validates points
    against them). Same charset as `name` — they become JSON object keys."""
    raw = entry.get("dimensions")
    if raw is None:
        return [], True
    if not isinstance(raw, list):
        errors.append(
            f"metrics[{index}].dimensions: expected a list of keys, got "
            f"{_type_name(raw)}"
        )
        return [], False
    if len(raw) > MAX_DIMENSIONS:
        errors.append(
            f"metrics[{index}].dimensions: {len(raw)} declared, limit is "
            f"{MAX_DIMENSIONS}"
        )
        return [], False
    out: List[str] = []
    for dindex, key in enumerate(raw):
        if not isinstance(key, str) or not _NAME_RE.match(key):
            errors.append(
                f"metrics[{index}].dimensions[{dindex}]: expected a key "
                f"matching {_NAME_RE.pattern}, got {_type_name(key)}"
            )
            return [], False
        if key in out:
            errors.append(
                f"metrics[{index}].dimensions[{dindex}]: duplicate key"
            )
            return [], False
        out.append(key)
    return out, True


def _split_extensions(
    entry: dict, index: int, errors: List[str]
) -> Tuple[Dict[str, Any], bool]:
    """The `x-` escape hatch: preserved verbatim, bounded, never interpreted.

    Kept rather than dropped so a tool that writes its own annotations into a
    metric declaration (ent#482's wizards, ent#483's validator) survives a
    round-trip through the registry.
    """
    extensions = {k: v for k, v in entry.items() if str(k).startswith("x-")}
    if not extensions:
        return {}, True
    if len(extensions) > MAX_EXTENSION_KEYS:
        errors.append(
            f"metrics[{index}]: {len(extensions)} 'x-' keys declared, limit is "
            f"{MAX_EXTENSION_KEYS}"
        )
        return {}, False
    try:
        # default=str: the hardened loader yields dates for unquoted YAML
        # dates, and an extension value is author-shaped by definition.
        encoded = json.dumps(extensions, default=str, sort_keys=True)
    except (TypeError, ValueError):
        errors.append(f"metrics[{index}]: 'x-' keys are not representable")
        return {}, False
    if len(encoded.encode("utf-8")) > MAX_EXTENSIONS_BYTES:
        errors.append(
            f"metrics[{index}]: 'x-' keys exceed the "
            f"{MAX_EXTENSIONS_BYTES}-byte limit"
        )
        return {}, False
    return extensions, True


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


def _parse(block: Any) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The single implementation behind both public functions.

    Returns `(normalized_entries, errors)`. Total: any input shape yields a
    value, never an exception.
    """
    if block is None or block == []:
        return [], []

    if not isinstance(block, list):
        return [], [
            f"metrics: expected a list of {{name, type}} entries, got "
            f"{_type_name(block)}"
        ]

    errors: List[str] = []
    entries = block
    if len(entries) > MAX_DECLARED_METRICS:
        errors.append(
            f"metrics: {len(entries)} entries declared, only the first "
            f"{MAX_DECLARED_METRICS} are registered"
        )
        entries = entries[:MAX_DECLARED_METRICS]

    out: List[Dict[str, Any]] = []
    seen_names: Dict[str, int] = {}

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(
                f"metrics[{index}]: expected a mapping with 'name' and 'type', "
                f"got {_type_name(entry)}"
            )
            continue

        # `key=str` for the same reason as `_status_values` above: mixed-type
        # keys are ordinary YAML and must produce a named error, never a raise.
        unknown = sorted(
            (
                k for k in entry
                if not (isinstance(k, str) and (k in _KNOWN_KEYS or k.startswith("x-")))
            ),
            key=str,
        )
        if unknown:
            errors.append(
                f"metrics[{index}]: unknown key "
                f"'{_safe_echo(unknown[0], 40)}'"
                f"{_did_you_mean(str(unknown[0]), _KNOWN_KEYS)}"
            )
            continue

        name = entry.get("name")
        if name is None:
            errors.append(f"metrics[{index}]: missing required key 'name'")
            continue
        if not isinstance(name, str):
            errors.append(
                f"metrics[{index}].name: expected a string, got "
                f"{_type_name(name)}"
            )
            continue
        if not _NAME_RE.match(name):
            # The value is NOT echoed (unbounded, author-controlled); the
            # pattern is what makes this actionable.
            errors.append(
                f"metrics[{index}].name: must match {_NAME_RE.pattern} "
                f"(lowercase, starts with a letter, ≤64 characters)"
            )
            continue
        if name in seen_names:
            errors.append(
                f"metrics[{index}].name: duplicate of metrics"
                f"[{seen_names[name]}] — only the first is registered"
            )
            continue

        raw_type = entry.get("type")
        if raw_type is None:
            errors.append(f"metrics[{index}]: missing required key 'type'")
            continue
        if not isinstance(raw_type, str):
            errors.append(
                f"metrics[{index}].type: expected one of {list(METRIC_TYPES)}, "
                f"got {_type_name(raw_type)}"
            )
            continue
        if raw_type not in METRIC_TYPES:
            errors.append(
                f"metrics[{index}].type: expected one of {list(METRIC_TYPES)}, "
                f"got '{_safe_echo(raw_type, 32)}'"
            )
            continue
        metric_type = raw_type

        label, ok_label = _optional_str(
            entry, "label", index, errors, MAX_LABEL_LEN
        )
        description, ok_desc = _optional_str(
            entry, "description", index, errors, MAX_DESCRIPTION_LEN
        )
        unit, ok_unit = _optional_str(entry, "unit", index, errors, MAX_UNIT_LEN)
        warning, ok_warn = _optional_number(
            entry, "warning_threshold", index, errors
        )
        critical, ok_crit = _optional_number(
            entry, "critical_threshold", index, errors
        )
        values, ok_values = _status_values(entry, index, errors, metric_type)
        direction, ok_dir = _enum(
            entry, "direction", index, errors, DIRECTIONS, DEFAULT_DIRECTION
        )
        aggregation, ok_agg = _enum(
            entry, "aggregation", index, errors, AGGREGATIONS, DEFAULT_AGGREGATION
        )
        dimensions, ok_dims = _dimensions(entry, index, errors)
        extensions, ok_ext = _split_extensions(entry, index, errors)

        cadence = entry.get("cadence")
        cadence_seconds: Optional[int] = None
        ok_cadence = True
        if cadence is not None:
            if not isinstance(cadence, str):
                errors.append(
                    f"metrics[{index}].cadence: expected '<n>(s|m|h|d|w)' or an "
                    f"ISO 8601 duration, got {_type_name(cadence)}"
                )
                ok_cadence = False
            else:
                cadence_seconds = parse_cadence(cadence)
                if cadence_seconds is None:
                    errors.append(
                        f"metrics[{index}].cadence: expected "
                        f"'<n>(s|m|h|d|w)' or an ISO 8601 duration between "
                        f"{CADENCE_MIN_SECONDS}s and {CADENCE_MAX_SECONDS}s "
                        f"(years and months are not fixed durations), got "
                        f"'{_safe_echo(cadence, 32)}'"
                    )
                    ok_cadence = False

        # Any error drops the entry — see the module docstring's drop policy.
        if not all((
            ok_label, ok_desc, ok_unit, ok_warn, ok_crit, ok_values,
            ok_dir, ok_agg, ok_dims, ok_ext, ok_cadence,
        )):
            continue

        seen_names[name] = index
        out.append({
            "name": name,
            "type": metric_type,
            "label": label,
            "description": description,
            "unit": unit,
            "warning_threshold": warning,
            "critical_threshold": critical,
            "values": values,
            # A malformed cadence dropped the entry above, so this is either
            # absent or the exact string `cadence_seconds` was derived from.
            "cadence": cadence,
            "cadence_seconds": cadence_seconds,
            "direction": direction,
            "aggregation": aggregation,
            "dimensions": dimensions,
            "extensions": extensions,
        })

    return out, errors


def definition_hash(entry: Dict[str, Any]) -> str:
    """Content address of one NORMALIZED entry (S4).

    sha256 over the canonical JSON of the entry. Two agents declaring the same
    metric the same way produce the same hash, which is what a fleet-wide
    "who measures this" query (ent#80) needs; `source` records the trigger that
    last wrote the row, which is what an operator needs. Provenance (template
    repo + commit SHA) is deliberately NOT stored — it is not free on the
    create path (it would cost a second GitHub call) and content-addressing
    answers the dedupe question without it.

    Never raises: `default=str` covers the YAML natives an `x-` value may hold.
    """
    return hashlib.sha256(
        json.dumps(entry, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def metric_shape_errors(block: Any) -> List[str]:
    """Named, operator-readable errors for a malformed `metrics:` block.

    An absent, null, or empty block all mean "this agent declares no metrics"
    and are NOT errors, so a template that comments its block out does not
    acquire a spurious finding. Never raises.
    """
    return _parse(block)[1]


def normalize_declared_metrics(block: Any) -> List[Dict[str, Any]]:
    """Well-formed declared metrics, tolerant of any input shape.

    Each entry carries every registry field already type-checked, bounded and
    enum-validated — safe to hand straight to
    `db.metric_definitions.MetricDefinitionOperations.reconcile`. Malformed
    entries are dropped (see `metric_shape_errors`). Never raises.
    """
    return _parse(block)[0]
