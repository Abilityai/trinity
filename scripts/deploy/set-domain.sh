#!/bin/bash
# Put a real domain in front of this instance (#2380).
#
#     sudo /opt/trinity/scripts/deploy/set-domain.sh example.com
#
# Runs on the SERVER, and it has to: Trinity runs in a container with no host
# privileges, so it can neither rewrite /etc/caddy/Caddyfile nor reload Caddy.
# That is why the first-run hardening card cannot finish this job with a button,
# and why "set the Public URL in Settings" alone does not work — that setting
# changes the name Trinity HANDS OUT; it does not make anything answer to it.
# Without this script the operator ends up advertising a name that nothing
# serves, and the card retires believing step one is done.
#
# What it does, in order: refuse unless DNS already points here, rewrite the
# Caddyfile for the name, validate it, reload, wait for the certificate, and
# only then tell Trinity its new address. Any failure restores the previous
# Caddyfile and reloads, so a bad run leaves the instance exactly as it was.
set -euo pipefail
export PATH="/usr/local/sbin:/usr/sbin:/sbin:${PATH}"

cd "$(dirname "$0")/../.."
. ./scripts/deploy/env-file.sh

DOMAIN="${1:-}"
die() { printf '\n❌ %s\n\n' "$1" >&2; exit 1; }

[ -n "$DOMAIN" ] || die "Usage: sudo $0 <domain>
       e.g. sudo $0 trinity.example.com"
