"""start.sh reads `.env` and derives the compose project name the way Compose does (#2390).

Review of #2390 found the dev→hosted data guard failing **open** on the directory
names most likely to be in use. It derived the compose project name with
``tr -cd '[:alnum:]'``, which strips ``_`` and ``-``; Compose *keeps* them. So in
a checkout named ``project_trinity`` (the layout CLAUDE.md documents),
``trinity-dev``, or a worktree like ``trinity-2280``, ``docker volume inspect``
looked up a name no volume has ever had, missed, and the guard silently passed —
producing exactly the empty-DB-plus-shared-Redis half-migration it was written to
prevent.

The same review found the four hand-rolled ``.env`` readers
(``grep ... | cut -d'=' -f2- | tr -d '[:space:]'``) disagreeing with Compose's own
dotenv parse in three ways: they kept surrounding quotes, swallowed an inline
``# comment`` into the value, and destroyed internal spaces. Because those values
are ``export``ed — and Compose gives the shell environment precedence over
``.env`` — a mis-parse there *overrode* a perfectly valid operator line and then
blamed them for it in the error message.

Both are one bug class: **a second, approximate implementation of a rule Compose
already owns.** So this file does not assert on the text of the fix; it EXECUTES
the two helpers under ``bash`` against the cases that broke, and pins the
absolute-path and both-directions properties the guard depends on. A static tail
guard fails the build if a third hand-rolled reader or project derivation
reappears.

Expected values were taken from ``docker compose config`` on this repo's Compose
version, not from reading compose-go:

    dir `project_trinity`   -> project `project_trinity`
    dir `proj_Trin-ity!x`   -> project `proj_trin-ityx`
    dir `--_Foo.Bar`        -> project `foobar`
    X="v0.9.0"              -> v0.9.0
    X=v0.9.0  # pinned      -> v0.9.0
    X=/srv/my data/dir      -> /srv/my data/dir
    X='has#hash'            -> has#hash

No docker daemon, no backend import — bash and the checked-in script only.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_START = _ROOT / "scripts" / "deploy" / "start.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is required to exercise start.sh helpers"
)


def _extract(func: str) -> str:
    """Pull one top-level shell function out of start.sh, verbatim."""
    text = _START.read_text()
    match = re.search(rf"^{func}\(\) \{{\n.*?^\}}$", text, re.MULTILINE | re.DOTALL)
    assert match, (
        f"start.sh no longer defines a top-level `{func}()`. It is the single "
        "implementation of a Compose-owned rule; if it moved, move this guard "
        "with it rather than deleting it."
    )
    return match.group(0)


@pytest.fixture(scope="module")
def helpers() -> str:
    return _extract("env_value") + "\n" + _extract("compose_project_name") + "\n"


def _run(helpers: str, cwd: Path, body: str, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(
        ["bash", "-c", helpers + body],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", **(env or {})},
    )
    assert proc.returncode == 0, f"helper invocation failed: {proc.stderr}"
    return proc.stdout


# --------------------------------------------------------------------------- #
# env_value: mirrors Compose's dotenv parse
# --------------------------------------------------------------------------- #

_DOTENV = """\
Q_VAL="v0.9.0"
C_VAL=v0.9.0  # pinned
S_VAL=/srv/my data/dir
H_VAL='has#hash'
E_VAL=
DUP=first
DUP=second
X_VAL=a=b=c
export EXP_VAL=exported
"""


@pytest.mark.parametrize(
    ("key", "expected", "why"),
    [
        ("Q_VAL", "v0.9.0", "surrounding quotes are Compose syntax, not part of the value"),
        ("C_VAL", "v0.9.0", "an inline ` # comment` is a comment, not part of the value"),
        ("S_VAL", "/srv/my data/dir", "internal spaces survive — a path may contain them"),
        ("H_VAL", "has#hash", "'#' inside quotes is literal, not a comment"),
        ("E_VAL", "", "an empty assignment reads as empty, not as missing"),
        ("DUP", "second", "last-wins, matching shell and Compose"),
        ("X_VAL", "a=b=c", "only the FIRST '=' separates key from value"),
        ("EXP_VAL", "exported", "Compose accepts an `export ` prefix"),
        ("MISSING_VAL", "", "an absent key reads as empty, never as an error"),
    ],
)
def test_env_value_matches_compose_dotenv(helpers, tmp_path, key, expected, why):
    (tmp_path / ".env").write_text(_DOTENV)
    got = _run(helpers, tmp_path, f'printf "%s" "$(env_value {key})"')
    assert got == expected, f"env_value {key}: {why}"


def test_env_value_tolerates_a_missing_env_file(helpers, tmp_path):
    """The port pre-flight runs before `.env` is seeded on a fresh install."""
    assert not (tmp_path / ".env").exists()
    assert _run(helpers, tmp_path, 'printf "%s" "$(env_value FRONTEND_PORT)"') == ""


# --------------------------------------------------------------------------- #
# compose_project_name: mirrors Compose's project-name normalisation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("directory", "expected"),
    [
        # The regression: '_' and '-' are KEPT. `tr -cd '[:alnum:]'` dropped them,
        # so the guard looked up a volume name that cannot exist.
        ("project_trinity", "project_trinity"),
        ("trinity-dev", "trinity-dev"),
        ("trinity-2280", "trinity-2280"),
        # Lowercased, and characters outside [a-z0-9_-] dropped.
        ("Trinity", "trinity"),
        ("proj_Trin-ity!x", "proj_trin-ityx"),
        # Leading '_' and '-' are trimmed after filtering.
        ("--_Foo.Bar", "foobar"),
    ],
)
def test_project_name_matches_compose(helpers, tmp_path, directory, expected):
    workdir = tmp_path / directory
    workdir.mkdir()
    assert _run(helpers, workdir, 'printf "%s" "$(compose_project_name)"') == expected


def test_project_name_honours_compose_project_name_from_env_file(helpers, tmp_path):
    workdir = tmp_path / "ignored_basename"
    workdir.mkdir()
    (workdir / ".env").write_text("COMPOSE_PROJECT_NAME=from_dotenv\n")
    got = _run(helpers, workdir, 'printf "%s" "$(compose_project_name)"')
    assert got == "from_dotenv", "COMPOSE_PROJECT_NAME in .env decides the real volume prefix"


def test_project_name_gives_the_shell_precedence_over_the_env_file(helpers, tmp_path):
    workdir = tmp_path / "ignored_basename"
    workdir.mkdir()
    (workdir / ".env").write_text("COMPOSE_PROJECT_NAME=from_dotenv\n")
    got = _run(
        helpers,
        workdir,
        'printf "%s" "$(compose_project_name)"',
        env={"COMPOSE_PROJECT_NAME": "from_shell"},
    )
    assert got == "from_shell", "shell over .env — Compose's own precedence"


# --------------------------------------------------------------------------- #
# The guard the project name feeds, and its reverse
# --------------------------------------------------------------------------- #


def test_data_guard_refuses_in_both_directions():
    """dev→hosted was guarded; hosted→dev is the likelier mistake and was not.

    An operator who installed with ``--hosted`` and later re-runs without the
    flag gets ``docker-compose.yml``, an empty ``trinity-data`` named volume and
    the same shared ``redis-data`` — the identical half-migrated state, with no
    warning at all.
    """
    text = _START.read_text()
    assert '[ "$HOSTED" = "1" ] && [ "$_hosted_db" = "0" ] && [ "$_dev_volume" = "1" ]' in text
    assert '[ "$HOSTED" != "1" ] && [ "$_hosted_db" = "1" ] && [ "$_dev_volume" = "0" ]' in text


def test_guard_copy_command_absolutises_the_data_path():
    """`"$(pwd)/${_data_path#./}"` on an absolute TRINITY_DATA_PATH emitted
    `/repo//srv/trinity-data` and would have copied the database somewhere
    nothing reads."""
    text = _START.read_text()
    assert 'case "$_data_path" in\n    /*) _data_abs="$_data_path" ;;' in text, (
        "an absolute TRINITY_DATA_PATH must be used as-is, never prefixed with $(pwd)"
    )
    assert '"$(pwd)/${_data_path#./}":/to' not in text


def test_port_preflight_probes_the_configured_frontend_port():
    """The warning's own advice is "set FRONTEND_PORT"; probing a hardcoded 80
    warned about a port Trinity no longer binds and stayed silent about the one
    it does."""
    text = _START.read_text()
    assert "for _pp in 80 8000 8080 6379" not in text
    assert 'for _pp in "$_preflight_frontend_port" 8000 8080 6379' in text


def test_hosted_platform_pull_failure_is_explained():
    """A bare `docker compose pull` died on `set -e` with a raw Compose error on
    the install least equipped to read one — while its two likeliest causes (an
    unpublished tag, a private GHCR package) name neither."""
    text = _START.read_text()
    assert 'if ! docker compose "${COMPOSE_FILES[@]}" pull; then' in text
    assert 'docker compose "${COMPOSE_FILES[@]}" pull\n' not in text.replace(
        'if ! docker compose "${COMPOSE_FILES[@]}" pull; then\n', ""
    )


def test_tunnel_profile_is_persisted_so_printed_commands_manage_it():
    """Compose acts only on services in the active profile set, so the summary's
    `stop` left `trinity-cloudflared` running and the tunnel live after the
    operator believed the stack was down."""
    text = _START.read_text()
    assert "persist_compose_profile() {" in text
    assert "persist_compose_profile tunnel" in text
    assert "COMPOSE_PROFILES" in text


# --------------------------------------------------------------------------- #
# Class guard: no third implementation of either rule
# --------------------------------------------------------------------------- #


def _code_lines() -> list[str]:
    """start.sh minus its comments.

    These guards must read the *script*, not the essay above each fix — every
    one of these comments quotes the broken form it replaced, and a naive scan
    flags the explanation as the offence.
    """
    return [
        line
        for line in _START.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_no_hand_rolled_env_reader_remains():
    """Every `.env` read goes through env_value(), which is the one place the
    Compose dotenv rules are written down. The hand-rolled shape is a `cut` on
    `.env`; env_value() itself uses none."""
    offenders = [
        line.strip()
        for line in _code_lines()
        if ".env" in line and "cut -d'='" in line
    ]
    assert not offenders, (
        "a hand-rolled `.env` reader is back:\n  "
        + "\n  ".join(offenders)
        + "\nUse env_value(): it strips quotes and inline comments and preserves "
        "internal spaces, which is what Compose does. A reader that disagrees is "
        "exported and therefore BEATS Compose's own parse."
    )


def test_no_hand_rolled_project_name_derivation_remains():
    code = "\n".join(_code_lines())
    assert "tr -cd '[:alnum:]'" not in code, (
        "`tr -cd '[:alnum:]'` strips the '_' and '-' Compose keeps — that is the "
        "#2390 guard-fails-open bug. Use compose_project_name()."
    )
    assert 'basename "$PWD")_redis-data' not in code, (
        "volume_exists() had a third, un-normalised spelling of the project name."
    )
