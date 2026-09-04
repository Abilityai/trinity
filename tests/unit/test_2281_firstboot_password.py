"""Behavioural guard: first boot's password generator survives `set -o pipefail` (#2281).

Bug: the generator was

    ADMIN_PASSWORD="$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)"

inside a script that opens with `set -euo pipefail`. `head` closes the pipe after
24 bytes, `tr` dies of SIGPIPE against an endless source, `pipefail` surfaces that
non-zero status out of the command substitution, and `set -e` ends the script.

It killed first boot on the very first droplet ever created from the snapshot —
three lines in, before anything but the header had been written to the log,
leaving a box with no Trinity, no certificate and no admin password. It could
never have worked on any droplet; nobody had run one.

The guard EXECUTES the real block rather than pattern-matching it. A static rule
would only know the one spelling that was wrong, and the failure mode is a
runtime signal, not a syntax error — `bash -n` passes on the broken version.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_FIRSTBOOT = (
    _ROOT / "packer" / "digitalocean" / "files" / "opt" / "trinity-firstboot" / "firstboot.sh"
)

_BLOCK_RE = re.compile(
    r"# --- password-generation.*?\n(.*?)\s*# --- end password-generation ---",
    re.DOTALL,
)


@pytest.fixture(scope="module")
def generator_block() -> str:
    m = _BLOCK_RE.search(_FIRSTBOOT.read_text())
    assert m, (
        "firstboot.sh no longer delimits its password generation with the "
        "`# --- password-generation ---` markers this test executes."
    )
    return m.group(1)


def test_generator_succeeds_under_pipefail(generator_block: str) -> None:
    """The exact failure: a non-zero status escaping the command substitution."""
    script = "set -euo pipefail\n" + generator_block + '\nprintf "%s" "$ADMIN_PASSWORD"\n'
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, (
        "first boot's password generation exits non-zero under `set -euo pipefail`, "
        "which ends the script before Trinity is ever started. "
        f"stderr: {r.stderr.strip()!r}"
    )
    assert len(r.stdout) == 24, f"expected a 24-character password, got {len(r.stdout)}"
    assert r.stdout.isalnum(), f"password is not alphanumeric: {r.stdout!r}"


def test_generator_does_not_pipe_an_endless_source_into_head(generator_block: str) -> None:
    """Named separately so the reason survives a future rewrite.

    Passing the behavioural test above is what matters, but a reader changing this
    block should see the specific shape that must not come back.
    """
    # Strip comments first — the block deliberately quotes the broken form in
    # prose so a future reader knows what must not come back.
    code = "\n".join(
        line for line in generator_block.splitlines() if not line.lstrip().startswith("#")
    )
    collapsed = " ".join(code.split())
    assert not re.search(r"/dev/urandom\s*\|\s*head", collapsed), (
        "piping /dev/urandom straight into `head` re-creates the SIGPIPE abort."
    )
    assert not re.search(r"tr\s+-dc[^|]*</dev/urandom\s*\|", collapsed), (
        "`tr -dc ... </dev/urandom |` puts an endless producer upstream of a "
        "reader that closes early — the original bug."
    )


def test_the_script_really_does_set_pipefail() -> None:
    """Without this the guard above is vacuous."""
    assert re.search(r"^set -euo pipefail$", _FIRSTBOOT.read_text(), re.MULTILINE), (
        "firstboot.sh no longer sets `-euo pipefail`; this test's premise is gone."
    )
