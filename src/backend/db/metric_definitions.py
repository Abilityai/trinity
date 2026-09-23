"""Declared metric registry — database operations (trinity-enterprise#477).

One row per metric an agent's `template.yaml metrics:` block declares. The
template is the **only** writer — there is no operator edit surface for a
definition — which is what makes an update-in-place reconcile correct here,
where the ent#89 schedules materializer had to be skip-by-name (it could
otherwise resurrect a schedule an operator deleted on purpose).

Rows are never DELETEd by `reconcile`, only **retired**: ent#478 stores points
keyed by `(agent_name, name)`, so a definition that vanished from the template
is still the only thing that can interpret the points already recorded under
its name.

SQLAlchemy Core (like `db/compatibility.py`) so it runs unchanged on SQLite and
PostgreSQL.

## Reconcile shape

One SELECT of the agent's rows, one batched upsert pass, one retire UPDATE —
all inside a single `engine.begin()`. The upsert is `on_conflict_do_update` on
`UNIQUE(agent_name, name)` (the `dashboard_history.py:328` idiom) rather than
select-then-write, so two workers reconciling the same agent concurrently
converge on the same rows instead of racing to insert.

## Refused type changes (T5)

A re-declared `type` is **refused**, not applied: `metric_points` are keyed by
name, so flipping `status` → `gauge` would leave every prior point
uninterpretable. The stored row keeps its type, the declared-but-refused type
lands in `type_conflict` (cleared the moment the template agrees again), and
the summary names it so the refresh response, the log line and the definitions
read all say the same thing. The remedy is an author-side rename: a new metric
name, with the old one retiring — an honest series break.
"""

import json
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update

from .engine import get_engine, make_insert
from .tables import metric_definitions
from utils.helpers import utc_now_iso

STATUS_ACTIVE = "active"
STATUS_RETIRED = "retired"


def _json_or_none(value: Any) -> Optional[str]:
    """JSON-encode a list/dict field, or `None` when it carries nothing.

    `default=str` because an `x-` extension value is author-shaped by
    definition and the hardened loader yields dates for unquoted YAML dates
    (#2110's lesson, one layer up): one odd value must not fail the whole
    reconcile.
    """
    if value is None or value == [] or value == {}:
        return None
    return json.dumps(value, sort_keys=True, default=str)


def _parse_json(raw: Optional[str], fallback):
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


