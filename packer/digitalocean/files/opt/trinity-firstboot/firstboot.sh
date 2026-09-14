#!/bin/bash
# First-boot configuration for a Trinity DigitalOcean 1-Click droplet (#2281).
# Runs ONCE per droplet, from /var/lib/cloud/scripts/per-instance/001-trinity.
#
# Everything droplet-specific happens here and nothing here is baked into the
# snapshot. Almost all of it is now `start.sh --provision --site-only`, which is
# the SAME code a doc-driven install runs (#2380) — the Caddyfile, the
# certificate poll and the .env keys used to be a second copy that had already
# drifted from the first. What is left is the one thing genuinely specific to a
# Marketplace droplet: where the admin password comes from.
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

# --- 1. Admin password -------------------------------------------------------
# A 1-Click has no vendor-defined input form at deploy time (verified against
# digitalocean/marketplace-partners: the only prompt is the optional Managed
# Database checkbox), so the password cannot be collected in the UI.
#
# Two sources, in order:
#   (a) user-data, if the operator supplied one. It must arrive as #cloud-config
#       `write_files` and NOT as a shell script: 1-Click per-instance code runs
#       from cloud-init's `scripts-per-instance` module, which runs BEFORE
#       `scripts-user`, so a user-data shell script would execute after this
#       script had already generated a password and started Trinity.
#   (b) generated here, and shown in the MOTD.
PW_SOURCE="generated"
if [ -s "$USER_SUPPLIED_PW" ]; then
    ADMIN_PASSWORD="$(head -c 512 "$USER_SUPPLIED_PW" | tr -d '\r\n')"
    PW_SOURCE="user-data"
    shred -u "$USER_SUPPLIED_PW" 2>/dev/null || rm -f "$USER_SUPPLIED_PW"
fi
if [ -z "${ADMIN_PASSWORD:-}" ]; then
    # --- password-generation (behaviour-tested; see test_2281_firstboot_password) ---
    # NOT `tr -dc ... </dev/urandom | head -c 24`. Under this script's own
    # `set -o pipefail` that is fatal, every time: head closes the pipe after 24
    # bytes, tr dies of SIGPIPE against an endless source, and the non-zero
    # status propagates out of the command substitution, where `set -e` ends the
    # script. First boot died on this line on the very first droplet ever created
    # from the snapshot — three lines in, before it had written anything but its
    # own header, leaving a droplet with no Trinity, no certificate and no
    # password.
    #
    # Reading a bounded chunk first means nothing closes a pipe early: head takes
    # 1024 bytes and exits, tr drains all of them and exits 0. LC_ALL=C keeps tr
    # byte-oriented rather than trusting the ambient locale to tolerate random
    # bytes.
    _pw_raw="$(head -c 1024 /dev/urandom | LC_ALL=C tr -dc 'A-Za-z0-9')"
    ADMIN_PASSWORD="${_pw_raw:0:24}"
    # 1024 random bytes yield ~635 alphanumerics on average, so this cannot
    # plausibly fail — but it is a credential, and a short one must never be
    # written rather than silently accepted.
    if [ "${#ADMIN_PASSWORD}" -ne 24 ]; then
        echo "FATAL: could not generate a 24-character admin password." >&2
        exit 1
    fi
    unset _pw_raw
    # --- end password-generation ---
fi

# The MOTD never echoes a password the operator chose — they already have it,
# and reprinting it widens where it exists for no benefit.
umask 077
if [ "$PW_SOURCE" = "user-data" ]; then
    printf 'source=user-data\npassword=\n' > "$CRED_FILE"
else
    printf 'source=generated\npassword=%s\n' "$ADMIN_PASSWORD" > "$CRED_FILE"
fi
chmod 0600 "$CRED_FILE"

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
cd "$TRINITY_DIR"
export ADMIN_PASSWORD
export TRINITY_IMAGE_TAG="$(cat "${STATE_DIR}/baked-image-tag" 2>/dev/null || echo latest)"
./scripts/deploy/start.sh \
    --provision --cloud digitalocean --site-only --provenance do-marketplace \
    --hosted --unattended

echo "=== Trinity first boot complete ==="
