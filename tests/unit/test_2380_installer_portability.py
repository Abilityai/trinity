"""Guards for the DigitalOcean installer's two host-portability traps (#2380).

`trinity-do-create.sh` runs on the OPERATOR's laptop, not on a server, so it is
the one script in this repo with no controlled environment. Two defects found
QA'ing it on Linux, neither visible on the author's macOS:

1. **`mktemp -t NAME`.** BSD/macOS takes a bare prefix; GNU coreutils requires
   the trailing X's and dies `too few X's in template`. Fixed on the branch tip;
   pinned here so it cannot regress to the `-t` spelling.

2. **A snap-installed doctl has a PRIVATE /tmp.** On most Linux distributions
   doctl comes from snap, whose mount namespace means a file written to the real
   /tmp is not there when doctl opens it — it fails on a file that demonstrably
   exists. The file goes under $HOME instead, which snap's `home` interface can
   read.

And one this QA pass found, which is not a portability bug at all but was in the
same lines:

3. **A single quote in an interpolated value breaks the droplet's first boot.**
   The image tag is interpolated into a single-quoted shell assignment in the
   user-data, so a `'` in it is a syntax error. The droplet is created and
   billing before it fails, and recovery is a rebuild.

The round-trip test is EXECUTED rather than pattern-matched: it runs the script's
own quoting through a shell and compares to the input, so it fails for any
breakage rather than only the spelling that was wrong.

The installer itself is executed too, against a stub `doctl`, which is how the
fourth one surfaced: under `set -u`, macOS's bash 3.2 calls an empty array
unbound, so an account with no SSH keys died at "Creating the droplet...".
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "deploy" / "trinity-do-create.sh"


def _script() -> str:
    return _SCRIPT.read_text()


def _code() -> str:
    """The script with comment lines removed.

    The comments deliberately name the broken spellings in order to explain
    them, so a guard that greps the raw file flags its own documentation.
    """
    return "\n".join(
        line for line in _SCRIPT.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )


def test_mktemp_uses_a_full_template_not_bare_dash_t() -> None:
    text = _code()
    assert not re.search(r"mktemp\s+-t\s+[A-Za-z]", text), (
        "`mktemp -t <bare prefix>` works on macOS and fails on GNU coreutils "
        "with 'too few X's in template'."
    )
    assert "XXXXXX" in text, "the mktemp template must carry the X's GNU requires"


def test_snap_doctl_gets_the_file_somewhere_it_can_read() -> None:
    text = _code()
    assert "snap" in text, "the snap-private-/tmp case must still be handled"
    assert 'USER_DATA_DIR="${HOME:-}"' in text or 'USER_DATA_DIR="$HOME"' in text, (
        "a snap doctl cannot see the real /tmp; the file belongs under $HOME"
    )


def test_every_single_quoted_interpolation_in_the_user_data_is_quoted() -> None:
    """The rule, not two spellings of it: every '${VAR}' in the heredoc is a _Q.

    A raw value in single quotes breaks first boot on any value containing `'`.
    """
    heredoc = re.search(r"<<USERDATA\n(.*?)\nUSERDATA$", _script(), re.S | re.M)
    assert heredoc, "the user-data heredoc is gone"
    interpolated = re.findall(r"'\$\{([A-Za-z_]+)\}'", heredoc.group(1))
    # The tag is the only operator value in the user-data (#3799): a password
    # under any other name would show up here.
    assert set(interpolated) == {"TRINITY_IMAGE_TAG_Q"}, interpolated
    raw = [v for v in interpolated if not v.endswith("_Q")]
    assert not raw, f"unquoted value(s) inside single quotes in the user-data: {raw}"


# ---------------------------------------------------------------------------
# The installer, executed. The greps above pin spellings; these run the real
# script against a stub `doctl` and check the file it would hand DigitalOcean.
# ---------------------------------------------------------------------------
_STUB_DOCTL = """#!/bin/bash
# `account get` succeeds, there are no SSH keys, `droplet create` records the
# user-data file it was given, and `droplet list` has no IP, so the script
# stops at "no public IP yet" instead of polling for 15 minutes.
# STUB_IP makes `droplet list` print an address; STUB_CREATE_RC / STUB_LIST_RC
# make those calls fail after the create has been accepted.
if [ "$1 $2 $3" = "compute droplet create" ]; then
    while [ $# -gt 0 ]; do
        if [ "$1" = "--user-data-file" ]; then
            cp "$2" "$CAPTURE"; printf '%s' "$2" > "$CAPTURE.path"
        fi
        shift
    done
    exit "${STUB_CREATE_RC:-0}"
fi
if [ "$1 $2 $3" = "compute droplet list" ]; then
    [ -n "${STUB_IP:-}" ] && echo "$STUB_IP"
    exit "${STUB_LIST_RC:-0}"
fi
exit 0
"""

# `curl` answers with STUB_CURL_RC. `date` returns the start time for the
# deadline and the first loop check, then a time past the 15-minute deadline,
# so the poll loop runs exactly one round.
_STUB_CURL = '#!/bin/bash\nexit "${STUB_CURL_RC:-0}"\n'
_STUB_DATE = """#!/bin/bash
echo x >> "$CAPTURE.date"
if [ "$(wc -l < "$CAPTURE.date")" -le 2 ]; then echo 1000; else echo 999999; fi
"""
_STUB_SLEEP = "#!/bin/bash\nexit 0\n"


def _run_installer(tmp_path: Path, *, snap: bool, stdin: str, home: bool = True,
                   image_tag: str = "v0.9.5-rc2",
                   env_extra: dict | None = None) -> subprocess.CompletedProcess:
    stub_dir = tmp_path / ("snapd/bin" if snap else "bin")
    stub_dir.mkdir(parents=True)
    for name, body in (("doctl", _STUB_DOCTL), ("curl", _STUB_CURL),
                       ("date", _STUB_DATE), ("sleep", _STUB_SLEEP)):
        stub = stub_dir / name
        stub.write_text(body)
        stub.chmod(0o755)
    for d in ("home", "tmp"):
        (tmp_path / d).mkdir()
    env = {
        "PATH": f"{stub_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "TMPDIR": str(tmp_path / "tmp"),
        "CAPTURE": str(tmp_path / "user-data.sh"),
        "TRINITY_IMAGE_TAG": image_tag,
    }
    if home:
        env["HOME"] = str(tmp_path / "home")
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", str(_SCRIPT)], input=stdin, env=env,
        capture_output=True, text=True, timeout=60,
    )


_ANSWERS = "\n\ny\n"  # default region, default name, confirm


@pytest.mark.parametrize("snap", [False, True], ids=["package-doctl", "snap-doctl"])
@pytest.mark.parametrize(
    "tag",
    ["v0.9.5-rc2", "v0.9.5-it's", "v0.9.5-$x`y`\\"],
    ids=["plain", "apostrophes", "shell-metachars"],
)
def test_generated_user_data_parses_and_carries_each_value_intact(
    tmp_path: Path, snap: bool, tag: str,
) -> None:
    """The heredoc context is what matters, which only a shell parsing the real
    file tests."""
    proc = _run_installer(tmp_path, snap=snap, image_tag=tag, stdin=_ANSWERS)
    assert "no public IP yet" in proc.stderr, proc.stderr  # reached the end, via the stub
    user_data = tmp_path / "user-data.sh"
    assert user_data.is_file(), "doctl was never handed a user-data file"

    where = Path((tmp_path / "user-data.sh.path").read_text()).parent
    expected = tmp_path / ("home" if snap else "tmp")
    assert where == expected, f"user-data written to {where}, a {'snap' if snap else 'package'} doctl reads {expected}"
    assert not list(where.glob("trinity-user-data.*")), "the EXIT trap left the user-data file behind"

    syntax = subprocess.run(["bash", "-n", str(user_data)], capture_output=True, text=True)
    assert syntax.returncode == 0, f"first boot would die on a syntax error:\n{syntax.stderr}"

    # Evaluate the real export lines in a shell and compare to the input.
    text = user_data.read_text()
    exports = "\n".join(l for l in text.splitlines() if l.startswith("export "))
    probe = f"""
{exports}
printf '%s\\n%s' "$ADMIN_PASSWORD_SOURCE" "$TRINITY_IMAGE_TAG"
"""
    out = subprocess.run(
        ["bash", "-c", probe], capture_output=True, text=True, timeout=30,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout == f"browser\n{tag}"


def test_the_installer_asks_for_no_claude_credential(tmp_path: Path) -> None:
    """Connecting Claude belongs to the onboarding overlay's blocking step, which
    checks the credential with Anthropic before saving it (trinity-enterprise#582).
    The installer takes a region and a name, and the droplet's first boot
    registers nothing: two answers plus the confirmation reach the end."""
    proc = _run_installer(tmp_path, snap=False, stdin=_ANSWERS)
    assert "no public IP yet" in proc.stderr, proc.stderr
    assert "setup-token" not in proc.stderr and "sk-ant-" not in proc.stderr, proc.stderr
    user_data = (tmp_path / "user-data.sh").read_text()
    assert "/api/subscriptions" not in user_data, "first boot still registers a Claude subscription"


def test_a_snap_doctl_without_home_is_refused_before_any_question(tmp_path: Path) -> None:
    """Checks first, questions second: the operator must not answer questions
    only to learn there is nowhere doctl can read the setup file from."""
    proc = _run_installer(tmp_path, snap=True, home=False, stdin="")
    assert proc.returncode == 1
    assert "installed as a snap but $HOME is not set" in proc.stderr, proc.stderr
    assert "region" not in proc.stderr.lower(), "it asked a question before refusing"


@pytest.mark.parametrize(
    "secret",
    [
        "CorrectHorse!7Battery",
        "Tr0ub4dor's!Horse",
        "a'b'c'd'e'f'1A!xyz",
        "Sh$ell`tick!7Aa",
        "Back\\slash!7Aa",
        "Uni¢ode!7Aaaa£",
    ],
)
def test_shquote_round_trips_through_a_shell(secret: str) -> None:
    """Run the script's own _shquote, then let a shell parse the result back."""
    shquote = re.search(r"^_shquote\(\).*$", _script(), re.M)
    assert shquote, "_shquote helper is gone — the quoting fix has been removed"

    program = f"""
    {shquote.group(0)}
    Q="$(_shquote "$1")"
    printf '%s' "$(eval "printf '%s' '$Q'")"
    """
    out = subprocess.run(
        ["bash", "-c", program, "_", secret],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout == secret, f"round-trip changed the secret: {out.stdout!r}"


def test_no_admin_password_reaches_the_droplets_user_data(tmp_path: Path) -> None:
    """#3799: user-data outlives first boot. cloud-init rewrites user-data.txt on
    every boot, pickles it into obj.pkl, and the metadata service serves it for
    the droplet's life, so no shred can keep a password in it from leaking. The
    installer asks for none, and first boot leaves the admin to /setup."""
    proc = _run_installer(tmp_path, snap=False, stdin=_ANSWERS,
                          env_extra={"ADMIN_PASSWORD": "Sentinel-3799-pw"})
    assert "no public IP yet" in proc.stderr, proc.stderr
    assert "choose a password" not in proc.stderr.lower(), "the installer still asks for a password"
    # The droplet exists and boots with no admin, so this exit also says to claim it.
    assert "create your admin account" in proc.stderr, proc.stderr
    user_data = (tmp_path / "user-data.sh").read_text()
    assert "ADMIN_PASSWORD=" not in user_data, "an admin password is in the user-data"
    assert "Sentinel-3799-pw" not in user_data, "the operator's ADMIN_PASSWORD reached the user-data"
    assert "export ADMIN_PASSWORD_SOURCE=browser" in user_data


# Every exit after the droplet exists must tell the operator to claim it: the
# droplet boots with no admin, and the first visitor to /setup becomes admin.
@pytest.mark.parametrize(
    "env_extra, rc",
    [
        ({"STUB_IP": "203.0.113.7", "STUB_CURL_RC": "0"}, 0),
        ({"STUB_IP": "203.0.113.7", "STUB_CURL_RC": "7"}, 1),
        ({"STUB_CREATE_RC": "1"}, 1),
        ({"STUB_LIST_RC": "1"}, 1),
    ],
    ids=["ready", "timeout", "create-wait-fails", "list-fails"],
)
def test_every_exit_after_create_says_to_claim_the_droplet(
    tmp_path: Path, env_extra: dict, rc: int
) -> None:
    proc = _run_installer(tmp_path, snap=False, stdin=_ANSWERS, env_extra=env_extra)
    assert proc.returncode == rc, proc.stderr
    assert proc.stderr.count("create your admin account") == 1, proc.stderr
    if "STUB_IP" in env_extra:  # the script's own warning, naming the address
        assert "may have been created" not in proc.stderr, proc.stderr
