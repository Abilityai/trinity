"""`start.sh --provision --cloud aws`, the instance-ID claim file, and the
public-IP refresh (#3004, PROV-019).

Executed, not grepped, wherever the logic branches: each case lifts the real
functions out of start.sh and runs them under bash with `curl`, `chown`,
`systemctl` and `sleep` stubbed on PATH, so the IMDSv2 exchange, the claim
file's once-per-instance rule and the refresh's no-op path are what actually
ran. Pure stdlib; no docker, no network, no root.
"""
from __future__ import annotations

import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_START = _ROOT / "scripts" / "deploy" / "start.sh"
_ENV_FILE_SH = _ROOT / "scripts" / "deploy" / "env-file.sh"
_SYS_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


def _extract(func: str) -> str:
    m = re.search(rf"^{func}\(\) \{{\n.*?^\}}$", _START.read_text(), re.MULTILINE | re.DOTALL)
    assert m, f"start.sh no longer defines a top-level `{func}()`"
    return m.group(0)


# A fake IMDS behind a fake curl. The token is only honoured on a PUT to
# /latest/api/token carrying the TTL header; every other read needs it, which
# is what an instance with IMDSv2 required enforces.
_FAKE_CURL = r"""#!/bin/bash
echo "$*" >> "$CURL_LOG"
url="" method=GET hdr=""
while [ $# -gt 0 ]; do
    case "$1" in
        -X) method="$2"; shift ;;
        -H) hdr="$hdr|$2"; shift ;;
        http*) url="$1" ;;
    esac
    shift
done
case "$url" in
    */latest/api/token)
        [ "$method" = PUT ] || exit 22
        case "$hdr" in *X-aws-ec2-metadata-token-ttl-seconds:*) ;; *) exit 22 ;; esac
        [ -n "${IMDS_UP:-}" ] || exit 7
        printf 'tok123'; exit 0 ;;
    */latest/meta-data/*)
        [ -n "${IMDS_UP:-}" ] || exit 7
        case "$hdr" in *"X-aws-ec2-metadata-token: tok123"*) ;; *) exit 22 ;; esac
        case "$url" in
            */public-ipv4) [ -n "${PUBLIC_IP:-}" ] || exit 22; printf '%s' "$PUBLIC_IP" ;;
            */instance-id) [ -n "${INSTANCE_ID:-}" ] || exit 22; printf '%s' "$INSTANCE_ID" ;;
            *) exit 22 ;;
        esac
        exit 0 ;;
esac
# Anything else (the TLS poll against https://<ip>/) succeeds.
exit 0
"""


def _bin(tmp_path: Path) -> Path:
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    (b / "curl").write_text(_FAKE_CURL)
    for name, body in {
        "chown": 'echo "chown $*" >> "$CALL_LOG"\n',
        "systemctl": 'echo "systemctl $*" >> "$CALL_LOG"\n',
        "sleep": ":\n",
    }.items():
        (b / name).write_text("#!/bin/bash\n" + body)
    for f in b.iterdir():
        f.chmod(0o755)
    return b


def _run(tmp_path: Path, body: str, funcs: list[str], env: dict[str, str] | None = None,
         state: Path | None = None) -> subprocess.CompletedProcess:
    state = state or (tmp_path / "etc-trinity")
    units = tmp_path / "units"
    units.mkdir(exist_ok=True)
    src = "\n".join(_extract(f) for f in funcs)
    src = src.replace("/etc/trinity", str(state)).replace("/etc/systemd/system", str(units))
    script = "\n".join([
        "set -e",
        f". '{_ENV_FILE_SH}'",
        _extract("env_value"),
        'provision_die() { echo "DIE: $1" >&2; exit 1; }',
        'provision_caddyfile() { echo "caddyfile $1 $2" >> "$CALL_LOG"; }',
        src,
        body,
    ])
    full_env = {
        "PATH": f"{_bin(tmp_path)}:{_SYS_PATH}",
        "CURL_LOG": str(tmp_path / "curl.log"),
        "CALL_LOG": str(tmp_path / "calls.log"),
        "IMDS_UP": "1",
        "PUBLIC_IP": "203.0.113.20",
        "INSTANCE_ID": "i-0abc1234def567890",
    }
    full_env.update(env or {})
    full_env = {k: v for k, v in full_env.items() if v is not None}
    return subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True,
                          text=True, env=full_env)


