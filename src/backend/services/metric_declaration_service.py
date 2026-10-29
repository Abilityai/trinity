"""Declare, edit and retire an agent's metrics from the UI (trinity-enterprise#1006).

The template stays the single source of truth (operator ruling 2026-10-28). A
form write is: read the agent's `template.yaml` → edit one `metrics:` item
(`metric_declaration_editor`) → write the file back through the audited Files
write → re-run the registry reconcile from the file just written. The
registry, `record_metrics`, git auto-sync and any later pull therefore see the
same definition a hand edit would have produced; nothing exists only in the
database.

**Compare-and-swap.** Every write carries the template hash the editor read
returned. The file is re-read immediately before the write and a mismatch is
`template_changed` — an agent that edited its own template in the meantime is
never overwritten. Writes for one agent are also serialised within the process.

**Never on absence of evidence (#2196).** An unreadable template or a stopped
agent refuses the write with a named reason and changes nothing; no metric is
ever retired because a read failed.

**The write is the Files write.** `update_agent_file_logic` applies the Files
tier (owner, or an agent key holding `instructions.manage` — `template.yaml` is
in the instruction file class) and writes the `file_write` audit row, so the
route gate in front of this service is belt-and-braces, not the only fence.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from database import db
from services import metric_declaration_editor as editor
from services import metric_references, metric_registry, template_metrics

logger = logging.getLogger(__name__)

TEMPLATE_FILE = "template.yaml"

# The retire warning counts points to here; past it the UI says "10,000+".
POINT_COUNT_LIMIT = 10_000

# Each reference lookup that crosses into the agent is bounded, so a hung
# agent cannot hold a retire dialog open.
_LOOKUP_TIMEOUT = 20.0

_locks: Dict[str, asyncio.Lock] = {}


class DeclarationRefused(Exception):
    """A write refused with a stable `code` and an HTTP status for the route."""

    def __init__(self, code: str, message: str, status: int, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra


def _lock(agent_name: str) -> asyncio.Lock:
    lock = _locks.get(agent_name)
    if lock is None:
        lock = _locks[agent_name] = asyncio.Lock()
    return lock


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def to_form(entry: Dict[str, Any]) -> Dict[str, Any]:
    """A raw `metrics:` entry as the form's fields, plus whether it is valid."""
    errors = template_metrics.metric_shape_errors([entry])
    recorded_by = entry.get(editor.RECORDED_BY_KEY)
    return {
        **{key: entry.get(key) for key in editor.ENTRY_KEY_ORDER},
        "recorded_by": recorded_by if isinstance(recorded_by, str) else None,
        "valid": not errors,
        "errors": [editor._field_error(e) for e in errors],
    }


async def _read(agent_name: str) -> str:
    try:
        return await metric_registry.read_template_text(agent_name)
    except metric_registry.RefreshUnavailable as e:
        raise DeclarationRefused(
            e.reason, _UNAVAILABLE_COPY.get(e.reason, e.message),
            409 if e.reason == "agent_not_running" else 503) from e


_UNAVAILABLE_COPY = {
    "agent_not_running": ("The agent must be running to change its metrics: they are "
                          "saved in template.yaml inside the agent. Nothing was changed."),
    "template_unreadable": ("template.yaml could not be read. It may have a YAML error. "
                            "Nothing was changed, and no metric was retired."),
}


async def read_editor(agent_name: str) -> Dict[str, Any]:
    """The form's read: entries as written, the CAS hash, and editability."""
    raw = await _read(agent_name)
    try:
        state = editor.read_declarations(raw)
    except editor.TemplateUnreadable as e:
        raise DeclarationRefused("template_unreadable", e.message, 503) from e
    return {
        "agent_name": agent_name,
        "template_hash": state["template_hash"],
        "editable": state["editable"],
        "reason": state["reason"],
        "declared_count": len(state["entries"]),
        "cap": template_metrics.MAX_DECLARED_METRICS,
        "declarations": [to_form(e) for e in state["entries"]],
    }


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


