"""Objective ↔ metric join — target vs actual, with freshness (ent#666).

The ONE place the platform answers "what is this agent supposed to move, where
is it now, and is that number still true?". An objective (Tandem framework
§3.4) names a metric **by name**; the declared-metric registry (ent#477) says
what that name means; the point store (ent#478) says what it currently reads;
`metric_read_service.freshness` says whether to believe it. This module is the
join of those four, and it is the only one — the role card (ent#527 / #2927),
the project hub (ent#661) and proactivity (ent#605) all consume it rather than
each growing their own.

**Files are truth; this is a projection** (framework E7/E13). Objectives are
read out of the agent's own container on every request, through the same agent
door the Files tab uses. Nothing is copied platform-side, so a stopped agent
answers `unavailable: agent_stopped` rather than a number that was true once.

## The grammar it reads (framework §3.4)

```yaml
schema_version: 1
id: q4-close-rate
statement: Lift close rate to 35% by the end of Q4.
horizon: day | week | month | quarter | year
owner: role:revenue-lead
supporting_agents: [sales-companion]
metrics:
  - name: close_rate          # the registry's metrics[].name — by NAME
    direction: up | down | hold
    target: 35
    by: 2026-12-31
status: active | achieved | dropped
review_by: 2026-10-15
```

Unknown keys are tolerated and never fatal (the ent#477 reader rule). Only
`status: active` objectives are joined; a file with no `status` is read as
active, and anything outside the enum is read as not-active and excluded.

## Never a blank

Every failure is a NAMED finding carrying a sentence a person can act on — an
objective that names a metric nobody declared, a file that will not parse, two
declared directions that disagree. The one thing this module will not do is
render an empty cell where a number was expected: that is how "we are measuring
it" survives having stopped measuring it.

## What `gap.status` means

**Position relative to target given direction — never pace** (TD-3). `behind`
says the number is on the wrong side of the target right now; it does NOT say
the agent is late against `by`. `by` and `horizon` ride every row so a consumer
that wants a pace judgment can compute one; nothing here does.

`hold` is its own arm: a metric the objective wants held AT a value is
`on_target` within `tolerance` (default: exact equality) and `off_target`
otherwise, with the signed delta. `behind` / `ahead` are never emitted for a
`hold`, because there is no good side to be on.

## Stale is orthogonal to gap

A stale metric still gets its gap computed, with `stale: true` beside it. The
card renders "7 / 12 · stale"; ent#605 refuses to act on it. The join reports,
the consumer decides — collapsing a stale metric to `not_computable` would hide
the number the card exists to show.

HTTP-free (Invariant #1): the router owns status codes; nothing here raises for
transport. Store failures propagate (the route maps them to 503); agent-door
failures are named fields on a successful read.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from services.metric_read_service import STALE_RULE, latest_by_metric
from utils.helpers import to_utc_iso

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bounds on what crosses from author-controlled files into a response.
# `template.yaml` and `objectives/*.yaml` are agent-writable, so a
# prompt-injected agent is an author here.
# ---------------------------------------------------------------------------

#: Objectives returned. The cap is on the OUTPUT, after the concern filter —
#: capping the listing first (the #2927 shape) hides an agent's own objective
#: behind twenty foreign ones in a shared fleet canon.
MAX_OBJECTIVES = 20
#: File names read before the filter runs. The bound on the fan-out.
MAX_OBJECTIVE_FILES_SCANNED = 100
MAX_METRICS_PER_OBJECTIVE = 12
MAX_SUPPORTING_AGENTS = 50
MAX_TEXT = 400
#: Concurrent objective reads in flight. Deliberately small: the agent-server
#: is single-process and `AgentClient`'s circuit trips at three failures, so a
#: wide fan-out against a sick agent would open the circuit that chat rides on.
READ_CONCURRENCY = 2

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PATH_SEG_RE = re.compile(r"^[A-Za-z0-9._-]+$")

#: The objective file's direction vocabulary, mapped onto the comparison this
#: module performs. `hold` is the registry's `neutral` **declared on purpose**,
#: which is exactly what an absent registry `direction` cannot be told apart
#: from — hence `direction_source`.
_OBJECTIVE_DIRECTIONS = {"up": "up_good", "down": "down_good", "hold": "hold"}
#: Registry directions that carry an opinion. `neutral` is the column default,
#: so it is read as "no opinion", never as "hold".
_REGISTRY_DIRECTIONS = {"up_good", "down_good"}

ACTIVE_STATUS = "active"


# ---------------------------------------------------------------------------
# Pure helpers — module top, no db / docker import at call time
# ---------------------------------------------------------------------------

def _text(value: Any, cap: int = MAX_TEXT) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s[:cap] if s else None


def _safe_id(value: Any) -> Optional[str]:
    s = _text(value, 64)
    return s if s and _ID_RE.match(s) else None


def finite_number(value: Any) -> Optional[float]:
    """A YAML scalar that may be compared, or None.

    `bool` is excluded (`isinstance(True, int)` is True, and `true` is not 1),
    and so are `.nan` / `.inf`, which YAML accepts, the hardened loader passes
    through, and `json.dumps` emits as bare `NaN` / `Infinity` — which is not
    JSON, so `JSON.parse` throws and the consumer renders a blank card. A
    non-number is not an error here: it is kept as `target_text`.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return value


