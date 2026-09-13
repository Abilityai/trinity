"""ent#615 — the platform PAT is no longer readable inside an agent container.

Trinity persisted every agent's git remote as
``<scheme>://oauth2:<PAT>@<host>/<org>/<repo>.git``. That put the fleet-wide
GitHub token in two places:

* **``.git/config`` on the workspace volume** — at rest, readable by the
  agent's own ``Bash`` tool for the life of the container. The larger surface,
  and the one an argv-only fix leaves untouched.
* **git child argv** — git expands the stored URL into ``git-remote-https``'s
  argv on EVERY fetch and push, including the 60s sync-health poll, so it was
  in the process table ~2x/min even for an agent that ran no git itself.

The argv half is the one with a platform-side sink: ``ps`` -> the agent
server's orphan-sweep reaped-cmdline logging -> Vector -> the host log files ->
the logs API -> **another agent's LLM context**. trinity-enterprise#292 closed
the last hop of that chain and was rated P0; this closes the cause.

**What these tests do NOT claim.** A prompt-injected agent can still read its
own credential: ``GITHUB_PAT`` stays in the container env and in
``/home/developer/.env``, and invoking the helper directly returns it. Root
ownership of the helper is integrity, not confidentiality. The structural fix
is a broker outside the container — trinity-enterprise#558 (AAuth).

Most of what follows EXECUTES the real shell — the helper script and the sweep
are shell, and a Python test that asserted on their source text would pass
against a script that cannot run. The CRIT-1 case is exactly that: a helper
registered under the wrong name is a fleet-wide outage that every source-level
assertion agrees is fine.
"""
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_HELPER_SH = _ROOT / "docker/base-image/git-credential-trinity.sh"
_STARTUP_SH = _ROOT / "docker/base-image/startup.sh"

pytestmark = pytest.mark.unit


# ===========================================================================
# AC1 — no code path builds a remote URL with a credential in it
# ===========================================================================

# Names that, interpolated into a URL's userinfo, mean a credential is being
# embedded. Deliberately broad: the point is to catch the SHAPE.
_SECRETISH = re.compile(r"(?i)(pat|token|secret|password|credential|auth)")

# The scan covers BOTH trees. The ent#314 lesson: that issue's AST scan had an
# empty allowlist over the whole backend and still missed six bare
# `yaml.safe_load` calls in the agent server, because it never looked there.
_SCANNED_TREES = ("src/backend", "docker/base-image")


def _placeholder_text(node: ast.JoinedStr) -> str:
    """An f-string flattened with each placeholder rendered as ``\\x00name\\x00``."""
    out = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            out.append(value.value)
        elif isinstance(value, ast.FormattedValue):
            out.append("\x00" + _expr_name(value.value) + "\x00")
    return "".join(out)


def _expr_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _expr_name(node.func)
    return ""


def _builds_credentialed_url(text: str) -> bool:
    """True when a flattened f-string interpolates a secret into URL userinfo.

    Matched on the PRODUCING shape, not on the literal ``oauth2:``. A naive
    scan for that literal would flag the scrubbers this change deliberately
    KEEPS (``startup.sh``'s clone-log sed, ``orphan_sweep``, both
    ``credential_sanitizer`` copies) — a stale token on an existing volume is
    still a token, so those must never be deleted just because their cause was.
    """
    for match in re.finditer(r"://([^/\s'\"]*)@", text):
        if _SECRETISH.search(match.group(1)):
            return True
    return False


def _python_offenders() -> list[str]:
    hits = []
    for tree in _SCANNED_TREES:
        for path in (_ROOT / tree).rglob("*.py"):
            try:
                parsed = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):  # pragma: no cover
                continue
            for node in ast.walk(parsed):
                if isinstance(node, ast.JoinedStr) and _builds_credentialed_url(
                    _placeholder_text(node)
                ):
                    rel = path.relative_to(_ROOT).as_posix()
                    hits.append(f"{rel}:{node.lineno}")
    return sorted(hits)


# `${VAR}` / `$VAR` inside a URL authority, before the `@`.
_SH_USERINFO = re.compile(r"://[^/\s\"']*\$\{?([A-Za-z_][A-Za-z0-9_]*)[^/\s\"']*@")


def _shell_offenders() -> list[str]:
    hits = []
    for tree in _SCANNED_TREES:
        for path in (_ROOT / tree).rglob("*"):
            if not path.is_file() or path.suffix not in (".sh", ""):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:  # pragma: no cover
                continue
            if not text.startswith("#!"):
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                for match in _SH_USERINFO.finditer(line):
                    if _SECRETISH.search(match.group(1)):
                        rel = path.relative_to(_ROOT).as_posix()
                        hits.append(f"{rel}:{lineno}")
    return sorted(hits)


def test_no_code_path_builds_a_credential_bearing_remote_url():
    """AC1, at the source. ZERO allowlist entries — an entry here is a smell.

    All four producers are gone: `git_service._git_remote_url` (deleted),
    `startup.sh`'s CLONE_URL branch (one credential-less form),
    `template_service.clone_github_repo` (deleted — it passed a token URL as
    argv to `subprocess.run` on the BACKEND HOST) and
    `skill_service._authenticated_url` (split, PAT moved to the environment).
    """
    offenders = _python_offenders() + _shell_offenders()
    assert offenders == [], (
        "A credential is being interpolated into a URL's userinfo. Every such "
        "URL is persisted (`.git/config`) and expanded into git's child argv, "
        "which is what ent#615 removed. Carry the credential in the git "
        "child's ENVIRONMENT (`git_credential_helper.git_auth_env`) or through "
        "the `trinity` credential helper instead:\n  " + "\n  ".join(offenders)
    )


