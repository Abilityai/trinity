"""Where a credential ask's value is to LAND when that is not the asking agent
(trinity-enterprise#1109).

A `credential` ask (ent#1066) may carry `context.credential_destination`:

    {"instance_url": "https://member.example.com",
     "agent_name":   "lopez-digest",
     "key_env":      "MEMBER_TRINITY_KEY"}

* `instance_url` — the other Trinity's base URL. **It must be an instance an
  admin has approved**: its origin (scheme + host + port, normalised the way
  the A2A client compares origins) must equal that of an endpoint in the
  outbound A2A endpoint registry (`services/a2a_outbound.py`, managed under
  Settings → Integrations → "Outbound A2A endpoints"), read through the
  effective provider so an endpoint a registered module scopes to the agent
  counts too. The registry entry is the approval and nothing else: its own
  credential is never used here (the asking agent's `key_env` is), and the
  base URL names no path — the sink appends the inject route itself. Stored
  normalised (lower-cased canonical host, default port dropped, an IPv6
  literal kept bracketed, the registered scheme kept — a trusted-network
  `http://` endpoint is never rewritten to `https://`).
* `agent_name` — the agent there; a slug the destination's own routes accept.
* `key_env` — the NAME of the variable in the ASKING agent's own `.env` that
  holds its key to that instance. The key itself never travels through the
  ask or the tool call: the sink reads it by name through the asking agent's
  own read endpoint at delivery time. A reserved name (`credential_ask_policy`)
  is refused — the asking agent's platform wiring is never forwarded.

**The outbound A2A kill switch governs this path.** When outbound A2A is off
(`a2a_outbound_service.is_outbound_enabled`: the `a2a_outbound_enabled`
setting, then `A2A_OUTBOUND_ENABLED`, default OFF) a destination is refused
at raise time (`destination_outbound_disabled`) and again at delivery.

**No DNS at raise time.** `raise_ask` is synchronous and is called from an
async route, so a resolver call here would block the worker's event loop for
the resolver's own timeout. The registry entry was resolved and address-
checked when the admin registered it (`upsert_endpoint`), and the sink
resolves the URL again, off the loop, at delivery (`a2a_client.validate_endpoint`)
— "it was registered" is not "it is safe to connect to now".

This leaf never sees a value. It validates at raise time (`validate`), reads
back at answer time (`destination_of`), re-checks the approval and the switch
at delivery (`check_deliverable`), and says where in plain words for the
person and the agent (`public_view`, `plain_words`) — the agent and the host,
never `key_env`, which describes the asking agent's environment and is nobody
else's business.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from services import credential_ask_policy
from utils.url_validation import SCHEME_DEFAULT_PORTS, canonical_host, effective_port

#: The context key a credential ask carries its destination under.
CONTEXT_KEY = "credential_destination"
#: The three fields, and only these, are stored.
FIELDS = ("instance_url", "agent_name", "key_env")
#: An agent slug the destination's path routes accept (`/api/agents/{name}/...`).
AGENT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
INSTANCE_URL_MAX = 512
#: Where an admin registers an instance — the Settings tab and the panel's
#: own heading, so the agent's refusal names a place a person can find.
REGISTRY_LOCATION = "Settings → Integrations → Outbound A2A endpoints"


class DestinationProblem(ValueError):
    """A named refusal: `code` is the 422 code, `reason` says which rule."""

    def __init__(self, code: str, reason: str, message: str):
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.message = message


# ---------------------------------------------------------------------------
# The URL: shape and normalisation, no resolution
# ---------------------------------------------------------------------------


def _origin(url: str) -> Tuple[str, str, Optional[int]]:
    """(scheme, host-as-written-in-an-authority, explicit port) of a base URL,
    or a `DestinationProblem` naming the first rule it breaks. No DNS."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        raise DestinationProblem("destination_url_invalid", "unparseable",
                                 "destination.instance_url could not be parsed as a URL.") from None
    scheme = (parts.scheme or "").lower()
    if scheme not in SCHEME_DEFAULT_PORTS:
        raise DestinationProblem(
            "destination_url_invalid", "scheme",
            "destination.instance_url must be the other Trinity's https:// base URL.")
    if parts.username is not None or parts.password is not None:
        raise DestinationProblem(
            "destination_url_invalid", "userinfo",
            "destination.instance_url must not embed credentials (user:password@host); "
            "the key is read from your environment by the name in key_env.")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise DestinationProblem(
            "destination_url_invalid", "path",
            "destination.instance_url is the other Trinity's base URL (scheme and host only); "
            "the platform adds the route itself.")
    try:
        raw_host = parts.hostname or ""
        port = parts.port
    except ValueError:
        raise DestinationProblem("destination_url_invalid", "unparseable",
                                 "destination.instance_url could not be parsed as a URL.") from None
    if not raw_host:
        raise DestinationProblem("destination_url_invalid", "missing_host",
                                 "destination.instance_url names no host.")
    host = canonical_host(raw_host) or raw_host.lower().rstrip(".")
    # `urlsplit().hostname` strips an IPv6 literal's brackets; an authority
    # needs them back or it is unparseable (the A2A client's own rule).
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return scheme, host, port