async def _write(agent_name: str, op: str, name: str, base_hash: str, *,
                 fields: Optional[Dict[str, Any]], current_user, request,
                 acknowledge_references: bool = False) -> Dict[str, Any]:
    from services.agent_service import update_agent_file_logic

    async with _lock(agent_name):
        raw = await _read(agent_name)
        if editor.template_hash(raw) != base_hash:
            raise DeclarationRefused(
                "template_changed",
                "template.yaml changed after you opened this form. Reload to apply "
                "your changes to the latest version.", 409)
        try:
            entries = editor.read_declarations(raw)["entries"]
        except editor.TemplateUnreadable as e:
            raise DeclarationRefused("template_unreadable", e.message, 503) from e

        entry = None
        if op in ("add", "replace"):
            existing = None
            if op == "replace":
                index = editor._find(entries, name)
                if index is None:
                    raise DeclarationRefused("metric_not_found",
                                             f"No metric named {name} is declared.", 404)
                existing = entries[index]
            entry = editor.build_entry(fields or {}, existing=existing)
            try:
                editor.validate(entries, op, name, entry)
            except editor.MetricNotFound as e:
                raise DeclarationRefused("metric_not_found",
                                         f"No metric named {name} is declared.", 404) from e
            except editor.DeclarationInvalid as e:
                raise DeclarationRefused("metric_invalid", "Some fields need a fix.", 422,
                                         errors=e.errors) from e
        else:
            if editor._find(entries, name) is None:
                raise DeclarationRefused("metric_not_found",
                                         f"No metric named {name} is declared.", 404)
            references, unchecked = await find_references(agent_name, name)
            if (references or unchecked) and not acknowledge_references:
                raise DeclarationRefused(
                    "metric_in_use",
                    "This metric is still in use. Confirm to retire it anyway.", 409,
                    references=references, unchecked=unchecked)

        try:
            new_raw = editor.apply(raw, op, name, entry)
        except editor.MetricNotFound as e:
            raise DeclarationRefused("metric_not_found",
                                     f"No metric named {name} is declared.", 404) from e
        except editor.TemplateNotEditable as e:
            raise DeclarationRefused(e.code, e.message, 409 if e.code == editor.TEMPLATE_NOT_EDITABLE else 503) from e

        # The last read before the write: an edit that landed while this one
        # was being computed wins, and this one is refused.
        if await _read(agent_name) != raw:
            raise DeclarationRefused(
                "template_changed",
                "template.yaml changed while this change was being saved. Reload and try again.", 409)
        await update_agent_file_logic(agent_name, TEMPLATE_FILE, new_raw, current_user, request)

    await _audit(op, agent_name, name, current_user, request)
    return await _after_write(agent_name, op, name, new_raw)


async def _after_write(agent_name: str, op: str, name: str, new_raw: str) -> Dict[str, Any]:
    """Refresh the registry from the file just written; report what it did."""
    result: Dict[str, Any] = {
        "success": True,
        "op": op,
        "name": name,
        "template_hash": editor.template_hash(new_raw),
        "registry_refreshed": True,
        "registry": None,
        "message": None,
    }
    try:
        summary = await metric_registry.refresh_from_running_agent(agent_name, source="ui")
        result["registry"] = summary.to_dict()
    except metric_registry.RefreshUnavailable as e:
        # The file is written, so the change is real; the registry catches up
        # on the next start, pull or refresh. Said, not hidden.
        result["registry_refreshed"] = False
        result["message"] = (f"Saved to template.yaml, but the metric list could not be "
                             f"refreshed yet ({e.reason}). It updates on the next refresh.")
    return result


async def _audit(op: str, agent_name: str, name: str, current_user, request) -> None:
    """One semantic row per declaration change, beside the Files `file_write`
    row: the file row says a file changed, this one says which metric."""
    try:
        from services.platform_audit_service import platform_audit_service, AuditEventType
        await platform_audit_service.log(
            event_type=AuditEventType.AGENT_FILES,
            event_action={"add": "metric_declared", "replace": "metric_declaration_updated",
                          "remove": "metric_retired"}[op],
            source="api",
            actor_user=current_user,
            actor_ip=request.client.host if request.client else None,
            target_type="agent",
            target_id=agent_name,
            endpoint=request.scope["path"],
            request_id=getattr(request.state, "request_id", None),
            details={"metric": name, "file": TEMPLATE_FILE},
        )
    except Exception:  # noqa: BLE001 — an audit failure never fails a write that happened
        logger.warning("metric declaration audit failed: agent=%s metric=%s", agent_name, name)


