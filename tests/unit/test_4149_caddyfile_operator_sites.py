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


# ---------------------------------------------------------------------------
# Review of #4176 — every finding executed through the real start.sh
# ---------------------------------------------------------------------------

CADDY_ONLY = ("--provision", "--cloud", "digitalocean", "--caddy-only")
REFRESH = ("--provision", "--cloud", "digitalocean", "--refresh-ip", "--hosted", "--unattended")

# The apt package's own /etc/caddy/Caddyfile, as a first boot finds it.
PACKAGED_DEFAULT = """# The Caddyfile is an easy way to configure your Caddy web server.
:80 {
	# Set this path to your site's directory.
	root * /usr/share/caddy

	# Enable the static file server.
	file_server
}
"""


def test_the_packaged_default_is_replaced_by_content_not_by_caller(tmp_path):
    """First boot's file is recognised off the file itself, so no caller has
    to say "fresh" — and none can wave an operator's file through as fresh."""
    r = _install(tmp_path, *CADDY_ONLY, prior_caddyfile=PACKAGED_DEFAULT, env=OLD_WEB_IMAGE)
    assert r.proc.returncode == 0, r.proc.stderr[-2000:]
    assert not _refused(r) and "https://203.0.113.10 {" in r.caddyfile


def test_an_edited_packaged_default_is_an_operators_file(tmp_path):
    prior = PACKAGED_DEFAULT.replace("file_server", "reverse_proxy 127.0.0.1:3000")
    r = _install(tmp_path, *CADDY_ONLY, prior_caddyfile=prior, env=OLD_WEB_IMAGE)
    assert r.proc.returncode != 0 and _refused(r)
    assert r.caddyfile == prior


@pytest.mark.parametrize("prior", [
    # A site on a private address, in the `http://` shape Trinity never wrote.
    PRIOR_CADDYFILE + "\nhttp://10.0.0.5 {\n    reverse_proxy 127.0.0.1:3000\n}\n",
    # A matcher added inside Trinity's own `http://` block.
    PRIOR_CADDYFILE.replace("    redir https://{host}{uri} permanent",
                            "    @app host app.example.com\n    handle @app {\n"
                            "        reverse_proxy 127.0.0.1:3000\n    }\n"
                            "    redir https://{host}{uri} permanent"),
    # A global option added to Trinity's own global block.
    PRIOR_CADDYFILE.replace("    default_sni 203.0.113.10", "    email ops@example.com\n    default_sni 203.0.113.10"),
], ids=["http-ip-site", "matcher-in-http-block", "global-email"])
def test_a_legacy_file_with_any_line_trinity_never_rendered_is_refused(tmp_path, prior):
    r = _install(tmp_path, *CADDY_ONLY, prior_caddyfile=prior, env=OLD_WEB_IMAGE)
    assert r.proc.returncode != 0 and _refused(r), r.proc.stderr[-2000:]
    assert r.caddyfile == prior
    assert "systemctl restart caddy" not in r.calls


def test_the_caddy_only_render_refuses_an_operators_file(tmp_path):
    prior = PRIOR_CADDYFILE + OPERATOR_SITE
    r = _install(tmp_path, *CADDY_ONLY, prior_caddyfile=prior, env=OLD_WEB_IMAGE)
    assert r.proc.returncode != 0 and _refused(r)
    assert r.caddyfile == prior and "Caddy configuration applied" not in r.proc.stdout
    assert not (r.root / "etc-caddy" / "Caddyfile.new").exists()


