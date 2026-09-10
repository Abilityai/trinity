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


def test_secrets_are_shell_quoted_before_interpolation() -> None:
    text = _code()
    assert "export ADMIN_PASSWORD='${ADMIN_PASSWORD_Q}'" in text, (
        "the user-data must interpolate the QUOTED password; the raw one breaks "
        "first boot on any password containing a single quote"
    )
    assert "--arg t '${CLAUDE_SUBSCRIPTION_TOKEN_Q}'" in text, (
        "the subscription token must be quoted the same way"
    )


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