def test_the_guard_would_catch_the_shape_it_claims_to():
    """A guard nobody has seen fail is not evidence. Prove it fires."""
    assert _builds_credentialed_url(
        "\x00scheme\x00://oauth2:\x00github_pat\x00@\x00host\x00/\x00repo\x00.git"
    )
    assert _builds_credentialed_url("https://\x00pat\x00@github.com/o/r")
    # …and that it does NOT fire on the scrubbers this change keeps.
    assert not _builds_credentialed_url("https://***@github.com/o/r")
    assert not _builds_credentialed_url("s|oauth2:[^@]*@|oauth2:***@|g")
    assert not _builds_credentialed_url("\x00scheme\x00://\x00host\x00/\x00repo\x00.git")


def test_the_scrubbers_are_still_there():
    """Never delete a scrubber because its cause was removed.

    Every workspace volume created before this change still holds a token in
    `.git/config`, and the sweep only reaches a container it can exec into.
    A stale token is still a token.
    """
    startup = _STARTUP_SH.read_text()
    assert startup.count("oauth2:[^@]*@") == 2, "the clone-log scrubbers went missing"

    sys.path.insert(0, str(_ROOT / "src/backend"))
    from utils.credential_sanitizer import redact_url_userinfo

    stale = "fatal: unable to access 'https://oauth2:ghp_stale@github.com/o/r.git/'"
    assert "ghp_stale" not in redact_url_userinfo(stale)

    sweep = (_ROOT / "docker/base-image/agent_server/utils/orphan_sweep.py").read_text()
    assert "oauth2" in sweep, "the reaped-cmdline scrubber (ent#292) went missing"


# ===========================================================================
# The helper — executed, never merely read
# ===========================================================================

def _git_available() -> bool:
    return shutil.which("git") is not None


@pytest.fixture
def helper_env(tmp_path):
    """An isolated git that has the helper registered exactly as the image does.

    `GIT_CONFIG_NOSYSTEM` + a scratch `GIT_CONFIG_GLOBAL` stand in for
    `/etc/gitconfig`, so the host's real credential helpers cannot answer —
    helper lists COMPOSE, and an inherited helper answering first would make
    every assertion below pass against a credential this helper never produced.
    """
    if not _git_available():  # pragma: no cover
        pytest.skip("git is not installed")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shutil.copy(_HELPER_SH, bin_dir / "git-credential-trinity")
    os.chmod(bin_dir / "git-credential-trinity", 0o755)
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[credential]\n\thelper = trinity\n")
    home = tmp_path / "agent-home"
    home.mkdir()
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_TERMINAL_PROMPT": "0",
        "TRINITY_AGENT_HOME": str(home),
        "HOME": str(tmp_path),
    }
    return type("HelperEnv", (), {"env": env, "home": home, "bin": bin_dir,
                                  "gitconfig": gitconfig, "tmp": tmp_path})()


def _fill(helper_env, protocol="https", host="github.com", **overrides):
    """Ask git for a credential; returns the CompletedProcess."""
    env = {**helper_env.env, **overrides}
    return subprocess.run(
        ["git", "credential", "fill"],
        input=f"protocol={protocol}\nhost={host}\n\n",
        env=env, capture_output=True, text=True, timeout=30,
    )


class TestTheHelperActuallyRuns:
    """CRIT-1. A string-only test passes against a helper that never runs."""

    def test_registered_as_trinity_the_helper_answers(self, helper_env):
        out = _fill(helper_env, GITHUB_PAT="tok_from_env").stdout
        assert "password=tok_from_env" in out
        assert "username=oauth2" in out

    def test_registered_as_the_filename_the_helper_never_runs(self, helper_env):
        """The bug the first design shipped, reproduced.

        git prepends `git-credential-` to any helper value that is not an
        absolute path, so the filename resolves to
        `git-credential-git-credential-trinity`. Combined with token-free URLs
        that is a silent fleet-wide fetch/push outage — and every source-level
        assertion about the helper still passes.
        """
        helper_env.gitconfig.write_text(
            "[credential]\n\thelper = git-credential-trinity\n"
        )
        result = _fill(helper_env, GITHUB_PAT="tok_from_env")
        assert "password=tok_from_env" not in result.stdout
        assert "is not a git command" in result.stderr

    def test_an_absolute_path_also_runs(self, helper_env):
        helper_env.gitconfig.write_text(
            f"[credential]\n\thelper = {helper_env.bin}/git-credential-trinity\n"
        )
        assert "password=tok" in _fill(helper_env, GITHUB_PAT="tok").stdout

    def test_the_image_registers_the_name_git_expands(self):
        dockerfile = (_ROOT / "docker/base-image/Dockerfile").read_text()
        assert "credential.helper trinity" in dockerfile
        assert "credential.helper git-credential-trinity" not in dockerfile


class TestHostScoping:
    def test_the_configured_origin_is_served(self, helper_env):
        assert "password=tok" in _fill(helper_env, GITHUB_PAT="tok").stdout

    def test_a_self_hosted_base_including_its_PORT_is_served(self, helper_env):
        """git feeds `host=<host>:<port>`, so a bare-hostname comparison
        silently refuses the self-hosted harness."""
        out = _fill(
            helper_env, protocol="http", host="trinity-gitea-dev:3000",
            TRINITY_GIT_BASE_URL="http://trinity-gitea-dev:3000",
            GITHUB_PAT="tok_gitea",
        ).stdout
        assert "password=tok_gitea" in out

    def test_a_lookalike_host_gets_nothing(self, helper_env):
        """A suffix comparison here would be an exfiltration primitive."""
        result = _fill(helper_env, host="github.com.evil.tld", GITHUB_PAT="tok")
        assert "tok" not in result.stdout

    def test_a_foreign_host_gets_nothing(self, helper_env):
        result = _fill(helper_env, host="gitlab.example.com", GITHUB_PAT="tok")
        assert "tok" not in result.stdout

    def test_the_wrong_protocol_gets_nothing(self, helper_env):
        result = _fill(helper_env, protocol="http", GITHUB_PAT="tok")
        assert "tok" not in result.stdout

    def test_a_self_hosted_install_does_not_serve_github(self, helper_env):
        result = _fill(
            helper_env, TRINITY_GIT_BASE_URL="http://trinity-gitea-dev:3000",
            GITHUB_PAT="tok_gitea",
        )
        assert "tok_gitea" not in result.stdout

    def test_git_itself_refuses_to_route_a_lookalike(self, helper_env):
        """Defence in depth, stated honestly: the in-helper check matters for
        the case git does NOT mediate — the agent invoking the script directly
        with hand-written stdin."""
        direct = subprocess.run(
            [str(helper_env.bin / "git-credential-trinity"), "get"],
            input="protocol=https\nhost=github.com.evil.tld\n\n",
            env={**helper_env.env, "GITHUB_PAT": "tok"},
            capture_output=True, text=True, timeout=30,
        )
        assert direct.stdout == ""


