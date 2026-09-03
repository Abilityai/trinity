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
# PINNED, not HEAD (#2281 review I4). Both scripts below run as root and are the
# last thing to touch the image a vendor review approves, so a moving
# third-party target is not an acceptable input to a certified artifact — even
# DigitalOcean's own canonical repo. Bump deliberately, having read the diff.
# `fetch <sha>` rather than `clone --depth 1`: a shallow clone can only take a
# ref, and github.com serves commit-SHA fetches.
MP_COMMIT=b70878804ca27c01d5f5e882d26485defbaba210  # master @ 2026-07-16
git init -q /tmp/marketplace-partners
git -C /tmp/marketplace-partners remote add origin \
  https://github.com/digitalocean/marketplace-partners.git
git -C /tmp/marketplace-partners fetch -q --depth 1 origin "$MP_COMMIT"
git -C /tmp/marketplace-partners checkout -q FETCH_HEAD

echo "=== cleanup.sh ==="
bash /tmp/marketplace-partners/scripts/90-cleanup.sh

echo "=== img_check.sh ==="
# img_check exits non-zero on any finding, which fails the Packer build — the
# snapshot is never created from a droplet that would be rejected at review.
bash /tmp/marketplace-partners/scripts/99-img-check.sh
