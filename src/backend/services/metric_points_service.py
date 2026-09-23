"""Metric point validation — the pure leaf behind `record_metrics` (ent#478).

`validate_batch` turns a batch of wire points plus the agent's declared metric
definitions into storable rows or per-point reason codes. It never touches the
database, never raises for bad input, and never reads settings: the route
fetches the definitions once and hands them in, exactly as
`services/template_metrics.py` is a leaf over the declaration side. That is
what makes every reason code testable without a DB and what keeps Invariant
#1's middle layer free of both SQL and HTTP.

## The rules, once

* **Declared or nothing.** A point names a metric the ent#477 registry holds
  for THIS agent, `status = active`. A retired row is a separate, more useful
  answer than "unknown".
* **The STORED type decides**, never `type_conflict`. A refused type change is
  informational: the points already recorded under that name were interpreted
  as the stored type, so new ones must be too. The hint names the remedy (an
  author-side rename), because the value is not what is wrong.
* **No coercion.** `True` is not `1`, `"42"` is not `42`, and `NaN`/`inf` are
  not values. Each gets a named code rather than a silently stored surprise.
* **Identity excludes the value.** `idempotency_key = sha256(metric \0 ts \0
  canonical_dims)`: the same observation posted twice is one row, and a genuine
  correction is a new `ts`.
* **A value is a label, not a document.** Text values are bounded at
  `METRIC_VALUE_TEXT_MAX_LEN`; `status` labels are bounded far tighter by
  their declared domain.
* **Echo nothing.** Messages carry codes, indices, closed-enum names and
  dimension KEYS (charset-bounded, via `_safe_echo`) — never a value and never
  a dimension value. This output lands in MCP tool results, i.e. in an LLM's
  context, and in logs.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from models import (
    METRIC_DIM_VALUE_MAX_LEN,
    METRIC_TS_FUTURE_SKEW_SECONDS,
    METRIC_VALUE_TEXT_MAX_LEN,
)
from services.template_metrics import (
    MAX_DIMENSIONS,
    MAX_STATUS_VALUE_LEN,
    _NAME_RE,
)
from utils.helpers import to_utc_iso, utc_now_iso

# `fromisoformat` is deliberately NOT the gate: it accepts week dates
# (`2026-W01-1`), bare dates, and year `0999` — which normalises to
# `999-01-01T...` and then sorts as the NEWEST row forever under a lexicographic
# ISO index. The shape comes first, the parse second.
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$"
)

# A dimension value may be any printable text; control characters may not pass.
# PostgreSQL's JSONB rejects a NUL inside a string with a `DataError`, which the
# route would otherwise have to classify — and an agent retrying a permanently
# unstorable point forever is the failure this prevents at the cheap end.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# One source for the wire limits: models.py is where the contract is read.
MAX_DIM_VALUE_LEN = METRIC_DIM_VALUE_MAX_LEN
TS_FUTURE_SKEW_SECONDS = METRIC_TS_FUTURE_SKEW_SECONDS
# Below this, a timestamp is far likelier to be a typo or a zero-year default
# than a real observation — and one such point steers the retention sweep.
TS_FLOOR = datetime(2000, 1, 1, tzinfo=timezone.utc)

_ECHO_SAFE_RE = re.compile(r"[^a-zA-Z0-9_.:-]")


def _safe_echo(value: Any, limit: int = 64) -> str:
    """A caller-supplied identifier, reduced to a charset safe to echo."""
    return _ECHO_SAFE_RE.sub("?", str(value))[:limit]


def canonical_dims(dims: Optional[Dict[str, str]]) -> str:
    """The ONE serialisation the hash and the stored column both derive from."""
    return json.dumps(
        dims or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def point_identity(metric: str, ts: str, dims: Optional[Dict[str, str]]) -> str:
    """`sha256(metric \0 ts \0 canonical_dims)` — the point's identity.

    NUL-joined like `idempotency_service.derive_effect_key`, so no combination
    of field contents can spell another combination's key.
    """
    raw = "\x00".join([metric, ts, canonical_dims(dims)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class PointError(dict):
    """One rejected point: `{index, metric, code, message, hint?}`."""

    def __init__(self, index: int, metric: Any, code: str, message: str,
                 hint: Optional[str] = None):
        super().__init__(
            index=index, metric=_safe_echo(metric), code=code, message=message
        )
        if hint:
            self["hint"] = hint


_REFRESH_HINT = (
    "declare it in the agent's template.yaml `metrics:` block, then call "
    "refresh_metric_definitions (or POST /api/agents/{name}/metrics/definitions"
    "/refresh) so the registry picks it up"
)


def _definitions_by_name(definitions: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {d["name"]: d for d in definitions if d.get("name")}


def _validate_ts(
    raw: Optional[str], now: datetime, retention_days: int
) -> Tuple[Optional[str], Optional[Tuple[str, str]]]:
    """`(normalised ISO-Z ts, (code, message))` — exactly one is set."""
    if raw is None:
        return (to_utc_iso(now), None)
    if not _RFC3339_RE.match(raw):
        return (None, (
            "ts_invalid",
            "ts must be RFC 3339 with an explicit offset, e.g. "
            "2026-09-22T08:00:00Z — a naive timestamp is ambiguous, not UTC",
        ))
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return (None, ("ts_invalid", "ts is not a parseable timestamp"))
    if parsed.tzinfo is None:  # defence: the shape regex already required one
        return (None, ("ts_invalid", "ts must carry an explicit UTC offset"))
    # Compared as DATETIMES, never as strings: year 999 renders as
    # `999-01-01T...`, which is lexicographically ABOVE `2000-...` — the same
    # three-digit-year trap that makes such a row head the `ts DESC` index
    # forever is exactly what a string floor would fail to catch.
    if parsed < TS_FLOOR:
        return (None, (
            "ts_out_of_range",
            f"ts must be at or after {to_utc_iso(TS_FLOOR)}",
        ))
    if parsed > now + timedelta(seconds=TS_FUTURE_SKEW_SECONDS):
        return (None, (
            "ts_in_future",
            f"ts is more than {TS_FUTURE_SKEW_SECONDS}s ahead of the server clock",
        ))
    normalised = to_utc_iso(parsed)
    if retention_days > 0:
        if parsed < now - timedelta(days=retention_days):
            return (None, (
                "ts_before_retention",
                f"ts is older than the {retention_days}-day retention window, "
                f"so the point would be pruned before it could be read",
            ))
    return (normalised, None)


def _validate_dims(
    dims: Optional[Dict[str, Any]], declared: List[str]
) -> Tuple[Optional[Dict[str, str]], Optional[Tuple[str, str]]]:
    if not dims:
        return (None, None)
    if len(dims) > MAX_DIMENSIONS:
        # Its OWN code: "you sent too many" and "you sent one nobody declared"
        # are different remedies, and reusing `dimension_undeclared` tells an
        # agent to go declare a dimension that would not help (ent#478 I8).
        # Pydantic's `max_length=10` on `dims` refuses this first over HTTP, so
        # this branch is defensive — it is the answer for any non-HTTP caller
        # of the leaf, which is the whole point of the leaf being pure.
        return (None, (
            "dimensions_too_many",
            f"a point may carry at most {MAX_DIMENSIONS} dimensions",
        ))
    allowed = set(declared or [])
    clean: Dict[str, str] = {}
    for key, value in dims.items():
        if key not in allowed:
            return (None, (
                "dimension_undeclared",
                f"dimension '{_safe_echo(key)}' is not declared for this metric"
                + (f"; declared: {', '.join(sorted(allowed))}" if allowed
                   else "; this metric declares no dimensions"),
            ))
        if not isinstance(value, str):
            return (None, (
                "dimension_value_invalid",
                f"dimension '{_safe_echo(key)}' must be a string — a number or "
                "boolean label would fork the series by its rendering",
            ))
        if not value or len(value) > MAX_DIM_VALUE_LEN:
            return (None, (
                "dimension_value_invalid",
                f"dimension '{_safe_echo(key)}' must be 1..{MAX_DIM_VALUE_LEN} "
                "characters",
            ))
        if _CONTROL_RE.search(value):
            return (None, (
                "dimension_value_invalid",
                f"dimension '{_safe_echo(key)}' contains control characters",
            ))
        clean[key] = value
    return (clean, None)


def _validate_value(
    value: Any, definition: Dict[str, Any]
) -> Tuple[Optional[float], Optional[str], Optional[Tuple[str, str, Optional[str]]]]:
    """`(value_numeric, value_text, (code, message, hint))`."""
    # Length first, and before the type dispatch: a text value is a LABEL, and
    # an unbounded one could spend the whole 2 MiB batch budget in a single
    # field that then lands in a chart axis, a log line and an LLM's context.
    # The byte cap is a batch-level bound that a single point can exhaust; this
    # is the per-field one. Named, so the caller is told which rule it hit
    # rather than reading it as a type problem (ent#478 I1).
    if isinstance(value, str) and len(value) > METRIC_VALUE_TEXT_MAX_LEN:
        return (None, None, (
            "value_too_long",
            f"a text value must be at most {METRIC_VALUE_TEXT_MAX_LEN} "
            "characters — a value is a label, not a document",
            None,
        ))
    stored_type = definition.get("type")
    conflict = definition.get("type_conflict")
    hint = None
    if conflict:
        hint = (
            f"a declared change to '{_safe_echo(conflict)}' was refused; points "
            f"must match the stored type '{_safe_echo(stored_type)}'. To change "
            "the shape, rename the metric — an honest series break"
        )

    if stored_type == "status":
        if not isinstance(value, str):
            return (None, None, (
                "type_mismatch",
                f"metric is declared '{stored_type}', so value must be a string",
                hint,
            ))
        if not value or len(value) > MAX_STATUS_VALUE_LEN:
            return (None, None, (
                "value_invalid",
                f"a status value must be 1..{MAX_STATUS_VALUE_LEN} characters",
                None,
            ))
        if _CONTROL_RE.search(value):
            return (None, None, (
                "value_invalid", "value contains control characters", None))
        declared = [
            v.get("value") for v in (definition.get("values") or [])
            if isinstance(v, dict)
        ]
        if declared:
            if value not in declared:
                return (None, None, (
                    "status_value_undeclared",
                    "value is not one of the declared status values: "
                    + ", ".join(_safe_echo(d) for d in declared if d),
                    None,
                ))
        else:
            # A status row with no domain. The declaration reader cannot
            # produce one, but a refused type change used to wipe it
            # (ent#477 T5) and a hand-edited row still can — refusing is the
            # honest answer, because "any string" would silently widen the
            # metric's domain to everything.
            return (None, None, (
                "type_mismatch",
                "metric is declared 'status' but carries no declared values",
                hint or (
                    "re-declare the metric's `values:` in template.yaml and "
                    "refresh the registry"
                ),
            ))
        return (None, value, None)

    # Every other type is numeric.
    if isinstance(value, bool):
        # `bool` is an `int` subclass, so this must be checked FIRST or True
        # silently becomes 1.0.
        return (None, None, (
            "value_invalid", "value must be a number, not a boolean", None))
    if isinstance(value, str):
        return (None, None, (
            "type_mismatch",
            f"metric is declared '{stored_type}', so value must be a number — "
            "a numeric string is not coerced",
            hint,
        ))
    if not isinstance(value, (int, float)):
        return (None, None, ("value_invalid", "value must be a number", None))
    numeric = float(value)
    if not math.isfinite(numeric):
        return (None, None, (
            "value_invalid", "value must be a finite number", None))
    return (numeric, None, None)


def validate_batch(
    definitions: List[Dict[str, Any]],
    points: List[Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    retention_days: int = 0,
    execution_id: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Validate a batch; returns `(rows, errors)` — all-or-nothing at the route.

    `rows` are ready for `db.insert_metric_points` (minus `agent_name`, which
    the store stamps from the auth-resolved name). `errors` carry one
    `PointError` per rejected point, in batch order.
    """
    now = now or datetime.now(timezone.utc)
    created_at = utc_now_iso()
    by_name = _definitions_by_name(definitions)
    rows: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}

    for index, point in enumerate(points):
        metric = point.get("metric")
        if not isinstance(metric, str) or not _NAME_RE.match(metric):
            errors.append(PointError(
                index, metric, "metric_name_invalid",
                "metric names are lowercase, start with a letter, and use only "
                "letters, digits and underscores (max 64 characters)",
            ))
            continue

        definition = by_name.get(metric)
        if definition is None:
            errors.append(PointError(
                index, metric, "metric_undeclared",
                f"'{metric}' is not a declared metric for this agent",
                hint=_REFRESH_HINT,
            ))
            continue
        if definition.get("status") != "active":
            errors.append(PointError(
                index, metric, "metric_retired",
                f"'{metric}' was retired when it left the template; re-declare "
                "it to revive the same series",
                hint=_REFRESH_HINT,
            ))
            continue

        ts, ts_error = _validate_ts(point.get("ts"), now, retention_days)
        if ts_error:
            errors.append(PointError(index, metric, *ts_error))
            continue

        dims, dim_error = _validate_dims(
            point.get("dims"), definition.get("dimensions") or []
        )
        if dim_error:
            errors.append(PointError(index, metric, *dim_error))
            continue

        numeric, text, value_error = _validate_value(point.get("value"), definition)
        if value_error:
            code, message, hint = value_error
            errors.append(PointError(index, metric, code, message, hint))
            continue

        key = point_identity(metric, ts, dims)
        if key in seen:
            errors.append(PointError(
                index, metric, "duplicate_in_batch",
                f"the same observation (metric, ts, dims) is already at index "
                f"{seen[key]} of this batch; one identity is one observation, "
                "and a correction is a new ts",
            ))
            continue
        seen[key] = index

        rows.append({
            "metric": metric,
            "ts": ts,
            "idempotency_key": key,
            "value_numeric": numeric,
            "value_text": text,
            # The DICT, not the canonical text: `metric_points.dims` is a JSON
            # column (JSONB on PostgreSQL), so the driver owns the on-disk
            # serialisation. The canonical form exists for the HASH only — the
            # identity must be stable across clients, the storage need not be
            # byte-identical to it (S6).
            "dims": dims or None,
            "execution_id": execution_id,
            "created_at": created_at,
        })

    return (rows, errors)