def _log(tmp_path: Path, name: str) -> str:
    p = tmp_path / name
    return p.read_text() if p.exists() else ""


_IMDS = ["provision_aws_imds", "provision_metadata_ip"]


# ---------------------------------------------------------------------------
# IMDSv2
# ---------------------------------------------------------------------------

def test_aws_public_ip_is_read_with_an_imdsv2_token(tmp_path):
    r = _run(tmp_path, "PROVISION_CLOUD=aws; provision_metadata_ip", _IMDS)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "203.0.113.20"
    calls = _log(tmp_path, "curl.log").splitlines()
    assert "-X PUT" in calls[0] and "api/token" in calls[0], "the token must be fetched first"
    assert "X-aws-ec2-metadata-token: tok123" in calls[1] and "public-ipv4" in calls[1]


def test_no_public_ipv4_reads_empty_and_the_site_phase_says_why(tmp_path):
    r = _run(tmp_path, 'PROVISION_CLOUD=aws; ip="$(provision_public_ip)" || exit 3; echo "IP=$ip"',
             _IMDS + ["provision_public_ip"], {"PUBLIC_IP": None})
    assert r.returncode == 3
    assert "no public IPv4" in r.stderr and "Elastic IP" in r.stderr


def test_the_aws_guard_is_the_metadata_service_not_the_public_ip(tmp_path):
    """An instance in a private subnet is still an EC2 instance; the guard must
    let it through so the site phase can name the real problem."""
    r = _run(tmp_path, "PROVISION_CLOUD=aws; provision_on_cloud && echo ON",
             _IMDS + ["provision_on_cloud"], {"PUBLIC_IP": None})
    assert r.stdout.strip() == "ON", r.stderr


def test_the_aws_guard_refuses_a_machine_with_no_metadata_service(tmp_path):
    r = _run(tmp_path, "PROVISION_CLOUD=aws; provision_on_cloud && echo ON || echo OFF",
             _IMDS + ["provision_on_cloud"], {"IMDS_UP": None})
    assert r.stdout.strip() == "OFF"


def test_aws_default_provenance_is_aws_script(tmp_path):
    r = _run(tmp_path, "PROVISION_CLOUD=aws; provision_default_provenance",
             ["provision_default_provenance"])
    assert r.stdout == "aws-script\n"


def test_the_cloud_allowlist_accepts_aws() -> None:
    body = _START.read_text()
    assert re.search(r"^\s*digitalocean\|aws\) ;;$", body, re.MULTILINE), "--cloud aws is not accepted"
    assert "supported: digitalocean, aws" in body


# ---------------------------------------------------------------------------
# Which admin path a --cloud aws install takes
# ---------------------------------------------------------------------------

def _admin_source(tmp_path, cloud: str, dotenv: str = "", env: dict | None = None) -> str:
    (tmp_path / ".env").write_text(dotenv)
    r = _run(tmp_path, f'PROVISION_CLOUD={cloud}; provision_default_admin_source; '
             'echo "SRC=${ADMIN_PASSWORD_SOURCE:-}"', ["provision_default_admin_source"], env)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip().split("SRC=", 1)[1]


def test_an_aws_install_with_no_password_takes_the_instance_id_claim(tmp_path):
    assert _admin_source(tmp_path, "aws", "ADMIN_PASSWORD=\n") == "instance-id"


@pytest.mark.parametrize("dotenv,env", [
    ("ADMIN_PASSWORD=from-dotenv-123\n", {}),
    ("", {"ADMIN_PASSWORD": "from-env-12345"}),
])
def test_a_supplied_password_is_never_overridden(tmp_path, dotenv, env):
    assert _admin_source(tmp_path, "aws", dotenv, env) == ""


def test_an_explicit_source_wins(tmp_path):
    assert _admin_source(tmp_path, "aws", "", {"ADMIN_PASSWORD_SOURCE": "browser"}) == "browser"
    assert _admin_source(tmp_path, "aws", "ADMIN_PASSWORD_SOURCE=browser\n") == ""


def test_digitalocean_is_unchanged(tmp_path):
    assert _admin_source(tmp_path, "digitalocean") == ""


# ---------------------------------------------------------------------------
# The claim file
# ---------------------------------------------------------------------------

_CLAIM = _IMDS + ["provision_setup_claim"]


def _claim(tmp_path, dotenv="", env=None, cloud="aws"):
    (tmp_path / ".env").write_text(dotenv)
    e = {"ADMIN_PASSWORD_SOURCE": "instance-id"}
    e.update(env or {})
    return _run(tmp_path, f"PROVISION_CLOUD={cloud}; provision_setup_claim", _CLAIM, e)