class MetricDefinitionOperations:
    """Database operations for the per-agent declared metric registry."""

    # ---------------------------------------------------------------------
    # Read
    # ---------------------------------------------------------------------

    def list_for_agent(
        self, agent_name: str, include_retired: bool = False
    ) -> List[Dict[str, Any]]:
        """Every declared metric for an agent, newest declaration order.

        Active rows only unless `include_retired` — a retired definition is
        history (it explains points ent#478 already stored), not something a
        caller should have to filter out of the common case.
        """
        t = metric_definitions
        stmt = select(t).where(t.c.agent_name == agent_name)
        if not include_retired:
            stmt = stmt.where(t.c.status == STATUS_ACTIVE)
        stmt = stmt.order_by(t.c.first_declared_at, t.c.name)
        with get_engine().connect() as conn:
            return [self._row_to_dict(r) for r in conn.execute(stmt).mappings()]

    # ---------------------------------------------------------------------
    # Write
    # ---------------------------------------------------------------------

    def reconcile(
        self, agent_name: str, declared: List[Dict[str, Any]], source: str
    ) -> Dict[str, Any]:
        """Set-diff the declared metrics against the stored ones.

        `declared` entries are the NORMALIZED shape
        `services/template_metrics.normalize_declared_metrics` produces, each
        already carrying a `definition_hash` (the service computes it — this
        layer must not import a service module).

        Four outcomes per metric: **insert** a name that has none, **update** a
        row whose hash moved, **revive** a retired row the template declares
        again, **retire** an active row the template no longer declares. A
        declared `type` that disagrees with the stored one is refused (see the
        module docstring).

        Returns a summary dict — the caller logs it, attaches it to the git
        audit `details`, and returns it from the refresh route.
        """
        now = utc_now_iso()
        t = metric_definitions
        summary: Dict[str, Any] = {
            "declared": len(declared),
            "created": [],
            "updated": [],
            "revived": [],
            "retired": [],
            "unchanged": 0,
            "type_change_refused": [],
        }

        with get_engine().begin() as conn:
            existing = {
                row["name"]: dict(row)
                for row in conn.execute(
                    select(t).where(t.c.agent_name == agent_name)
                ).mappings()
            }

            for entry in declared:
                name = entry["name"]
                payload = self._payload(entry)
                prior = existing.get(name)

                if prior is None:
                    # Conflict-safe rather than a bare INSERT: the SELECT above
                    # and this write are one transaction, but a SECOND worker
                    # reconciling the same agent may have inserted between them
                    # (E5). `type` / `first_declared_at` / `id` are deliberately
                    # NOT in the conflict update — the row that won the race
                    # already carries the same declaration, and re-asserting its
                    # identity columns is how a race turns into a silent
                    # first-declared rewrite.
                    ins = make_insert(t).values(
                        id=str(uuid.uuid4()),
                        agent_name=agent_name,
                        name=name,
                        type=entry["type"],
                        status=STATUS_ACTIVE,
                        source=source,
                        first_declared_at=now,
                        last_synced_at=now,
                        retired_at=None,
                        type_conflict=None,
                        created_at=now,
                        updated_at=now,
                        **payload,
                    )
                    conn.execute(ins.on_conflict_do_update(
                        index_elements=[t.c.agent_name, t.c.name],
                        set_={
                            **{k: getattr(ins.excluded, k) for k in payload},
                            "status": ins.excluded.status,
                            "retired_at": ins.excluded.retired_at,
                            "source": ins.excluded.source,
                            "last_synced_at": ins.excluded.last_synced_at,
                            "updated_at": ins.excluded.updated_at,
                        },
                    ))
                    summary["created"].append(name)
                    continue

                values: Dict[str, Any] = dict(
                    payload,
                    source=source,
                    last_synced_at=now,
                    updated_at=now,
                )

                # T5: the stored type is load-bearing for ent#478's by-name
                # point store, so a declared change is RECORDED, never applied.
                type_refused = entry["type"] != prior["type"]
                if type_refused:
                    values["type_conflict"] = entry["type"]
                    # The stored TYPE stands, so the column that only exists for
                    # that type must stand with it: re-declaring a `status`
                    # metric as `counter` carries no `values`, and letting the
                    # payload overwrite them would leave a status row with no
                    # declared domain at all (ent#478's point validator would
                    # then have to widen to "any string").
                    values["status_values_json"] = prior.get("status_values_json")
                    summary["type_change_refused"].append({
                        "name": name,
                        "from": prior["type"],
                        "to": entry["type"],
                    })
                elif prior.get("type_conflict"):
                    # The template agrees again — clear the conflict rather than
                    # leaving a resolved disagreement on the definitions read.
                    values["type_conflict"] = None

                if prior["status"] != STATUS_ACTIVE:
                    values["status"] = STATUS_ACTIVE
                    values["retired_at"] = None
                    summary["revived"].append(name)
                elif (
                    prior.get("definition_hash") == payload["definition_hash"]
                    and not type_refused
                    and not prior.get("type_conflict")
                ):
                    summary["unchanged"] += 1
                else:
                    summary["updated"].append(name)

                conn.execute(
                    update(t)
                    .where(t.c.agent_name == agent_name, t.c.name == name)
                    .values(**values)
                )

            gone = [
                name for name, row in existing.items()
                if name not in {e["name"] for e in declared}
                and row["status"] == STATUS_ACTIVE
            ]
            if gone:
                conn.execute(
                    update(t)
                    .where(t.c.agent_name == agent_name, t.c.name.in_(gone))
                    .values(
                        status=STATUS_RETIRED,
                        retired_at=now,
                        source=source,
                        updated_at=now,
                    )
                )
                summary["retired"] = sorted(gone)

        return summary

    # ---------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------

    @staticmethod
    def _payload(entry: Dict[str, Any]) -> Dict[str, Any]:
        """The declaration-derived columns of one normalized entry."""
        return {
            "label": entry.get("label"),
            "description": entry.get("description"),
            "unit": entry.get("unit"),
            "warning_threshold": entry.get("warning_threshold"),
            "critical_threshold": entry.get("critical_threshold"),
            "status_values_json": _json_or_none(entry.get("values")),
            "cadence": entry.get("cadence"),
            "cadence_seconds": entry.get("cadence_seconds"),
            "direction": entry.get("direction"),
            "aggregation": entry.get("aggregation"),
            "dimensions_json": _json_or_none(entry.get("dimensions")),
            "extensions_json": _json_or_none(entry.get("extensions")),
            "definition_hash": entry.get("definition_hash"),
        }

    @staticmethod
    def _row_to_dict(row) -> Dict[str, Any]:
        """RowMapping → the API shape: JSON columns parsed, `*_json` dropped."""
        result = dict(row)
        result["values"] = _parse_json(result.pop("status_values_json", None), None)
        result["dimensions"] = _parse_json(result.pop("dimensions_json", None), [])
        result["extensions"] = _parse_json(result.pop("extensions_json", None), {})
        return result
