"""#4149 — `start.sh --hosted` never drops a site an operator added to Caddy.

The upgrade re-renders /etc/caddy/Caddyfile on a host Trinity provisioned
(trinity-enterprise#873). An operator who had added a site block for their own
hostname lost it on the next upgrade, and the hostname stopped answering until
the file was restored from a backup. Now:

* the render imports /etc/caddy/Caddyfile.d/*.caddy, the documented home for an
  operator's own sites, and never writes there;
* a live Caddyfile this script did not write — or wrote and someone has since
  edited — is refused, with the running config left serving, unless
  --caddy-overwrite is passed (which keeps the old file as a backup);
* --dry-run prints the render and changes nothing.

The installer is EXECUTED with the host stubbed, via the harness in
test_ent873_caddy_upgrade.py.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Sibling import (the unit dir is a package, not implicitly on sys.path).
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ent873_caddy_upgrade import PRIOR_CADDYFILE, _BASH, _START, _install, _scratch

pytestmark = [pytest.mark.unit, pytest.mark.skipif(_BASH is None, reason="bash required")]

OPERATOR_SITE = """
app.example.com {
    reverse_proxy 127.0.0.1:3000
}
"""
UPGRADE = ("--hosted", "--unattended")
OLD_WEB_IMAGE = {"DOCKER_PORT_OUT": ""}   # the ordinary listener; no probe needed


def _refused(r) -> bool:
    return "has been edited since Trinity wrote it" in r.proc.stderr


def test_a_legacy_file_with_an_operator_site_is_kept_and_the_upgrade_finishes(tmp_path):
    prior = PRIOR_CADDYFILE + OPERATOR_SITE
    r = _install(tmp_path, *UPGRADE, prior_caddyfile=prior, env=OLD_WEB_IMAGE)
    assert r.proc.returncode == 0, r.proc.stderr[-2000:]
    assert r.caddyfile == prior
    assert "systemctl restart caddy" not in r.calls
    assert _refused(r)
    assert "Caddyfile.d" in r.proc.stderr and "--caddy-overwrite" in r.proc.stderr
    assert "previous configuration is still serving" in r.proc.stderr


def test_a_legacy_file_with_only_trinitys_blocks_is_replaced_and_recorded(tmp_path):
    r = _install(tmp_path, *UPGRADE, env=OLD_WEB_IMAGE)
    assert r.proc.returncode == 0, r.proc.stderr[-2000:]
    assert not _refused(r)
    assert "import " in r.caddyfile and "Caddyfile.d/*.caddy" in r.caddyfile
    assert (r.root / "etc-caddy" / "Caddyfile.trinity-sha256").read_text().strip()
    assert "systemctl restart caddy" in r.calls


@pytest.mark.parametrize("legacy_head", ["https:// {", "https://*.*, https://*.*.*, https://*.*.*.*, "
                                         "https://*.*.*.*.*, https://*.*.*.*.*.* {"])
def test_every_site_shape_trinity_ever_rendered_reads_as_its_own(tmp_path, legacy_head):
    prior = PRIOR_CADDYFILE + f"\n{legacy_head}\n    tls {{\n        on_demand\n    }}\n}}\n"
    r = _install(tmp_path, *UPGRADE, prior_caddyfile=prior, env=OLD_WEB_IMAGE)
    assert r.proc.returncode == 0 and not _refused(r), r.proc.stderr[-2000:]
    assert r.caddyfile != prior


def test_an_edit_after_trinity_wrote_the_file_is_refused(tmp_path):
    first = _install(tmp_path, *UPGRADE, env=OLD_WEB_IMAGE)
    assert first.proc.returncode == 0
    live = first.root / "etc-caddy" / "Caddyfile"
    # Even an edit inside Trinity's own blocks: the checksum, not a heuristic.
    edited = live.read_text().replace("encode gzip", "encode zstd gzip", 1)
    live.write_text(edited)
    (first.root / "calls.log").unlink()
    again = _install(tmp_path, *UPGRADE, env=OLD_WEB_IMAGE, root=first.root)
    assert again.proc.returncode == 0
    assert _refused(again) and live.read_text() == edited
    assert "systemctl restart caddy" not in again.calls


def test_caddy_overwrite_replaces_it_and_keeps_a_backup(tmp_path):
    prior = PRIOR_CADDYFILE + OPERATOR_SITE
    r = _install(tmp_path, *UPGRADE, "--caddy-overwrite", prior_caddyfile=prior, env=OLD_WEB_IMAGE)
    assert r.proc.returncode == 0, r.proc.stderr[-2000:]
    assert "app.example.com" not in r.caddyfile and "Caddyfile.d/*.caddy" in r.caddyfile
    backups = list((r.root / "etc-caddy").glob("Caddyfile.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == prior
    assert "systemctl restart caddy" in r.calls


def test_the_operators_include_directory_is_imported_and_never_touched(tmp_path):
    root = _scratch(tmp_path)
    sites = root / "etc-caddy" / "Caddyfile.d"
    sites.mkdir()
    (sites / "app.caddy").write_text(OPERATOR_SITE)
    first = _install(tmp_path, *UPGRADE, env=OLD_WEB_IMAGE, root=root)
    again = _install(tmp_path, *UPGRADE, env=OLD_WEB_IMAGE, root=root)
    assert first.proc.returncode == 0 and again.proc.returncode == 0
    assert not _refused(again)
    assert (sites / "app.caddy").read_text() == OPERATOR_SITE
    assert sorted(p.name for p in sites.iterdir()) == ["app.caddy"]
    # The import is the file's last top-level line, after every Trinity site.
    assert first.caddyfile.rstrip().endswith(f"import {sites}/*.caddy")


def test_dry_run_prints_the_render_and_writes_nothing(tmp_path):
    prior = PRIOR_CADDYFILE + OPERATOR_SITE
    r = _install(tmp_path, "--provision", "--cloud", "digitalocean", "--caddy-only", "--dry-run",
                 prior_caddyfile=prior, env=OLD_WEB_IMAGE)
    assert r.proc.returncode == 0, r.proc.stderr[-2000:]
    assert "as it would be written (dry run" in r.proc.stdout
    assert "https://203.0.113.10 {" in r.proc.stdout and "Caddyfile.d/*.caddy" in r.proc.stdout
    assert "Would REFUSE" in r.proc.stdout
    assert r.caddyfile == prior
    assert not (r.root / "etc-caddy" / "Caddyfile.new").exists()
    assert not (r.root / "etc-caddy" / "Caddyfile.trinity-sha256").exists()
    assert "systemctl restart caddy" not in r.calls and "Caddy reconfigured" not in r.proc.stdout


def test_first_boot_replaces_the_packaged_default():
    """The site phase passes `fresh`: the apt package's own Caddyfile (`:80`
    and a file_server) is not an operator's, and first boot must replace it."""
    src = _START.read_text()
    body = src[src.index("provision_apply_ip() {"):]
    body = body[: body.index("\n}\n")]
    assert '[ -z "$old" ] && _fresh=fresh' in body
    assert 'provision_caddyfile "$ip" "$provenance" "$_fresh"' in body