def test_a_refused_refresh_changes_nothing_and_is_retried(tmp_path):
    """The refresh used to rewrite FRONTEND_URL, then have the Caddyfile
    refused: .env on the new IP, Caddy on the old one, the backend never
    recreated, repeated every tick. Now nothing is applied."""
    root = _scratch(tmp_path)
    (root / "etc-trinity").mkdir()
    (root / "etc-trinity" / "public-ip").write_text("198.51.100.1\n")
    before = (root / ".env").read_text().replace("https://203.0.113.10", "https://198.51.100.1")
    (root / ".env").write_text(before)
    prior = PRIOR_CADDYFILE.replace("203.0.113.10", "198.51.100.1") + OPERATOR_SITE
    r = _install(tmp_path, *REFRESH, root=root, prior_caddyfile=prior, managed=False,
                 env={**OLD_WEB_IMAGE, "METADATA_IP": "203.0.113.10"})
    assert r.proc.returncode != 0 and _refused(r)
    assert "203.0.113.10 was NOT applied" in r.proc.stderr
    assert r.dotenv == before
    assert r.caddyfile == prior
    assert "up -d --no-deps --no-build --pull never backend" not in r.calls
    assert (root / "etc-trinity" / "public-ip").read_text().strip() == "198.51.100.1"


@pytest.mark.parametrize("args", [
    ("--hosted", "--unattended", "--dry-run"),
    ("--provision", "--cloud", "digitalocean", "--refresh-ip", "--dry-run"),
], ids=["hosted", "refresh-ip"])
def test_dry_run_outside_caddy_only_is_refused_before_anything_runs(tmp_path, args):
    root = _scratch(tmp_path)
    before = (root / ".env").read_text()
    r = _install(tmp_path, *args, root=root, env={**OLD_WEB_IMAGE, "METADATA_IP": "203.0.113.99"})
    assert r.proc.returncode == 1
    assert "--dry-run is supported only with --provision --caddy-only" in r.proc.stderr
    assert r.dotenv == before
    assert "docker compose" not in r.calls and "docker pull" not in r.calls


def test_the_dropin_directory_is_traversable_under_a_strict_umask(tmp_path):
    """The 1-Click first boot runs under umask 077. A `mkdir`ed directory was
    0700, which the `caddy` user cannot traverse: the sites never loaded while
    root's validate passed."""
    import os
    old = os.umask(0o077)
    try:
        r = _install(tmp_path, *CADDY_ONLY, env=OLD_WEB_IMAGE)
    finally:
        os.umask(old)
    assert r.proc.returncode == 0, r.proc.stderr[-2000:]
    assert ((r.root / "etc-caddy" / "Caddyfile.d").stat().st_mode & 0o777) == 0o755


def test_an_unreadable_dropin_is_refused_before_caddy_restarts(tmp_path):
    root = _scratch(tmp_path)
    sites = root / "etc-caddy" / "Caddyfile.d"
    sites.mkdir()
    (sites / "app.caddy").write_text(OPERATOR_SITE)
    (sites / "app.caddy").chmod(0o600)
    r = _install(tmp_path, *CADDY_ONLY, root=root, env=OLD_WEB_IMAGE)
    assert r.proc.returncode != 0
    assert "caddy service user cannot read" in r.proc.stderr and "app.caddy" in r.proc.stderr
    assert r.caddyfile == PRIOR_CADDYFILE
    assert "systemctl restart caddy" not in r.calls and "systemctl reload caddy" not in r.calls


def test_a_new_dropin_is_loaded_with_a_reload(tmp_path):
    """The Caddyfile is unchanged, so nothing restarted Caddy and the new site
    was never read. A changed Caddyfile.d now reloads it; an unchanged one
    leaves Caddy alone."""
    first = _install(tmp_path, *CADDY_ONLY, env=OLD_WEB_IMAGE)
    assert first.proc.returncode == 0, first.proc.stderr[-2000:]
    (first.root / "calls.log").unlink()
    unchanged = _install(tmp_path, *CADDY_ONLY, env=OLD_WEB_IMAGE, root=first.root)
    assert "systemctl reload caddy" not in unchanged.calls
    assert "systemctl restart caddy" not in unchanged.calls
    (first.root / "calls.log").unlink(missing_ok=True)
    (first.root / "etc-caddy" / "Caddyfile.d" / "app.caddy").write_text(OPERATOR_SITE)
    added = _install(tmp_path, *CADDY_ONLY, env=OLD_WEB_IMAGE, root=first.root)
    assert added.proc.returncode == 0, added.proc.stderr[-2000:]
    assert "systemctl reload caddy" in added.calls
    assert "systemctl restart caddy" not in added.calls
