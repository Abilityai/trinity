"""Behavioural guard: where first boot's admin comes from (#2281, ent#580).

History. This file used to execute first boot's password GENERATOR — a
`tr -dc ... </dev/urandom | head -c 24` that died of SIGPIPE under the script's
own `set -o pipefail` on the very first droplet ever created (#2281). ent#580
deleted the generator outright: a one-click droplet now provisions NO admin, and
the first person to open it in a browser creates one at /setup. Nothing is
generated, so nothing is printed in the MOTD and nobody needs a terminal.

What is pinned now, by EXECUTING the real block (a grep only knows one
spelling, and the #2281 failure was a runtime signal `bash -n` passes):

* no user-data password → nothing exported but ADMIN_PASSWORD_SOURCE=browser,
  the claim signal start.sh persists (tests/unit/test_ent580_*);
* a user-data password → exported as ADMIN_PASSWORD, never recorded anywhere
  else, the one-shot file removed — the operator-supplied path, unchanged;
* the state file the MOTD reads holds only the source, never a password;
* all of it still runs clean under `set -euo pipefail`.
"""
from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_FIRSTBOOT = (
    _ROOT / "packer" / "digitalocean" / "files" / "opt" / "trinity-firstboot" / "firstboot.sh"
)

_BLOCK_RE = re.compile(
    r"# --- admin-source.*?\n(.*?)\n# --- end admin-source ---",
    re.DOTALL,
)


@pytest.fixture(scope="module")
def admin_block() -> str:
    m = _BLOCK_RE.search(_FIRSTBOOT.read_text())
    assert m, (
        "firstboot.sh no longer delimits its admin-source block with the "
        "`# --- admin-source ---` markers this test executes."
    )
    return m.group(1)


def _run(block: str, tmp_path: Path, supplied: str | None) -> dict:
    state = tmp_path / "etc-trinity"
    state.mkdir()
    pw_file = state / "admin-password"
    if supplied is not None:
        pw_file.write_text(supplied)
    script = (
        "set -euo pipefail\n"
        f'CRED_FILE="{state}/admin-credentials"\n'
        f'USER_SUPPLIED_PW="{pw_file}"\n'
        + block
        + '\nprintf "PW_SOURCE=%s\\n" "$PW_SOURCE"\n'
        # What start.sh inherits: the EXPORTED environment, nothing else.
        + 'env | grep -E "^ADMIN_PASSWORD(_SOURCE)?=" | sed "s/^/EXPORTED:/" || true\n'
    )
    r = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    assert r.returncode == 0, f"admin-source block failed under pipefail: {r.stderr!r}"
    lines = r.stdout.splitlines()
    cred = state / "admin-credentials"
    return {
        "source": next(l.split("=", 1)[1] for l in lines if l.startswith("PW_SOURCE=")),
        "exported": dict(
            l[len("EXPORTED:"):].split("=", 1) for l in lines if l.startswith("EXPORTED:")
        ),
        "cred": cred.read_text(),
        "cred_mode": stat.S_IMODE(cred.stat().st_mode),
        "pw_file_left": pw_file.exists(),
    }


def test_no_user_data_means_no_admin_and_the_browser_claims_it(admin_block, tmp_path):
    out = _run(admin_block, tmp_path, supplied=None)
    assert out["source"] == "browser"
    assert out["exported"] == {"ADMIN_PASSWORD_SOURCE": "browser"}, (
        "the claimable path must export the claim signal and NO password — a "
        "password here would pre-provision an admin and close /setup"
    )
    assert out["cred"] == "source=browser\n"
    assert out["cred_mode"] == 0o600


def test_a_blank_user_data_file_is_the_claim_path_not_an_empty_password(admin_block, tmp_path):
    """`-s` is true for a file holding only a newline; the password is not."""
    out = _run(admin_block, tmp_path, supplied="\r\n\n")
    assert out["source"] == "browser"
    assert out["exported"] == {"ADMIN_PASSWORD_SOURCE": "browser"}
    assert not out["pw_file_left"]


def test_user_data_password_is_the_pre_provisioned_path_unchanged(admin_block, tmp_path):
    out = _run(admin_block, tmp_path, supplied="Correct-Horse-Battery-9!\n")
    assert out["source"] == "user-data"
    assert out["exported"] == {"ADMIN_PASSWORD": "Correct-Horse-Battery-9!"}, (
        "the operator's password must reach start.sh (which writes it to .env), "
        "and the claim signal must NOT ride along with it"
    )
    assert out["cred"] == "source=user-data\n"
    assert "Correct-Horse" not in out["cred"], "the state file must never hold the password"
    assert not out["pw_file_left"], "the one-shot user-data file must be removed"


def test_first_boot_generates_nothing() -> None:
    """The generator is gone, not merely unused: nothing may mint a password."""
    code = "\n".join(
        line for line in _FIRSTBOOT.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    assert "/dev/urandom" not in code
    assert "openssl rand" not in code
    assert "password=%s" not in code, "the MOTD state file must not carry a password"


def test_the_script_really_does_set_pipefail() -> None:
    """Without this the pipefail premise of the behavioural tests is vacuous."""
    assert re.search(r"^set -euo pipefail$", _FIRSTBOOT.read_text(), re.MULTILINE), (
        "firstboot.sh no longer sets `-euo pipefail`; this test's premise is gone."
    )
