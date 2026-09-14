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

3. **A single quote in either secret breaks the droplet's first boot.** Both are
   interpolated into single-quoted shell assignments in the user-data. The
   password rules demand a special character and `'` is one, so
   `Tr0ub4dor's!Horse` passes this script's check AND the backend's, then writes
   `export ADMIN_PASSWORD='Tr0ub4dor's!Horse'` — a syntax error. The droplet is
   created and billing before it fails, the operator watches a 15-minute progress
   bar end in a timeout, and recovery is a rebuild.

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
    Checking only the two secrets left the image tag as a third such site."""
    heredoc = re.search(r"<<USERDATA\n(.*?)\nUSERDATA$", _script(), re.S | re.M)
    assert heredoc, "the user-data heredoc is gone"
    interpolated = re.findall(r"'\$\{([A-Za-z_]+)\}'", heredoc.group(1))
    assert {"ADMIN_PASSWORD_Q", "CLAUDE_SUBSCRIPTION_TOKEN_Q", "TRINITY_IMAGE_TAG_Q"} <= set(interpolated)
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
if [ "$1 $2 $3" = "compute droplet create" ]; then
    while [ $# -gt 0 ]; do
        if [ "$1" = "--user-data-file" ]; then
            cp "$2" "$CAPTURE"; printf '%s' "$2" > "$CAPTURE.path"
        fi
        shift
    done
fi
exit 0
"""


def _run_installer(tmp_path: Path, *, snap: bool, stdin: str, home: bool = True,
                   image_tag: str = "v0.9.5-rc2") -> subprocess.CompletedProcess:
    stub_dir = tmp_path / ("snapd/bin" if snap else "bin")
    stub_dir.mkdir(parents=True)
    stub = stub_dir / "doctl"
    stub.write_text(_STUB_DOCTL)
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
    return subprocess.run(
        ["bash", str(_SCRIPT)], input=stdin, env=env,
        capture_output=True, text=True, timeout=60,
    )


@pytest.mark.parametrize("snap", [False, True], ids=["package-doctl", "snap-doctl"])
@pytest.mark.parametrize(
    "password,token,tag",
    [
        ("CorrectHorse!7Battery", "sk-ant-oat01-abcDEF123", "v0.9.5-rc2"),
        ("Tr0ub4dor's!Horse", "sk-ant-oat01-it's-q'uoted", "v0.9.5-it's"),
        ("Sh$ell`tick!7Aa\\", "sk-ant-oat01-$(id)`x`", "v0.9.5-rc2"),
    ],
    ids=["plain", "apostrophes", "shell-metachars"],
)
def test_generated_user_data_parses_and_carries_each_value_intact(
    tmp_path: Path, snap: bool, password: str, token: str, tag: str,
) -> None:
    """The heredoc context is what matters: the token sits in `'…'` inside a
    `$(…)` inside double quotes, which only a shell parsing the real file tests."""
    proc = _run_installer(
        tmp_path, snap=snap, image_tag=tag,
        stdin=f"{password}\n{password}\n{token}\n\n\ny\n",
    )
    assert "no public IP yet" in proc.stderr, proc.stderr  # reached the end, via the stub
    user_data = tmp_path / "user-data.sh"
    assert user_data.is_file(), "doctl was never handed a user-data file"

    where = Path((tmp_path / "user-data.sh.path").read_text()).parent
    expected = tmp_path / ("home" if snap else "tmp")
    assert where == expected, f"user-data written to {where}, a {'snap' if snap else 'package'} doctl reads {expected}"
    assert not list(where.glob("trinity-user-data.*")), "the EXIT trap left the secrets file behind"

    syntax = subprocess.run(["bash", "-n", str(user_data)], capture_output=True, text=True)
    assert syntax.returncode == 0, f"first boot would die on a syntax error:\n{syntax.stderr}"

    # Evaluate the real lines in their real context, with curl/jq stubbed to
    # print what they were given instead of calling anything.
    text = user_data.read_text()
    exports = "\n".join(l for l in text.splitlines() if l.startswith("export "))
    block = re.search(r'^curl -fsS -X POST "\$API/api/subscriptions".*?>/dev/null$', text, re.S | re.M)
    assert block, "the subscription-registration call is gone from the user-data"
    probe = f"""
{exports}
jq() {{ while [ $# -gt 0 ]; do [ "$1" = --arg ] && {{ printf '%s' "$3"; return; }}; shift; done; }}
curl() {{ while [ $# -gt 0 ]; do [ "$1" = -d ] && {{ printf '%s' "$2" > "$OUT"; return; }}; shift; done; }}
API=http://stub AUTH=stub
{block.group(0)}
printf '%s\\n%s' "$ADMIN_PASSWORD" "$TRINITY_IMAGE_TAG"
"""
    out = subprocess.run(
        ["bash", "-c", probe], capture_output=True, text=True, timeout=30,
        env={"PATH": "/usr/bin:/bin", "OUT": str(tmp_path / "token.out")},
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout == f"{password}\n{tag}"
    assert (tmp_path / "token.out").read_text() == token


def test_a_snap_doctl_without_home_is_refused_before_any_question(tmp_path: Path) -> None:
    """Checks first, questions second: the operator must not type two secrets
    only to learn there is nowhere doctl can read the setup file from."""
    proc = _run_installer(tmp_path, snap=True, home=False, stdin="")
    assert proc.returncode == 1
    assert "installed as a snap but $HOME is not set" in proc.stderr, proc.stderr
    assert "password" not in proc.stderr.lower(), "it asked for the password before refusing"


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