def _comparable(value: Any) -> Optional[float]:
    """The actual/target as a number, or None when there is nothing to compare."""
    return finite_number(value)


def canon_root(template: Any) -> Optional[str]:
    """`x-canon.clone_path` (default `canon`), validated as a plain segment chain.

    Author-controlled and it reaches a file read, so every segment must match
    `_PATH_SEG_RE` and `..` is refused outright.
    """
    xc = template.get("x-canon") if isinstance(template, dict) else None
    raw = (xc.get("clone_path") if isinstance(xc, dict) else None) or "canon"
    raw = str(raw).strip().strip("/")
    if not raw or any(not _PATH_SEG_RE.match(seg) or seg == ".."
                      for seg in raw.split("/")):
        return None
    return raw


def objective_concerns(obj: dict, role_id: Optional[str],
                       agent_name: str) -> bool:
    """An objective belongs to this agent when its role owns it or it supports it.

    Framework §3.4: `owner: role:<id>`, `supporting_agents: [<agent name>]`.
    An agent with no `x-role` has no owned objectives — it can still support.
    """
    owner = _text(obj.get("owner"), 128) or ""
    if role_id and owner == f"role:{role_id}":
        return True
    return agent_name in (obj.get("supporting_agents") or [])


def objective_is_active(obj: dict) -> bool:
    """Only `status: active` objectives are joined (orchestrator ruling).

    A missing status reads as active — the reader-tolerance rule, and the
    oldest canon files predate the field. A value outside the enum reads as
    NOT active: an unrecognised state is not a licence to keep nagging.
    """
    status = obj.get("status")
    return status is None or status == ACTIVE_STATUS


def resolve_direction(
    registry_direction: Any,
    objective_direction: Any,
) -> Tuple[Optional[str], str, bool]:
    """(direction, direction_source, mismatch) — which way is good (TD-2).

    The registry wins when it has an opinion, because it is the platform's own
    declaration of what the metric means. But `neutral` is the column's
    DEFAULT, indistinguishable from a template that never said — and no bundled
    template declares `direction:` — so a registry `neutral` falls through to
    the objective file's `up` / `down` / `hold` rather than making every gap
    uncomputable on day one.

    Two directions that both speak and disagree is a `direction_mismatch`
    finding; the registry still wins. Neither speaking leaves `None`, which the
    gap reports as `no_direction` with a finding naming the one-line fix.
    """
    registry = registry_direction if registry_direction in _REGISTRY_DIRECTIONS else None
    declared = _OBJECTIVE_DIRECTIONS.get(
        (objective_direction or "").strip().lower()
        if isinstance(objective_direction, str) else "")

    if registry:
        # A declared `hold` against a registry `up_good` is two opinions, not
        # one opinion and a silence.
        mismatch = bool(declared) and declared != registry
        return registry, "registry", mismatch
    if declared:
        return declared, "objective", False
    return None, "none", False