class TestResolutionOrder:
    """CRIT-3. `.env` FIRST, baked env second — the inverse of `startup.sh`."""

    def test_env_file_beats_a_stale_baked_env(self, helper_env):
        """The regression that would make a global rotation a silent no-op.

        #1967 does not recreate the container, so `Config.Env` stays frozen at
        the last recreate while `.env` carries the fresh token. A baked-env-
        first ladder authenticates with the REVOKED token forever, and nobody
        notices until the old token is turned off.
        """
        (helper_env.home / ".env").write_text("GITHUB_PAT=fresh_rotated\n")
        out = _fill(helper_env, GITHUB_PAT="stale_baked").stdout
        assert "password=fresh_rotated" in out
        assert "stale_baked" not in out

    def test_baked_env_is_used_when_the_env_file_has_none(self, helper_env):
        (helper_env.home / ".env").write_text("OTHER=1\n")
        assert "password=baked" in _fill(helper_env, GITHUB_PAT="baked").stdout

    def test_an_empty_env_file_line_does_not_shadow_baked_env(self, helper_env):
        (helper_env.home / ".env").write_text("GITHUB_PAT=\n")
        assert "password=baked" in _fill(helper_env, GITHUB_PAT="baked").stdout

    def test_the_harvest_file_is_the_LAST_rung(self, helper_env):
        (helper_env.home / ".trinity").mkdir()
        (helper_env.home / ".trinity/git-credential").write_text("harvested")
        # Anything else wins over it…
        assert "password=baked" in _fill(helper_env, GITHUB_PAT="baked").stdout
        # …and it serves only the agent that has nothing else.
        env = dict(helper_env.env)
        env.pop("GITHUB_PAT", None)
        result = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            env=env, capture_output=True, text=True, timeout=30,
        )
        assert "password=harvested" in result.stdout


class TestEnvFileParsing:
    def test_the_env_file_is_parsed_never_sourced(self, helper_env):
        """`.env` is agent-writable, so sourcing it is arbitrary execution on
        every git operation the platform runs."""
        marker = helper_env.tmp / "pwned"
        (helper_env.home / ".env").write_text(
            f"GITHUB_PAT=tok\nEVIL=$(touch {marker})\n"
        )
        assert "password=tok" in _fill(helper_env).stdout
        assert not marker.exists(), "the helper executed a line from .env"
        # Statically too — on COMMAND position, not on prose. (An earlier
        # version of this assertion was a bare substring test and tripped on
        # the phrase "no other source" in the script's own comments.)
        code = [
            line.strip() for line in _HELPER_SH.read_text().splitlines()
            if not line.strip().startswith("#")
        ]
        assert not [ln for ln in code if re.match(r"^(\.|source|eval)\b", ln)]

    def test_it_agrees_with_the_startup_sh_parse(self):
        """One shape, two readers. `startup.sh`'s fallback and the helper must
        answer the same for the same file, or an agent authenticates with a
        different token than its own boot script thinks it has."""
        startup_shape = "grep -m1 '^GITHUB_PAT=' /home/developer/.env"
        assert startup_shape in _STARTUP_SH.read_text()
        helper = _HELPER_SH.read_text()
        for fragment in ("grep -m1", "cut -d= -f2-", "tr -d"):
            assert fragment in helper

    @pytest.mark.parametrize("line, expected", [
        ("GITHUB_PAT=plain", "plain"),
        ('GITHUB_PAT="quoted"', "quoted"),
        ("GITHUB_PAT='single'", "single"),
        ("GITHUB_PAT=with=equals", "with=equals"),
    ])
    def test_dotenv_shapes(self, helper_env, line, expected):
        (helper_env.home / ".env").write_text(line + "\n")
        env = dict(helper_env.env)
        env.pop("GITHUB_PAT", None)
        result = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            env=env, capture_output=True, text=True, timeout=30,
        )
        assert f"password={expected}" in result.stdout


class TestDegradesHonestly:
    """AC4 — a NAMED auth error, never a generic failure."""

    def test_nothing_resolvable_emits_the_marker_and_no_credential(self, helper_env):
        result = _fill(helper_env)
        assert result.stdout.count("password=") == 0
        assert "TRINITY_GIT_NO_CREDENTIAL host=github.com" in result.stderr

    def test_git_then_fails_with_a_shape_the_classifier_already_knows(self, helper_env):
        """No new pattern was needed for the generic case, and that is the
        point: a silent helper plus `GIT_TERMINAL_PROMPT=0` produces
        "could not read Username", which `_AUTH_PATTERNS` already matched."""
        result = _fill(helper_env)
        assert "could not read Username" in result.stderr
        assert "terminal prompts disabled" in result.stderr

    def test_the_marker_carries_no_credential_and_no_agent_identity(self, helper_env):
        (helper_env.home / ".env").write_text("GITHUB_PAT=sekrit\n")
        result = _fill(helper_env, host="gitlab.example.com")
        assert "sekrit" not in result.stderr + result.stdout

    def test_the_marker_is_classified_as_an_auth_failure(self):
        sys.path.insert(0, str(_ROOT / "src/backend"))
        from services.git_credential_helper import NO_CREDENTIAL_MARKER

        import services.git_service as gs

        assert any(
            p.search(f"{NO_CREDENTIAL_MARKER} host=github.com")
            for p in gs._AUTH_PATTERNS
        )

    def test_a_bare_403_is_NOT_treated_as_an_auth_failure(self):
        """`classify_conflict` evaluates auth patterns FIRST, so a bare 403
        would relabel a secondary-rate-limit, a SAML-SSO-enforcement and an
        archived-repo push as "no write credentials"."""
        sys.path.insert(0, str(_ROOT / "src/backend"))
        import services.git_service as gs

        rate_limited = (
            "remote: You have exceeded a secondary rate limit\n"
            "fatal: unable to access 'https://github.com/o/r/': "
            "The requested URL returned error: 403"
        )
        assert not any(p.search(rate_limited) for p in gs._AUTH_PATTERNS)


