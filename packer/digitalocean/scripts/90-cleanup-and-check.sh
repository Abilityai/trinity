#!/bin/bash
# Final build step: DigitalOcean's own cleanup, then their validation.
#
# Order is load-bearing and this must be the LAST provisioner. cleanup.sh
# removes what must not be shared by every droplet created from the snapshot
# (shell history, logs, SSH host keys, apt caches, cloud-init state); img_check.sh
# then verifies it is gone. Anything that runs after img_check reintroduces
# exactly what it just certified as absent.
set -euo pipefail

echo "=== Fetching DigitalOcean marketplace validation tooling ==="
git clone --depth 1 https://github.com/digitalocean/marketplace-partners.git \
  /tmp/marketplace-partners

echo "=== cleanup.sh ==="
bash /tmp/marketplace-partners/scripts/90-cleanup.sh

echo "=== img_check.sh ==="
# img_check exits non-zero on any finding, which fails the Packer build — the
# snapshot is never created from a droplet that would be rejected at review.
bash /tmp/marketplace-partners/scripts/99-img-check.sh