def test_the_claim_file_holds_the_instance_id_owner_only(tmp_path):
    r = _claim(tmp_path)
    assert r.returncode == 0, r.stderr
    f = tmp_path / "trinity-data" / "setup-claim"
    assert f.read_text() == "i-0abc1234def567890\n"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert re.search(r"chown -h 1000:1000 \S*setup-claim", _log(tmp_path, "calls.log"))
    assert not list((tmp_path / "trinity-data").glob("*.tmp*")), "the temp file was left behind"


def test_a_planted_symlink_at_the_temp_path_is_not_followed(tmp_path):
    """The data dir is uid-1000-owned, so the backend could pre-plant
    `setup-claim.tmp -> /etc/something`. Root must replace the link, never write,
    chmod or chown through it."""
    d = tmp_path / "trinity-data"
    d.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("untouched\n")
    victim.chmod(0o644)
    (d / "setup-claim.tmp").symlink_to(victim)
    r = _claim(tmp_path)
    assert r.returncode == 0, r.stderr
    assert victim.read_text() == "untouched\n"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644
    assert str(victim) not in _log(tmp_path, "calls.log"), "chown followed the link"
    f = d / "setup-claim"
    assert not f.is_symlink() and f.read_text() == "i-0abc1234def567890\n"


def test_the_claim_file_honours_trinity_data_path(tmp_path):
    r = _claim(tmp_path, "TRINITY_DATA_PATH=./srv-data\n")
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "srv-data" / "setup-claim").exists()


def test_an_existing_claim_file_is_never_rewritten(tmp_path):
    d = tmp_path / "trinity-data"
    d.mkdir()
    (d / "setup-claim").write_text("i-00000000000000000\n")
    assert _claim(tmp_path).returncode == 0
    assert (d / "setup-claim").read_text() == "i-00000000000000000\n"


def test_a_rerun_after_the_claim_path_recorded_itself_writes_nothing(tmp_path):
    """The backend deletes the file once the admin exists. A later
    `start.sh --provision` must not hand the credential back."""
    assert _claim(tmp_path, "ADMIN_PASSWORD_SOURCE=instance-id\n").returncode == 0
    assert not (tmp_path / "trinity-data" / "setup-claim").exists()


def test_no_claim_unless_the_source_was_exported(tmp_path):
    (tmp_path / ".env").write_text("")
    r = _run(tmp_path, "PROVISION_CLOUD=aws; provision_setup_claim", _CLAIM)
    assert r.returncode == 0
    assert not (tmp_path / "trinity-data").exists()


def test_the_instance_id_claim_is_aws_only(tmp_path):
    r = _claim(tmp_path, cloud="digitalocean")
    assert r.returncode != 0 and "DIE:" in r.stderr


def test_an_unreadable_instance_id_stops_the_install(tmp_path):
    """Fail closed: a blank claim would leave an instance-id install with no
    way in, or (worse, depending on the backend) with any value accepted."""
    r = _claim(tmp_path, env={"INSTANCE_ID": None})
    assert r.returncode != 0 and "DIE:" in r.stderr
    assert not (tmp_path / "trinity-data" / "setup-claim").exists()


# ---------------------------------------------------------------------------
# The public-IP refresh
# ---------------------------------------------------------------------------

_REFRESH = _IMDS + ["provision_apply_ip", "provision_refresh_target", "provision_refresh_apply"]
_DOTENV = "TRINITY_INSTALL_SOURCE=aws-marketplace\nFRONTEND_URL=https://198.51.100.1\n"


def _docker_stub(tmp_path, rc: int = 0) -> None:
    b = _bin(tmp_path)
    (b / "docker").write_text(f'#!/bin/bash\necho "docker $*" >> "$CALL_LOG"\nexit {rc}\n')
    (b / "docker").chmod(0o755)


def _target(tmp_path, old: str | None, env=None):
    state = tmp_path / "etc-trinity"
    state.mkdir(exist_ok=True)
    if old is not None:
        (state / "public-ip").write_text(old + "\n")
    return _run(tmp_path, "PROVISION_CLOUD=aws; provision_refresh_target", _REFRESH, env, state)


def test_an_unchanged_address_is_a_silent_no_op(tmp_path):
    r = _target(tmp_path, "203.0.113.20")
    assert r.returncode == 1 and r.stdout == "" and r.stderr == ""