class TestStoreAndEraseAreNoOps:
    def test_store_writes_nothing(self, helper_env):
        result = subprocess.run(
            [str(helper_env.bin / "git-credential-trinity"), "store"],
            input="protocol=https\nhost=github.com\nusername=x\npassword=y\n\n",
            env=helper_env.env, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0 and result.stdout == ""
        assert not (helper_env.tmp / ".git-credentials").exists()

    def test_erase_is_a_no_op(self, helper_env):
        (helper_env.home / ".env").write_text("GITHUB_PAT=tok\n")
        subprocess.run(
            [str(helper_env.bin / "git-credential-trinity"), "erase"],
            input="protocol=https\nhost=github.com\n\n",
            env=helper_env.env, capture_output=True, text=True, timeout=30,
        )
        assert (helper_env.home / ".env").read_text() == "GITHUB_PAT=tok\n"


# ===========================================================================
# The remediation sweep — executed against real git repos
# ===========================================================================

def _load_helper_module():
    sys.path.insert(0, str(_ROOT / "src/backend"))
    import services.git_credential_helper as gch

    return gch


@pytest.fixture
def sweep(helper_env, tmp_path):
    """Run the REAL sweep script against a real repo, out of container.

    The script is the artifact under test — a Python reimplementation of its
    logic would be testing the reimplementation. `TRINITY_AGENT_HOME` is the
    seam that lets it run outside a container; it redirects reads to files the
    invoking user already owns, so it grants nothing.
    """
    gch = _load_helper_module()
    script = tmp_path / "scrub.sh"
    script.write_text(gch.SCRUB_SCRIPT)

    def _run(repo: Path, **overrides):
        env = {**helper_env.env, **overrides}
        result = subprocess.run(
            ["sh", str(script), str(repo)],
            env=env, capture_output=True, text=True, timeout=60,
        )
        return gch.parse_scrub_report(result.stdout), result

    return _run


def _mkrepo(path: Path, origin: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True,
                   env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"})
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin],
                   check=True, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"})
    return path


def _origin(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "config", "--get", "remote.origin.url"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()


_TOKEN_URL = "https://oauth2:FAKETOKEN_A@github.com/acme/agent.git"


class TestTheSweep:
    def test_an_orphan_is_harvested_BEFORE_its_url_is_stripped(self, sweep, helper_env, tmp_path):
        """The class that a strip-first sweep strands permanently.

        `POST /{agent}/git/initialize` writes a git config row and pushes with
        the resolved — often GLOBAL — platform PAT, but bakes no git env,
        persists no per-agent row and writes no `.env`: "its only credential
        lives in the container's `.git/config` origin URL". Stripping that URL
        without rescuing the token first is destruction of the agent's last
        credential.
        """
        repo = _mkrepo(tmp_path / "orphan", _TOKEN_URL)
        env = dict(helper_env.env)
        env.pop("GITHUB_PAT", None)
        report, _ = sweep(repo, **{k: v for k, v in env.items()})
        assert report["harvested"] == 1
        assert report["remotes_scrubbed"] == 1
        assert report["refused"] == 0
        assert _origin(repo) == "https://github.com/acme/agent.git"
        harvest = helper_env.home / ".trinity/git-credential"
        assert harvest.read_text() == "FAKETOKEN_A"
        assert oct(harvest.stat().st_mode)[-3:] == "600"

    def test_it_REFUSES_rather_than_stripping_what_it_cannot_replace(
        self, sweep, helper_env, tmp_path
    ):
        """The negative of the test above, and the one that matters on upgrade.

        When the replacement cannot be placed, the URL is left exactly as it
        was and the refusal is REPORTED — the caller files an operator-queue
        entry. Never strip a credential you could not replace.
        """
        repo = _mkrepo(tmp_path / "refused", _TOKEN_URL)
        # A FILE where `.trinity/` must go: the harvest write cannot succeed.
        blocked = tmp_path / "blocked-home"
        blocked.write_text("not a directory")
        env = dict(helper_env.env)
        env.pop("GITHUB_PAT", None)
        env["TRINITY_AGENT_HOME"] = str(blocked)
        report, result = sweep(repo, **env)
        assert report["helper_ok"] == 0
        assert report["harvested"] == 0
        assert report["refused"] == 1
        assert report["remotes_scrubbed"] == 0
        assert _origin(repo) == _TOKEN_URL, "the only credential was destroyed"
        assert "FAKETOKEN_A" not in result.stdout + result.stderr

    def test_it_is_idempotent(self, sweep, helper_env, tmp_path):
        repo = _mkrepo(tmp_path / "idem", _TOKEN_URL)
        sweep(repo, GITHUB_PAT="env_tok")
        report, _ = sweep(repo, GITHUB_PAT="env_tok")
        assert report == {
            "remotes_scrubbed": 0, "harvested": 0, "seeded": 0, "refused": 0,
            "gitmodules_hits": 0, "helper_ok": 1, "competing_helpers": 0,
        }

    def test_a_caller_held_credential_can_be_SEEDED(self, sweep, helper_env, tmp_path):
        """What stops `initialize_git_in_container` creating new orphans: it
        has the PAT in hand and, before this, persisted it nowhere."""
        repo = _mkrepo(tmp_path / "seeded", "https://github.com/acme/agent.git")
        env = dict(helper_env.env)
        env.pop("GITHUB_PAT", None)
        env["TRINITY_SEED_PAT"] = "seeded_tok"
        report, _ = sweep(repo, **env)
        assert report["seeded"] == 1 and report["helper_ok"] == 1
        assert (helper_env.home / ".trinity/git-credential").read_text() == "seeded_tok"

    def test_a_seed_is_ignored_when_something_already_resolves(
        self, sweep, helper_env, tmp_path
    ):
        repo = _mkrepo(tmp_path / "noseed", "https://github.com/acme/agent.git")
        report, _ = sweep(repo, GITHUB_PAT="already", TRINITY_SEED_PAT="seeded_tok")
        assert report["seeded"] == 0
        assert not (helper_env.home / ".trinity/git-credential").exists()

    def test_a_foreign_host_is_neither_harvested_nor_stripped(
        self, sweep, helper_env, tmp_path
    ):
        """Scoped to the configured origin in BOTH directions: a foreign
        remote's credential must not be promoted into the GitHub slot, and must
        not be stripped from a remote the helper cannot serve."""
        repo = _mkrepo(tmp_path / "foreign", _TOKEN_URL)
        foreign = "https://user:FAKETOKEN_C@gitlab.example.com/x/y.git"
        subprocess.run(["git", "-C", str(repo), "remote", "add", "other", foreign],
                       check=True, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"})
        sweep(repo, GITHUB_PAT="env_tok")
        assert _origin(repo) == "https://github.com/acme/agent.git"
        other = subprocess.run(
            ["git", "-C", str(repo), "config", "--get", "remote.other.url"],
            capture_output=True, text=True,
        ).stdout.strip()
        assert other == foreign

    def test_the_ent123_pushurl_sentinel_is_left_alone(
        self, sweep, helper_env, tmp_path
    ):
        """It is not a URL; a sweep that "normalised" it would silently restore
        push for an agent that is meant to be read-only."""
        repo = _mkrepo(tmp_path / "blackholed", "https://github.com/acme/agent.git")
        sentinel = ("no-write-credentials--this-agent-is-read-only--"
                    "fork-to-own-or-add-a-github-token-to-push")
        subprocess.run(
            ["git", "-C", str(repo), "remote", "set-url", "--push", "origin", sentinel],
            check=True, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
        )
        sweep(repo, GITHUB_PAT="env_tok")
        pushurl = subprocess.run(
            ["git", "-C", str(repo), "config", "--get", "remote.origin.pushurl"],
            capture_output=True, text=True,
        ).stdout.strip()
        assert pushurl == sentinel

    def test_nested_submodule_configs_are_reached(self, sweep, helper_env, tmp_path):
        """`.git/modules/*/config` is a glob that misses
        `.git/modules/<a>/modules/<b>/config`."""
        repo = _mkrepo(tmp_path / "subs", _TOKEN_URL)
        nested = repo / ".git/modules/a/modules/b"
        nested.mkdir(parents=True)
        for cfg, url in (
            (repo / ".git/modules/a/config", "https://oauth2:FAKETOKEN_D1@github.com/acme/sub.git"),
            (nested / "config", "https://oauth2:FAKETOKEN_D2@github.com/acme/nested.git"),
        ):
            subprocess.run(["git", "config", "--file", str(cfg),
                            "remote.origin.url", url], check=True)
        report, _ = sweep(repo, GITHUB_PAT="env_tok")
        assert report["remotes_scrubbed"] == 3
        blob = "".join(
            p.read_text() for p in (repo / ".git").rglob("config") if p.is_file()
        )
        assert "FAKETOKEN" not in blob

    def test_an_insteadOf_subsection_is_renamed_not_left(
        self, sweep, helper_env, tmp_path
    ):
        """A second config location a token lives in — and the shape this
        product's own deploy path uses."""
        repo = _mkrepo(tmp_path / "insteadof", "https://github.com/acme/agent.git")
        subprocess.run(
            ["git", "-C", str(repo), "config",
             "url.https://x-access-token:FAKETOKEN_E@github.com/.insteadOf",
             "git@github.com:"],
            check=True, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
        )
        sweep(repo, GITHUB_PAT="env_tok")
        assert "FAKETOKEN_E" not in (repo / ".git/config").read_text()
        assert "insteadof" in (repo / ".git/config").read_text().lower()

    def test_a_token_in_the_TRACKED_gitmodules_is_reported_not_silently_kept(
        self, sweep, helper_env, tmp_path
    ):
        """The sweep cannot fix it — it is already committed and pushed — so
        finding one turns "rotate the platform PAT after adoption" from advice
        into a requirement."""
        repo = _mkrepo(tmp_path / "gitmodules", "https://github.com/acme/agent.git")
        (repo / ".gitmodules").write_text(
            '[submodule "a"]\n\tpath = a\n'
            '\turl = https://oauth2:FAKETOKEN_F@github.com/acme/sub.git\n'
        )
        report, _ = sweep(repo, GITHUB_PAT="env_tok")
        assert report["gitmodules_hits"] == 1

    def test_a_competing_helper_is_reported(self, sweep, helper_env, tmp_path):
        """Helper lists COMPOSE: one registered ahead of ours answers first and
        masks it — including masking the probe, which would then report a
        credential this helper never produced."""
        repo = _mkrepo(tmp_path / "competing", "https://github.com/acme/agent.git")
        subprocess.run(
            ["git", "-C", str(repo), "config", "--add", "credential.helper", "store"],
            check=True, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
        )
        report, _ = sweep(repo, GITHUB_PAT="env_tok")
        assert report["competing_helpers"] == 1

    def test_no_credential_ever_crosses_the_exec_boundary(
        self, sweep, helper_env, tmp_path
    ):
        """The sweep probes with `git credential fill`, which PRINTS the
        credential on stdout — and this script's output reaches
        `logger.warning(result["output"][:200])` and `GitInitResult.error`,
        i.e. the platform log and possibly an HTTP body. Probe by EXIT CODE.
        """
        repo = _mkrepo(tmp_path / "quiet", _TOKEN_URL)
        (helper_env.home / ".env").write_text("GITHUB_PAT=FAKETOKEN_ENVFILE\n")
        _report, result = sweep(repo, GITHUB_PAT="FAKETOKEN_BAKED")
        combined = result.stdout + result.stderr
        assert "password=" not in combined
        for secret in ("FAKETOKEN_A", "FAKETOKEN_ENVFILE", "FAKETOKEN_BAKED"):
            assert secret not in combined, f"{secret} crossed the exec boundary"
        # And the report itself is counts only — no URL, host or value.
        assert re.fullmatch(r"[A-Za-z0-9_=\s]+", result.stdout.strip())


# ===========================================================================
# startup.sh — extracted and EXECUTED
# ===========================================================================
#
# `/verify-local`'s agent stage boots `local:test-echo`, so `startup.sh`'s
# clone path and `configure_push_remote` never run there. A source-text
# assertion is not evidence that a shell branch behaves; extract the functions
# and run them.

_HELPER_ABS = "/usr/local/bin/git-credential-trinity"


def _startup_region() -> str:
    """The clone-URL composition plus the three functions, verbatim."""
    text = _STARTUP_SH.read_text()
    start = text.index('    GIT_BASE_URL="${TRINITY_GIT_BASE_URL:-https://github.com}"')
    end = text.index("    # Check if GIT_SYNC_ENABLED")
    return textwrap.dedent(text[start:end])


def test_the_extracted_region_is_the_real_one():
    """The extraction itself has to be pinned, or this whole section drifts
    into testing a snippet the image does not ship."""
    region = _startup_region()
    for name in ("credential_resolves()", "retemplate_origin()", "configure_push_remote()"):
        assert name in region, f"{name} is no longer in the extracted region"
    # The substitution the harness makes below, pinned so it cannot silently
    # stop matching and leave these tests exercising a binary that is not
    # there — which would make every "credential resolves" branch untested
    # while every test still passed.
    assert _HELPER_ABS in region
    assert _HELPER_ABS not in region.replace(_HELPER_ABS, "<substituted>")


@pytest.fixture
def startup(helper_env, tmp_path):
    """Run `retemplate_origin` / `configure_push_remote` for real."""
    region = _startup_region().replace(_HELPER_ABS,
                                       str(helper_env.bin / "git-credential-trinity"))

    def _run(repo: Path, call: str, **overrides):
        script = (
            'set -u\n'
            f'cd {repo}\n'
            + region
            + f"\n{call}\n"
        )
        env = {
            **helper_env.env,
            "GITHUB_REPO": "acme/agent",
            "GITHUB_PAT": "",
            **overrides,
        }
        return subprocess.run(["bash", "-c", script], env=env,
                              capture_output=True, text=True, timeout=30)

    return _run


class TestStartupShRewriteIsConditional:
    """CRIT-2 — the defect that strands agents permanently on upgrade."""

    def test_a_token_url_is_scrubbed_once_a_credential_resolves(self, startup, tmp_path):
        repo = _mkrepo(tmp_path / "has-pat", _TOKEN_URL)
        startup(repo, "retemplate_origin", GITHUB_PAT="tok")
        assert _origin(repo) == "https://github.com/acme/agent.git"

    def test_the_helper_ladder_counts_as_a_credential(self, startup, helper_env, tmp_path):
        """Not just `GITHUB_PAT`: an agent whose credential was HARVESTED into
        the helper's last rung has one, and its URL is safe to scrub."""
        repo = _mkrepo(tmp_path / "harvested", _TOKEN_URL)
        (helper_env.home / ".trinity").mkdir()
        (helper_env.home / ".trinity/git-credential").write_text("harvested_tok")
        startup(repo, "retemplate_origin")
        assert _origin(repo) == "https://github.com/acme/agent.git"

    def test_an_orphans_only_credential_is_PRESERVED(self, startup, tmp_path):
        """The upgrade case. This rewrite was unconditional, and with a
        credential-less replacement URL that is destruction of the agent's last
        credential — at container start, BEFORE any backend sweep could
        harvest it."""
        repo = _mkrepo(tmp_path / "orphan", _TOKEN_URL)
        result = startup(repo, "retemplate_origin")
        assert _origin(repo) == _TOKEN_URL
        assert "TRINITY_GIT_CREDENTIAL_PRESERVED" in result.stdout
        assert "FAKETOKEN_A" not in result.stdout + result.stderr

    def test_a_credential_less_url_is_still_rewritten(self, startup, tmp_path):
        """ent#123 unchanged: no userinfo to lose, so the rewrite is always
        safe and the tokenless agent keeps converging on the canonical URL."""
        repo = _mkrepo(tmp_path / "tokenless", "https://github.com/old/name.git")
        startup(repo, "retemplate_origin")
        assert _origin(repo) == "https://github.com/acme/agent.git"

    def test_a_missing_origin_is_added(self, startup, tmp_path):
        repo = tmp_path / "noremote"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        startup(repo, "retemplate_origin")
        assert _origin(repo) == "https://github.com/acme/agent.git"


class TestConfigurePushRemoteInputIsUnchanged:
    """CRIT-4 — the harvest must not become a privilege GRANT.

    `startup.sh` exports `.env`'s `GITHUB_PAT` as `GH_TOKEN` AND `GITHUB_TOKEN`,
    authenticating the whole `gh` CLI and REST API, and `configure_push_remote`
    gates the ent#123 push blackhole on the same name. So harvesting into that
    name would hand a previously-tokenless agent the platform's credentials —
    the ent#162 class, applied fleet-wide by the sweep. The harvest goes to
    `.trinity/git-credential` instead, which nothing exports.
    """

    _SENTINEL = "no-write-credentials"

    def _pushurl(self, repo: Path) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), "config", "--get", "remote.origin.pushurl"],
            capture_output=True, text=True,
        ).stdout.strip()

    def test_a_tokenless_agent_is_still_blackholed(self, startup, tmp_path):
        repo = _mkrepo(tmp_path / "tokenless", "https://github.com/acme/agent.git")
        startup(repo, "configure_push_remote")
        assert self._SENTINEL in self._pushurl(repo)

    def test_a_tokenless_agent_stays_blackholed_after_a_sweep(
        self, sweep, startup, helper_env, tmp_path
    ):
        """The regression test for the grant. An ent#123 agent has no userinfo
        to harvest, so the sweep places nothing and the blackhole holds."""
        repo = _mkrepo(tmp_path / "swept", "https://github.com/acme/agent.git")
        env = dict(helper_env.env)
        env.pop("GITHUB_PAT", None)
        report, _ = sweep(repo, **env)
        assert report["harvested"] == 0
        assert not (helper_env.home / ".env").exists(), (
            "the sweep wrote .env — that name is exported as GH_TOKEN"
        )
        startup(repo, "configure_push_remote")
        assert self._SENTINEL in self._pushurl(repo)

    def test_the_harvest_never_writes_the_exported_name(self):
        gch = _load_helper_module()
        code = [
            line for line in gch.SCRUB_SCRIPT.splitlines()
            if not line.strip().startswith("#")
        ]
        assert not [ln for ln in code if "GITHUB_PAT" in ln], (
            "the sweep touches the name startup.sh exports as GH_TOKEN"
        )
        assert gch.HARVEST_PATH == "/home/developer/.trinity/git-credential"

    def test_the_harvest_file_is_ignored_contents_only(self):
        """`.trinity/*` (#2070), so the rescued credential is never committed."""
        sys.path.insert(0, str(_ROOT / "src/backend"))
        import services.git_service as gs

        assert ".trinity/*" in gs._GITIGNORE_PATTERNS
        assert "!.trinity/git-credential" not in gs._GITIGNORE_PATTERNS

    def test_a_credentialed_agent_has_its_blackhole_cleared(self, startup, tmp_path):
        repo = _mkrepo(tmp_path / "credentialed", "https://github.com/acme/agent.git")
        startup(repo, "configure_push_remote")
        assert self._SENTINEL in self._pushurl(repo)
        startup(repo, "configure_push_remote", GITHUB_PAT="tok")
        assert self._pushurl(repo) == ""

    def test_a_harvested_agent_regains_push(self, startup, helper_env, tmp_path):
        """It could push before the sweep (from its URL); it must still be able
        to after. The widened gate is exactly this case and no wider."""
        repo = _mkrepo(tmp_path / "harvested", "https://github.com/acme/agent.git")
        startup(repo, "configure_push_remote")
        assert self._SENTINEL in self._pushurl(repo)
        (helper_env.home / ".trinity").mkdir()
        (helper_env.home / ".trinity/git-credential").write_text("harvested_tok")
        startup(repo, "configure_push_remote")
        assert self._pushurl(repo) == ""


