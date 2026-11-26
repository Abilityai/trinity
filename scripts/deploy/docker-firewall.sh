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
#   3. a container reaching a PUBLISHED port through the host is dropped. Seen
#      from inside, every published port is also reachable at the host's
#      addresses (each bridge gateway, the public IP). When the port belongs to
#      a container on a DIFFERENT bridge, Docker DNATs the connection and it is
#      forwarded here — where rule 4 used to return it to Docker's own chains,
#      which accept published ports from anywhere. So a service that sits only
#      on the platform network and publishes a port was one hop from every
#      agent, the network split notwithstanding. Containers reach each other by
#      Docker DNS on a network they share, which is never DNAT'd, so the rule
#      costs nothing legitimate. It has to sit ahead of the bridge RETURNs for
#      the same reason rule 2 does. (A port owned by a container on the SAME
#      bridge is not DNAT'd at all: it arrives on the host's INPUT chain for
#      docker-proxy, which is ufw's ground, and `default deny incoming` drops
#      it there.)
#   4. -i docker0 / -i br+ — container->internet and container->container.
#      Naming what is INSIDE, rather than which interface is outside, is why this
#      also covers a cloud provider's PRIVATE interface (DigitalOcean's eth1 /
#      VPC) for free. The old rule named eth0 and left the VPC side open.
#   5. everything else entering a container: DROP.
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

# Own chain, rebuilt on EVERY run, in one transaction. iptables-restore parses
# the whole block and commits it atomically: a chain line under --noflush
# creates TRINITY-FW or empties it, and the kernel swaps old rules for new in a
# single step, so there is never a moment the chain is live but short of its
# final DROP. A rule the kernel refuses fails the whole commit and leaves the
# previous chain exactly as it was.
#
# This used to rebuild line by line, and only when the final DROP was missing.
# Both halves were wrong for an upgrade: the probe is true on every existing
# install, so a rule added to this file never reached one, and a rebuild that
# died half-way under `set -e` left the chain flushed with the jump still
# installed — every published port open to the internet.
if ! iptables-restore --noflush <<'RULES'
*filter
:TRINITY-FW - [0:0]
-A TRINITY-FW -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN
-A TRINITY-FW -d 169.254.0.0/16 -j DROP
-A TRINITY-FW -i docker0 -m conntrack --ctstate DNAT -j DROP
-A TRINITY-FW -i br+ -m conntrack --ctstate DNAT -j DROP
-A TRINITY-FW -i docker0 -j RETURN
-A TRINITY-FW -i br+ -j RETURN
-A TRINITY-FW -j DROP
COMMIT
RULES
then
    echo "TRINITY-FW: rebuild refused; the previous rules are untouched." >&2
    exit 1
fi
echo "TRINITY-FW: rules applied."

# Jump inserted only after the chain is populated, so a first run whose
# commit is refused leaves traffic hitting Docker's own rules rather than an
# empty allow-everything chain.
if ! iptables -C DOCKER-USER -j TRINITY-FW 2>/dev/null; then
    iptables -I DOCKER-USER 1 -j TRINITY-FW
    echo "DOCKER-USER: published container ports are now unreachable from off-box."
else
    echo "DOCKER-USER: TRINITY-FW jump already present."
fi
