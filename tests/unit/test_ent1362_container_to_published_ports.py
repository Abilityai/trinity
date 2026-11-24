"""A container does not reach the stack's published ports through the host, and
the collector's read side is not on the agent network (trinity-enterprise#1362).

Two properties, both about what an agent container can open by itself.

1. ``TRINITY-FW`` drops a container's connection to a published port when
   Docker DNATs it, which is what happens whenever the port belongs to a
   container on a different bridge. The rule sits ahead of the bridge RETURNs,
   or it is never evaluated. The chain is also rebuilt on every run in one
   ``iptables-restore`` transaction: the old "only when the final DROP is
   missing" probe was true on every existing install, so a new rule never
   reached one, and a line-by-line rebuild that died half-way left the chain
   flushed behind a live jump.

2. The collector's Prometheus exporter listens on an alias that exists on the
   platform network only. The collector has to sit on the agent network too
   (agents push to it), and the exported series are fleet-wide usage data, so
   a wildcard listener put the read side on the wrong network.

These read files; the behaviour itself was exercised against a real Docker
daemon with ufw (see the PR), which a unit run cannot do.
"""
import re
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_FIREWALL = _REPO / "scripts" / "deploy" / "docker-firewall.sh"
_COLLECTOR = _REPO / "config" / "otel-collector.yaml"
_COSTS = _REPO / "src" / "backend" / "services" / "ops_costs_service.py"
COMPOSE_FILES = ["docker-compose.yml", "docker-compose.prod.yml", "docker-compose.hosted.yml"]
ALIAS = "trinity-otel-metrics"


def _code(path: Path) -> str:
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def _rules() -> list[str]:
    return [
        line.strip() for line in _FIREWALL.read_text(encoding="utf-8").splitlines()
        if re.match(r"^\s*-A TRINITY-FW\b", line)
    ]


def _compose(name: str) -> dict:
    return yaml.safe_load((_REPO / name).read_text(encoding="utf-8"))


# --- 1. the firewall -------------------------------------------------------

def test_a_container_reaching_a_published_port_through_the_host_is_dropped():
    rules = _rules()
    dnat = [i for i, r in enumerate(rules) if "--ctstate DNAT" in r and r.endswith("-j DROP")]
    returns = [i for i, r in enumerate(rules) if r.endswith("-j RETURN") and ("-i docker0" in r or "-i br+" in r)]
    assert returns, "the bridge RETURNs are gone; the ordering claim below is meaningless"
    assert dnat, "no DNAT drop: a container reaches any published port via the host"
    assert max(dnat) < min(returns), (
        "the DNAT drop must precede the bridge RETURNs, or container traffic "
        "returns to Docker's chains before the rule is evaluated"
    )


@pytest.mark.parametrize("iface", ["docker0", "br+"])
def test_the_drop_covers_every_container_interface_the_chain_returns(iface):
    """One rule per interface the chain lets through. A bridge that is returned
    but not covered here is the hole again, from that bridge."""
    assert f"-A TRINITY-FW -i {iface} -m conntrack --ctstate DNAT -j DROP" in _rules()


def test_the_drop_names_no_port_and_no_subnet():
    """Same reasoning as the off-box rule (#2380): a list has to be maintained,
    and the compose subnets are a second copy of a number."""
    for rule in (r for r in _rules() if "--ctstate DNAT" in r):
        assert "--dport" not in rule and "multiport" not in rule
        assert not re.search(r"\d+\.\d+\.\d+\.\d+", rule), rule


def test_established_replies_still_return_first():
    """`--ctstate DNAT` is true of every packet of a DNAT'd connection,
    including the replies to an inbound one Caddy or the operator opened. Only
    the conntrack RETURN ahead of it keeps the drop to NEW connections."""
    rules = _rules()
    assert "RELATED,ESTABLISHED" in rules[0] and rules[0].endswith("-j RETURN")