# ===========================================================================
# The backend orchestration — how the credential reaches the container
# ===========================================================================

def _git_service():
    sys.path.insert(0, str(_ROOT / "src/backend"))
    import services.git_service as gs

    return gs


class _RecordingExec:
    """Stands in for `execute_command_in_container`, recording every exec."""

    def __init__(self, helper_ok: int = 1):
        self.calls: list[dict] = []
        self.helper_ok = helper_ok

    async def __call__(self, container_name, command, timeout=60, *,
                       environment=None, user="developer"):
        self.calls.append({
            "command": command, "environment": environment, "user": user,
        })
        if "base64 -d" in command and "sh -s" in command:
            return {"exit_code": 0, "output": (
                f"TRINITY_SCRUB_REPORT remotes_scrubbed=1 harvested=0 seeded=0 "
                f"refused=0 gitmodules_hits=0 helper_ok={self.helper_ok} "
                f"competing_helpers=0"
            )}
        return {"exit_code": 0, "output": ""}

    @property
    def commands(self) -> str:
        return "\n".join(c["command"] for c in self.calls)


@pytest.fixture
def gs_with_exec():
    """Install a recording `execute_command_in_container`; returns the recorder."""
    gs = _git_service()
    patches = []

    def _install(helper_ok: int = 1) -> _RecordingExec:
        recorder = _RecordingExec(helper_ok=helper_ok)
        ctx = patch.multiple(
            gs,
            execute_command_in_container=recorder,
            _detect_git_dir=AsyncMock(return_value="/home/developer"),
        )
        ctx.start()
        patches.append(ctx)
        return recorder

    yield _install

    for ctx in patches:
        ctx.stop()


