#!/bin/bash
# Close the Docker/ufw gap. Run at first boot AND at every boot thereafter, via
# trinity-docker-firewall.service.
#
# docker-compose.hosted.yml publishes 8000 (backend), 8080 (MCP), 8686 (Vector),
# the OTel collector's four ports, and the frontend's own FRONTEND_PORT on
# 0.0.0.0. Docker's iptables rules are consulted BEFORE ufw's chain, so those
# ports are reachable from the internet on a droplet whose ufw says otherwise —
# `ufw deny 8000` is silently inert.
#
# 8081 is the entry that matters most and was missing (#2281 review C1): it is
# the SPA itself, moved off :80 so Caddy can own 80/443. Compose publishes it
# with the short syntax `"${FRONTEND_PORT:-80}:8080"`, which binds 0.0.0.0 — so
# without it the login page answers plain HTTP on http://<ip>:8081, past both the
# TLS termination and the http->https redirect this whole image is built around.
#
# One variable feeds both the probe and the insert: two hand-maintained lists
# that disagree means the -C probe never matches its own rule and every re-run
# stacks another. tests/unit/test_2281_firstboot_port_exposure.py keeps this
# list from drifting from what compose actually publishes.
#
# DOCKER-USER is the one chain Docker leaves for the operator and evaluates
# first, so the drop belongs here. Everything a user needs is served by Caddy on
# 80/443; the container ports stay reachable from the host (Caddy proxies
# 127.0.0.1:8081) and from other containers, which is what the platform uses.
set -euo pipefail

# Packer/systemd both run this without a login shell; iptables lives in /usr/sbin.
export PATH="/usr/local/sbin:/usr/sbin:/sbin:${PATH}"

DROP_PORTS=8000,8080,8081,8686,4317,4318,8889,13133

# Docker creates DOCKER-USER when it starts. The unit is ordered After=docker.service,
# but ordering is not readiness, so wait briefly rather than racing it.
for _ in $(seq 1 30); do
    iptables -n -L DOCKER-USER >/dev/null 2>&1 && break
    sleep 1
done

if ! iptables -C DOCKER-USER -i eth0 -p tcp -m multiport \
       --dports "$DROP_PORTS" -j DROP 2>/dev/null; then
    iptables -I DOCKER-USER -i eth0 -p tcp -m multiport \
      --dports "$DROP_PORTS" -j DROP
    echo "DOCKER-USER: dropped external access to ${DROP_PORTS}"
else
    echo "DOCKER-USER: rule already present for ${DROP_PORTS}"
fi