def test_the_chain_is_rebuilt_in_one_transaction():
    body = _code(_FIREWALL)
    assert re.search(r"iptables-restore --noflush <<'RULES'", body), (
        "TRINITY-FW must be rebuilt by iptables-restore --noflush: atomic, and "
        "--noflush leaves Docker's and ufw's chains alone"
    )
    block = body.split("<<'RULES'", 1)[1].split("\nRULES", 1)[0]
    lines = [l.strip() for l in block.splitlines() if l.strip()]
    assert lines[0] == "*filter" and lines[-1] == "COMMIT"
    assert ":TRINITY-FW - [0:0]" in lines, "the chain line is what creates or empties it inside the transaction"
    assert lines[-2] == "-A TRINITY-FW -j DROP", "the catch-all DROP must be the last rule committed"
    # Nothing but the chain declaration and its own rules: a second chain in
    # the block would be flushed along with it.
    assert all(l.startswith("-A TRINITY-FW ") for l in lines[2:-1])


def test_no_step_can_leave_the_chain_flushed_behind_a_live_jump():
    """A separate flush or append is a window: under `set -e` a failure between
    it and the final DROP leaves every published port open to the internet."""
    body = _code(_FIREWALL)
    assert not re.search(r"\biptables\s+-F\b", body)
    assert not re.search(r"\biptables\s+-A\s+TRINITY-FW\b", body)


def test_the_rebuild_is_not_gated_on_the_old_chain_looking_complete():
    """`iptables -C TRINITY-FW -j DROP` is true on every existing install, so
    gating on it meant a rule added to the script never reached one."""
    assert not re.search(r"iptables\s+-C\s+TRINITY-FW\b", _code(_FIREWALL))


def test_the_jump_is_still_inserted_after_the_rules_exist():
    body = _code(_FIREWALL)
    assert body.index("iptables-restore --noflush") < body.index("iptables -I DOCKER-USER 1 -j TRINITY-FW")


# --- 2. the collector's read side -----------------------------------------

def test_the_exporter_listens_on_the_platform_alias_only():
    config = yaml.safe_load(_COLLECTOR.read_text(encoding="utf-8"))
    assert config["exporters"]["prometheus"]["endpoint"] == f"{ALIAS}:8889"


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_the_alias_exists_on_the_platform_network_and_nowhere_else(name):
    services = _compose(name)["services"]
    networks = services["otel-collector"]["networks"]
    assert isinstance(networks, dict), "aliases need the mapping form"
    assert ALIAS in (networks["trinity-platform"] or {}).get("aliases", [])
    for net, spec in networks.items():
        if net != "trinity-platform":
            assert ALIAS not in ((spec or {}).get("aliases") or []), (
                f"{name}: {ALIAS} on {net} puts the exporter back where agents are"
            )
    # No other service may answer to the name either.
    for svc, spec in services.items():
        if svc == "otel-collector":
            continue
        nets = spec.get("networks") or {}
        if isinstance(nets, dict):
            for net_spec in nets.values():
                assert ALIAS not in ((net_spec or {}).get("aliases") or []), svc
        assert spec.get("container_name") != ALIAS and svc != ALIAS


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_agents_can_still_push(name):
    """The receivers stay on every interface and the collector stays on the
    agent network: only the read side moved."""
    assert "trinity-agent" in _compose(name)["services"]["otel-collector"]["networks"]
    receivers = yaml.safe_load(_COLLECTOR.read_text(encoding="utf-8"))["receivers"]["otlp"]["protocols"]
    assert receivers["grpc"]["endpoint"] == "0.0.0.0:4317"
    assert receivers["http"]["endpoint"] == "0.0.0.0:4318"


def test_the_backend_reads_the_alias_not_the_container_name():
    """The backend is on both networks, so the container name resolves to both
    of the collector's addresses and half the reads would be refused."""
    source = _COSTS.read_text(encoding="utf-8")
    default = re.search(r'os\.getenv\("OTEL_PROMETHEUS_ENDPOINT",\s*"([^"]+)"\)', source).group(1)
    assert default == f"http://{ALIAS}:8889/metrics"
