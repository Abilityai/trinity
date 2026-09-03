#!/bin/bash
# Build-time provisioner for the Trinity DigitalOcean 1-Click snapshot (#2281).
# Everything here is baked into the image and shared by every droplet created
# from it — so nothing droplet-specific and nothing secret may be produced here.
set -euo pipefail

: "${TRINITY_IMAGE_TAG:?TRINITY_IMAGE_TAG must be passed by the Packer build}"

echo "=== Trinity 1-Click build: baking ${TRINITY_IMAGE_TAG} ==="

# --- Base packages -----------------------------------------------------------
apt-get update -q
apt-get install -y -q ca-certificates curl gnupg git jq ufw debian-goodies

# --- Docker (official repo, not the distro package) --------------------------
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -q
apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable docker

# --- iptables-persistent -----------------------------------------------------
# Installed HERE rather than at first boot (#2281 review I1). First boot did it
# as `>/dev/null 2>&1 || true` at the busiest apt moment a droplet ever has, so
# a transient mirror failure left the DOCKER-USER DROP rules unsaved and gone at
# the next reboot — silently. Baked once at build time it is deterministic for
# every droplet from this snapshot, and first boot only has to run the save.
#
# The debconf answers must be preseeded: the package otherwise prompts to save
# the CURRENT ruleset, and a prompt in a non-interactive Packer run hangs until
# the provisioner times out. `false` on both — first boot saves the ruleset that
# actually matters, after Docker and the DROP rules exist.
echo iptables-persistent iptables-persistent/autosave_v4 boolean false | debconf-set-selections
echo iptables-persistent iptables-persistent/autosave_v6 boolean false | debconf-set-selections
apt-get install -y -q iptables-persistent

# --- Caddy (reverse proxy + automatic certificates) --------------------------
# PINNED, and the floor is load-bearing rather than hygiene. The whole
# no-domain HTTPS story rests on Caddy issuing a Let's Encrypt certificate for
# a bare IP, and Caddy could not do that until recently:
# caddyserver/caddy#7399 — v2.10.0 fails outright with
# "subject '<ip>' cannot have public IP certificate", the IPv4 fix landed via
# mholt/acmez#47 (2025-12-17), and IPv6 was not resolved until 2026-04-25.
# v2.11.3 (2026-05-12) is the first stable release carrying both.
#
# An unpinned `apt-get install caddy` happened to work only because the repo's
# current stable is new enough. That is a silent dependency on a moving
# upstream for a property this image is built around, and the symptom of
# getting it wrong appears on a customer's droplet as a browser warning.
CADDY_VERSION="2.11.4"
CADDY_MIN_VERSION="2.11.3"
curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
  > /etc/apt/sources.list.d/caddy-stable.list
apt-get update -q
apt-get install -y -q "caddy=${CADDY_VERSION}"

# Assert the floor rather than trusting the pin. The pin can be edited, the
# repo can drop a version, and a `caddy upgrade` on a running droplet can move
# it — this is the check that survives all three. Deliberately NOT `apt-mark
# hold`: forward versions carry the fix, and holding would block security
# updates for the life of the droplet.
_caddy_installed="$(caddy version | head -1 | sed 's/^v//' | cut -d' ' -f1)"
if [ "$(printf '%s\n%s\n' "$CADDY_MIN_VERSION" "$_caddy_installed" \
        | sort -V | head -1)" != "$CADDY_MIN_VERSION" ]; then
    echo "FATAL: caddy ${_caddy_installed} is below ${CADDY_MIN_VERSION}, which" >&2
    echo "       cannot issue Let's Encrypt IP certificates (caddyserver/caddy#7399)." >&2
    exit 1
fi
echo "Caddy ${_caddy_installed} (floor ${CADDY_MIN_VERSION}) — IP certificates supported."
# Not enabled here: first boot writes the Caddyfile (it needs the droplet's own
# IP) and starts it. A Caddy enabled with the packaged default would race first
# boot for :80 and take a certificate for the wrong name.
systemctl disable caddy

# --- Trinity checkout --------------------------------------------------------
# Pinned to the same tag as the images. A snapshot whose checkout and images
# disagree is the one combination `start.sh --hosted` cannot detect: compose
# would come from one release and the containers from another.
git clone --depth 1 --branch "${TRINITY_IMAGE_TAG}" \
  https://github.com/abilityai/trinity.git /opt/trinity

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

# --- Firewall ----------------------------------------------------------------
# ufw governs host ports. It does NOT govern Docker-published ports — Docker
# inserts its own iptables rules ahead of ufw's chain, so `ufw deny 8000` on a
# droplet publishing 8000:8000 is silently inert. That gap is closed by the
# DOCKER-USER rules the first-boot script installs; ufw here covers ssh and the
# Caddy listeners only.
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# --- Place the per-instance and MOTD files -----------------------------------
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