class TestTheCredentialNeverTouchesArgv:
    """The whole point. An exec's argv is visible in the container's process
    table, which is the leak — so base64-ing a token onto argv would relocate
    it, not remove it."""

    def test_update_remote_pat_carries_the_token_in_the_exec_environment(
        self, gs_with_exec
    ):
        gs = _git_service()
        recorder = gs_with_exec()
        import asyncio

        assert asyncio.run(gs.update_remote_pat("a1", "SEKRIT_TOKEN", "acme/agent"))
        assert "SEKRIT_TOKEN" not in recorder.commands, "the token reached argv"
        envs = [c["environment"] for c in recorder.calls if c["environment"]]
        assert any("SEKRIT_TOKEN" in str(e) for e in envs), (
            "the token reached neither argv nor the exec environment"
        )

    def test_the_env_write_and_the_sweep_run_as_root(self, gs_with_exec):
        """A same-uid process can read `/proc/<pid>/environ` for the life of
        the exec, and the agent runs as `developer`."""
        gs = _git_service()
        recorder = gs_with_exec()
        import asyncio

        asyncio.run(gs.update_remote_pat("a1", "SEKRIT_TOKEN", "acme/agent"))
        credential_execs = [
            c for c in recorder.calls
            if c["environment"] and "SEKRIT_TOKEN" in str(c["environment"])
        ]
        assert credential_execs
        assert all(c["user"] == "root" for c in credential_execs)

    def test_the_re_pointed_origin_is_credential_less(self, gs_with_exec):
        gs = _git_service()
        recorder = gs_with_exec()
        import asyncio

        asyncio.run(gs.update_remote_pat("a1", "SEKRIT_TOKEN", "acme/agent"))
        seturl = [c["command"] for c in recorder.calls if "remote set-url" in c["command"]]
        assert seturl, "origin was never re-pointed"
        assert all("@" not in c.split("set-url origin ")[1].split()[0] for c in seturl)

    def test_nothing_is_re_pointed_when_no_credential_resolves(self, gs_with_exec):
        """The same rule the sweep follows, one level up: `update_remote_pat`
        must not install a credential-less origin over a URL that may be the
        agent's only credential."""
        gs = _git_service()
        recorder = gs_with_exec(helper_ok=0)
        import asyncio

        assert asyncio.run(gs.update_remote_pat("a1", "TOK", "acme/agent")) is False
        assert not [c for c in recorder.calls if "remote set-url" in c["command"]]

    def test_the_sweep_is_bounded_in_the_container_too(self, gs_with_exec):
        """`execute_command_in_container` accepts a `timeout` and forwards it
        nowhere, so `asyncio.wait_for` alone frees the caller while the
        `_docker_executor` pool thread stays pinned — fleet-wide, from a
        background pass."""
        gs = _git_service()
        recorder = gs_with_exec()
        import asyncio

        asyncio.run(gs.scrub_git_remote_tokens("a1"))
        sweep_cmd = next(c["command"] for c in recorder.calls if "sh -s" in c["command"])
        assert sweep_cmd.startswith(f"timeout {gs.SCRUB_TIMEOUT_S} ")