def validate_instance_url(url: object) -> str:
    """The normalised origin of ``url`` (`scheme://host[:port]`), or a
    `DestinationProblem` named `destination_url_invalid`. Shape only — the
    registry decides whether it is an approved instance, the sink resolves it
    at delivery."""
    if not isinstance(url, str):
        raise DestinationProblem("destination_url_invalid", "not_a_string",
                                 "destination.instance_url must be the other Trinity's base URL.")
    if not url.strip():
        raise DestinationProblem("destination_url_invalid", "missing",
                                 "destination.instance_url is required.")
    if len(url) > INSTANCE_URL_MAX:
        raise DestinationProblem("destination_url_invalid", "too_long",
                                 f"destination.instance_url is at most {INSTANCE_URL_MAX} characters.")
    scheme, host, port = _origin(url)
    effective = effective_port(port, scheme)
    netloc = host if effective in (None, SCHEME_DEFAULT_PORTS[scheme]) else f"{host}:{effective}"
    return urlunsplit((scheme, netloc, "", "", ""))


# ---------------------------------------------------------------------------
# The approval and the switch — seams reached through the module objects
# ---------------------------------------------------------------------------


def outbound_enabled() -> bool:
    """The outbound A2A kill switch, as the A2A path reads it."""
    from services import a2a_outbound_service
    return bool(a2a_outbound_service.is_outbound_enabled())


def approved_endpoint_url(agent_name: str, url: str) -> Optional[str]:
    """The registered outbound A2A endpoint whose origin equals ``url``'s, or
    None. Through the EFFECTIVE provider (`a2a_outbound.list_endpoints`), so a
    module that scopes endpoints per agent answers for that agent; the
    comparison is the A2A client's own (`_same_origin`: default-port
    equivalence, canonical host, IP literals as addresses)."""
    from services import a2a_client, a2a_outbound
    for row in a2a_outbound.list_endpoints(agent_name):
        registered = str(row.get("url") or "")
        if registered and a2a_client._same_origin(registered, url):
            return registered
    return None


def check_deliverable(agent_name: str, instance_url: str) -> None:
    """The two admin-held gates, in the order a refusal should name them: the
    switch, then the registry. Raises `DestinationProblem`."""
    if not outbound_enabled():
        raise DestinationProblem(
            "destination_outbound_disabled", "outbound_disabled",
            "Outbound A2A is switched off on this Trinity, so a credential cannot be "
            f"delivered to another instance. An admin can allow outbound A2A calls under "
            f"{REGISTRY_LOCATION}.")
    if approved_endpoint_url(agent_name, instance_url) is None:
        raise DestinationProblem(
            "destination_not_approved", "not_registered",
            "destination.instance_url is not a Trinity an admin has approved for this agent. "
            f"An admin registers it under {REGISTRY_LOCATION}; the registered address must "
            "match the destination's scheme, host and port.")


# ---------------------------------------------------------------------------
# The ask
# ---------------------------------------------------------------------------


def validate(destination: object, *, agent_name: str) -> Dict[str, str]:
    """The stored form of ``destination`` for an ask ``agent_name`` raises, or
    a `DestinationProblem`. Shape, then the switch, then the registry; no
    resolver call (see the module docstring)."""
    if not isinstance(destination, Mapping):
        raise DestinationProblem("destination_invalid", "not_an_object",
                                 "destination must be an object with instance_url, agent_name and key_env.")
    missing = [f for f in FIELDS if f not in destination]
    if missing:
        raise DestinationProblem("destination_invalid", "missing_field",
                                 f"destination is missing {', '.join(missing)}.")
    instance_url = validate_instance_url(destination.get("instance_url"))
    agent = destination.get("agent_name")
    if not isinstance(agent, str) or not AGENT_NAME_RE.match(agent):
        raise DestinationProblem(
            "destination_agent_invalid", "not_a_name",
            "destination.agent_name must be the agent's name on that Trinity: letters, digits, "
            "'_', '.' or '-', at most 64 characters.")
    key_env = destination.get("key_env")
    problem = credential_ask_policy.credential_name_problem(key_env)
    if problem is not None:
        raise DestinationProblem(
            "destination_key_env_invalid", problem,
            "destination.key_env must name the variable in YOUR environment that holds your key "
            "to that Trinity — a variable name of at most 64 characters that is not one the "
            "platform writes.")
    assert isinstance(key_env, str)
    check_deliverable(agent_name, instance_url)
    return {"instance_url": instance_url, "agent_name": agent, "key_env": key_env}


def destination_of(context: object) -> Optional[Dict[str, str]]:
    """The destination a stored row carries, SHAPE-checked (no DNS, no
    registry), or None. Read at answer time: the sink re-checks the switch and
    the registry and resolves the URL itself before it connects."""
    if not isinstance(context, Mapping):
        return None
    raw = context.get(CONTEXT_KEY)
    if not isinstance(raw, Mapping):
        return None
    out = {f: raw.get(f) for f in FIELDS}
    if not all(isinstance(v, str) and v for v in out.values()):
        return None
    if not AGENT_NAME_RE.match(out["agent_name"]):
        return None
    if credential_ask_policy.credential_name_problem(out["key_env"]) is not None:
        return None
    return out  # type: ignore[return-value]


def host_of(destination: Mapping[str, Any]) -> str:
    """The host (with its explicit port, if any) a person recognises."""
    parts = urlsplit(str(destination.get("instance_url") or ""))
    return parts.netloc


def public_view(destination: Optional[Mapping[str, Any]]) -> Optional[Dict[str, str]]:
    """What the person and the agent are shown: the agent and the host.
    Never `key_env`."""
    if not destination:
        return None
    return {"agent_name": str(destination.get("agent_name") or ""), "host": host_of(destination)}


def plain_words(destination: Mapping[str, Any]) -> str:
    return f"your agent {destination.get('agent_name')} on your Trinity at {host_of(destination)}"
