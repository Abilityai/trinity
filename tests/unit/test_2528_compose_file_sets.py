"""The compose files have exactly one supported shape each, and every path in (#2528).

Restarting a production host after two weeks off surfaced two failures back to
back, and both trace to the same root: **nothing said which compose files go
together**, so an operator had to guess, and both guesses were wrong.

1. ``quickstart.sh`` — a root-level installer that no document links to — ran a
   bare ``docker compose up -d``. That loads ``docker-compose.yml``, whose ``/data``
   is the named volume ``trinity-data``; the host's real database sat in the
   ``TRINITY_DATA_PATH`` bind mount that only ``docker-compose.prod.yml`` and
   ``docker-compose.hosted.yml`` use. The backend booted on an empty store, found
   zero agents, seeded a demo workspace into the wrong volume, and reported
   healthy. ``start.sh`` had refused exactly this crossing since #2280 (HOST-015);
   ``quickstart.sh`` was a forked copy that predated the guard and never grew it —
   the "second installer script" HOST-006 was written to forbid. It also never
   generated the Redis passwords #589 made mandatory, so it only *worked* on a
   host whose ``.env`` was already complete.

2. Corrected to "use the prod file", the operator stacked it on the base file
   (``-f docker-compose.yml -f docker-compose.prod.yml``). ``docker-compose.prod.yml``
   has been a complete standalone file since the initial commit — its
   ``security_opt`` / ``group_add`` entries are not duplicates of base, they ARE
   the prod hardening — so Compose ≥ 2.24 rejects the merge on exact-duplicate
   list items, and older Compose concatenates ``ports`` into two host mappings
   for one container and the frontend never joins its network. The hotfix that
   "removed the duplicates" removed hardening from the standalone prod file.

So this file pins three things:

* ``quickstart.sh`` is an alias for ``start.sh`` and owns no logic of its own.
* ``start.sh``'s data-switch guard names the bind-mount installs it protects
  (prod as well as hosted), and when BOTH stores exist it says which one the
  stack is about to use rather than staying silent.
* Every SUPPORTED compose file set renders — locally when Compose is available,
  and in CI unconditionally — and the two standalone files say they are
  standalone in their own headers.

The unsupported combination (base + prod) is deliberately NOT asserted to fail:
whether Compose rejects it or silently doubles ``ports`` depends on the Compose
version, which is precisely why it cannot be relied on as a guard.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_START = _ROOT / "scripts" / "deploy" / "start.sh"
_QUICKSTART = _ROOT / "quickstart.sh"
_PROD = _ROOT / "docker-compose.prod.yml"
_HOSTED = _ROOT / "docker-compose.hosted.yml"
_WORKFLOW = _ROOT / ".github" / "workflows" / "container-security.yml"

# The supported file sets, as the exact `-f` sequences an operator (or start.sh)
# passes. This list is the contract: docs/DEPLOYMENT.md documents it, the
# container-security workflow renders every entry, and the local test below does
# the same when a Compose CLI is present. Base + prod is absent ON PURPOSE.
SUPPORTED_FILE_SETS: tuple[tuple[str, ...], ...] = (
    ("docker-compose.yml",),
    ("docker-compose.yml", "docker-compose.override.example.yml"),
    ("docker-compose.prod.yml",),
    ("docker-compose.prod.yml", "docker-compose.prod.enterprise.yml"),
    ("docker-compose.hosted.yml",),
    ("docker-compose.hosted.yml", "docker-compose.override.example.yml"),
)


def _flags(file_set: tuple[str, ...]) -> str:
    return " ".join(f"-f {name}" for name in file_set)


_BASH = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")


# --------------------------------------------------------------------------- #
# quickstart.sh is an alias, not a second installer
# --------------------------------------------------------------------------- #


def _code_lines(path: Path) -> list[str]:
    return [
        line
        for line in path.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_quickstart_delegates_to_start_sh() -> None:
    """The whole fix for Bug 1. A second script that brings the stack up is a
    second place every guard has to be re-implemented — and the one that
    mattered (the #2280 data-switch refusal) never was."""
    code = "\n".join(_code_lines(_QUICKSTART))
    assert re.search(r"exec\s+(\./)?scripts/deploy/start\.sh", code), (
        "quickstart.sh must exec scripts/deploy/start.sh — it is an alias, not an installer"
    )
    assert "docker compose" not in code, (
        "quickstart.sh must not invoke compose itself; that is how it started the "
        "dev stack against a production bind mount (#2528)"
    )
    assert "openssl rand" not in code and "cut -d'='" not in code, (
        "quickstart.sh must not seed .env or parse it — start.sh owns both, with the "
        "Compose-faithful reader #2390 fixed"
    )


@_BASH
@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--defaults"], ["--unattended"]),
        ([], []),
        (["--hosted"], ["--hosted"]),
        (["--defaults", "--hosted"], ["--unattended", "--hosted"]),
    ],
)
def test_quickstart_maps_its_old_flag_and_passes_the_rest_through(
    tmp_path: Path, argv: list[str], expected: list[str]
) -> None:
    """`--defaults` was quickstart's own spelling of "don't prompt"; start.sh's
    is `--unattended`. Everything else (`--hosted`) reaches start.sh verbatim."""
    (tmp_path / "scripts" / "deploy").mkdir(parents=True)
    stub = tmp_path / "scripts" / "deploy" / "start.sh"
    stub.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n')
    stub.chmod(0o755)
    shutil.copy(_QUICKSTART, tmp_path / "quickstart.sh")
    proc = subprocess.run(
        ["bash", str(tmp_path / "quickstart.sh"), *argv],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == expected


# --------------------------------------------------------------------------- #
# start.sh: the data-switch guard covers prod, and speaks when both stores exist
# --------------------------------------------------------------------------- #


def _extract_function(func: str) -> str:
    text = _START.read_text()
    match = re.search(rf"^{func}\(\) \{{\n.*?^\}}$", text, re.MULTILINE | re.DOTALL)
    assert match, f"start.sh no longer defines `{func}()`"
    return match.group(0)


def _extract_guard_block() -> str:
    """The inline guard, from its banner comment to the base-image check that
    follows it. Extracted rather than re-typed so the test runs the shipped
    code."""
    text = _START.read_text()
    start = text.index("# --- Refuse a silent data switch")
    end = text.index("# Check base image before starting", start)
    return text[start:end]


def _run_guard(tmp_path: Path, *, hosted: bool, db_file: bool, dev_volume: bool) -> subprocess.CompletedProcess:
    """Execute the guard with a `docker` shim whose `volume inspect` answer is
    the only thing it consults, and a data dir that may or may not hold a DB."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "volume" ] && [ "$2" = "inspect" ]; then exit "${SHIM_VOLUME_RC:-1}"; fi\n'
        "exit 1\n"
    )
    docker.chmod(0o755)
    data_dir = tmp_path / "trinity-data"
    data_dir.mkdir(exist_ok=True)
    if db_file:
        (data_dir / "trinity.db").write_bytes(b"")
    script = (
        "set -e\n"
        + _extract_function("env_value")
        + "\n"
        + _extract_function("compose_project_name")
        + "\n"
        + f'HOSTED={"1" if hosted else "0"}\n'
        + f'TRINITY_DATA_PATH="{data_dir}"\n'
        + _extract_guard_block()
        + '\necho "GUARD_PASSED"\n'
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
            "SHIM_VOLUME_RC": "0" if dev_volume else "1",
        },
    )


@_BASH
def test_dev_stack_refusal_names_prod_as_well_as_hosted(tmp_path: Path) -> None:
    """The bind-mounted DB the dev stack cannot see is what BOTH
    docker-compose.prod.yml and docker-compose.hosted.yml write. The refusal
    used to say "installed with --hosted", which on a source-built production
    host is wrong, and its only remedy was `--hosted` — which would have pulled
    GHCR images onto a box that builds its own."""
    proc = _run_guard(tmp_path, hosted=False, db_file=True, dev_volume=False)
    assert proc.returncode == 1
    assert "GUARD_PASSED" not in proc.stdout
    assert "Refusing to start" in proc.stderr
    assert "docker-compose.prod.yml" in proc.stderr, "the prod file must be named as a source of the bind mount"
    assert "docker compose -f docker-compose.prod.yml up -d" in proc.stderr, (
        "a source-built production host needs the prod invocation as a remedy, not only --hosted"
    )
    assert "installed with --hosted, and" not in proc.stderr, (
        "the refusal must not assert the install was hosted — a prod source build produces the same state"
    )


@_BASH
def test_hosted_refusal_is_unchanged(tmp_path: Path) -> None:
    proc = _run_guard(tmp_path, hosted=True, db_file=False, dev_volume=True)
    assert proc.returncode == 1
    assert "dev-stack database that --hosted cannot see" in proc.stderr


@_BASH
@pytest.mark.parametrize("hosted", [False, True])
def test_both_stores_present_warns_and_names_the_one_in_use(tmp_path: Path, hosted: bool) -> None:
    """After a wrong-file start (Bug 1's actual outcome) both stores exist: the
    real DB in the bind mount and a freshly seeded one in the named volume.
    Neither refusal fires — each is written for a clean crossing — so a repeat
    of the same mistake was SILENT. Refusing here would block the copy-across
    remedy the refusals themselves print (it leaves both in place), so this
    warns, and says which store the stack is about to use."""
    proc = _run_guard(tmp_path, hosted=hosted, db_file=True, dev_volume=True)
    assert proc.returncode == 0, proc.stderr
    assert "GUARD_PASSED" in proc.stdout
    out = proc.stdout + proc.stderr
    assert "Two databases" in out
    if hosted:
        assert "will use" in out and "trinity.db" in out.split("will use", 1)[1].splitlines()[0]
    else:
        assert "will use" in out and "_trinity-data" in out.split("will use", 1)[1].splitlines()[0]


@_BASH
def test_clean_states_are_silent(tmp_path: Path) -> None:
    for hosted in (False, True):
        proc = _run_guard(tmp_path, hosted=hosted, db_file=False, dev_volume=False)
        assert proc.returncode == 0, proc.stderr
        assert "Two databases" not in proc.stdout + proc.stderr
        assert "Refusing" not in proc.stderr


def test_guard_conditions_from_2390_are_intact() -> None:
    """The #2390 test pins these strings; the generalisation must not move them."""
    text = _START.read_text()
    assert '[ "$HOSTED" = "1" ] && [ "$_hosted_db" = "0" ] && [ "$_dev_volume" = "1" ]' in text
    assert '[ "$HOSTED" != "1" ] && [ "$_hosted_db" = "1" ] && [ "$_dev_volume" = "0" ]' in text
    assert '[ "$_hosted_db" = "1" ] && [ "$_dev_volume" = "1" ]' in text


# --------------------------------------------------------------------------- #
# The standalone files say so, and every supported set renders
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", [_PROD, _HOSTED], ids=["prod", "hosted"])
def test_standalone_compose_files_say_they_are_standalone(path: Path) -> None:
    """The base+prod stack was a guess made in the absence of a statement. The
    statement now lives where the guess is made: the top of the file."""
    header = path.read_text().split("\nservices:", 1)[0]
    assert "NOT an overlay" in header, f"{path.name} must state it is a complete file, not an overlay"
    assert f"-f docker-compose.yml -f {path.name}" in header, (
        f"{path.name} must name the unsupported combination so a reader recognises it"
    )


def _compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(
            ["docker", "compose", "version"], capture_output=True, timeout=20
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _compose_available(), reason="docker compose CLI not available")
@pytest.mark.parametrize("file_set", SUPPORTED_FILE_SETS, ids=[" + ".join(s) for s in SUPPORTED_FILE_SETS])
def test_supported_file_set_renders(file_set: tuple[str, ...]) -> None:
    """`docker compose config` needs no daemon; it is the same validation the
    operator's `up -d` runs first. `--env-file /dev/null` keeps a developer's
    local .env out of the result, so a set that only renders because of a
    value on this machine still fails here."""
    args = ["docker", "compose", "--env-file", "/dev/null"]
    for name in file_set:
        args += ["-f", name]
    args += ["config", "-q"]
    proc = subprocess.run(
        args,
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **{k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG")},
            # The `${VAR:?}` render-blockers — random, never a literal that could
            # read as a default credential.
            "ADMIN_PASSWORD": secrets.token_urlsafe(18) + "Aa1!",
            "REDIS_PASSWORD": secrets.token_hex(24),
            "REDIS_BACKEND_PASSWORD": secrets.token_hex(24),
        },
    )
    assert proc.returncode == 0, f"{_flags(file_set)} does not render:\n{proc.stderr}"


def test_ci_renders_every_supported_file_set() -> None:
    """The local test above skips wherever Compose is absent, so CI is the
    guarantee: the container-security workflow renders each set by its exact
    `-f` sequence. Add a set here and this fails until the workflow renders it."""
    text = _WORKFLOW.read_text()
    assert "verify-compose-file-sets:" in text
    for file_set in SUPPORTED_FILE_SETS:
        assert _flags(file_set) in text, f"container-security.yml does not render `{_flags(file_set)}`"
    assert "-f docker-compose.yml -f docker-compose.prod.yml" not in text, (
        "base + prod is not a supported set; rendering it in CI would legitimise it"
    )