# Shape before privilege: telling somebody who typo'd the domain that they
# should have used sudo sends them off to re-run the same typo with more rights.
case "$DOMAIN" in
    *://*) die "Give a bare hostname, with no https:// in front: ${DOMAIN#*://}" ;;
    */*)   die "Give a bare hostname, with no path: ${DOMAIN%%/*}" ;;
    *\ *)  die "A hostname cannot contain spaces." ;;
esac
printf '%s' "$DOMAIN" | grep -qE '^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$' \
    || die "'${DOMAIN}' is not a valid hostname."

[ "$(id -u)" = "0" ] || die "Must run as root — it rewrites /etc/caddy/Caddyfile. Use sudo."

command -v caddy >/dev/null 2>&1 \
    || die "Caddy is not installed. This script is for an instance set up by 'start.sh --provision'."
[ -f /etc/caddy/Caddyfile ] || die "/etc/caddy/Caddyfile does not exist — nothing to update."
[ -f .env ] || die "No .env in $(pwd) — run this from the Trinity checkout (usually /opt/trinity)."

# --- 1. Refuse before touching anything if DNS is not ready ------------------
# This is the failure this script exists to prevent. Let's Encrypt validates by
# connecting to the name over HTTP; if the A record does not point here yet,
# issuance fails, Caddy serves an error for the new name, and the operator has
# swapped a working instance for a broken one. Checking first costs one lookup.
MY_IP="$(cat /etc/trinity/public-ip 2>/dev/null || true)"
if [ -z "$MY_IP" ]; then
    MY_IP="$(curl -fsS --max-time 10 \
        http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address 2>/dev/null || true)"
fi
[ -n "$MY_IP" ] || die "Could not work out this machine's public IP address."

RESOLVED="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ')"
[ -n "$RESOLVED" ] || die "${DOMAIN} does not resolve to anything yet.
       Add an A record pointing ${DOMAIN} at ${MY_IP}, wait for it to spread
       (usually minutes, occasionally an hour), then run this again."
case " ${RESOLVED} " in
    *" ${MY_IP} "*) ;;
    *) die "${DOMAIN} points at ${RESOLVED%% }, but this machine is ${MY_IP}.
       Fix the A record, wait for the change to spread, then run this again." ;;
esac
echo "DNS: ${DOMAIN} → ${MY_IP} ✓"

# --- 2. Rewrite the Caddyfile ------------------------------------------------
BACKUP="/etc/caddy/Caddyfile.before-${DOMAIN}.$(date -u +%Y%m%d-%H%M%S)"
cp -p /etc/caddy/Caddyfile "$BACKUP"

restore() {
    cp -p "$BACKUP" /etc/caddy/Caddyfile
    systemctl reload caddy 2>/dev/null || systemctl restart caddy || true
    printf '\nPrevious configuration restored from %s — the instance is as it was.\n' "$BACKUP" >&2
}

# The bare-IP site is KEPT, redirecting to the name, rather than dropped: links
# and bookmarks handed out before the domain existed keep working instead of
# failing with a certificate error. It keeps its short-lived IP certificate for
# the same reason — a redirect that cannot complete a handshake is not a
# redirect. The DOMAIN site takes no `profile shortlived`: a real name gets an
# ordinary ~90-day certificate, which is the actual upgrade being made here.
cat > /etc/caddy/Caddyfile <<CADDY
{
    acme_ca https://acme-v02.api.letsencrypt.org/directory
}

https://${DOMAIN} {
    encode gzip

    # Streaming endpoints must not be buffered: the Workspace reads execution
    # logs over SSE (GET /api/executions/{id}/stream).
    reverse_proxy 127.0.0.1:8081 {
        flush_interval -1
    }
}

http://${DOMAIN} {
    redir https://${DOMAIN}{uri} permanent
}

https://${MY_IP} {
    tls {
        issuer acme {
            profile shortlived
        }
    }
    redir https://${DOMAIN}{uri} permanent
}

http://${MY_IP} {
    redir https://${DOMAIN}{uri} permanent
}
CADDY

if ! caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
    restore
    die "The generated Caddy configuration was rejected by 'caddy validate'."
fi

systemctl reload caddy 2>/dev/null || systemctl restart caddy || { restore; die "Caddy would not reload."; }
echo "Caddy: reloaded for ${DOMAIN}."

# --- 3. Wait for the certificate --------------------------------------------
# Verified against the real name with the system trust store, so success means
# the certificate is valid for THIS domain — not merely that something answered.
printf 'Waiting for the certificate'
TLS_OK=0
for _try in $(seq 1 30); do
    if curl -fsS -o /dev/null --max-time 10 "https://${DOMAIN}/" 2>/dev/null; then
        TLS_OK=1
        break
    fi
    printf '.'
    sleep 5
done
printf '\n'
if [ "$TLS_OK" != "1" ]; then
    restore
    die "No valid certificate for ${DOMAIN} after ~150s.
       Check: journalctl -u caddy -n 100"
fi
echo "TLS: certificate issued for ${DOMAIN} ✓"

# --- 4. Tell Trinity its new address -----------------------------------------
# LAST, deliberately: the address Trinity advertises is what the hardening card
# reads to decide step one is done, so it must not change until the name really
# works. `public_chat_url` is the canonical resolver and takes effect
# immediately; setting it through the API rather than the database also
# re-registers the Telegram and WhatsApp webhooks that were pinned to the old
# address. `FRONTEND_URL` in .env is the fallback beneath it and is updated too,
# so a later container recreate does not hand back the IP.
ADMIN_PASSWORD="$(grep -E '^ADMIN_PASSWORD=' .env | head -1 | cut -d= -f2-)"
if [ -n "$ADMIN_PASSWORD" ]; then
    TOKEN="$(curl -fsS -X POST http://localhost:8000/api/token \
        --data-urlencode "username=admin" \
        --data-urlencode "password=${ADMIN_PASSWORD}" 2>/dev/null \
        | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')"
fi
if [ -n "${TOKEN:-}" ] && curl -fsS -X PUT "http://localhost:8000/api/settings/public_chat_url" \
        -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' \
        -d "{\"value\": \"https://${DOMAIN}\"}" >/dev/null 2>&1; then
    echo "Trinity: now advertising https://${DOMAIN} ✓"
    SETTING_OK=1
else
    # Not fatal, and not silent: the certificate is real and the site works. The
    # operator finishes in the UI in ten seconds, and is told exactly where.
    echo "NOTE: could not set the Public URL automatically." >&2
    echo "      Set it by hand: Settings → General → Public URL → https://${DOMAIN}" >&2
    SETTING_OK=0
fi
set_env_key FRONTEND_URL "https://${DOMAIN}"

cat <<DONE

  ───────────────────────────────────────────────────────────────────────
  ${DOMAIN} is live.

      Open: https://${DOMAIN}
      The old address redirects here, so existing links keep working.
DONE
[ "$SETTING_OK" = "1" ] || echo "      One step left: Settings → General → Public URL → https://${DOMAIN}"
echo "      Previous Caddy config saved at ${BACKUP}"
echo
