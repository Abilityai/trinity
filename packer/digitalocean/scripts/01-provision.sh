#!/bin/bash
# Build-time provisioner for the Trinity DigitalOcean 1-Click snapshot (#2281).
# Everything here is baked into the image and shared by every droplet created
# from it — so nothing droplet-specific and nothing secret may be produced here.
#
# The machine setup itself (Docker, pinned Caddy, ufw, the DOCKER-USER firewall
# unit) is NOT written here any more: it is `start.sh --provision --machine-only`
# (#2380), the same code a doc-driven install runs. This script is now only what
# is genuinely specific to baking a snapshot — pre-pulling the images so first
# boot fetches nothing, and installing the staged MOTD / cloud-init files.
set -euo pipefail

# Packer's shell provisioner runs a non-login, non-interactive SSH shell whose
# PATH does not reliably carry the sbin directories, and the provisioning step
# below needs them (ufw, iptables). Ubuntu 24.04 merged /sbin into /usr/sbin, so
# both names are listed for older bases.
export PATH="/usr/local/sbin:/usr/sbin:/sbin:${PATH}"

: "${TRINITY_IMAGE_TAG:?TRINITY_IMAGE_TAG must be passed by the Packer build}"

echo "=== Trinity 1-Click build: baking ${TRINITY_IMAGE_TAG} ==="

# --- Checkout ----------------------------------------------------------------
# Cloned FIRST, because the provisioning logic now lives in the checkout.
# Pinned to the same tag as the images: a snapshot whose checkout and images
# disagree is the one combination `start.sh --hosted` cannot detect — compose
# from one release, containers from another.
#
# `debian-goodies` is not used by Trinity; DigitalOcean's own img_check.sh
# expects `checkrestart` to be available.
apt-get update -q
apt-get install -y -q ca-certificates curl git debian-goodies
git clone --depth 1 --branch "${TRINITY_IMAGE_TAG}" \
  https://github.com/abilityai/trinity.git /opt/trinity
cd /opt/trinity

# --- Machine provisioning (shared with the docs install path) ----------------
# Docker, Caddy 2.11.x with the IP-certificate floor asserted, ufw, and
# trinity-docker-firewall.service. --machine-only stops before anything
# droplet-specific: no IP, no Caddyfile, no certificate, no .env, no Trinity.
# First boot runs the matching --site-only half.
./scripts/deploy/start.sh --provision --cloud digitalocean --machine-only

# --- Pull the images so first boot pulls nothing -----------------------------
# This is the entire point of the snapshot. Tags must match
# docker-compose.hosted.yml's ghcr.io/abilityai/trinity-* references.
for img in backend frontend scheduler mcp-server; do
  docker pull "ghcr.io/abilityai/trinity-${img}:${TRINITY_IMAGE_TAG}"
done

# The agent base image is NOT a compose service and never will be: the backend
# creates agent containers from the literal local tag `trinity-agent-base:latest`
# (hardcoded in agent_service/lifecycle.py, SEC-172-allowlisted), and compose
# cannot retag. `start.sh --hosted` does this pull+retag itself at run time; we
# do it at build time so first boot has nothing to fetch. Both must agree.
docker pull "ghcr.io/abilityai/trinity-agent-base:${TRINITY_IMAGE_TAG}"
docker tag "ghcr.io/abilityai/trinity-agent-base:${TRINITY_IMAGE_TAG}" trinity-agent-base:latest

# Third-party images docker-compose.hosted.yml pulls that are not ours.
docker pull redis:7-alpine
docker pull timberio/vector:0.43.1-alpine
docker pull alpine:3.20

# The OTel collector is NOT profile-gated in docker-compose.hosted.yml — unlike
# cloudflared, which is correctly skippable under `profiles: [tunnel]` — so
# `start.sh --hosted` starts it on every droplet. Omitting it here (#2281 review
# I2) left first boot pulling a few hundred megabytes, against the one property
# this snapshot exists to have. Read from the checkout rather than hardcoded, so
# a collector bump in compose cannot leave this line pinning a stale digest.
OTEL_IMAGE="$(grep -oE 'otel/opentelemetry-collector-contrib:[0-9][^"'"'"'[:space:]]*' \
  /opt/trinity/docker-compose.hosted.yml | head -1)"
if [ -z "$OTEL_IMAGE" ]; then
    echo "FATAL: could not resolve the OTel collector image from docker-compose.hosted.yml." >&2
    exit 1
fi
docker pull "$OTEL_IMAGE"

# Record what was baked, for the MOTD and for support.
mkdir -p /etc/trinity
echo "${TRINITY_IMAGE_TAG}" > /etc/trinity/baked-image-tag

# --- Place the per-instance and MOTD files -----------------------------------
# Only files that have no home in the checkout: the cloud-init hook, the login
# banner, and the first-boot script the hook calls. The firewall script and its
# systemd unit are NOT staged any more — the script ships in the checkout and
# the unit is written by --provision, pointing at it.
install -D -m 0755 /tmp/trinity-files/opt/trinity-firstboot/firstboot.sh \
  /opt/trinity-firstboot/firstboot.sh
install -D -m 0755 /tmp/trinity-files/var/lib/cloud/scripts/per-instance/001-trinity \
  /var/lib/cloud/scripts/per-instance/001-trinity
install -D -m 0755 /tmp/trinity-files/etc/update-motd.d/99-trinity \
  /etc/update-motd.d/99-trinity
rm -rf /tmp/trinity-files

# Ubuntu's stock MOTD is noisy and pushes ours off the first screen; the
# credential line is the one thing a 1-Click user must not miss.
chmod -x /etc/update-motd.d/10-help-text /etc/update-motd.d/50-motd-news 2>/dev/null || true

echo "=== build provisioning complete ==="
