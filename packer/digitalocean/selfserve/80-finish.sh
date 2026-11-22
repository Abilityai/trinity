#!/bin/bash
# Build step after 01-provision.sh for the self-serve snapshot
# (trinity-enterprise#1347).
#
#   - installs the self-serve first boot where the per-instance hook calls it;
#   - records the baked commit and enterprise pin for support and the clean check;
#   - empties the machine id, which DigitalOcean's cleanup leaves alone. Every
#     instance made from the snapshot would otherwise share one, and systemd
#     writes a fresh id at boot when the file is empty.
set -euo pipefail

: "${TRINITY_COMMIT:?TRINITY_COMMIT must be passed by the Packer build}"

install -D -m 0755 /tmp/trinity-selfserve/firstboot.sh /opt/trinity-firstboot/firstboot.sh
rm -rf /tmp/trinity-selfserve

pin="$(git -C /opt/trinity ls-tree HEAD src/backend/enterprise | awk '$2 == "commit" { print $3 }')"
mkdir -p /etc/trinity
echo "self-serve" > /etc/trinity/variant
echo "${TRINITY_COMMIT}" > /etc/trinity/baked-commit
echo "${pin}" > /etc/trinity/baked-enterprise-pin

truncate -s 0 /etc/machine-id
if [ -f /var/lib/dbus/machine-id ] && [ ! -L /var/lib/dbus/machine-id ]; then
    rm -f /var/lib/dbus/machine-id
    ln -s /etc/machine-id /var/lib/dbus/machine-id
fi

echo "=== self-serve finish complete ==="
