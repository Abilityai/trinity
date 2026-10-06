#!/bin/bash
# Trinity on Vultr — Marketplace Vendor Data (#2282).
#
# Paste this WHOLE file into the Vendor Portal (App → Builds → Build from Vendor
# Data, base OS Ubuntu 24.04). Vultr runs it once per instance, as root, through
# cloud-init at first boot, on a clean OS. There is no snapshot: everything the
# DigitalOcean 1-Click bakes at build time happens here instead, which is why a
# first boot takes ~10 minutes rather than ~2.
#
# It resolves the latest Trinity RELEASE at boot rather than carrying a version.
# `releases/latest` excludes pre-releases, so an RC never reaches a customer, and
# nobody has to re-paste this file every release — the one thing a pinned literal
# would demand and nothing could enforce, since the copy that matters lives in
# Vultr's portal, not in this repo.
#
# Everything provider-independent is `start.sh --provision` (PROV-012), the same
# code the DigitalOcean image and doc installer run. What is genuinely Vultr's
# here: the release resolve, the failure banner, and the marketplace provenance.
#
# It also works unchanged as ordinary Vultr **User Data** for a hand-driven
# install, which is how it gets tested before the listing exists.
set -euo pipefail
export PATH="/usr/local/sbin:/usr/sbin:/sbin:${PATH}"

# NOT 077. The backend container runs as UID 1000 and bind-mounts ./config
# read-only; a root-only clone makes the local template catalog unreadable and
# the first-run system seed finds nothing to deploy — with no error at install
# time (docker-compose.hosted.yml, "THIS FILE IS NOT STANDALONE").
umask 022

STATE_DIR="${TRINITY_STATE_DIR:-/etc/trinity}"
TRINITY_DIR="${TRINITY_DIR:-/opt/trinity}"
TRINITY_REPO="${TRINITY_REPO:-https://github.com/abilityai/trinity.git}"
LOG="${TRINITY_LOG:-/var/log/trinity-install.log}"

exec > >(tee -a "$LOG") 2>&1
echo "=== Trinity first boot: $(date -u +%FT%TZ) ==="

mkdir -p "$STATE_DIR"
chmod 0700 "$STATE_DIR"
rm -f "${STATE_DIR}/ready" "${STATE_DIR}/firstboot-failed"

# A marketplace customer's only surface is the web UI, and a failed boot leaves
# nothing answering on it. `set -e` exits without a word, so the marker is
# written from a trap rather than after the last command, and the banner is
# installed BEFORE anything can fail.
# shellcheck disable=SC2154  # rc is assigned inside the trap body itself
trap 'rc=$?; if [ "$rc" -ne 0 ]; then echo "=== Trinity first boot FAILED (exit ${rc}) ==="; : > "${STATE_DIR}/firstboot-failed"; fi' EXIT

install_banner() {
    mkdir -p /etc/update-motd.d
    cat > /etc/update-motd.d/99-trinity <<'BANNER'
#!/bin/bash
# Trinity login banner (Vultr). Prints where to go and what actually happened —
# never a password: the first visitor creates the admin in the browser.
STATE_DIR=/etc/trinity
IP="$(cat ${STATE_DIR}/public-ip 2>/dev/null)"
TAG="$(cat ${STATE_DIR}/release 2>/dev/null)"

echo
echo "  Trinity ${TAG:-} — the sovereign AI agents platform"
echo "  ---------------------------------------------------------------"

if [ -f "${STATE_DIR}/firstboot-failed" ]; then
    echo "  ⚠  First boot did NOT complete."
    echo "     Log:   /var/log/trinity-install.log"
    echo "     Retry: cd /opt/trinity && sudo ./scripts/deploy/start.sh \\"
    echo "              --provision --cloud vultr --provenance vultr-marketplace \\"
    echo "              --hosted --unattended"
    echo
    exit 0
fi

# Neither failed nor finished: first boot pulls ~1.6 GB, and for those minutes
# the web UI refuses connections. Saying so beats printing a URL that is right
# but does not answer yet.
if [ ! -f "${STATE_DIR}/ready" ]; then
    echo "  Still installing — Trinity downloads ~1.6 GB on first boot."
    [ -n "$IP" ] && echo "  Web UI:  https://${IP}  (not answering yet)"
    echo "  Watch:   tail -f /var/log/trinity-install.log"
    echo
    exit 0
fi

# The scheme is what first boot VERIFIED, not what the Caddyfile intended:
# printing https:// after a failed issuance sends the user to a browser warning
# and tells them the box is fine.
if [ "$(cat ${STATE_DIR}/tls-status 2>/dev/null)" = "ok" ]; then
    SCHEME="https"
else
    SCHEME="http"
fi

if [ -n "$IP" ]; then
    echo "  Web UI:  ${SCHEME}://${IP}"
else
    echo "  Web UI:  ${SCHEME}://<this server's IP>"
fi

if [ "$SCHEME" != "https" ]; then
    echo
    echo "  ⚠  HTTPS is NOT active — no certificate was issued for this IP."
    echo "     Traffic is unencrypted. Check: journalctl -u caddy -n 100"
fi

if curl -fsS --max-time 2 http://127.0.0.1:8000/api/setup/status 2>/dev/null \
        | grep -q '"setup_completed": *true'; then
    echo "  Login:   the admin account created in the browser at first visit"
else
    # Unclaimed (or the backend is not answering yet): the one action is to open
    # the URL above. Until someone does, anyone who finds it can.
    echo "  Admin:   none yet. Open the Web UI above and create it (email +"
    echo "           password). The first visitor does — so do it now."
fi

if [ -f "${STATE_DIR}/plan-warning" ]; then
    echo
    echo "  ⚠  This plan is below Trinity's 8 GB floor. Agents and the platform"
    echo "     contend for memory and turns start failing under load."
fi

echo
echo "  Next:    open the Web UI and follow the first-run setup"
echo "           (add a domain, then serve it through a Cloudflare Tunnel)."
echo "  Docs:    https://docs.ability.ai"
echo "  Support: https://github.com/abilityai/trinity/issues  (label: vultr-marketplace)"
echo "  Vultr does not build or support Trinity."
echo
BANNER
    chmod 0755 /etc/update-motd.d/99-trinity
}

