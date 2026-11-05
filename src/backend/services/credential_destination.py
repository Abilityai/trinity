"""Where a credential ask's value is to LAND when that is not the asking agent
(trinity-enterprise#1109).

A `credential` ask (ent#1066) may carry `context.credential_destination`:

    {"instance_url": "https://member.example.com",
     "agent_name":   "lopez-digest",
     "key_env":      "MEMBER_TRINITY_KEY"}

* `instance_url` — the other Trinity's base URL. It passes the one outbound-
  destination gate this backend has, `validate_a2a_endpoint_url` (HTTPS, no
  userinfo, every resolved address public — or a host the admin listed in the
  A2A trusted networks, ent#838), and names no path: the sink appends the
  inject route itself. Stored normalised (lower-cased host, no trailing slash).
* `agent_name` — the agent there; a slug the destination's own routes accept.
* `key_env` — the NAME of the variable in the ASKING agent's own `.env` that
  holds its key to that instance. The key itself never travels through the
  ask or the tool call: the sink reads it by name through the asking agent's
  own read endpoint at delivery time. A reserved name (`credential_ask_policy`)
  is refused — the asking agent's platform wiring is never forwarded.

This leaf never sees a value. It validates at raise time (`validate`), reads
back at answer time (`destination_of`), and says where in plain words for the
person and the agent (`public_view`, `plain_words`) — the agent and the host,
never `key_env`, which describes the asking agent's environment and is nobody
else's business.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

from services import credential_ask_policy
from utils.url_validation import A2AEndpointUrlError, ValidatedPublicUrl, validate_a2a_endpoint_url

#: The context key a credential ask carries its destination under.
CONTEXT_KEY = "credential_destination"
#: The three fields, and only these, are stored.
FIELDS = ("instance_url", "agent_name", "key_env")
#: An agent slug the destination's path routes accept (`/api/agents/{name}/...`).
AGENT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
INSTANCE_URL_MAX = 512


class DestinationProblem(ValueError):
    """A named refusal: `code` is the 422 code, `reason` says which rule."""

    def __init__(self, code: str, reason: str, message: str):
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.message = message


def _resolver() -> Optional[Callable[..., list]]:
    """The DNS resolver the URL gate uses — None for `socket.getaddrinfo`.
    A seam for tests only."""
    return None


def validate_instance_url(url: object) -> ValidatedPublicUrl:
    """The URL gate, with its own refusals named `destination_url_invalid`."""
    if not isinstance(url, str):
        raise DestinationProblem("destination_url_invalid", "not_a_string",
                                 "destination.instance_url must be the other Trinity's base URL.")
    if not url.strip():
        raise DestinationProblem("destination_url_invalid", "missing",
                                 "destination.instance_url is required.")
    if len(url) > INSTANCE_URL_MAX:
        raise DestinationProblem("destination_url_invalid", "too_long",
                                 f"destination.instance_url is at most {INSTANCE_URL_MAX} characters.")
    parts = urlsplit(url.strip())
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise DestinationProblem(
            "destination_url_invalid", "path",
            "destination.instance_url is the other Trinity's base URL (scheme and host only); "
            "the platform adds the route itself.")
    try:
        return validate_a2a_endpoint_url(url, resolver=_resolver())
    except A2AEndpointUrlError as exc:
        raise DestinationProblem("destination_url_invalid", exc.reason, str(exc)) from None


def _normalised_url(validated: ValidatedPublicUrl) -> str:
    parts = urlsplit(validated.url)
    netloc = validated.hostname if parts.port is None else f"{validated.hostname}:{parts.port}"
    return urlunsplit(("https", netloc, "", "", ""))


def validate(destination: object) -> Dict[str, str]:
    """The stored form of ``destination``, or a `DestinationProblem`.

    Resolves the host (the gate's rule: DNS failure is fatal), so this is a
    raise-time check that costs one lookup; the sink runs it again at delivery
    time, because "it was valid when asked" is not "it is safe to connect to".
    """
    if not isinstance(destination, Mapping):
        raise DestinationProblem("destination_invalid", "not_an_object",
                                 "destination must be an object with instance_url, agent_name and key_env.")
    missing = [f for f in FIELDS if f not in destination]
    if missing:
        raise DestinationProblem("destination_invalid", "missing_field",
                                 f"destination is missing {', '.join(missing)}.")
    validated = validate_instance_url(destination.get("instance_url"))
    agent_name = destination.get("agent_name")
    if not isinstance(agent_name, str) or not AGENT_NAME_RE.match(agent_name):
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
    return {"instance_url": _normalised_url(validated), "agent_name": agent_name, "key_env": key_env}


def destination_of(context: object) -> Optional[Dict[str, str]]:
    """The destination a stored row carries, SHAPE-checked (no DNS), or None.
    Read at answer time: the sink re-validates the URL itself before it
    connects."""
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
