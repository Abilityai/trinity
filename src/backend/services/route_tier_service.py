"""The route census, read at runtime (trinity-enterprise#926, PR #3778 review).

`agent_route_tiers.json` (beside this file) declares the tier of every route
that names an agent; `tests/unit/test_ent926_agent_route_census.py` holds it to
the code (an undeclared route, a stale row or a row whose gate disagrees fails
CI). This module lets the auth entry point ask the same table which routes an
agent's key may not use while it acts for a turn the platform restricts:

  * every `owner` row, unless the row carries `restricted_turn` with a reason;
  * every `resource` row, on the same terms (PR #3778 review): a resource
    gate is creator-or-owner, and an agent key carries the OWNER's id and
    username, so in a shared person's turn it passes for every owner-created
    schedule — reading a webhook URL (a bearer credential that starts an
    owner-tier run) or deleting the owner's schedules;
  * every row flagged `standing_work` (work that later runs as the owner);
  * an agent-naming route with NO row (the private submodule's, or one added
    since the table was read): owner tier, the census's own default;
  * the two standing-work routes that name no agent (`STANDING_WORK_UNNAMED`);
  * the creator-gated routes that name no agent and create or act on the
    owner's agents (`OWNER_TIER_UNNAMED`, PR #3778 review): `require_role`
    passes on the role the agent key carries, the OWNER's, so a shared
    person's turn could create an agent owned by the owner — cloned with the
    owner's GitHub PAT, which is resolved at creation (Invariant #12).

A route is matched by the template FastAPI matched (`scope["route"].path`), so a
renamed path stays tied to its row through the census test. Without one (a
request that never went through the router), the concrete path is matched
against the table's templates, the most literal template winning.
"""
import json
import re
from pathlib import Path
from typing import Optional, Tuple

_TABLE_PATH = Path(__file__).with_name("agent_route_tiers.json")
TABLE: dict = json.loads(_TABLE_PATH.read_text())

OWNER = "owner"
RESOURCE = "resource"

# Standing work on routes whose path names no agent (so the census has no row).
STANDING_WORK_UNNAMED = frozenset({
    ("POST", "/api/events"),
    ("PUT", "/api/event-subscriptions/{subscription_id}"),
})

# Owner-tier routes that name no agent (so the census has no row). The census
# test fails a creator-gated route that names no agent and is in neither this
# set nor its read-only allowlist, so a new create/deploy route fails closed.
OWNER_TIER_UNNAMED = frozenset({
    ("POST", "/api/agents"),
    ("POST", "/api/agents/deploy-local"),
    ("POST", "/api/systems/deploy"),
    ("POST", "/api/systems/{system_name}/restart"),
})


def names_an_agent(template: str) -> bool:
    """The census scanner's own test for an agent-naming route."""
    return ("{agent_name}" in template or "/agents/{" in template
            or template.startswith("/api/agent-dashboard/{name}"))


_BY_ROUTE = {(row["method"], row["path"]): row for row in TABLE.values()}


def _template_regex(template: str) -> "re.Pattern":
    parts = re.split(r"(\{[^}]+\})", template)
    out = []
    for part in parts:
        if part.startswith("{") and part.endswith("}"):
            out.append(".+" if part.endswith(":path}") else "[^/]+")
        else:
            out.append(re.escape(part))
    return re.compile("^" + "".join(out) + "$")


_TEMPLATES = sorted(
    {(m, p) for m, p in _BY_ROUTE} | set(STANDING_WORK_UNNAMED) | set(OWNER_TIER_UNNAMED),
    key=lambda mp: (mp[1].count("{"), -len(mp[1])),
)
_COMPILED = [(m, p, _template_regex(p)) for m, p in _TEMPLATES]


def matched_template(request) -> Optional[str]:
    route = (getattr(request, "scope", None) or {}).get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and path else None


def resolve(method: str, request) -> Tuple[Optional[str], Optional[dict]]:
    """(template, census row) for this request; the row is None when the
    census has none, the template None when nothing matched at all."""
    template = matched_template(request)
    if template is not None:
        return template, _BY_ROUTE.get((method, template))
    scope = getattr(request, "scope", None) or {}
    path = scope.get("path") or ""
    for m, p, rx in _COMPILED:
        if m == method and rx.match(path):
            return p, _BY_ROUTE.get((m, p))
    return None, None


def refused_in_restricted_turn(method: str, request) -> Optional[str]:
    """`"standing_work"` or `"owner_tier"` when an agent key acting in a
    restricted turn may not use this route, else None."""
    method = (method or "").upper()
    template, row = resolve(method, request)
    if template is None:
        return None
    if (method, template) in STANDING_WORK_UNNAMED:
        return "standing_work"
    if (method, template) in OWNER_TIER_UNNAMED:
        return "owner_tier"
    if row is None:
        return "owner_tier" if names_an_agent(template) else None
    if row.get("standing_work"):
        return "standing_work"
    if row.get("tier") in (OWNER, RESOURCE) and not row.get("restricted_turn"):
        return "owner_tier"
    return None
