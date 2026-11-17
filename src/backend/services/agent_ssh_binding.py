"""Where an agent container's SSH port is published, and what to tell the admin
about it (trinity-enterprise#1168).

Two concerns, one module, because they are the two halves of one promise —
*the port is published where the operator said, and the grant response says
where that is*:

1. **Create-time publication.** `agent_ssh_port_binding(port)` is the only
   place the `22/tcp` mapping is built. docker-py publishes a bare int on
   every host interface (`HostIp == ""` → `0.0.0.0`); the tuple form
   `(host_ip, port)` binds it to one. All three agent-container create sites
   pass `ports=agent_ssh_port_binding(...)`, and
   `tests/unit/test_1168_agent_ssh_bind.py` pins that by name — the fourth
   creation-time property to prove "the create path is never one call site"
   (#1098 tmpfs, #1871 log_config, #2541 restart policy before it). The
   constants those three share live in `agent_service/capabilities.py`; this
   one is here because the API-side table below needs it too.

2. **The advertised connection.** `decide_ssh_connection(...)` reads what
   Docker ACTUALLY published for an existing container (Invariant #11 — a
   container created before the default changed still publishes on every
   interface until it is recreated) and returns the host to advertise plus
   the warnings that make the loopback case non-silent.

Stdlib only, on purpose: this sits on the create path and in the unit-test
island, and must never pull docker / redis / fastapi in. The host-detection
chain (`ssh_service.get_ssh_host_detail`) is passed IN, never imported — the
service imports this module, not the other way round.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

AGENT_SSH_BIND_ADDRESS_ENV = "AGENT_SSH_BIND_ADDRESS"

# Loopback when unset. Deliberately NOT the ent#1101 `MCP_BIND_ADDRESS` shape
# (unset = every interface, `.env.example` ships loopback): that knob's port is
# published by compose, so its default is a compose constraint; this one is
# published by the backend, so its default is a policy — and the listener it
# guards serves nobody on a default install (`ssh_access_enabled` is off).
DEFAULT_AGENT_SSH_BIND_ADDRESS = "127.0.0.1"

# The container-side port. The ONLY spelling of this constant under
# services/** — the guard rejects any other (S7).
SSH_CONTAINER_PORT = "22/tcp"

_ALL_INTERFACES = frozenset({"0.0.0.0", "::"})

# Host-detection sources that name a host an admin can reach from their own
# machine, so a `-J <you>@<host>` jump through the Trinity host makes sense.
# `host.docker.internal`, the Docker gateway and `tailscale` (a container-side
# probe) do not: from a Mac the first two are undialable (E10).
_JUMPABLE_HOST_SOURCES = frozenset({"ssh_host", "frontend_url"})

# Values already reported, so a bad `.env` logs once per process rather than
# once per container create (the sibling of `_resolve_agent_tmp_size`'s
# silent fallback, but a published interface is worth one loud line).
_logged_values: set[str] = set()


def _reset_logged_values_for_tests() -> None:
    _logged_values.clear()


def resolve_agent_ssh_bind_address_detail() -> tuple[str, str]:
    """``(ip, source)`` where source is ``env`` / ``default`` / ``invalid``.

    Unset or blank → the loopback default, silently. An IP literal (v4 or v6,
    bracket-free — docker-py's `HostIp` form) → itself. Anything else — a
    hostname, an interface name, a CIDR, `ip:port`, `[::1]` — → the loopback
    default plus ONE ERROR naming the key and the value, never `0.0.0.0`: an
    invalid value must fail towards the closed posture, not the open one.
    """
    raw = os.getenv(AGENT_SSH_BIND_ADDRESS_ENV)
    value = (raw or "").strip()
    if not value:
        return DEFAULT_AGENT_SSH_BIND_ADDRESS, "default"
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        if value not in _logged_values:
            _logged_values.add(value)
            logger.error(
                "%s=%r is not an IP address; agent SSH ports are published on %s "
                "instead. Use an IP literal (127.0.0.1, a VPN interface IP, or "
                "0.0.0.0 for every interface) — hostnames and interface names are "
                "not accepted because Docker binds to an address, not a name.",
                AGENT_SSH_BIND_ADDRESS_ENV, value, DEFAULT_AGENT_SSH_BIND_ADDRESS,
            )
        return DEFAULT_AGENT_SSH_BIND_ADDRESS, "invalid"
    text = str(ip)
    if text not in _ALL_INTERFACES and not ip.is_loopback and text not in _logged_values:
        # E5: a specific interface IP must be assigned on the host at every
        # container start. After a reboot an `unless-stopped` agent restarts
        # before tailscaled / the VPN is up, Docker fails the bind with "cannot
        # assign requested address", and the agent restart-loops until the
        # interface appears. Say so once; the runbook names the alternative.
        _logged_values.add(text)
        logger.warning(
            "%s=%s binds agent SSH to a specific host interface. Every agent "
            "start requires that address to be assigned on the host — after a "
            "reboot, agents restart-loop until the interface is up. For a VPN "
            "host, 0.0.0.0 plus scripts/deploy/docker-firewall.sh is the "
            "robust posture.",
            AGENT_SSH_BIND_ADDRESS_ENV, text,
        )
    return text, "env"


def resolve_agent_ssh_bind_address() -> str:
    """The host IP agent SSH ports are published on — see the `_detail` form."""
    return resolve_agent_ssh_bind_address_detail()[0]


def agent_ssh_port_binding(port) -> dict[str, tuple[str, int]]:
    """The ``ports=`` argument for an agent container create.

    ``{'22/tcp': (host_ip, port)}`` — docker-py's tuple form, which writes
    ``HostConfig.PortBindings['22/tcp'][0].HostIp``. A bare int here is the bug
    this module exists to close.
    """
    return {SSH_CONTAINER_PORT: (resolve_agent_ssh_bind_address(), int(port))}


def is_all_interfaces(ip: Optional[str]) -> bool:
    return ip in _ALL_INTERFACES


def is_loopback(ip: Optional[str]) -> bool:
    if not ip:
        return False
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def published_ssh_host_ip(container_attrs) -> Optional[str]:
    """What Docker actually publishes the SSH port on, for an EXISTING container.

    Reads ``HostConfig.PortBindings['22/tcp'][0].HostIp``: ``""`` is Docker's
    spelling of every interface and is returned as ``"0.0.0.0"``; a specific
    address is returned as written. ``None`` when the container has no such
    mapping at all (not an agent create, or a stripped test double) — never
    guessed, because the caller falls back to today's host detection on None
    and would otherwise claim a binding that does not exist.
    """
    try:
        bindings = ((container_attrs or {}).get("HostConfig") or {}).get("PortBindings") or {}
        entries = bindings.get(SSH_CONTAINER_PORT) or []
        if not entries:
            return None
        host_ip = (entries[0] or {}).get("HostIp")
    except (AttributeError, TypeError, IndexError):
        return None
    if host_ip is None:
        return None
    return host_ip or "0.0.0.0"


@dataclass
class SshConnectionDecision:
    """What the grant response advertises, and why."""

    host: str
    port: int
    bind_address: Optional[str]
    warnings: list[str] = field(default_factory=list)

    @property
    def command(self) -> str:
        return f"ssh -p {self.port} developer@{self.host}"


def decide_ssh_connection(container_attrs, port, detected_host: str, host_source: str) -> SshConnectionDecision:
    """The decision table (requirements SSH-BIND-002).

    | published HostIp (Docker)   | host advertised   | warning                                   |
    |-----------------------------|-------------------|-------------------------------------------|
    | every interface             | `detected_host`   | if the configured bind differs: recreate  |
    | loopback                    | `127.0.0.1`       | connect from the host; `-J` if jumpable;  |
    |                             |                   | how to bind a VPN interface instead       |
    | a specific IP               | that IP           | if `SSH_HOST` is set and disagrees        |
    | none (no `22/tcp` mapping)  | `detected_host`   | none                                      |

    `detected_host` / `host_source` come from `ssh_service.get_ssh_host_detail`
    and are passed in so this module stays a leaf.
    """
    port = int(port)
    published = published_ssh_host_ip(container_attrs)
    warnings: list[str] = []

    if published is None:
        return SshConnectionDecision(detected_host, port, None, warnings)

    if is_all_interfaces(published):
        configured = resolve_agent_ssh_bind_address()
        if not is_all_interfaces(configured):
            warnings.append(
                "This agent still publishes SSH on every host interface: it was "
                f"created before {AGENT_SSH_BIND_ADDRESS_ENV}={configured} applied. "
                "Recreate the agent to apply the configured binding (a plain restart "
                "keeps the old one)."
            )
        return SshConnectionDecision(detected_host, port, published, warnings)

    if is_loopback(published):
        jump = ""
        if host_source in _JUMPABLE_HOST_SOURCES and detected_host and detected_host != published:
            jump = (
                f", or jump through it: ssh -J <your-user>@{detected_host} "
                f"-p {port} developer@{published}"
            )
        warnings.append(
            f"Agent SSH is published on the Trinity host's loopback interface only "
            f"({AGENT_SSH_BIND_ADDRESS_ENV}={published}). Connect from the host itself"
            f"{jump}. For direct access over a VPN interface set "
            f"{AGENT_SSH_BIND_ADDRESS_ENV} to that interface's IP (and SSH_HOST to "
            "match) and recreate the agent."
        )
        return SshConnectionDecision(published, port, published, warnings)

    # A specific interface: the only host that can work is the one the port is
    # bound to. An explicit SSH_HOST that says otherwise is worth naming; an
    # auto-detected host that differs is just detection, not a disagreement.
    if host_source == "ssh_host" and detected_host and detected_host != published:
        warnings.append(
            f"SSH_HOST={detected_host} disagrees with the interface this agent "
            f"publishes SSH on ({published}); the command uses {published}."
        )
    return SshConnectionDecision(published, port, published, warnings)


class AgentSshBindAddressError(RuntimeError):
    """Docker could not bind the agent's SSH port to the configured host IP."""


_UNASSIGNABLE_TEXT = "cannot assign requested address"


def describe_ssh_bind_failure(exc: BaseException) -> Optional[str]:
    """A message naming the knob when `exc` is Docker's EADDRNOTAVAIL
    (`cannot assign requested address`) — the configured interface is not
    assigned on the host right now (VPN down, re-keyed address). Otherwise
    ``None``, so callers re-raise the original.

    Duck-typed cause/context walk with a cycle guard, mirroring crud.py's
    `_is_port_bind_conflict` (never `isinstance` against the docker module —
    it may be a test double). Disjoint from that classifier's texts
    (`port is already allocated` / `address already in use`), so a bind
    conflict is never misread as an unassignable address.
    """
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        try:
            text = str(exc).lower()
        except Exception:
            text = ""
        if _UNASSIGNABLE_TEXT in text:
            configured = resolve_agent_ssh_bind_address()
            return (
                f"Docker could not bind the agent's SSH port to "
                f"{AGENT_SSH_BIND_ADDRESS_ENV}={configured}: that address is not "
                "assigned on the Trinity host right now (a VPN interface that is "
                "down, or an address that changed). Bring the interface up, or set "
                f"{AGENT_SSH_BIND_ADDRESS_ENV} to 127.0.0.1 or 0.0.0.0 and restart "
                "the backend, then retry."
            )
        exc = exc.__cause__ or exc.__context__
    return None


def raise_for_ssh_bind_failure(exc: BaseException) -> None:
    """Raise `AgentSshBindAddressError` (chained) when `exc` is an unassignable
    bind address; return None otherwise so the caller's bare `raise` keeps the
    original exception and its classifiers intact."""
    message = describe_ssh_bind_failure(exc)
    if message:
        raise AgentSshBindAddressError(message) from exc
    return None
