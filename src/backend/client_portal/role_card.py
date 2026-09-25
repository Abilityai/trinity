"""The role card in Agent details (trinity-enterprise#527, #663).

When a companion has a role (Tandem, ent#497), the Info rail shows what role it
fills, the objectives it owns or supports with each metric's value / target /
freshness, the viewer's relationship to it, and its readiness state. Framework
§8's "organisation UI over the canon" shrunk to one agent.

**Files are truth; this is a projection.** Everything about the role is read
from the agent's own container on each request, through the same agent door
the Files tab uses — never copied platform-side:

* `template.yaml` → `x-role: {role, status, seat?}` (the wizard writes it,
  #511) and `x-canon.clone_path` (default `canon`);
* `<canon>/roles/<id>.yaml`, `<canon>/objectives/*.yaml` (framework §3.4);
* metric values from the agent's own `/api/metrics` (`metrics.json`, whose
  `last_updated` is the freshness stamp).

Every read is fail-soft and NAMED: no `x-role` → no card; an unreadable or
unparseable role file → `role.error`, never an empty role; a stopped agent →
`unavailable: agent_stopped`.

**Readiness is the one thing that is NOT read from the file.** `x-role.status`
is agent-writable and the 2026-09-20 ruling (#663) is that only the agent
OWNER flips `calibrating → ready`, never the agent, never automatically. So the
platform keeps the owner's stamp (`agent_role_readiness`) and the effective
state is: the stamp if one exists; else the template's `calibrating`; a
template that says `ready` with no stamp is shown as calibrating with the note
that no owner has stamped it.

HTTP-free (Invariant #1): the router maps `RoleCardRefused` to a status.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from database import db

from . import db as portal_db

logger = logging.getLogger(__name__)

#: Framework §3.5 — the staleness bound for roles, objectives and the map, and
#: the one rule the framework states for "is this metric current". A per-metric
#: cadence is the business-metrics workstream's to declare.
STALE_AFTER_DAYS = 30
#: §7.2 step 9 — the three-strikes test asks for ten real asks.
WALKTHROUGH_ASKS = 10
#: Bounds on what crosses from author-controlled files to the card.
MAX_TEXT = 400
MAX_OBJECTIVES = 20
MAX_METRICS_PER_OBJECTIVE = 12
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PATH_SEG_RE = re.compile(r"^[A-Za-z0-9._-]+$")

READINESS_STATES = ("calibrating", "ready")


class RoleCardRefused(Exception):
    def __init__(self, code: str, detail: str, status_code: int = 409):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


# ---------------------------------------------------------------------------
# small pure helpers (unit-tested directly)
# ---------------------------------------------------------------------------

def _text(v: Any, cap: int = MAX_TEXT) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s[:cap] if s else None


def _safe_id(v: Any) -> Optional[str]:
    s = _text(v, 64)
    return s if s and _ID_RE.match(s) else None


def canon_root(template: dict) -> Optional[str]:
    """`x-canon.clone_path` (default `canon`), validated as one plain path
    segment chain — it is author-controlled and reaches a file read."""
    xc = template.get("x-canon") if isinstance(template, dict) else None
    raw = (xc.get("clone_path") if isinstance(xc, dict) else None) or "canon"
    raw = str(raw).strip().strip("/")
    if not raw or any(not _PATH_SEG_RE.match(seg) or seg == ".." for seg in raw.split("/")):
        return None
    return raw


def parse_iso(ts: Any) -> Optional[datetime]:
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_stale(as_of: Optional[str], *, now: Optional[datetime] = None,
             bound_days: int = STALE_AFTER_DAYS) -> bool:
    """Missing stamp, unparseable stamp, or older than the bound → stale.
    Never optimistic: an unknown age is stale, not current (quality bar #4)."""
    dt = parse_iso(as_of)
    if dt is None:
        return True
    now = now or datetime.now(timezone.utc)
    return dt < now - timedelta(days=bound_days)


def objective_concerns(obj: dict, role_id: str, agent_name: str) -> bool:
    """An objective belongs on this card when the role owns it or this agent
    supports it (framework §3.4: `owner: role:<id>`, `supporting_agents`)."""
    owner = _text(obj.get("owner"), 128) or ""
    if owner == f"role:{role_id}":
        return True
    supporting = obj.get("supporting_agents")
    return isinstance(supporting, list) and agent_name in [str(a) for a in supporting]


def effective_readiness(template_status: Any, stamp: Optional[dict]) -> dict:
    """The one rule that keeps `ready` honest (#663).

    The owner's stamp wins when it exists. Without one, the template's word is
    `calibrating` whatever it says — and when it says `ready`, the card also
    says that no owner has stamped it, because that is the defect the ruling
    exists to make visible.
    """
    if stamp and stamp.get("status") in READINESS_STATES:
        # ent#689: the rollout's one-time seed is not an owner's act. It says
        # so, and carries no person — `changed_by` is a sentinel, not an email.
        rollout = str(stamp.get("changed_by") or "").startswith("rollout:")
        return {
            "status": stamp["status"],
            "changed_at": stamp.get("changed_at"),
            "changed_by": None if rollout else stamp.get("changed_by"),
            "source": "rollout" if rollout else "owner",
            "unstamped_ready": False,
        }
    claimed = (_text(template_status, 32) or "calibrating").lower()
    return {
        "status": "calibrating",
        "changed_at": None,
        "changed_by": None,
        "source": "template",
        "unstamped_ready": claimed == "ready",
    }


def metric_row(name: str, spec: dict, values: dict, as_of: Optional[str], *,
               now: Optional[datetime] = None) -> dict:
    value = values.get(name) if isinstance(values, dict) else None
    stale = value is None or is_stale(as_of, now=now)
    return {
        "name": name,
        "direction": _text(spec.get("direction"), 16),
        "target": spec.get("target") if isinstance(spec.get("target"), (int, float, str)) else None,
        "by": _text(spec.get("by"), 32),
        "value": value if isinstance(value, (int, float, str)) else None,
        "as_of": as_of if value is not None else None,
        "stale": stale,
    }


# ---------------------------------------------------------------------------
# agent-side reads (all fail-soft, all through the agent door)
# ---------------------------------------------------------------------------

async def _read_yaml(client, path: str) -> tuple[Optional[dict], Optional[str]]:
    """(parsed, error_code). A missing file is `not_found`; anything that is
    not a mapping is `invalid`."""
    from utils.safe_yaml import load_template_yaml
    try:
        res = await client.read_file(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("role card: read %s failed: %s", path, e)
        return None, "unreadable"
    if not res or not res.get("success"):
        return None, "unreadable"
    if res.get("not_found") or res.get("content") is None:
        return None, "not_found"
    try:
        data = load_template_yaml(res["content"])
    except Exception as e:  # noqa: BLE001
        logger.warning("role card: parse %s failed: %s", path, e)
        return None, "invalid"
    return (data, None) if isinstance(data, dict) else (None, "invalid")


async def _list_yaml_files(client, directory: str) -> list[str]:
    """Names of `*.yaml` / `*.yml` directly under `directory`, or [] when the
    directory is absent or unreadable."""
    try:
        resp = await client.get(f"/api/files?path=/home/developer/{directory}")
    except Exception as e:  # noqa: BLE001
        logger.warning("role card: list %s failed: %s", directory, e)
        return []
    if resp is None or getattr(resp, "status_code", 0) != 200:
        return []
    try:
        body = resp.json()
    except ValueError:
        return []
    names: list[str] = []
    for item in (body.get("tree") or body.get("children") or []):
        if not isinstance(item, dict) or item.get("type") == "directory" or item.get("is_dir"):
            continue
        name = str(item.get("name") or "")
        if name.endswith((".yaml", ".yml")) and _PATH_SEG_RE.match(name):
            names.append(name)
    return sorted(names)[:MAX_OBJECTIVES]


async def _read_metrics(client) -> tuple[dict, Optional[str]]:
    """(values, last_updated) from the agent's own metrics endpoint."""
    try:
        resp = await client.get("/api/metrics")
        if resp is None or resp.status_code != 200:
            return {}, None
        body = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("role card: metrics read failed: %s", e)
        return {}, None
    values = body.get("values") if isinstance(body, dict) else None
    return (values if isinstance(values, dict) else {}), (body.get("last_updated") if isinstance(body, dict) else None)


# ---------------------------------------------------------------------------
# the card
# ---------------------------------------------------------------------------

async def build_role_card(agent_name: str, email: str, *, is_platform: bool) -> dict:
    """Everything the Role card shows, or `{"role": None}` when the agent has no role.

    The relationship line reads from ent#500's assignments when they land;
    until then it is `None`, which the client renders as "no assignment
    recorded" rather than blank.
    """
    from services import docker_utils
    from services.agent_client import get_agent_client

    stamp = _readiness_stamp(agent_name)

    try:
        state = await docker_utils.agent_container_state_async(agent_name)
    except Exception:  # noqa: BLE001
        state = None
    if state != "running":
        # Files are truth and the files live in the container. Say so rather
        # than render a card from nothing — but the owner's stamp still shows,
        # it is the one fact that is not in the container.
        return {
            "agent_name": agent_name,
            "role": None,
            "unavailable": "agent_stopped" if state else "agent_unreachable",
            "readiness": effective_readiness(None, stamp) if stamp else None,
            "relationship": None,
            "can_flip_readiness": _is_owner(agent_name, email, is_platform),
        }

    client = get_agent_client(agent_name)
    template, terr = await _read_yaml(client, "template.yaml")
    xrole = template.get("x-role") if isinstance(template, dict) else None
    if not isinstance(xrole, dict):
        # No role → no card (AC 5). An unreadable template is the same answer:
        # there is nothing to show and nothing to warn about.
        return {"agent_name": agent_name, "role": None}

    role_id = _safe_id(xrole.get("role"))
    root = canon_root(template)
    card: dict[str, Any] = {
        "agent_name": agent_name,
        "role": {
            "id": role_id,
            "title": None, "mission": None, "status": None, "review_by": None,
            "path": f"{root}/roles/{role_id}.yaml" if (root and role_id) else None,
            "error": None,
        },
        "seat": _text(xrole.get("seat"), 128),
        "objectives": [],
        "readiness": effective_readiness(xrole.get("status"), stamp),
        # Platform viewers only: an external client can neither act on a held
        # brief nor see the schedules it comes from (the flip is platform-only too).
        "brief_held": is_platform and _brief_held(agent_name, stamp),
        "walkthrough": _walkthrough(agent_name, email, is_platform),
        "relationship": None,
        "can_flip_readiness": _is_owner(agent_name, email, is_platform),
    }
    if not role_id or not root:
        card["role"]["error"] = "role_id_invalid" if not role_id else "canon_path_invalid"
        return card

    role, rerr = await _read_yaml(client, card["role"]["path"])
    if rerr:
        card["role"]["error"] = f"role_file_{rerr}"
        return card
    card["role"].update({
        "title": _text(role.get("title"), 120) or role_id,
        "mission": _text(role.get("mission")),
        "status": _text(role.get("status"), 32),
        "review_by": _text(role.get("review_by"), 32),
        "stale": is_stale(_text(role.get("review_by"), 32)) if role.get("review_by") else False,
    })

    # Objectives: the role owns them or this agent supports them.
    values, as_of = await _read_metrics(client)
    now = datetime.now(timezone.utc)
    for fname in await _list_yaml_files(client, f"{root}/objectives"):
        obj, oerr = await _read_yaml(client, f"{root}/objectives/{fname}")
        if oerr or not objective_concerns(obj, role_id, agent_name):
            continue
        metrics = obj.get("metrics") if isinstance(obj.get("metrics"), list) else []
        rows = []
        for m in metrics[:MAX_METRICS_PER_OBJECTIVE]:
            if isinstance(m, dict) and _safe_id(m.get("name")):
                rows.append(metric_row(_safe_id(m.get("name")), m, values, as_of, now=now))
        card["objectives"].append({
            "id": _safe_id(obj.get("id")) or fname.rsplit(".", 1)[0],
            "statement": _text(obj.get("statement")),
            "horizon": _text(obj.get("horizon"), 16),
            "status": _text(obj.get("status"), 32),
            "owned": (_text(obj.get("owner"), 128) or "") == f"role:{role_id}",
            "metrics": rows,
        })
    return card


def _brief_held(agent_name: str, stamp: Optional[dict]) -> bool:
    """Whether a proactive brief is being held (ent#689): the agent has a live
    seat-delivery schedule and its stamp is not `ready`. Reads the stamp the
    gate reads, never the template. Fail-soft: an unreadable schedule list says
    nothing rather than a claim about a pause."""
    if stamp and stamp.get("status") == "ready":
        return False
    try:
        # Autonomy off stops every schedule before readiness is asked; saying
        # "paused until you mark it ready" then would promise a flip that
        # starts nothing.
        if not db.get_autonomy_enabled(agent_name):
            return False
        return any(
            s.enabled and (s.deliver_to_workspace_email or "").strip()
            for s in db.list_agent_schedules(agent_name)
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("role card: schedule read failed for %s: %s", agent_name, e)
        return False


def _readiness_stamp(agent_name: str) -> Optional[dict]:
    try:
        return db.get_agent_role_readiness(agent_name)
    except Exception as e:  # noqa: BLE001
        logger.warning("role card: readiness read failed for %s: %s", agent_name, e)
        return None


def _is_owner(agent_name: str, email: str, is_platform: bool) -> bool:
    """The platform's OWNER of the agent record — creator / infra owner, not an
    assignment kind (R8/R11). A portal-token principal is never an owner."""
    if not is_platform or not email:
        return False
    try:
        return agent_name in {r["agent_name"] for r in portal_db.get_owned_roster(email)}
    except Exception as e:  # noqa: BLE001
        logger.warning("role card: owner check failed for %s: %s", agent_name, e)
        return False


def _walkthrough(agent_name: str, email: str, is_platform: bool) -> dict:
    """The viewer's own asks in their Main chat with this agent (capped at the
    ten the three-strikes test names) and how many replies they rated down.
    A proxy for the §7.2 walkthrough, labelled as the viewer's own count."""
    out = {"asks": 0, "target": WALKTHROUGH_ASKS, "rated_down": 0, "unavailable": False}
    try:
        from .service import workspace_evaluator
        main_id = portal_db.get_main_portal_session_id(agent_name, email)
        if not main_id:
            return out
        messages = portal_db.get_portal_messages(agent_name, email, limit=200, session_id=main_id)
        asks = [m for m in messages if m.get("role") == "user"]
        replies = [m.get("id") for m in messages if m.get("role") == "assistant" and m.get("id")]
        out["asks"] = min(len(asks), WALKTHROUGH_ASKS)
        if replies:
            mine = db.list_workspace_ratings_for_targets(
                workspace_evaluator(email, is_platform=is_platform), "message", replies)
            out["rated_down"] = sum(1 for q in mine.values() if q is not None and q < 0.5)
    except Exception as e:  # noqa: BLE001
        logger.warning("role card: walkthrough read failed for %s: %s", agent_name, e)
        out["unavailable"] = True
    return out


def flip_readiness(agent_name: str, email: str, *, is_platform: bool, status: str) -> dict:
    """The owner's act (#663). Anyone else is refused by name; the value is
    validated before the row is touched."""
    if status not in READINESS_STATES:
        raise RoleCardRefused("readiness_unknown_state",
                              f"Readiness is 'calibrating' or 'ready', not {status!r}.", 422)
    if not _is_owner(agent_name, email, is_platform):
        raise RoleCardRefused(
            "readiness_owner_only",
            "Only the agent's owner can change its readiness — the person who created "
            "it on this instance, not an assignment.", 403)
    stamp = db.set_agent_role_readiness(agent_name, status, email)
    return effective_readiness(None, stamp)
