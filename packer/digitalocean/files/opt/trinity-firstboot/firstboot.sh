#!/bin/bash
# First-boot configuration for a Trinity DigitalOcean 1-Click droplet (#2281).
# Runs ONCE per droplet, from /var/lib/cloud/scripts/per-instance/001-trinity.
#
# Everything droplet-specific lives here and nothing here is baked into the
# snapshot: the admin password, the droplet's own IP, the certificate.
set -euo pipefail

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
    ADMIN_PASSWORD="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)"
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

# --- 2. Droplet identity -----------------------------------------------------
# The metadata service is reachable only from the droplet itself.
PUBLIC_IP="$(curl -fsS --max-time 10 \
  http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address || true)"
if [ -z "$PUBLIC_IP" ]; then
    echo "WARNING: could not read the public IP from the metadata service."
    echo "         Caddy will be left unconfigured; Trinity will still start."
fi
echo "$PUBLIC_IP" > "${STATE_DIR}/public-ip"

IMAGE_TAG="$(cat "${STATE_DIR}/baked-image-tag" 2>/dev/null || echo latest)"

# --- 3. .env -----------------------------------------------------------------
# start.sh --unattended generates SECRET_KEY, INTERNAL_API_SECRET,
# CREDENTIAL_ENCRYPTION_KEY and AGENT_AUTH_SECRET itself when they are blank; we
# only write what it cannot know.
#
# FRONTEND_PORT moves the SPA off :80 so Caddy can own :80/:443 and terminate
# TLS in front of it.
#
# TRINITY_INSTALL_SOURCE is the #2380 provenance marker. It is an env var in
# .env by that issue's explicit decision, NOT a marker file — config.py reads no
# files, and a file would need a read-only bind mount in every compose file.
cd "$TRINITY_DIR"
if [ ! -f .env ]; then
    cp .env.example .env
fi
set_env() {
    local key="$1" value="$2"
    if grep -qE "^${key}=" .env; then
        sed -i "s|^${key}=.*|${key}=${value}|" .env
    else
        printf '%s=%s\n' "$key" "$value" >> .env
    fi
}
set_env ADMIN_PASSWORD "$ADMIN_PASSWORD"
set_env FRONTEND_PORT 8081
set_env TRINITY_IMAGE_TAG "$IMAGE_TAG"
set_env TRINITY_INSTALL_SOURCE do-marketplace
[ -n "$PUBLIC_IP" ] && set_env FRONTEND_URL "https://${PUBLIC_IP}"
chmod 0600 .env

# --- 4. Close the Docker/ufw gap --------------------------------------------
# docker-compose.hosted.yml publishes 8000 (backend), 8080 (MCP), 8686 (Vector)
# and the OTel collector's ports on 0.0.0.0. Docker's iptables rules are
# consulted BEFORE ufw's chain, so those ports are reachable from the internet
# on a droplet whose ufw says otherwise — `ufw deny 8000` is silently inert.
#
# DOCKER-USER is the one chain Docker leaves for the operator and evaluates
# first, so the drop belongs here. Everything a user needs is served by Caddy on
# 80/443; the container ports stay reachable from the host and from other
# containers, which is what the platform itself uses.
if ! iptables -C DOCKER-USER -i eth0 -p tcp -m multiport \
       --dports 8000,8080,8686,4317,4318,8889,13133 -j DROP 2>/dev/null; then
    iptables -I DOCKER-USER -i eth0 -p tcp -m multiport \
      --dports 8000,8080,8686,4317,4318,8889,13133 -j DROP
fi
apt-get install -y -q iptables-persistent >/dev/null 2>&1 || true
netfilter-persistent save >/dev/null 2>&1 || true

# --- 5. Caddy ----------------------------------------------------------------
# Let's Encrypt issues certificates for bare IP addresses as of 2026-01-15, via
# the `shortlived` ACME profile (~6-day validity, http-01/tls-alpn-01 only). DO's
# own 1-Click build standard mandates this pattern for any app with an HTTP
# interface, so the droplet lands on browser-trusted HTTPS with no domain and no
# user input. A domain becomes a post-login upgrade, not a prerequisite.
if [ -n "$PUBLIC_IP" ]; then
    cat > /etc/caddy/Caddyfile <<CADDY
{
    acme_ca https://acme-v02.api.letsencrypt.org/directory
}

https://${PUBLIC_IP} {
    tls {
        issuer acme {
            profile shortlived
        }
    }
    encode gzip

    # Streaming endpoints must not be buffered: the Workspace reads execution
    # logs over SSE (GET /api/executions/{id}/stream).
    reverse_proxy 127.0.0.1:8081 {
        flush_interval -1
    }
}

http://${PUBLIC_IP} {
    redir https://${PUBLIC_IP}{uri} permanent
}
CADDY
    systemctl enable caddy
    systemctl restart caddy

    # --- Verify the certificate was actually issued --------------------------
    # Without this the failure is SILENT and worse than silent: Caddy serves a
    # TLS error, this script still exits 0, and the MOTD prints a confident
    # `https://<ip>` URL. The user's first contact with Trinity is a browser
    # warning on a box that reported success.
    #
    # The probe is `curl` against our own public IP with normal verification.
    # Trinity is not started yet (that is step 6), so Caddy answers 502 — which
    # is FINE and is the point: curl without `-f` treats an HTTP error as
    # success, so a zero exit means the TLS handshake completed and the chain
    # validated against the system trust store. That is exactly the claim being
    # tested, and nothing weaker distinguishes "real Let's Encrypt certificate"
    # from "Caddy fell back to its internal CA".
    #
    # ACME issuance is not instant, hence the poll. Never fatal: a droplet that
    # serves over HTTP with an honest banner is far better than one that
    # refuses to finish booting.
    TLS_STATUS="failed"
    for _ in $(seq 1 30); do
        if curl -sS -o /dev/null --max-time 10 "https://${PUBLIC_IP}/" 2>/dev/null; then
            TLS_STATUS="ok"
            break
        fi
        sleep 5
    done
    printf '%s\n' "$TLS_STATUS" > "${STATE_DIR}/tls-status"
    if [ "$TLS_STATUS" = "ok" ]; then
        echo "TLS: certificate issued for ${PUBLIC_IP}."
    else
        echo "TLS: WARNING — no valid certificate for ${PUBLIC_IP} after ~150s." >&2
        echo "     Trinity will still start and is reachable over http://${PUBLIC_IP}." >&2
        echo "     Check: journalctl -u caddy -n 100" >&2
    fi
else
    printf '%s\n' "unknown" > "${STATE_DIR}/tls-status"
fi

# --- 6. Start Trinity --------------------------------------------------------
# --hosted uses docker-compose.hosted.yml and the prebuilt images; --unattended
# never prompts. Both flags are on the ONE installer rather than a marketplace
# copy of it, which is the shape that has gone stale here before (#1039, #1056,
# #1707, #1871).
./scripts/deploy/start.sh --hosted --unattended

echo "=== Trinity first boot complete ==="
