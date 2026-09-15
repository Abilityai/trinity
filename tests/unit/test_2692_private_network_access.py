"""#2692 — an instance on a VPN has a URL to browse.

Every site in the provisioned Caddyfile is matched by HOSTNAME: the droplet's
public IP, and the domain an admin saved. A tailnet address matches neither, the
certificate gate refuses it (correctly — that refusal is what stops the instance
being an open certificate requester), and no public authority could issue for
carrier-grade NAT space anyway. So an operator who moved onto a VPN and closed
80/443 had shell access and nothing to open in a browser.

`PRIVATE_NETWORK_CIDRS` serves those sources over plain HTTP, which costs
nothing: the VPN already encrypts the transport.

The matcher is the load-bearing part, so it is tested by RUNNING the shell
functions rather than grepping for them — `remote_ip` versus `host_regexp` is
the difference between "arrived from the private network" and "claims to have",
and a static check cannot tell those apart.
"""
from __future__ import annotations

import subprocess
import tempfile
import textwrap
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_START = _ROOT / "scripts" / "deploy" / "start.sh"


def _run_cidrs(value: str) -> tuple[str, str, int]:
    """Execute `provision_private_cidrs` with the rest of the script left out."""
    src = _START.read_text()
    start = src.index("provision_private_cidrs() {")
    end = src.index("\n}\n", start) + 3
    script = textwrap.dedent(src[start:end]) + '\nprovision_private_cidrs "$1"\n'
    # The value goes in as a POSITIONAL ARGUMENT, never interpolated into the
    # script text. Interpolating it makes the harness expand `$(...)` before the
    # function ever sees it, so the injection case would pass by testing the
    # test — which is exactly what happened on the first run of this file.
    proc = subprocess.run(
        ["bash", "-c", script, "bash", value], capture_output=True, text=True
    )
    return proc.stdout, proc.stderr, proc.returncode


# ---------------------------------------------------------------------------
# The matcher
# ---------------------------------------------------------------------------

def test_the_rule_matches_the_source_address_not_the_host_header() -> None:
    """A `Host` header is supplied by the caller. Matching on it would let
    anyone on the internet send `Host: 100.64.0.1` to port 80 and be served the
    login page in cleartext, having bypassed the HTTPS redirect. A source
    address cannot be forged into a completed TCP handshake."""
    body = _START.read_text()
    assert "@private remote_ip" in body, "the private-network rule is gone"
    assert "host_regexp" not in body, (
        "matching the Host header lets a public caller claim a private address"
    )


def test_tailscale_ranges_pass_through_unchanged() -> None:
    out, _, code = _run_cidrs("100.64.0.0/10 fd7a:115c:a1e0::/48")
    assert code == 0
    assert out == "100.64.0.0/10 fd7a:115c:a1e0::/48"


def test_the_whole_internet_is_refused() -> None:
    """`0.0.0.0/0` would serve the UI in cleartext to anyone, which is the
    opposite of what this variable is for. Refuse rather than render it."""
    for everything in ("0.0.0.0/0", "::/0", "100.64.0.0/10 0.0.0.0/0"):
        out, err, code = _run_cidrs(everything)
        assert code != 0, f"{everything!r} was accepted"
        assert "refusing" in err
        assert out == ""


def test_junk_is_dropped_rather_than_rendered_into_the_config() -> None:
    """A malformed Caddyfile does not degrade the web server, it stops it — and
    on a box reached only over the network that is indistinguishable from
    bricking it. Anything that is not an address range is dropped with a warning
    and the valid entries still apply."""
    out, err, code = _run_cidrs("100.64.0.0/10 not-a-cidr 10.0.0.0/8")
    assert code == 0
    assert out == "100.64.0.0/10 10.0.0.0/8"
    assert "ignoring" in err

    # A shell metacharacter must never reach the generated config, and must not
    # be executed on the way there.
    marker = Path("/tmp/trinity-2692-injection-probe")
    marker.unlink(missing_ok=True)
    out, _, code = _run_cidrs(f"$(touch {marker})/8")
    assert code == 0
    assert out == "", f"an unexpanded substitution was rendered into the config: {out!r}"
    assert not marker.exists(), "the value was executed rather than treated as text"


def test_unset_renders_no_rule_at_all() -> None:
    """The default must behave exactly as before: every HTTP request redirected
    to HTTPS, nothing served in the clear."""
    out, _, code = _run_cidrs("")
    assert code == 0
    assert out == ""


# ---------------------------------------------------------------------------
# The rendered file
# ---------------------------------------------------------------------------

def _render(cidrs: str, provenance: str = "do-marketplace") -> str:
    """Run the real renderer against a temp path, with the host stubbed out.

    The private-network block is built in one heredoc and interpolated into
    another. That nesting either works or produces a file that stops the web
    server, and nothing else in the suite would notice.
    """
    src = _START.read_text()
    # `provision_caddyfile` cannot be sliced on the first column-0 `}` — the
    # Caddyfile heredoc it writes is full of them. Take it up to the next
    # function definition instead.
    cidr_fn = src[src.index("provision_private_cidrs() {") : src.index("\n}\n", src.index("provision_private_cidrs() {")) + 3]
    caddy_fn = src[src.index("provision_caddyfile() {") : src.index("\nprovision_site() {")]
    body = cidr_fn + "\n" + caddy_fn
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "Caddyfile"
        harness = (
            body.replace("/etc/caddy/Caddyfile", str(out))
            + "\n"
            + "env_value() { printf '%s' \"$CIDRS\"; }\n"
            + "systemctl() { :; }\n"
            + "caddy() { :; }\n"
            + 'provision_caddyfile "203.0.113.10" "$PROV"\n'
        )
        proc = subprocess.run(
            ["bash", "-c", harness],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "CIDRS": cidrs, "PROV": provenance},
        )
        assert proc.returncode == 0, proc.stderr
        return out.read_text()


