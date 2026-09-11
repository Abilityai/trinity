"""Regression guards for the three DigitalOcean requirements the bundle missed.

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

3. **Submit-on-build ordering.** The `shell-local` post-processor reads the file
   the `manifest` post-processor writes. Packer runs sibling ``post-processor``
   blocks in PARALLEL and the members of one ``post-processors`` block as a
   CHAIN, so the difference between the two spellings is a race that would
   usually pass on a fast disk and fail on a release day.

Plus the no-op default, executed rather than pattern-matched (the #2522
precedent): a static rule only knows the spelling it was written against.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PACKER = _REPO_ROOT / "packer" / "digitalocean"
_PROVISION = _PACKER / "scripts" / "01-provision.sh"
_FIRSTBOOT = _PACKER / "files" / "opt" / "trinity-firstboot" / "firstboot.sh"
_TEMPLATE = _PACKER / "trinity.pkr.hcl"
_SUBMIT = _PACKER / "scripts" / "mp-submit.sh"
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
    heredoc = text[text.index("cat > /etc/caddy/Caddyfile") : text.index("\nCADDY\n")]
    assert "${_do_header}" in heredoc, "header variable is outside the Caddyfile heredoc"


def test_submit_is_chained_after_the_manifest_not_parallel_to_it() -> None:
    text = _TEMPLATE.read_text()
    assert "post-processors {" in text, (
        "the submit post-processor must live in a `post-processors` (plural) "
        "CHAIN block; sibling `post-processor` blocks run in parallel and would "
        "race the manifest file they read."
    )
    block_start = text.index("post-processors {")
    block = text[block_start:]
    assert block.index('post-processor "manifest"') < block.index(
        'post-processor "shell-local"'
    ), "manifest must be chained before the shell-local that reads manifest.json"


def test_submit_script_is_a_noop_without_an_app_id(tmp_path: Path) -> None:
    """Executed, not pattern-matched: a plain `packer build` must not submit."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("TRINITY_")}
    env.pop("DIGITALOCEAN_API_TOKEN", None)

    result = subprocess.run(
        ["bash", str(_SUBMIT)],
        cwd=tmp_path,  # no manifest.json here, and none should be needed
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        "mp-submit.sh must exit 0 when TRINITY_DO_APP_ID is unset — otherwise "
        f"every local `packer build` fails at the post-processor.\n{result.stderr}"
    )
    assert "not submitted" in result.stdout, result.stdout
