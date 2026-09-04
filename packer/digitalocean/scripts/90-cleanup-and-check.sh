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

# Stage the checker OUTSIDE /tmp before cleanup runs. DigitalOcean's own
# 90-cleanup.sh does `rm -rf /tmp/* /var/tmp/*`, which deletes the checkout it
# was just cloned into — so running img_check.sh from /tmp after cleanup is not
# merely fragile, it is impossible:
#
#     bash: /tmp/marketplace-partners/scripts/99-img-check.sh: No such file or directory
#
# This is why img_check had never actually run: `packer build` had never run
# either, and the two failures hid each other.
#
# /dev/shm rather than /opt or /root, for two reasons. It is tmpfs, so the staged
# copy is not part of the snapshot's filesystem and nothing has to be deleted
# afterwards — which keeps the rule above intact, that NOTHING runs after
# img_check. And it survives cleanup, which only clears /tmp and /var/tmp.
IMG_CHECK=/dev/shm/99-img-check.sh
cp /tmp/marketplace-partners/scripts/99-img-check.sh "$IMG_CHECK"

# Purge DigitalOcean's own droplet-agent before their cleanup runs.
#
# The stock ubuntu-24-04-x64 base carries it (the build's apt sources show
# repos-droplet.digitalocean.com/apt/droplet-agent), and img_check treats its
# directory as a hard failure, not a warning:
#
#     [FAIL] DigitalOcean directory detected.
#
# Any FAIL makes img_check `exit 1`, which fails the build — so with this left in
# place no snapshot can ever be produced. Their own 90-cleanup.sh does NOT remove
# it; the remedy in img_check's own output is this purge. DigitalOcean installs
# the agent per-droplet, so it must not be baked into a submitted image.
echo "=== purging droplet-agent ==="
DEBIAN_FRONTEND=noninteractive apt-get purge -y -q droplet-agent || true
# The package purge leaves empty directories behind on some base images, and the
# check tests for the directory, not the package.
rm -rf /opt/digitalocean

echo "=== cleanup.sh ==="
bash /tmp/marketplace-partners/scripts/90-cleanup.sh

echo "=== img_check.sh ==="
# img_check exits non-zero on any finding, which fails the Packer build — the
# snapshot is never created from a droplet that would be rejected at review.
bash "$IMG_CHECK"