def gap(
    target: Any,
    actual: Any,
    direction: Optional[str],
    *,
    tolerance: Any = None,
) -> Dict[str, Any]:
    """Position of `actual` relative to `target`, given `direction`.

    Never pace (TD-3). `delta = actual − target`, signed, numeric only.

    * `up_good`   → `behind` when below the target, `ahead` when above
    * `down_good` → the mirror
    * `hold`      → `on_target` within `tolerance` (default: exact), else
      `off_target`. Never `behind` / `ahead`: there is no good side.
    * no direction / no target / a non-numeric either side → `not_computable`
      with the reason named.
    """
    target_n = _comparable(target)
    if target_n is None:
        return {"status": "not_computable", "delta": None,
                "reason": "non_numeric" if target is not None else "no_target"}
    if actual is None:
        return {"status": "not_computable", "delta": None,
                "reason": "no_points"}
    actual_n = _comparable(actual)
    if actual_n is None:
        return {"status": "not_computable", "delta": None,
                "reason": "non_numeric"}
    if direction is None:
        return {"status": "not_computable", "delta": None,
                "reason": "no_direction"}

    delta = actual_n - target_n

    if direction == "hold":
        band = finite_number(tolerance)
        band = abs(band) if band is not None else 0.0
        status = "on_target" if abs(delta) <= band else "off_target"
        return {"status": status, "delta": delta, "reason": None}

    if delta == 0:
        return {"status": "on_target", "delta": 0.0, "reason": None}
    if direction == "up_good":
        return {"status": "behind" if delta < 0 else "ahead",
                "delta": delta, "reason": None}
    if direction == "down_good":
        return {"status": "behind" if delta > 0 else "ahead",
                "delta": delta, "reason": None}
    # An unknown direction is a silence, not an opinion.
    return {"status": "not_computable", "delta": delta,
            "reason": "no_direction"}


def _finding(code: str, message: str, *, objective_id: Optional[str] = None,
             metric: Optional[str] = None,
             path: Optional[str] = None) -> Dict[str, Any]:
    return {"code": code, "objective_id": objective_id, "metric": metric,
            "path": path, "message": message}


