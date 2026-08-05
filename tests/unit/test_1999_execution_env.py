"""#1999 — a key removed from `.env` must stop reaching spawned executions.

The bug: the credential endpoints mirrored every submitted key into the
long-lived agent-server process's `os.environ`, every spawn passed
`env={**os.environ, ...}`, and the mirror had **no delete phase**. So a key
removed from `.env` by any non-mirroring write path (SSH, `docker exec`, an
agent editing its own `.env`) kept reaching every subsequently-spawned
execution until container restart — and was invisible to both
`/proc/<pid>/environ` (an exec-time snapshot) and `docker exec` (which shows
the container baseline, not this process's mutated env). Credential revocation
silently failed with no inspection route that could reveal it.

These tests pin the three properties that close it:

  1. the execution env is a pure function of (baseline, `.env`, overrides),
     rebuilt per spawn — so deletion propagates;
  2. the process mirror deletes what a re-written `.env` dropped, restoring the
     container baseline rather than blindly popping;
  3. #1089 token rotation, which deliberately never writes `.env`, still wins.

The module is loaded standalone by path (the agent server ships in its own
image and cannot import `src/backend`), matching the vendored-mirror idiom in
`test_1965_agent_server_safe_yaml.py`.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MODULE = (
    _ROOT / "docker" / "base-image" / "agent_server" / "services" / "execution_env.py"
)

pytestmark = pytest.mark.unit


def _load(monkeypatch, baseline: dict):
    """Import a FRESH copy of the module under a controlled baseline.

    `INITIAL_ENV` is captured at import time on purpose — it is the container
    baseline, the thing `docker exec` shows. Re-importing per test is the only
    honest way to control it, and it also keeps the module-level override /
    mirrored-key state from leaking between tests.
    """
    monkeypatch.setattr(os, "environ", dict(baseline), raising=True)
    spec = importlib.util.spec_from_file_location(
        f"_execution_env_1999_{len(sys.modules)}", _MODULE
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def env_file(tmp_path):
    return tmp_path / ".env"


# ---------------------------------------------------------------------------
# The reported bug
# ---------------------------------------------------------------------------

class TestRemovedKeyStopsReachingExecutions:

    def test_key_deleted_out_of_band_does_not_reach_the_next_execution(
        self, monkeypatch, env_file
    ):
        """The exact reproduction from the issue, minus the container.

        Inject a key, then delete the line by a path that does NOT go through
        the credentials API (the issue used `docker exec`; here, a plain
        rewrite of the file). The next spawned execution must not see it.
        """
        mod = _load(monkeypatch, {"PATH": "/usr/bin"})

        env_file.write_text('CANARY_VAR="ghost123"\n')
        mod.sync_process_env(env_file)
        assert mod.build_execution_env(env_file=env_file)["CANARY_VAR"] == "ghost123"

        env_file.write_text("")  # out-of-band deletion

        assert "CANARY_VAR" not in mod.build_execution_env(env_file=env_file)

    def test_reinjecting_a_cleaned_env_clears_the_process_mirror(
        self, monkeypatch, env_file
    ):
        """AC: re-injecting a file with the key ABSENT must clear it.

        The pre-fix mirror only ever *set* keys, so "delete the stale line,
        then re-push credentials" — the intuitive operator action — returned
        success and left the runtime poisoned.
        """
        mod = _load(monkeypatch, {})

        env_file.write_text('CANARY_VAR="ghost123"\n')
        mod.sync_process_env(env_file)
        assert os.environ["CANARY_VAR"] == "ghost123"

        env_file.write_text('OTHER="x"\n')
        result = mod.sync_process_env(env_file)

        assert "CANARY_VAR" not in os.environ
        assert result == {"applied": 1, "removed": 1}

    def test_a_baseline_key_is_restored_not_deleted(self, monkeypatch, env_file):
        """A key that also exists in the container baseline must fall BACK to
        the baseline value, not vanish — the mirror may only undo its own
        writes. Deleting it would break platform code reading it from the
        process env (AGENT_RUNTIME, ANTHROPIC_API_KEY, GOOGLE_API_KEY)."""
        mod = _load(monkeypatch, {"ANTHROPIC_API_KEY": "baked-at-create"})

        env_file.write_text('ANTHROPIC_API_KEY="from-dot-env"\n')
        mod.sync_process_env(env_file)
        assert os.environ["ANTHROPIC_API_KEY"] == "from-dot-env"

        env_file.write_text("")
        mod.sync_process_env(env_file)

        assert os.environ["ANTHROPIC_API_KEY"] == "baked-at-create"

    def test_mirror_never_removes_a_key_it_did_not_write(self, monkeypatch, env_file):
        mod = _load(monkeypatch, {"AGENT_RUNTIME": "claude"})
        env_file.write_text('SOMETHING="1"\n')

        mod.sync_process_env(env_file)
        env_file.write_text("")
        mod.sync_process_env(env_file)

        assert os.environ["AGENT_RUNTIME"] == "claude"


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------

class TestPrecedence:

    def test_env_file_is_authoritative_over_the_baseline(self, monkeypatch, env_file):
        mod = _load(monkeypatch, {"K": "baseline"})
        env_file.write_text('K="from-file"\n')
        assert mod.build_execution_env(env_file=env_file)["K"] == "from-file"

    def test_rotated_token_beats_a_stale_one_in_the_env_file(
        self, monkeypatch, env_file
    ):
        """#1089 must not regress, including from the other side.

        `/api/credentials/reload-token` rotates the subscription token WITHOUT
        writing `.env` — so if a stale token is present in the file (a hand
        edit, a credential export that captured it), file-last precedence
        would serve the pre-rotation value to every execution. The explicit
        hot-reload is the most recent signal and wins.
        """
        mod = _load(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "at-boot"})
        env_file.write_text('CLAUDE_CODE_OAUTH_TOKEN="stale-in-file"\n')

        mod.set_runtime_override("CLAUDE_CODE_OAUTH_TOKEN", "rotated")

        env = mod.build_execution_env(env_file=env_file)
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "rotated"

    def test_rotation_reaches_the_next_execution_with_no_env_file_at_all(
        self, monkeypatch, env_file
    ):
        mod = _load(monkeypatch, {})
        mod.set_runtime_override("CLAUDE_CODE_OAUTH_TOKEN", "rotated")
        assert (
            mod.build_execution_env(env_file=env_file)["CLAUDE_CODE_OAUTH_TOKEN"]
            == "rotated"
        )

    def test_remove_api_key_is_a_force_unset_a_dict_merge_cannot_express(
        self, monkeypatch, env_file
    ):
        """`reload-token(remove_api_key=True)` must drop the key even though
        it is present in the container baseline."""
        mod = _load(monkeypatch, {"ANTHROPIC_API_KEY": "baked"})
        mod.set_runtime_override("ANTHROPIC_API_KEY", None)
        assert "ANTHROPIC_API_KEY" not in mod.build_execution_env(env_file=env_file)

    def test_execution_tag_cannot_be_displaced_by_the_env_file(
        self, monkeypatch, env_file
    ):
        """#407's orphan sweep keys on EXECUTION_TAG_NAME; a `.env` key of the
        same name must not be able to shadow it."""
        mod = _load(monkeypatch, {})
        env_file.write_text('TRINITY_EXECUTION_ID="attacker"\n')

        env = mod.build_execution_env(
            {"TRINITY_EXECUTION_ID": "real-exec"}, env_file=env_file
        )
        assert env["TRINITY_EXECUTION_ID"] == "real-exec"

    def test_baseline_is_never_mutated_by_a_build(self, monkeypatch, env_file):
        mod = _load(monkeypatch, {"K": "baseline"})
        env_file.write_text('K="from-file"\nNEW="x"\n')

        mod.build_execution_env(env_file=env_file)

        assert mod.INITIAL_ENV == {"K": "baseline"}


# ---------------------------------------------------------------------------
# .env parsing
# ---------------------------------------------------------------------------

class TestParsing:

    @pytest.mark.parametrize(
        "line,expected",
        [
            ('K="v"', "v"),
            ("K='v'", "v"),
            ("K=v", "v"),
            ('K=""', ""),
            ("K=", ""),
            ('K="  spaced  "', "  spaced  "),
        ],
    )
    def test_value_shapes(self, monkeypatch, env_file, line, expected):
        mod = _load(monkeypatch, {})
        env_file.write_text(line + "\n")
        assert mod.parse_env_file(env_file)["K"] == expected

    @pytest.mark.parametrize("line,key,expected", [
        # Byte-faithful to the export loop this replaces, ON PURPOSE. Each of
        # these is a quirk someone might "fix" — and the ent#127 predicate
        # (`credential_requirements_service._env_pairs`, whose source is
        # spliced into an in-container probe) is defined as agreement with
        # exactly these results. Improving the parse is a separate change with
        # ent#127's parity test as its gate; doing it inside a revocation fix
        # would move that predicate silently.
        ('K=""""', "K", ""),            # every quote layer peeled, not one pair
        ('K="\'v\'"', "K", "v"),         # both quote chars stripped, in order
        ("K='\"v\"'", "K", '"v"'),       # ... so the inner pair survives here
        ('K="', "K", ""),               # a lone quote is not a value
        ("export K=v", "export K", "v"),  # the name really is two words
        ("K-E-Y=v", "K-E-Y", "v"),      # os.environ accepts it; the child got it
    ])
    def test_quirks_are_preserved_deliberately(
        self, monkeypatch, env_file, line, key, expected
    ):
        mod = _load(monkeypatch, {})
        env_file.write_text(line + "\n")
        assert mod.parse_env_file(env_file)[key] == expected

    @pytest.mark.parametrize(
        "content",
        [
            "# comment\n",
            "\n\n",
            "no_equals_sign\n",
            "=novalue\n",   # empty key
        ],
    )
    def test_junk_lines_are_skipped_not_fatal(self, monkeypatch, env_file, content):
        mod = _load(monkeypatch, {})
        env_file.write_text(content)
        assert mod.parse_env_file(env_file) == {}

    @pytest.mark.parametrize("content,key", [
        ("1BAD=x\n", "1BAD"),
        ("BAD KEY=x\n", "BAD KEY"),
        ("-BAD=x\n", "-BAD"),
    ])
    def test_odd_names_are_still_bound(self, monkeypatch, env_file, content, key):
        """Not a valid shell identifier, but `os.environ[...]` accepts it and
        the loop this replaces bound it — so the child DID receive it. Adding a
        name filter here would be a silent behaviour narrowing inside a
        revocation fix, and would also fork the ent#127 predicate."""
        mod = _load(monkeypatch, {})
        env_file.write_text(content)
        assert mod.parse_env_file(env_file)[key] == "x"

    def test_missing_file_is_empty_not_an_error(self, monkeypatch, tmp_path):
        mod = _load(monkeypatch, {})
        assert mod.parse_env_file(tmp_path / "nope") == {}

    def test_oversized_file_is_ignored(self, monkeypatch, env_file):
        mod = _load(monkeypatch, {})
        env_file.write_text("K=" + "x" * (1024 * 1024 + 10) + "\n")
        assert mod.parse_env_file(env_file) == {}

    def test_a_spawn_still_works_when_the_env_file_is_unreadable(
        self, monkeypatch, env_file
    ):
        """Parsing runs on the spawn path — refusing to parse means refusing
        to run, so a broken file must degrade to the baseline, not raise."""
        mod = _load(monkeypatch, {"K": "baseline"})
        env_file.write_text("K=x\n")
        env_file.chmod(0o000)
        try:
            env = mod.build_execution_env(env_file=env_file)
        finally:
            env_file.chmod(0o600)
        assert env["K"] == "baseline"