async def declare(agent_name: str, fields: Dict[str, Any], base_hash: str, *,
                  current_user, request) -> Dict[str, Any]:
    return await _write(agent_name, "add", fields.get("name") or "", base_hash,
                        fields=fields, current_user=current_user, request=request)


async def update(agent_name: str, name: str, fields: Dict[str, Any], base_hash: str, *,
                 current_user, request) -> Dict[str, Any]:
    return await _write(agent_name, "replace", name, base_hash,
                        fields=fields, current_user=current_user, request=request)


async def retire(agent_name: str, name: str, base_hash: str, *, acknowledge_references: bool,
                 current_user, request) -> Dict[str, Any]:
    return await _write(agent_name, "remove", name, base_hash, fields=None,
                        current_user=current_user, request=request,
                        acknowledge_references=acknowledge_references)


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


def _points(agent_name: str, metric: str) -> List[Dict[str, Any]]:
    count, newest = db.metric_point_usage(agent_name, metric, POINT_COUNT_LIMIT + 1)
    if not count:
        return []
    shown = f"{POINT_COUNT_LIMIT:,}+" if count > POINT_COUNT_LIMIT else f"{count:,}"
    return [{"kind": "points", "label": f"{shown} recorded points",
             "detail": None, "count": min(count, POINT_COUNT_LIMIT), "last_point_at": newest}]


async def _objectives(agent_name: str, metric: str) -> List[Dict[str, Any]]:
    from services import objective_join_service

    joined = await asyncio.wait_for(
        objective_join_service.read_objective_join(agent_name), _LOOKUP_TIMEOUT)
    if joined.get("unavailable"):
        raise RuntimeError(joined["unavailable"])
    out = []
    for objective in joined.get("objectives") or []:
        if any((m or {}).get("name") == metric for m in objective.get("metrics") or []):
            out.append({"kind": "objective", "label": str(objective.get("id") or "objective")[:200],
                        "detail": (objective.get("statement") or None)})
    return out


async def _widgets(agent_name: str, metric: str) -> List[Dict[str, Any]]:
    from services.agent_service.helpers import agent_http_request

    response = await agent_http_request(agent_name, "GET", "/api/dashboard",
                                        max_retries=1, timeout=_LOOKUP_TIMEOUT)
    if response.status_code != 200:
        raise RuntimeError(f"dashboard read {response.status_code}")
    config = (response.json() or {}).get("config") or {}
    out = []
    for section in config.get("sections") or []:
        for widget in (section or {}).get("widgets") or []:
            if isinstance(widget, dict) and widget.get("metric") == metric:
                label = widget.get("label") or widget.get("title") or metric
                out.append({"kind": "widget", "label": str(label)[:200], "detail": "dashboard.yaml"})
    return out


async def find_references(agent_name: str, metric: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Everything that still reads `metric`, and the sources that could not be checked.

    A source that fails is UNCHECKED, never "no references" (#2196): the retire
    then needs the same acknowledgement a real reference would.
    """
    references: List[Dict[str, Any]] = []
    unchecked: List[str] = []
    try:
        references.extend(_points(agent_name, metric))
    except Exception as e:  # noqa: BLE001
        logger.warning("metric references: points for %s/%s failed: %s", agent_name, metric, e)
        unchecked.append("points")
    for kind, finder in (("objectives", _objectives), ("dashboard", _widgets)):
        try:
            references.extend(await finder(agent_name, metric))
        except Exception as e:  # noqa: BLE001
            logger.info("metric references: %s for %s/%s unchecked: %s",
                        kind, agent_name, metric, type(e).__name__)
            unchecked.append(kind)
    found, failed = await metric_references.from_providers(agent_name, metric)
    references.extend(found)
    unchecked.extend(failed)
    return references, unchecked


async def references(agent_name: str, metric: str) -> Dict[str, Any]:
    refs, unchecked = await find_references(agent_name, metric)
    return {"agent_name": agent_name, "metric": metric,
            "references": refs, "unchecked": unchecked}