class TestRotationWithoutARecreate:
    """CRIT-3 — the regression that would make #1967 a silent no-op.

    A global rotation does not recreate the container, so `Config.Env` keeps
    the OLD token while `.env` gets the new one. The helper therefore resolves
    `.env` FIRST. Proven end-to-end here: the ladder, then the plumbing that
    puts the new token on its first rung.
    """

    def test_the_ladder_prefers_the_live_env_file(self, helper_env):
        (helper_env.home / ".env").write_text("GITHUB_PAT=rotated_new\n")
        out = _fill(helper_env, GITHUB_PAT="revoked_old").stdout
        assert "password=rotated_new" in out

    def test_update_remote_pat_writes_the_env_file_over_docker_exec(
        self, gs_with_exec
    ):
        """Not only over HTTP. `_apply_pat_to_env` posts to
        `http://agent-<name>:8000/api/credentials/*` and RAISES when the agent
        server is wedged, restarting or OOM; `docker exec` works in all three.
        Without this, a rotation that cannot reach the agent server leaves the
        revoked token as the only thing the helper can resolve.
        """
        gs = _git_service()
        recorder = gs_with_exec()
        import asyncio

        asyncio.run(gs.update_remote_pat("a1", "rotated_new", "acme/agent"))
        env_writes = [
            c for c in recorder.calls
            if c["environment"] and "TRINITY_NEW_GITHUB_PAT" in c["environment"]
        ]
        assert env_writes, "the .env write never happened over docker exec"
        assert env_writes[0]["environment"]["TRINITY_NEW_GITHUB_PAT"] == "rotated_new"
        assert "/home/developer}/.env" in env_writes[0]["command"] or \
            ".env" in env_writes[0]["command"]

    def test_the_env_write_never_logs_its_output(self):
        """That exec is editing `.env`; its output could echo the file."""
        source = (_ROOT / "src/backend/services/git_service.py").read_text()
        fn_start = source.index("async def write_container_github_pat")
        fn_end = source.index("\ndef _alarm_git_token_scrub_refused")
        body = source[fn_start:fn_end]
        assert 'result.get("output"' not in body