# ---------------------------------------------------------------------------
# Protected keys
# ---------------------------------------------------------------------------

class TestProtectedKeys:

    @pytest.mark.parametrize("key", ["PATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "BASH_ENV"])
    def test_env_file_cannot_redirect_what_the_child_executes_or_loads(
        self, monkeypatch, env_file, key
    ):
        """`.env` is agent-writable and is now read at EVERY spawn — a wider
        trigger than the old backend-only mirror. Credential names are fine;
        loader/exec redirection is not."""
        mod = _load(monkeypatch, {"PATH": "/usr/bin"})
        env_file.write_text(f'{key}="/tmp/evil"\n')

        env = mod.build_execution_env(env_file=env_file)

        assert env.get(key) != "/tmp/evil"

    def test_protected_keys_are_not_mirrored_into_the_process_either(
        self, monkeypatch, env_file
    ):
        mod = _load(monkeypatch, {"PATH": "/usr/bin"})
        env_file.write_text('PATH="/tmp/evil"\n')
        mod.sync_process_env(env_file)
        assert os.environ["PATH"] == "/usr/bin"


# ---------------------------------------------------------------------------
# Diagnosability
# ---------------------------------------------------------------------------

class TestDriftReport:

    def test_drift_is_reported_by_name_only(self, monkeypatch, env_file):
        """Values are never included — this endpoint is read during incident
        response on a credential-bearing surface."""
        mod = _load(monkeypatch, {})
        env_file.write_text('TOKEN_A="secret-value"\n')

        report = mod.env_drift_report(env_file)

        assert report == [
            {"key": "TOKEN_A", "in_file": True, "in_process_env": False, "equal": False}
        ]
        assert "secret-value" not in repr(report)

    def test_a_ghost_key_is_visible_in_the_report(self, monkeypatch, env_file):
        """The whole point: the state that took hours to diagnose is one call."""
        mod = _load(monkeypatch, {})
        env_file.write_text('CANARY_VAR="ghost123"\n')
        mod.sync_process_env(env_file)

        # Simulate the pre-fix ghost: file cleaned, process env still carrying it.
        env_file.write_text("")
        os.environ["CANARY_VAR"] = "ghost123"

        entry = next(e for e in mod.env_drift_report(env_file) if e["key"] == "CANARY_VAR")
        assert entry == {
            "key": "CANARY_VAR",
            "in_file": False,
            "in_process_env": True,
            "equal": False,
        }


# ---------------------------------------------------------------------------
# Uniform application across runtimes (AC)
# ---------------------------------------------------------------------------

def test_every_spawn_site_builds_its_env_through_the_helper():
    """AC: applied uniformly across Claude Code, Codex and Gemini.

    Static, on purpose — a sixth spawn site added with the old
    `env={**os.environ, ...}` form would silently re-open the bug, and only a
    grep-shaped assertion fails on that.
    """
    services = _ROOT / "docker" / "base-image" / "agent_server" / "services"
    offenders = []
    for path in services.rglob("*.py"):
        if path.name == "execution_env.py":
            continue
        for num, line in enumerate(path.read_text().splitlines(), 1):
            if "env={**os.environ" in line or "**os.environ," in line:
                offenders.append(f"{path.relative_to(_ROOT)}:{num}")
    assert not offenders, (
        "these spawn sites still snapshot the mutated process env instead of "
        f"calling build_execution_env (#1999): {offenders}"
    )
