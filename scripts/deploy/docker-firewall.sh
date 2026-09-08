#!/bin/bash
# Close the Docker/ufw gap. Installed and started by `start.sh --provision`, and
# re-run on every boot by trinity-docker-firewall.service.
#
# Docker publishes container ports on 0.0.0.0 and inserts its own iptables rules
# AHEAD of ufw's chain, so `ufw deny 8000` on a machine publishing 8000:8000 is
# silently inert. DOCKER-USER is the one chain Docker leaves for the operator and
# evaluates first, so the block belongs here.
#
# This used to carry a hand-typed port list (`DROP_PORTS=8000,8080,8081,...`)
# kept in step with docker-compose.hosted.yml by a unit test. That is this repo's
# most-shipped bug shape (#1039, #1056, #1707, #1871) and it had already shipped
# here: 8081 was missing (#2281 review C1), so the login page answered plain HTTP
# past the certificate and past the http->https redirect. A list that has to be
# maintained cannot be the mechanism.
#
# So the rule is INVERTED: nothing Docker publishes is reachable from off-box,
# whatever the port, and a port added to compose tomorrow is covered the day it
# lands. Everything a user needs is served by Caddy on 80/443 — HOST ports, which
# never traverse this chain (Caddy proxies to 127.0.0.1:8081, a local DNAT that
# goes through OUTPUT, not FORWARD).
#
# Order inside the chain is load-bearing:
#   1. RELATED,ESTABLISHED — replies to connections a CONTAINER opened. Without
#      it the agents lose outbound internet completely: their traffic leaves via
#      the public interface and the answers arrive back on it, inbound, into the
#      container. This rule is the difference between "closed" and "broken".
#   2. link-local (169.254.0.0/16, RFC 3927) is dropped OUTBOUND from containers,
#      and it has to sit ahead of the RETURNs below or container traffic leaves
#      before ever reaching it. That range is where every cloud publishes its
#      instance metadata service, and on this platform the droplet's user-data is
#      served from it verbatim, for the life of the machine — which on a
#      script-installed instance means the Trinity admin password and the Claude
#      subscription token. Nothing Trinity runs has any business reading it, and
#      an agent is precisely the untrusted-code case. Written as the RFC range
#      rather than the well-known .169.254 address: the property being blocked is
#      "link-local, host-adjacent, not routable", not one magic host.
#   3. -i docker0 / -i br+ — container->internet and container->container.
#      Naming what is INSIDE, rather than which interface is outside, is why this
#      also covers a cloud provider's PRIVATE interface (DigitalOcean's eth1 /
#      VPC) for free. The old rule named eth0 and left the VPC side open.
#   4. everything else entering a container: DROP.
#
# IPv4 only, deliberately: Docker only maintains a DOCKER-USER chain in ip6tables
# when the daemon has IPv6 enabled, which no Trinity compose file does.
set -euo pipefail

# systemd runs this without a login shell; iptables lives in /usr/sbin.
export PATH="/usr/local/sbin:/usr/sbin:/sbin:${PATH}"

# Docker creates DOCKER-USER when it starts. The unit is ordered
# After=docker.service, but ordering is not readiness, so wait rather than race.
for _ in $(seq 1 30); do
    iptables -n -L DOCKER-USER >/dev/null 2>&1 && break
    sleep 1
done

# Own chain, so the rules are idempotent without -C probing every line and
# cannot stack duplicates. The chain is (re)populated ONLY when it is new or
# incomplete — a steady-state boot finds the final DROP already in place and
# touches nothing, so there is no window in which the chain is live but empty.
if iptables -N TRINITY-FW 2>/dev/null || ! iptables -C TRINITY-FW -j DROP 2>/dev/null; then
    iptables -F TRINITY-FW
    iptables -A TRINITY-FW -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN
    iptables -A TRINITY-FW -d 169.254.0.0/16 -j DROP
    iptables -A TRINITY-FW -i docker0 -j RETURN
    iptables -A TRINITY-FW -i br+ -j RETURN
    iptables -A TRINITY-FW -j DROP
    echo "TRINITY-FW: rules (re)built."
fi

# Jump inserted only after the chain is populated, so a first run that dies
# half-way leaves traffic hitting Docker's own rules rather than an empty
# allow-everything chain.
if ! iptables -C DOCKER-USER -j TRINITY-FW 2>/dev/null; then
    iptables -I DOCKER-USER 1 -j TRINITY-FW
    echo "DOCKER-USER: published container ports are now unreachable from off-box."
else
    echo "DOCKER-USER: TRINITY-FW jump already present."
fi