def test_a_metadata_miss_is_a_silent_no_op(tmp_path):
    r = _target(tmp_path, "198.51.100.1", env={"PUBLIC_IP": None})
    assert r.returncode == 1 and r.stdout == "" and r.stderr == ""


def test_a_changed_address_is_reported_for_apply(tmp_path):
    r = _target(tmp_path, "198.51.100.1")
    assert r.returncode == 0 and r.stdout == "203.0.113.20"


def _apply(tmp_path, dotenv=_DOTENV, docker_rc=0, caddy_rc=0, hosted=True):
    state = tmp_path / "etc-trinity"
    state.mkdir(exist_ok=True)
    (state / "public-ip").write_text("198.51.100.1\n")
    (tmp_path / ".env").write_text(dotenv)
    _docker_stub(tmp_path, docker_rc)
    files = "(-f docker-compose.hosted.yml)" if hosted else "()"
    body = (
        f"COMPOSE_FILES={files}\n"
        f'provision_caddyfile() {{ echo "caddyfile $1 $2" >> "$CALL_LOG"; return {caddy_rc}; }}\n'
        "PROVISION_CLOUD=aws; provision_refresh_apply 198.51.100.1 203.0.113.20\n"
        "echo DONE\n"
    )
    r = _run(tmp_path, body, _REFRESH, None, state)
    return r, state


def test_a_new_address_is_applied_and_only_the_backend_is_recreated(tmp_path):
    r, state = _apply(tmp_path)
    assert r.returncode == 0 and "DONE" in r.stdout, r.stderr
    dot = (tmp_path / ".env").read_text()
    assert "FRONTEND_URL=https://203.0.113.20" in dot
    assert "TRINITY_INSTALL_SOURCE=aws-marketplace" in dot, "provenance must never be re-stamped"
    calls = _log(tmp_path, "calls.log")
    assert "caddyfile 203.0.113.20 aws-marketplace" in calls
    # Never the install: no pull, no image-tag resolution, no other service.
    assert "docker compose -f docker-compose.hosted.yml up -d --no-deps --no-build --pull never backend" in calls
    assert "pull" not in calls.replace("--pull never", "")
    assert (state / "public-ip").read_text().strip() == "203.0.113.20"
    assert (state / "tls-status").read_text().strip() == "ok"


def test_a_source_built_install_uses_the_default_compose_files(tmp_path):
    r, _ = _apply(tmp_path, hosted=False)
    assert r.returncode == 0, r.stderr
    assert "docker compose up -d --no-deps --no-build --pull never backend" in _log(tmp_path, "calls.log")


def test_an_operator_domain_in_frontend_url_is_kept(tmp_path):
    dot = "TRINITY_INSTALL_SOURCE=aws-script\nFRONTEND_URL=https://trinity.example.com\n"
    r, state = _apply(tmp_path, dotenv=dot)
    assert r.returncode == 0, r.stderr
    assert "FRONTEND_URL=https://trinity.example.com" in (tmp_path / ".env").read_text()
    assert (state / "public-ip").read_text().strip() == "203.0.113.20"


@pytest.mark.parametrize("docker_rc,caddy_rc", [(1, 0), (0, 1)])
def test_a_failed_apply_leaves_the_old_address_recorded_so_the_next_tick_retries(tmp_path, docker_rc, caddy_rc):
    r, state = _apply(tmp_path, docker_rc=docker_rc, caddy_rc=caddy_rc)
    assert r.returncode != 0 and "DONE" not in r.stdout
    assert (state / "public-ip").read_text().strip() == "198.51.100.1"


@pytest.mark.parametrize("hosted", ["0", "1"])
def test_the_refresh_timer_repeats_the_install_mode_and_logs_quietly(tmp_path, hosted):
    r = _run(tmp_path, f"PROVISION_CLOUD=aws; HOSTED={hosted}; provision_refresh_units",
             ["provision_refresh_units"])
    assert r.returncode == 0, r.stderr
    svc = (tmp_path / "units" / "trinity-ip-refresh.service").read_text()
    timer = (tmp_path / "units" / "trinity-ip-refresh.timer").read_text()
    assert "--provision --cloud aws --refresh-ip" in svc
    assert ("--hosted" in svc) == (hosted == "1")
    # Output at notice, anything below dropped: the service manager's own
    # Starting/Finished lines every five minutes stay out of the journal, while
    # a real change or a failure (the script is silent otherwise) stays in.
    assert "SyslogLevel=notice" in svc and "LogLevelMax=notice" in svc
    assert "OnBootSec=1min" in timer and "OnUnitActiveSec=5min" in timer
    assert "WantedBy=timers.target" in timer
    assert "enable --now trinity-ip-refresh.timer" in _log(tmp_path, "calls.log")


