"""The seats an agent's canon defines — `<canon>/roles/*.yaml` (trinity-enterprise#817).

Lists each role file's id (the file stem), `title` and `updated` stamp, read out
of the agent's own container through the agent door the objective join and the
role card already use. **Files are truth; this is a projection**: nothing is
copied platform-side.

Consumers: the assignments section's seat picker and header (the title and the
`updated` stamp beside the seat), and the assignment drift check, which compares
a row's recorded stamp with the file's current one.

OSS-core (operator ruling 2026-10-06), like the objective join beside it: the
read is edition-agnostic; only the gated assignments section uses it.

Never a blank: every reason the list is empty is named — the agent is stopped,
the template declares no canon, there is no `roles/` directory, the listing could
not be read — so "no seats defined" is never confused with "could not look".
The fan-out draws on the objectives read budget, the container's one bucket.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from services import objective_join_service as oj

logger = logging.getLogger(__name__)

#: A canon defines a handful of seats; a hundred is a misconfigured directory.
MAX_ROLES = 100
#: The whole read, listing included — well inside a page load.
ROLES_READ_BUDGET_SEC = 15.0
_CONCURRENCY = 5

UNAVAILABLE_COPY = {
    "agent_stopped": "this agent is stopped — its role files live in its container; start it to read them",
    "agent_missing": "no container for this agent — recreate it to read its role files",
    "agent_unreachable": "the agent is not answering — its role files are read from its container",
}
REASON_COPY = {
    "no_canon": "no `x-canon` in template.yaml — this agent declares no canon, so it defines no seats",
    "canon_path_invalid": "`x-canon.clone_path` in template.yaml is not a plain path — no file was read with it",
    "absent": "the canon has no `roles/` directory",
    "unreadable": "the canon's `roles/` directory could not be listed",
    "timeout": "the role files took too long to read — retry",
    "template_unreadable": "template.yaml could not be read from this agent",
}


def _stamp(value: Any) -> Optional[str]:
    """`updated:` as text. YAML reads an unquoted date as a date object."""
    if value is None:
        return None
    return oj._text(str(value), 64)


def _empty(agent_name: str, *, root=None, unavailable=None, reason=None) -> Dict[str, Any]:
    message = UNAVAILABLE_COPY.get(unavailable) or REASON_COPY.get(reason)
    return {"agent_name": agent_name, "canon_root": root, "roles": [],
            "unavailable": unavailable, "reason": reason, "message": message,
            "truncated": False}


async def read_canon_roles(agent_name: str, *, client=None) -> Dict[str, Any]:
    """`{agent_name, canon_root, roles: [{id, title, updated, path, error}],
    unavailable, reason, message, truncated}`. Never raises."""
    from services import docker_utils
    from services.agent_client import get_agent_client

    try:
        state = await docker_utils.agent_container_state_async(agent_name)
    except Exception as e:  # noqa: BLE001
        logger.warning("canon roles: container state for %s failed: %s", agent_name, e)
        state = None
    if state != "running":
        unavailable = {"stopped": "agent_stopped", "missing": "agent_missing"}.get(state, "agent_unreachable")
        return _empty(agent_name, unavailable=unavailable)

    client = client or get_agent_client(agent_name)
    try:
        return await asyncio.wait_for(_read(agent_name, client), ROLES_READ_BUDGET_SEC)
    except asyncio.TimeoutError:
        return _empty(agent_name, reason="timeout")
    except oj._Unreachable:
        return _empty(agent_name, unavailable="agent_unreachable")
    except Exception as e:  # noqa: BLE001 — a bad canon is never a 500
        logger.warning("canon roles: read for %s failed: %s", agent_name, e)
        return _empty(agent_name, reason="unreadable")


async def _read(agent_name: str, client) -> Dict[str, Any]:
    template, terr = await oj._read_yaml(client, "template.yaml")
    if terr:
        return _empty(agent_name, reason="template_unreadable")
    if not isinstance(template, dict) or "x-canon" not in template:
        return _empty(agent_name, reason="no_canon")
    root = oj.canon_root(template)
    if root is None:
        return _empty(agent_name, reason="canon_path_invalid")

    names, _skipped, source = await oj._list_objective_files(client, root, "roles")
    if source != "read":
        return _empty(agent_name, root=root, reason=source)

    truncated = len(names) > MAX_ROLES
    names = names[:MAX_ROLES]
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def one(name: str) -> Optional[Dict[str, Any]]:
        role_id = oj._safe_id(name.rsplit(".", 1)[0])
        if not role_id:
            return None
        path = f"{root}/roles/{name}"
        async with sem:
            doc, err = await oj._read_yaml(client, path)
        if err:
            return {"id": role_id, "title": None, "updated": None, "path": path, "error": err}
        return {"id": role_id, "title": oj._text(doc.get("title"), 120),
                "updated": _stamp(doc.get("updated")), "path": path, "error": None}

    roles = [r for r in await asyncio.gather(*(one(n) for n in names)) if r]
    roles.sort(key=lambda r: r["id"])
    return {"agent_name": agent_name, "canon_root": root, "roles": roles,
            "unavailable": None, "reason": None if roles else "empty",
            "message": None if roles else "the canon's `roles/` directory holds no role file",
            "truncated": truncated}
