#!/bin/bash
# First-boot configuration for a Trinity DigitalOcean 1-Click droplet (#2281).
# Runs ONCE per droplet, from /var/lib/cloud/scripts/per-instance/001-trinity.
#
# Everything droplet-specific happens here and nothing here is baked into the
# snapshot. Almost all of it is now `start.sh --provision --site-only`, which is
# the SAME code a doc-driven install runs (#2380) — the Caddyfile, the
# certificate poll and the .env keys used to be a second copy that had already
# drifted from the first. What is left is the one thing genuinely specific to a
# Marketplace droplet: whether an admin account exists before anyone opens it.
set -euo pipefail

# start.sh --provision needs iptables and systemctl out of /usr/sbin; cloud-init
# normally supplies a root PATH that covers them, but the cost of not depending
# on that is one line.
export PATH="/usr/local/sbin:/usr/sbin:/sbin:${PATH}"

STATE_DIR=/etc/trinity
CRED_FILE="${STATE_DIR}/admin-credentials"
USER_SUPPLIED_PW="${STATE_DIR}/admin-password"
TRINITY_DIR=/opt/trinity
LOG=/var/log/trinity-firstboot.log

exec > >(tee -a "$LOG") 2>&1
echo "=== Trinity first boot: $(date -u +%FT%TZ) ==="

mkdir -p "$STATE_DIR"
chmod 0700 "$STATE_DIR"

# --- 1. Admin account (ent#580) ----------------------------------------------
# A 1-Click has no vendor-defined input form at deploy time (verified against
# digitalocean/marketplace-partners: the only prompt is the optional Managed
# Database checkbox), so nothing about the admin can be collected at create time.
#
# Two paths:
#   (a) user-data, if the operator supplied a password. It must arrive as
#       #cloud-config `write_files` and NOT as a shell script: 1-Click
#       per-instance code runs from cloud-init's `scripts-per-instance` module,
#       which runs BEFORE `scripts-user`, so a user-data shell script would
#       execute after this script had already started Trinity. The admin is
#       provisioned at boot and the /setup wizard never opens.
#   (b) otherwise NO admin is provisioned. ADMIN_PASSWORD stays blank and
#       ADMIN_PASSWORD_SOURCE=browser tells start.sh that is deliberate, so the
#       first person to open the instance creates the admin at /setup — email,
#       password, product-updates consent — without ever opening a terminal.
#       Nothing is generated, so there is nothing for the MOTD to print.
#
# Accepted risk (2026-09-10): until that first visit, anyone who finds the IP
# can claim the instance. It is empty at that moment and can be destroyed; see
# docs/DEPLOYMENT.md -> Security Recommendations.
# --- admin-source (behaviour-tested; see test_2281_firstboot_password) ---
ADMIN_PASSWORD=""
if [ -s "$USER_SUPPLIED_PW" ]; then
    ADMIN_PASSWORD="$(head -c 512 "$USER_SUPPLIED_PW" | tr -d '\r\n')"
    shred -u "$USER_SUPPLIED_PW" 2>/dev/null || rm -f "$USER_SUPPLIED_PW"
fi
if [ -n "$ADMIN_PASSWORD" ]; then
    PW_SOURCE="user-data"
    export ADMIN_PASSWORD
else
    PW_SOURCE="browser"
    unset ADMIN_PASSWORD
    export ADMIN_PASSWORD_SOURCE=browser
fi

# Only the SOURCE is recorded, for the MOTD. It never holds a password: the
# operator's own is theirs already, and the browser path has none to hold.
umask 077
printf 'source=%s\n' "$PW_SOURCE" > "$CRED_FILE"
chmod 0600 "$CRED_FILE"
# --- end admin-source ---

# --- 2. Close the Docker/ufw gap --------------------------------------------
# trinity-docker-firewall.service was enabled at BUILD time and applies these
# rules on every boot, including this one — but "enabled at boot" and "applied
# before the containers start" are different claims, and cloud-init's
# per-instance scripts share multi-user.target with the unit. Calling it
# directly removes the ordering question; the script is idempotent.
if ! "${TRINITY_DIR}/scripts/deploy/docker-firewall.sh"; then
    echo "WARNING: could not apply the DOCKER-USER rules. Container ports may be" >&2
    echo "         reachable from the internet." >&2
    echo "         Re-run: ${TRINITY_DIR}/scripts/deploy/docker-firewall.sh" >&2
fi

# --- 3. Site config + Trinity ------------------------------------------------
# One call does the rest: the droplet's own IP, the .env keys, the Caddyfile,
# the Let's Encrypt certificate for the bare IP, then the install itself.
#
# --site-only because the machine phase (Docker, Caddy, ufw, the firewall unit)
# is already baked into the snapshot — re-running it here would spend an
# `apt-get update` on every droplet, against the one property this image exists
# to have.
#
# --provenance do-marketplace is the #2380 install-source marker. It travels as
# an argument rather than being hardcoded in the installer, because the very
# same code path serves a doc-driven install, which is honestly a different
# provenance (`do-script`).
#
# The admin source travels in the environment exported above: ADMIN_PASSWORD
# (user-data) or ADMIN_PASSWORD_SOURCE=browser (claim at /setup). start.sh
# persists either into .env.
cd "$TRINITY_DIR"
export TRINITY_IMAGE_TAG="$(cat "${STATE_DIR}/baked-image-tag" 2>/dev/null || echo latest)"
./scripts/deploy/start.sh \
    --provision --cloud digitalocean --site-only --provenance do-marketplace \
    --hosted --unattended

echo "=== Trinity first boot complete ==="