install_banner

# 8 GB is a floor, not a recommendation (HOST-011) — but Vultr cannot enforce a
# plan size at deploy time, so this warns and installs anyway: a running instance
# with an honest banner beats one that refuses to come up on a plan the customer
# has already paid for.
_mem_kb="$(awk '/^MemTotal:/{print $2}' /proc/meminfo 2>/dev/null || echo 0)"
if [ "${_mem_kb:-0}" -lt 7340032 ]; then
    echo "⚠️  $(( _mem_kb / 1024 ))MB RAM — below Trinity's 8 GB floor. Installing anyway."
    : > "${STATE_DIR}/plan-warning"
fi

# curl feeds the resolve below and git the clone. Both ship on Vultr's stock
# Ubuntu, but "present on the image we tested" is not a contract — and a missing
# curl would otherwise surface as "could not resolve the latest release", which
# points at GitHub for a problem on this machine.
_missing=()
command -v curl >/dev/null 2>&1 || _missing+=(curl)
command -v git  >/dev/null 2>&1 || _missing+=(git)
if [ "${#_missing[@]}" -gt 0 ]; then
    echo "→ Installing ${_missing[*]}"
    apt-get -o DPkg::Lock::Timeout=600 update -q
    DEBIAN_FRONTEND=noninteractive \
        apt-get -o DPkg::Lock::Timeout=600 install -y -q "${_missing[@]}"
fi

# The release to install. `releases/latest` skips pre-releases by construction,
# which is the property that matters: /release cuts every RC with --prerelease so
# it can publish images without becoming what operators install.
#
# A resolve failure ABORTS rather than falling back to some baked-in tag: a
# successful install of the wrong release is invisible to the customer and to us.
if [ -z "${TRINITY_IMAGE_TAG:-}" ]; then
    TRINITY_IMAGE_TAG="$(curl -fsS --max-time 30 \
        https://api.github.com/repos/abilityai/trinity/releases/latest 2>/dev/null \
        | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)"
fi
if [ -z "${TRINITY_IMAGE_TAG:-}" ]; then
    echo "❌ Could not resolve the latest Trinity release from GitHub." >&2
    echo "   Retry, or install a known release: TRINITY_IMAGE_TAG=v0.9.5 $0" >&2
    exit 1
fi
# EXPORTED, not just set: start.sh persists TRINITY_IMAGE_TAG into .env only when
# it is in the environment, and without it resolve_image_tag falls back to the
# `latest` docker tag — so every later --hosted run becomes an unplanned upgrade.
export TRINITY_IMAGE_TAG
printf '%s\n' "$TRINITY_IMAGE_TAG" > "${STATE_DIR}/release"
echo "→ Installing Trinity ${TRINITY_IMAGE_TAG}"

# Guarded so a retry after a failed boot resumes instead of dying on a
# non-empty directory.
[ -d "${TRINITY_DIR}/.git" ] || \
    git clone --depth 1 --branch "$TRINITY_IMAGE_TAG" "$TRINITY_REPO" "$TRINITY_DIR"
cd "$TRINITY_DIR"

# No admin is provisioned (trinity-enterprise#580): the first person to open the
# web UI registers the admin there — email, password, product-updates consent —
# without ever opening a terminal. Nothing is generated, so there is no password
# in Vultr's metadata service, which serves it to every host process for the life
# of the machine (trinity-enterprise#622 item 2).
export ADMIN_PASSWORD_SOURCE=browser

./scripts/deploy/start.sh \
    --provision --cloud vultr --provenance vultr-marketplace \
    --hosted --unattended

: > "${STATE_DIR}/ready"
echo "=== Trinity is ready at https://$(cat "${STATE_DIR}/public-ip" 2>/dev/null) ==="