def test_the_site_phase_wires_all_of_it_in_order() -> None:
    site = _extract("provision_site")
    order = [site.index(s) for s in (
        "provision_public_ip", "provision_default_admin_source", "provision_setup_claim",
        "provision_apply_ip", "> /etc/trinity/public-ip", "provision_refresh_units",
    )]
    assert order == sorted(order), "provision_site calls its steps out of order"


def test_the_refresh_phase_and_the_lock_exist() -> None:
    body = _START.read_text()
    assert "--refresh-ip)   PROVISION_PHASE=refresh ;;" in body
    assert "exec 9>/run/trinity-provision.lock" in body
    # The timer never queues behind another run; a person gets told why it waits.
    assert "flock -n 9 || exit 0" in body
    assert "Waiting for another provisioning run to finish" in body


def test_the_refresh_never_falls_through_into_the_install() -> None:
    body = _START.read_text()
    at = body.rindex('if [ "$PROVISION_PHASE" = "refresh" ]; then', 0,
                     body.index('_new_ip="$(provision_refresh_target)"'))
    block = body[at:]
    block = block[: block.index("\n    fi\n")]
    assert "provision_refresh_apply" in block and re.search(r"^\s*exit 0$", block, re.M)
    # Detection is the only step allowed under `||` (which disables set -e);
    # the writes run as plain statements.
    assert not re.search(r"provision_refresh_apply[^\n]*\|\|", block)


def test_the_refresh_skips_the_banner() -> None:
    body = _START.read_text()
    banner = body.index('echo "Trinity Agent Platform - Starting"')
    assert '"--refresh-ip" ] && _quiet_banner=1' in body[:banner]


# ---------------------------------------------------------------------------
# Clients that send no SNI get the IP certificate
# ---------------------------------------------------------------------------

_DOMAIN_SITE = "https://*.*, https://*.*.*, https://*.*.*.*, https://*.*.*.*.*, https://*.*.*.*.*.* {"


def _rendered_caddyfile(tmp_path) -> str:
    src = _START.read_text()
    cidr_fn = _extract("provision_private_cidrs")
    caddy_fn = src[src.index("provision_caddyfile() {"): src.index("\nprovision_site() {")]
    out = tmp_path / "Caddyfile"
    harness = (
        (cidr_fn + "\n" + caddy_fn).replace("/etc/caddy/Caddyfile", str(out))
        + "\nenv_value() { :; }\nsystemctl() { :; }\ncaddy() { :; }\n"
        + 'provision_caddyfile "203.0.113.10" "aws-marketplace"\n'
    )
    r = subprocess.run(["bash", "-c", harness], capture_output=True, text=True,
                       env={"PATH": _SYS_PATH})
    assert r.returncode == 0, r.stderr
    return out.read_text()


def test_a_client_without_sni_is_served_the_ip_certificate(tmp_path):
    """curl and browsers send no SNI to an IP address. Caddy picks the TLS
    automation policy from the RAW ServerName before `default_sni` is applied
    (caddytls connpolicy.go: getConfigForName(hello.ServerName)), so an empty
    name landed on the subject-less on-demand policy, which asked the backend
    about the IP and was refused: `tlsv1 alert internal error` on EC2, where the
    public IP is not on the interface. Two changes together fix it, and each
    alone was reproduced failing in caddy 2.11.4: `default_sni <ip>`, and the
    on-demand site's subjects are hostname wildcards, which an empty name
    cannot match."""
    c = _rendered_caddyfile(tmp_path)
    glob = c[: c.index("\n}\n")]
    assert "default_sni 203.0.113.10" in glob
    assert _DOMAIN_SITE in c.splitlines(), "the on-demand site must not be a subject-less catch-all"
    assert not re.search(r"^https:// \{", c, re.M)
    site = c[c.index(_DOMAIN_SITE):]
    site = site[: site.index("\n}\n")]
    assert re.search(r"tls \{\s*on_demand\s*\}", site)
    assert "https://203.0.113.10 {" in c, "the IP site is unchanged"
