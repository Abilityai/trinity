#!/bin/bash
# Final build step for the Trinity AWS AMI (#3004): remove what must not be
# shared by every instance launched from the image, then fail the build if any
# of it is still there. Replaces DigitalOcean's 90-cleanup.sh + 99-img-check.sh,
# against AWS Marketplace's AMI checklist: no authorized keys, no preset
# passwords, no shell history, no SSH host keys, per-instance cloud-init state
# cleared, SSH password login off, root locked.
#
# Must be the LAST provisioner: anything after it can put back what it removed.
set -euo pipefail
export PATH="/usr/local/sbin:/usr/sbin:/sbin:${PATH}"

HOOK=/var/lib/cloud/scripts/per-instance/001-trinity
FAIL=0
fail() { echo "[FAIL] $1" >&2; FAIL=1; }

[ -x "$HOOK" ] || { echo "FATAL: ${HOOK} missing before cleanup — 01-provision.sh did not install it." >&2; exit 1; }
[ "$(cat /etc/trinity/cloud 2>/dev/null)" = "aws" ] \
    || { echo "FATAL: /etc/trinity/cloud is not 'aws' — first boot would take the DigitalOcean path." >&2; exit 1; }

# Checked while the host keys still exist: `sshd -T` refuses to print the
# effective config without them.
sshd -T 2>/dev/null | grep -qx 'passwordauthentication no' \
    || fail "SSH password authentication is not disabled."

# `cloud-init clean` deletes everything under /var/lib/cloud except seed/,
# including scripts/per-instance/ — the hook that runs first boot. Keep a copy
# outside the image's filesystem (tmpfs) and put it back afterwards.
cp -p "$HOOK" /dev/shm/001-trinity

echo "=== apt caches ==="
apt-get -y -q autoremove --purge
apt-get -y -q clean
rm -rf /var/lib/apt/lists/*

echo "=== keys, history, temp files ==="
find /root /home -name authorized_keys -type f -delete
rm -f /etc/ssh/ssh_host_*
rm -f /root/.bash_history /home/*/.bash_history /root/.lesshst /home/*/.lesshst
rm -rf /tmp/* /var/tmp/*

echo "=== cloud-init state and machine-id ==="
cloud-init clean --logs --machine-id
install -D -m 0755 /dev/shm/001-trinity "$HOOK"
rm -f /dev/shm/001-trinity

echo "=== logs ==="
# The journal is journald's: truncating its files under it corrupts them. It is
# rotated and vacuumed instead; everything else under /var/log is emptied.
find /var/log -path /var/log/journal -prune -o -type f -name '*.gz' -delete
find /var/log -path /var/log/journal -prune -o -type f -name '*.[0-9]' -delete
find /var/log -path /var/log/journal -prune -o -type f -exec truncate -s 0 {} +
journalctl --rotate >/dev/null 2>&1 || true
journalctl --vacuum-time=1s >/dev/null 2>&1 || true

echo "=== checks ==="
[ -x "$HOOK" ] || fail "first-boot hook ${HOOK} is missing — instances would never configure themselves."
[ -z "$(find /root /home -name authorized_keys -type f)" ] || fail "an authorized_keys file remains."
! ls /etc/ssh/ssh_host_* >/dev/null 2>&1 || fail "SSH host keys remain; every instance would share them."
[ -z "$(find /root /home -name .bash_history -type f)" ] || fail "shell history remains."
[ "$(passwd -S root | awk '{print $2}')" = "L" ] || fail "root is not locked."
# No account may carry a usable password hash (a preset password).
_pw="$(awk -F: '$2 != "" && $2 !~ /^[!*]/ {print $1}' /etc/shadow)"
[ -z "$_pw" ] || fail "accounts with a usable password: ${_pw}"
# The instance-ID claim is per instance. A baked one would be the build
# instance's ID on every customer's machine.
[ -z "$(find / -xdev -name setup-claim 2>/dev/null)" ] || fail "a setup-claim file is baked into the image."
# The machine phase writes no .env, so none may exist — and with it no admin
# password or claim source.
[ ! -e /opt/trinity/.env ] || fail "/opt/trinity/.env is baked into the image."
[ ! -s /etc/machine-id ] || [ "$(cat /etc/machine-id)" = "uninitialized" ] || fail "/etc/machine-id is not reset."

if [ "$FAIL" != "0" ]; then
    echo "AMI cleanup checks FAILED — not creating an image from this instance." >&2
    exit 1
fi
echo "=== AMI cleanup checks passed ==="