class TestInitializeSeedsRatherThanOrphaning:
    def test_a_supplied_pat_is_seeded_before_any_remote_is_written(self, gs_with_exec):
        gs = _git_service()
        recorder = gs_with_exec()
        import asyncio

        asyncio.run(gs.initialize_git_in_container(
            "a1", "acme/agent", "INIT_TOKEN", create_working_branch=False,
        ))
        commands = [c["command"] for c in recorder.calls]
        seed_index = next(
            i for i, c in enumerate(recorder.calls)
            if c["environment"] and "INIT_TOKEN" in str(c["environment"])
        )
        seturl_index = next(
            i for i, c in enumerate(commands) if "remote set-url origin" in c
        )
        assert seed_index < seturl_index, "the remote was written before the seed"
        assert "INIT_TOKEN" not in "\n".join(commands)

    def test_it_fails_honestly_when_the_credential_cannot_be_placed(
        self, gs_with_exec
    ):
        """AC4. Better than a cryptic auth error three commands later."""
        gs = _git_service()
        gs_with_exec(helper_ok=0)
        import asyncio

        result = asyncio.run(gs.initialize_git_in_container(
            "a1", "acme/agent", "INIT_TOKEN", create_working_branch=False,
        ))
        assert result.success is False
        assert "credential" in (result.error or "").lower()
        assert "INIT_TOKEN" not in (result.error or "")

    def test_a_tokenless_agent_is_not_failed(self, gs_with_exec):
        """ent#123: an anonymous public-template clone legitimately resolves
        nothing. That is a read-only agent, not a failure."""
        gs = _git_service()
        gs_with_exec(helper_ok=0)
        import asyncio

        result = asyncio.run(gs.initialize_git_in_container(
            "a1", "acme/agent", "", create_working_branch=False,
        ))
        assert result.success is True