def test_the_private_block_renders_as_valid_looking_config() -> None:
    rendered = _render("100.64.0.0/10 fd7a:115c:a1e0::/48")
    assert "@private remote_ip 100.64.0.0/10 fd7a:115c:a1e0::/48" in rendered
    assert "handle @private {" in rendered
    # The fallback still redirects everyone else, inside the same site block.
    assert "redir https://{host}{uri} permanent" in rendered
    # Braces balance — the nested heredoc is where that would break.
    assert rendered.count("{") == rendered.count("}"), rendered


def test_an_unset_variable_renders_the_original_redirect_only() -> None:
    rendered = _render("")
    assert "@private" not in rendered
    assert "remote_ip" not in rendered
    assert "http:// {\n    redir https://{host}{uri} permanent\n}" in rendered
    assert rendered.count("{") == rendered.count("}"), rendered


def test_the_marketplace_header_is_keyed_on_provenance_not_the_cloud() -> None:
    """Pre-existing behaviour that moved into the extracted renderer."""
    assert 'header X-DO-MARKETPLACE "trinity"' in _render("", provenance="do-marketplace")
    assert "X-DO-MARKETPLACE" not in _render("", provenance="do-script")


# ---------------------------------------------------------------------------
# How it reaches a running instance
# ---------------------------------------------------------------------------

def test_the_config_can_be_re_rendered_without_redoing_the_site_phase() -> None:
    """The site phase also rewrites FRONTEND_URL and TRINITY_INSTALL_SOURCE, so
    re-running it to pick up one variable would silently re-stamp a marketplace
    droplet's provenance as a doc-driven install. `--caddy-only` exists so that
    cannot happen."""
    body = _START.read_text()
    assert "--caddy-only)" in body
    caddy_phase = body[body.index('if [ "$PROVISION_PHASE" = "caddy" ]'):]
    caddy_phase = caddy_phase[: caddy_phase.index("exit 0")]
    assert "provision_caddyfile" in caddy_phase
    for redone in ("set_env_key FRONTEND_URL", "set_env_key TRINITY_INSTALL_SOURCE", "provision_machine"):
        assert redone not in caddy_phase, f"the caddy phase redoes {redone}"
    # It reads provenance back rather than recomputing a default.
    assert "env_value TRINITY_INSTALL_SOURCE" in caddy_phase


def test_the_generated_config_is_validated_before_caddy_is_restarted() -> None:
    body = _START.read_text()
    renderer = body[body.index("provision_caddyfile() {"):]
    renderer = renderer[: renderer.index("\nprovision_site() {")]
    validate = renderer.index("caddy validate")
    restart = renderer.index("systemctl restart caddy")
    assert validate < restart, "Caddy is restarted before the config is checked"
    assert "leaving the running config alone" in renderer


def test_a_failed_validate_leaves_the_previous_caddyfile_on_disk() -> None:
    """Caddy keeps a rejected config only in memory. If the invalid file has
    already replaced the live one, the next reboot takes the site down — so the
    render must land beside the live file and only move over it once valid.
    `1.2.3.4/99` passes the charset filter and fails at validate, which makes
    this reachable from `.env`."""
    src = _START.read_text()
    cidr_fn = src[src.index("provision_private_cidrs() {") : src.index("\n}\n", src.index("provision_private_cidrs() {")) + 3]
    caddy_fn = src[src.index("provision_caddyfile() {") : src.index("\nprovision_site() {")]
    with tempfile.TemporaryDirectory() as tmp:
        live = Path(tmp) / "Caddyfile"
        live.write_text("previous working config\n")
        for validate_rc, expect_rc in ((1, 1), (0, 0)):
            harness = (
                (cidr_fn + "\n" + caddy_fn).replace("/etc/caddy/Caddyfile", str(live))
                + "\n"
                + "env_value() { printf '%s' \"$CIDRS\"; }\n"
                + "systemctl() { :; }\n"
                + f"caddy() {{ return {validate_rc}; }}\n"
                + 'provision_caddyfile "203.0.113.10" "do-marketplace"\n'
            )
            proc = subprocess.run(
                ["bash", "-c", harness],
                capture_output=True,
                text=True,
                env={"PATH": "/usr/bin:/bin", "CIDRS": "1.2.3.4/99"},
            )
            assert proc.returncode == expect_rc, proc.stderr
            assert not Path(f"{live}.new").exists(), "the rejected render was left behind"
            if validate_rc:
                assert live.read_text() == "previous working config\n", "an invalid render replaced the live file"
            else:
                assert "remote_ip 1.2.3.4/99" in live.read_text(), "a valid render was not installed"


def test_the_variable_is_documented_where_an_operator_will_set_it() -> None:
    env_example = (_ROOT / ".env.example").read_text()
    assert "PRIVATE_NETWORK_CIDRS=" in env_example
    # The value a Tailscale user needs, so nobody has to look it up.
    assert "100.64.0.0/10" in env_example
    # And how to apply it, since a plain restart does not re-render the config.
    assert "--caddy-only" in env_example