def parse_objective(
    doc: Any,
    *,
    path: str,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """(objective, findings) from one parsed YAML document.

    Everything author-controlled is bounded here, once, so no downstream
    consumer has to remember to. A document that is not a mapping is a
    `objective_invalid` finding with its path — never a silent `continue`,
    which is how an objective disappears from a card that claims to show them
    all.
    """
    fallback_id = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if not isinstance(doc, dict):
        return None, [_finding(
            "objective_invalid",
            f"{path} is not a YAML mapping — an objective file is a mapping "
            "with id / statement / metrics (framework §3.4).",
            objective_id=_safe_id(fallback_id), path=path)]

    findings: List[Dict[str, Any]] = []
    obj_id = _safe_id(doc.get("id")) or _safe_id(fallback_id) or fallback_id[:64]

    supporting = doc.get("supporting_agents")
    supporting_agents = (
        [s for s in (_text(a, 64) for a in supporting[:MAX_SUPPORTING_AGENTS])
         if s]
        if isinstance(supporting, list) else []
    )

    metrics: List[Dict[str, Any]] = []
    raw_metrics = doc.get("metrics")
    raw_metrics = raw_metrics if isinstance(raw_metrics, list) else []
    seen: set = set()
    for item in raw_metrics:
        if len(metrics) >= MAX_METRICS_PER_OBJECTIVE:
            break
        if not isinstance(item, dict):
            findings.append(_finding(
                "metric_name_invalid",
                f"objective `{obj_id}` has a metrics: entry that is not a "
                "mapping — each entry is {name, direction, target, by}.",
                objective_id=obj_id, path=path))
            continue
        name = _safe_id(item.get("name"))
        if not name:
            findings.append(_finding(
                "metric_name_invalid",
                f"objective `{obj_id}` names a metric that is not a valid "
                "metric name — use the name from the agent's template.yaml "
                "`metrics:` block.",
                objective_id=obj_id, path=path))
            continue
        if name in seen:
            findings.append(_finding(
                "metric_duplicate",
                f"objective `{obj_id}` lists metric `{name}` more than once — "
                "the first entry is used and the rest ignored.",
                objective_id=obj_id, metric=name, path=path))
            continue
        seen.add(name)
        target_raw = item.get("target")
        metrics.append({
            "name": name,
            "target": finite_number(target_raw),
            "target_text": (None if finite_number(target_raw) is not None
                            else _text(target_raw, 64)),
            "tolerance": finite_number(item.get("tolerance")),
            "by": _text(item.get("by"), 32),
            "direction": _text(item.get("direction"), 16),
        })

    objective = {
        "id": obj_id,
        "path": path,
        "schema_version": _text(doc.get("schema_version"), 16),
        "statement": _text(doc.get("statement")),
        "horizon": _text(doc.get("horizon"), 16),
        "status": _text(doc.get("status"), 32),
        "owner": _text(doc.get("owner"), 128),
        "review_by": _text(doc.get("review_by"), 32),
        "supporting_agents": supporting_agents,
        "metrics": metrics,
        "metrics_truncated": len(raw_metrics) > MAX_METRICS_PER_OBJECTIVE,
    }
    return objective, findings


def select_objectives(
    objectives: List[Dict[str, Any]],
    *,
    role_id: Optional[str],
    agent_name: str,
) -> Tuple[List[Dict[str, Any]], bool]:
    """(the ones this agent answers for, truncated) — active only, capped last.

    Scan-then-filter: the cap is applied to what SURVIVES the filter, so an
    agent whose objective sorts after twenty foreign ones in a shared fleet
    canon still sees it. A non-active objective is dropped silently and
    produces no findings — it is not a defect, it is finished.
    """
    kept = [o for o in objectives
            if objective_is_active(o)
            and objective_concerns(o, role_id, agent_name)]
    return kept[:MAX_OBJECTIVES], len(kept) > MAX_OBJECTIVES


def join_objectives(
    objectives: List[Dict[str, Any]],
    definitions: List[Dict[str, Any]],
    latest_by_name: Dict[str, Dict[str, Any]],
    *,
    agent_name: str,
    role_id: Optional[str],
    now: datetime,
) -> Dict[str, Any]:
    """The pure join. Every unit test drives this; nothing here does I/O.

    `objectives` are already parsed and filtered (`select_objectives`);
    `definitions` are the registry rows INCLUDING retired ones, because a
    retired metric must be named as retired rather than silently read as
    undeclared; `latest_by_name` is `metric_read_service.latest_by_metric`.
    """
    by_name = {d["name"]: d for d in definitions if d.get("name")}
    findings: List[Dict[str, Any]] = []
    rows_out: List[Dict[str, Any]] = []
    summary = {
        "objectives": 0, "metrics": 0, "behind": 0, "ahead": 0,
        "on_target": 0, "off_target": 0, "not_computable": 0, "stale": 0,
        "undeclared": 0, "declared_elsewhere": 0,
    }

    seen_ids: Dict[str, str] = {}
    for obj in objectives:
        obj_id = obj["id"]
        if obj_id in seen_ids:
            findings.append(_finding(
                "objective_id_duplicate",
                f"two objective files declare id `{obj_id}` "
                f"({seen_ids[obj_id]} and {obj['path']}) — both are shown; "
                "give one of them its own id.",
                objective_id=obj_id, path=obj["path"]))
        else:
            seen_ids[obj_id] = obj["path"]

        owned = bool(role_id) and (obj.get("owner") or "") == f"role:{role_id}"
        supporting = agent_name in (obj.get("supporting_agents") or [])

        metrics_out = []
        for spec in obj["metrics"]:
            row, row_findings = _metric_row(
                spec, obj, by_name, latest_by_name,
                owned=owned, agent_name=agent_name, now=now,
            )
            metrics_out.append(row)
            findings.extend(row_findings)
            summary["metrics"] += 1
            summary[row["gap"]["status"]] += 1
            if row["stale"]:
                summary["stale"] += 1
            if row["declared_elsewhere"]:
                summary["declared_elsewhere"] += 1
            elif not row["declared"]:
                summary["undeclared"] += 1

        summary["objectives"] += 1
        rows_out.append({
            "id": obj_id,
            "path": obj["path"],
            "schema_version": obj.get("schema_version"),
            "statement": obj.get("statement"),
            "horizon": obj.get("horizon"),
            "status": obj.get("status") or ACTIVE_STATUS,
            "owner": obj.get("owner"),
            "review_by": obj.get("review_by"),
            "owned": owned,
            "supporting": supporting,
            "metrics": metrics_out,
            "metrics_truncated": obj.get("metrics_truncated", False),
        })

    return {"objectives": rows_out, "findings": findings, "summary": summary}


def _metric_row(
    spec: Dict[str, Any],
    obj: Dict[str, Any],
    by_name: Dict[str, Dict[str, Any]],
    latest_by_name: Dict[str, Dict[str, Any]],
    *,
    owned: bool,
    agent_name: str,
    now: datetime,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """One objective metric: target from the file, everything else from the join."""
    name = spec["name"]
    obj_id = obj["id"]
    definition = by_name.get(name)
    latest = latest_by_name.get(name) or {}
    findings: List[Dict[str, Any]] = []

    retired = bool(definition) and definition.get("status") != ACTIVE_STATUS
    declared = bool(definition) and not retired
    # A supporting agent does not declare the owner's metric, and never will:
    # the registry is per-agent and cross-agent reads are ent#80's grant.
    # Telling it to "declare it and refresh" would be advice it cannot take.
    declared_elsewhere = (not definition) and (not owned)

    direction, direction_source, mismatch = resolve_direction(
        definition.get("direction") if definition else None,
        spec.get("direction"),
    )

    actual = None
    if declared:
        latest_value = latest.get("latest")
        actual = latest_value.get("value") if latest_value else None

    row = {
        "name": name,
        "target": spec.get("target"),
        "target_text": spec.get("target_text"),
        "tolerance": spec.get("tolerance"),
        "by": spec.get("by"),
        "horizon": obj.get("horizon"),
        "objective_direction": spec.get("direction"),
        "declared": declared,
        "declared_elsewhere": declared_elsewhere,
        "direction": direction,
        "direction_source": direction_source,
        "unit": definition.get("unit") if definition else None,
        "type": definition.get("type") if definition else None,
        "label": (definition.get("label") or name) if definition else None,
        "actual": actual,
        "last_point_at": latest.get("last_point_at") if declared else None,
        "stale": bool(latest.get("stale")) if declared else False,
        "freshness": latest.get("freshness") if declared else None,
        "stale_after": latest.get("stale_after") if declared else None,
        "finding": None,
    }

    if declared_elsewhere:
        row["gap"] = {"status": "not_computable", "delta": None,
                      "reason": "declared_elsewhere"}
        row["finding"] = {
            "code": "metric_not_declared_here",
            "message": (
                f"objective `{obj_id}` names metric `{name}`, which is "
                f"declared by the owning role's agent, not by `{agent_name}` — "
                "this agent supports the objective without measuring it "
                "(cross-agent metric reads are ent#80)."),
        }
        findings.append(_finding(
            "metric_not_declared_here", row["finding"]["message"],
            objective_id=obj_id, metric=name, path=obj["path"]))
        return row, findings

    if retired:
        row["gap"] = {"status": "not_computable", "delta": None,
                      "reason": "retired"}
        row["finding"] = {
            "code": "metric_retired",
            "message": (
                f"objective `{obj_id}` names metric `{name}`, which "
                f"`{agent_name}` retired at {definition.get('retired_at')} — "
                "its last value is not shown, because a retired number "
                "rendering as current is the defect this read exists to "
                "prevent. Re-declare it in template.yaml `metrics:` and call "
                "refresh_metric_definitions."),
        }
        findings.append(_finding(
            "metric_retired", row["finding"]["message"],
            objective_id=obj_id, metric=name, path=obj["path"]))
        return row, findings

    if not declared:
        row["gap"] = {"status": "not_computable", "delta": None,
                      "reason": "undeclared"}
        row["finding"] = {
            "code": "metric_undeclared",
            "message": (
                f"objective `{obj_id}` names metric `{name}`, which "
                f"`{agent_name}` does not declare in template.yaml "
                "`metrics:` — declare it there and call "
                "refresh_metric_definitions, then record points against it."),
        }
        findings.append(_finding(
            "metric_undeclared", row["finding"]["message"],
            objective_id=obj_id, metric=name, path=obj["path"]))
        return row, findings

    row["gap"] = gap(spec.get("target"), actual, direction,
                     tolerance=spec.get("tolerance"))

    if mismatch:
        row["finding"] = {
            "code": "direction_mismatch",
            "message": (
                f"objective `{obj_id}` says metric `{name}` should go "
                f"`{spec.get('direction')}`, but the registry declares "
                f"`{definition.get('direction')}` — the registry wins. Fix "
                "whichever one is wrong."),
        }
        findings.append(_finding(
            "direction_mismatch", row["finding"]["message"],
            objective_id=obj_id, metric=name, path=obj["path"]))
    elif direction is None and row["gap"]["reason"] == "no_direction":
        row["finding"] = {
            "code": "direction_undeclared",
            "message": (
                f"neither objective `{obj_id}` nor the registry says which way "
                f"`{name}` should move, so nothing can be behind or ahead of "
                "the target. Add `direction: up` (or `down`, or `hold`) to the "
                "metric in template.yaml and call refresh_metric_definitions, "
                "or to the objective's metric entry."),
        }
        findings.append(_finding(
            "direction_undeclared", row["finding"]["message"],
            objective_id=obj_id, metric=name, path=obj["path"]))

    return row, findings


# ---------------------------------------------------------------------------
# Agent-door reads — everything below touches the container
# ---------------------------------------------------------------------------

class _Unreachable(Exception):
    """The agent door is gone, not just this one file.

    Raised for the typed transport failures (`AgentNotReachableError`,
    `AgentCircuitOpenError`) so a fan-out aborts instead of spending its
    remaining reads driving the circuit breaker that chat rides on further open
    (`CIRCUIT_FAILURE_THRESHOLD = 3`).
    """


async def _agent_get(client, path: str, *, timeout: float = 20.0):
    """GET through the agent door. Transport death raises `_Unreachable`.

    Deliberately not `AgentClient.read_file`, which swallows every
    `AgentClientError` into `{"success": False}` — that flattens "this agent is
    unreachable" into "this file is unreadable", and the difference is whether
    the remaining nineteen reads are worth attempting.
    """
    from services.agent_client.client import (
        AgentCircuitOpenError, AgentClientError, AgentNotReachableError,
    )
    try:
        return await client.get(path, timeout=timeout)
    except (AgentNotReachableError, AgentCircuitOpenError) as e:
        raise _Unreachable(str(e)) from e
    except AgentClientError as e:
        logger.warning("objective join: GET %s failed: %s", path, e)
        return None
    except Exception as e:  # noqa: BLE001 — a bad file is never a 500
        logger.warning("objective join: GET %s failed: %s", path, e)
        return None


async def _read_yaml(client, path: str) -> Tuple[Optional[dict], Optional[str]]:
    """(parsed, error_code) — `not_found` / `unreadable` / `invalid`, or None."""
    import urllib.parse

    from utils.safe_yaml import load_template_yaml

    encoded = urllib.parse.quote(path, safe="")
    response = await _agent_get(client, f"/api/files/download?path={encoded}")
    if response is None:
        return None, "unreadable"
    status = getattr(response, "status_code", 0)
    if status == 404:
        return None, "not_found"
    if status != 200:
        return None, "unreadable"
    try:
        data = load_template_yaml(response.text)
    except Exception as e:  # noqa: BLE001
        logger.warning("objective join: parse %s failed: %s", path, e)
        return None, "invalid"
    return (data, None) if isinstance(data, dict) else (None, "invalid")


async def _list_objective_files(client, root: str) -> Tuple[List[str], str]:
    """(file names, source code) for `<root>/objectives`.

    The agent-server listing is RECURSIVE with no depth cap, so only top-level
    `type: file` entries are taken; nested folders are ignored. A 404 is
    `absent` (the directory is not there — a real answer with its own copy),
    anything else unhappy is `unreadable`.
    """
    response = await _agent_get(
        client, f"/api/files?path=/home/developer/{root}/objectives")
    if response is None:
        return [], "unreadable"
    status = getattr(response, "status_code", 0)
    if status == 404:
        return [], "absent"
    if status != 200:
        return [], "unreadable"
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        return [], "unreadable"
    if not isinstance(body, dict):
        return [], "unreadable"

    names: List[str] = []
    for item in (body.get("tree") or body.get("children") or []):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "directory" or item.get("is_dir"):
            continue
        name = str(item.get("name") or "")
        if name.endswith((".yaml", ".yml")) and _PATH_SEG_RE.match(name):
            names.append(name)
    return sorted(names), "read"


async def read_objective_files(
    client,
    root: str,
    *,
    agent_name: str,
    role_id: Optional[str],
) -> Dict[str, Any]:
    """List, read and filter `<root>/objectives/*.yaml` through the agent door.

    Exported so a cross-agent consumer (the ent#661 project hub) can do ONE
    file read and then compose `join_objectives` per agent over store-only
    reads, rather than putting the agent door inside its loop. Still one join.

    Returns `{objectives, findings, source, unavailable}`; `unavailable` is set
    only when the door itself died mid-fan-out.
    """
    source = {
        "objectives_dir": "skipped",
        "objectives_listed": 0,
        "objectives_scanned": 0,
        "objectives_unscanned": 0,
        "objectives_truncated": False,
    }
    findings: List[Dict[str, Any]] = []

    try:
        names, dir_state = await _list_objective_files(client, root)
    except _Unreachable:
        return {"objectives": [], "findings": findings, "source": source,
                "unavailable": "agent_unreachable"}

    source["objectives_dir"] = dir_state
    source["objectives_listed"] = len(names)
    scanned = names[:MAX_OBJECTIVE_FILES_SCANNED]
    source["objectives_scanned"] = len(scanned)
    source["objectives_unscanned"] = len(names) - len(scanned)
    if not scanned:
        return {"objectives": [], "findings": findings, "source": source,
                "unavailable": None}

    semaphore = asyncio.Semaphore(READ_CONCURRENCY)
    aborted = {"value": False}

    async def _one(name: str):
        path = f"{root}/objectives/{name}"
        if aborted["value"]:
            return path, None, "aborted"
        async with semaphore:
            if aborted["value"]:
                return path, None, "aborted"
            try:
                doc, err = await _read_yaml(client, path)
            except _Unreachable:
                aborted["value"] = True
                return path, None, "aborted"
        return path, doc, err

    results = await asyncio.gather(
        *[_one(n) for n in scanned], return_exceptions=True)

    parsed: List[Dict[str, Any]] = []
    for result in results:
        if isinstance(result, BaseException):
            logger.warning("objective join: read task failed: %s", result)
            continue
        path, doc, err = result
        if err == "aborted":
            continue
        if err or doc is None:
            code = ("objective_invalid" if err == "invalid"
                    else "objective_unreadable")
            findings.append(_finding(
                code,
                (f"{path} could not be read from the agent "
                 f"({err}) — the other objectives still joined."
                 if code == "objective_unreadable" else
                 f"{path} is not a valid objective file ({err}) — fix the "
                 "YAML; framework §3.4 names the fields."),
                path=path))
            continue
        objective, obj_findings = parse_objective(doc, path=path)
        findings.extend(obj_findings)
        if objective is not None:
            parsed.append(objective)

    if aborted["value"]:
        return {"objectives": [], "findings": findings, "source": source,
                "unavailable": "agent_unreachable"}

    kept, truncated = select_objectives(
        parsed, role_id=role_id, agent_name=agent_name)
    source["objectives_truncated"] = truncated
    return {"objectives": kept, "findings": findings, "source": source,
            "unavailable": None}


def _empty(agent_name: str, now: datetime, *, unavailable=None, role=None,
           root=None, source=None, message=None,
           findings=None) -> Dict[str, Any]:
    """The answer shape, with nothing joined. Always carries `message`."""
    return {
        "agent_name": agent_name,
        "generated_at": to_utc_iso(now),
        "stale_rule": STALE_RULE,
        "role": role,
        "canon_root": root,
        "unavailable": unavailable,
        "source": source or {
            "template": "skipped", "objectives_dir": "skipped",
            "objectives_listed": 0, "objectives_scanned": 0,
            "objectives_unscanned": 0, "objectives_truncated": False,
        },
        "objectives": [],
        "findings": findings or [],
        "summary": {
            "objectives": 0, "metrics": 0, "behind": 0, "ahead": 0,
            "on_target": 0, "off_target": 0, "not_computable": 0, "stale": 0,
            "undeclared": 0, "declared_elsewhere": 0,
        },
        "message": message,
    }


_UNAVAILABLE_COPY = {
    "agent_stopped": ("this agent is stopped — its objectives live in its own "
                      "container (files are truth), so start it to read them"),
    "agent_missing": ("no container for this agent — recreate it, then its "
                      "objectives can be read again"),
    "agent_unreachable": ("the agent is not answering — its objectives are "
                          "read from its container on every request"),
}


async def read_objective_join(
    agent_name: str,
    *,
    now: Optional[datetime] = None,
    template: Optional[dict] = None,
    client=None,
) -> Dict[str, Any]:
    """The composition: container state → template → objective files → store.

    `template` and `client` are accepted so a caller that has already read the
    template through the same door (the role card) does not read it twice.

    The store is touched only for metric names an objective actually
    references, and not at all when there is nothing to join — an agent with no
    role and no canon pays one Docker state read and one template read, the
    same cost its Agent page already pays.
    """
    from services import docker_utils
    from services.agent_client import get_agent_client

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    try:
        state = await docker_utils.agent_container_state_async(agent_name)
    except Exception as e:  # noqa: BLE001
        logger.warning("objective join: container state for %s failed: %s",
                       agent_name, e)
        state = None
    if state != "running":
        unavailable = {"stopped": "agent_stopped",
                       "missing": "agent_missing"}.get(state, "agent_unreachable")
        return _empty(agent_name, now, unavailable=unavailable,
                      message=_UNAVAILABLE_COPY[unavailable])

    client = client or get_agent_client(agent_name)

    source = {
        "template": "skipped", "objectives_dir": "skipped",
        "objectives_listed": 0, "objectives_scanned": 0,
        "objectives_unscanned": 0, "objectives_truncated": False,
    }
    if template is None:
        try:
            template, terr = await _read_yaml(client, "template.yaml")
        except _Unreachable:
            return _empty(agent_name, now, unavailable="agent_unreachable",
                          message=_UNAVAILABLE_COPY["agent_unreachable"])
        source["template"] = terr or "read"
        if terr:
            return _empty(
                agent_name, now, source=source,
                message=(
                    "template.yaml could not be read from this agent "
                    f"({terr}) — it is where `x-role` and `x-canon` say which "
                    "role this agent fills and where its canon lives"))
    else:
        source["template"] = "read"

    xrole = template.get("x-role") if isinstance(template, dict) else None
    has_canon = isinstance(template, dict) and "x-canon" in template
    findings: List[Dict[str, Any]] = []

    if not isinstance(xrole, dict) and not has_canon:
        # Zero config: no role, no canon, nothing to join — and NO store read.
        return _empty(
            agent_name, now, source=source,
            message=("no `x-role` or `x-canon` in template.yaml — nothing to "
                     "join. An objective is a canon file that names a metric; "
                     "declare the role this agent fills to read them here."))

    role_id = _safe_id(xrole.get("role")) if isinstance(xrole, dict) else None
    if isinstance(xrole, dict) and xrole.get("role") and not role_id:
        findings.append(_finding(
            "role_id_invalid",
            f"`x-role.role` in template.yaml is not a valid id, so no owned "
            f"objective can be matched for `{agent_name}` — ids are letters, "
            "digits, dot, dash and underscore.",
        ))

    root = canon_root(template)
    if root is None:
        findings.append(_finding(
            "canon_path_invalid",
            "`x-canon.clone_path` in template.yaml is not a plain path — no "
            "file was read with it. Use a simple relative path such as "
            "`canon`.",
        ))
        return _empty(
            agent_name, now, source=source, findings=findings,
            role={"id": role_id, "path": None},
            message=("`x-canon.clone_path` is not a readable path — fix it in "
                     "template.yaml"))

    role = {
        "id": role_id,
        "path": f"{root}/roles/{role_id}.yaml" if role_id else None,
    }

    files = await read_objective_files(
        client, root, agent_name=agent_name, role_id=role_id)
    source.update(files["source"])
    findings.extend(files["findings"])

    if files["unavailable"]:
        return _empty(agent_name, now, unavailable=files["unavailable"],
                      role=role, root=root, source=source, findings=findings,
                      message=_UNAVAILABLE_COPY[files["unavailable"]])

    objectives = files["objectives"]
    if not objectives:
        message = {
            "absent": (f"`{root}/objectives/` was not found — objectives live "
                       "there (framework §3.4)"),
            "unreadable": (f"`{root}/objectives/` could not be listed — the "
                           "agent answered, but not with a directory"),
        }.get(source["objectives_dir"])
        if message is None:
            message = ("no active objective in "
                       f"`{root}/objectives/` names this agent — an objective "
                       "reaches it via `owner: role:<id>` or "
                       "`supporting_agents:`")
        return _empty(agent_name, now, role=role, root=root, source=source,
                      findings=findings, message=message)

    referenced = {m["name"] for o in objectives for m in o["metrics"]}

    from database import db  # deferred: the pure half imports without a store

    definitions = db.list_metric_definitions(agent_name, include_retired=True)
    declared_names = [d["name"] for d in definitions
                      if d.get("name") in referenced]
    latest = latest_by_metric(
        agent_name, definitions=definitions, names=declared_names, now=now)

    joined = join_objectives(
        objectives, definitions, latest,
        agent_name=agent_name, role_id=role_id, now=now)

    return {
        "agent_name": agent_name,
        "generated_at": to_utc_iso(now),
        "stale_rule": STALE_RULE,
        "role": role,
        "canon_root": root,
        "unavailable": None,
        "source": source,
        "objectives": joined["objectives"],
        "findings": findings + joined["findings"],
        "summary": joined["summary"],
        "message": None,
    }
