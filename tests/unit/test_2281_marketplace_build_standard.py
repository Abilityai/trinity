"""Regression guards for the two DigitalOcean requirements the bundle missed.

Audited 2026-09-10 against `digitalocean/marketplace-partners@b708788` and
`digitalocean/droplet-1-clicks@master`, after DigitalOcean granted Vendor Portal
access and named that repo as the process of record.

Each guard pins a property whose failure mode is silent:

1. **Security updates.** Their `99-img-check.sh` scores a pending security update
   as ``[FAIL]``, not a warning, and any FAIL exits non-zero — so the build dies
   at the last provisioner, furthest from the cause, and reads as a flake. The
   bundle ran no upgrade at all; every green build was luck about what the base
   image happened to carry that day. Position is asserted, not just presence:
   an upgrade after the installs leaves the freshly installed packages unpatched
   and still fails the check.

2. **`X-DO-MARKETPLACE`.** Rule 10 of their build standard specifies this header
   on the reverse-proxy block and their own catalog apps ship it. Its absence
   breaks nothing at runtime, which is exactly why it survived review — it is
   visible only to a Marketplace reviewer.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PACKER = _REPO_ROOT / "packer" / "digitalocean"
_PROVISION = _PACKER / "scripts" / "01-provision.sh"
_FIRSTBOOT = _PACKER / "files" / "opt" / "trinity-firstboot" / "firstboot.sh"
# #2380 moved the Caddyfile out of firstboot.sh: `start.sh --provision --site-only`
# writes it now, for the marketplace image and the doc-driven install alike.
_START = _REPO_ROOT / "scripts" / "deploy" / "start.sh"


def test_build_installs_security_updates_before_anything_else() -> None:
    text = _PROVISION.read_text()

    upgrade = re.search(r"^\s*apt-get\s.*\\?\s*$", text, re.M)  # sanity: apt is used
    assert upgrade, "01-provision.sh no longer calls apt-get at all"

    assert "full-upgrade" in text, (
        "01-provision.sh runs no upgrade. img_check.sh scores a pending security "
        "update as [FAIL] and exits non-zero, so the build fails on any day Ubuntu "
        "has published one the base image does not carry."
    )

    upgrade_at = text.index("full-upgrade")
    first_install = re.search(r"^apt-get install\b", text, re.M)
    assert first_install, "01-provision.sh installs nothing — has it been rewritten?"
    assert upgrade_at < first_install.start(), (
        "full-upgrade must run BEFORE the first apt-get install; upgrading after "
        "leaves the packages this script installs unpatched."
    )


def test_caddyfile_carries_the_marketplace_header() -> None:
    text = _START.read_text()
    assert 'header X-DO-MARKETPLACE "trinity"' in text, (
        "Rule 10 of DigitalOcean's 1-Click build standard specifies "
        "X-DO-MARKETPLACE on the reverse-proxy block."
    )
    # The same `provision_site` serves the doc-driven install, which did not
    # originate from the catalog — so the header is keyed on the marketplace
    # provenance, and the heredoc carries the variable that holds it.
    assignment = text[text.index('_do_header=\'header X-DO-MARKETPLACE') - 400 : text.index('_do_header=\'header X-DO-MARKETPLACE')]
    assert 'provenance" = "do-marketplace"' in assignment, (
        "the header must be set only for the do-marketplace provenance"
    )
    heredoc = text[text.index("cat > /etc/caddy/Caddyfile.new") : text.index("\nCADDY\n")]
    assert "${_do_header}" in heredoc, "header variable is outside the Caddyfile heredoc"
