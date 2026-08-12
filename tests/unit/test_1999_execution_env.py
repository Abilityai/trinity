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


@pytest.fixture(autouse=True)
def _isolate_environ():
    """Snapshot and restore the CONTENTS of `os.environ`, never the object.

    An earlier version of this fixture did
    `monkeypatch.setattr(os, "environ", dict(baseline))`, i.e. replaced the
    process-global mapping with a plain dict for the duration of each test.
    That is hermetic for the test and hostile to everything else in the
    process: `os.environ` stops writing through to the C environment, and any
    thread still alive from another test — an APScheduler tick, an event-bus
    dispatcher — sees an env with no PATH or HOME for that window. Whether such
    a thread overlaps these tests is decided by `pytest-randomly`'s seed, which
    is exactly the shape of a failure that appears under one seed and not the
    other two.

    Restoring contents keeps the real mapping (and its putenv side effects)
    intact for everyone else while still giving each test a clean slate.
    """
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


def _load(monkeypatch, baseline: dict):
    """Import a FRESH copy of the module under a controlled baseline.

    `INITIAL_ENV` is the container baseline — what `docker exec` shows —
    captured at import time. It is set explicitly here rather than by staging
    a fake `os.environ` before the import, so controlling the baseline costs
    nothing outside this module. Re-importing per test also keeps the
    module-level override / mirrored-key state from leaking between tests.

    Keys the baseline does not define are removed from the real environ, so a
    test asserting "this key must not reach the child" cannot pass or fail on
    whatever the developer's shell happened to export.
    """
    spec = importlib.util.spec_from_file_location(
        f"_execution_env_1999_{len(sys.modules)}", _MODULE
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    mod.INITIAL_ENV = dict(baseline)
    for key in _CONTROLLED_KEYS:
        if key in baseline:
            os.environ[key] = baseline[key]
        else:
            os.environ.pop(key, None)
    return mod


# The keys these tests reason about. Scoped rather than clearing the whole
# environ, so PATH/HOME stay real for anything else running in-process.
_CONTROLLED_KEYS = (
    "CANARY_VAR", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "AGENT_RUNTIME",
    "GOOGLE_API_KEY", "K", "OTHER", "SOMETHING", "TOKEN_A", "TRINITY_EXECUTION_ID",
)


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
        # These WERE byte-faithful to the export loop #1999 replaced, pinned
        # so that improving the parse had to be a deliberate, separate change
        # with ent#127's parity test as its gate — not a silent drift inside a
        # revocation fix.
        #
        # #2023 is that change, and the pin did its job: three of these fired
        # when the reader learned to strip ONE matched pair and reverse the
        # writer's escaping, forcing the decision into the open instead of
        # letting the predicate move underneath its consumers. The rows that
        # did not move are left as they were — still quirks a future reader
        # might "fix" by accident.
        ('K=""""', "K", '""'),          # #2023: ONE pair stripped, not every layer
        ('K="\'v\'"', "K", "'v'"),       # #2023: the inner single quotes are data
        ("K='\"v\"'", "K", '"v"'),       # ... so the inner pair survives here
        ('K="', "K", '"'),              # #2023: an unterminated quote is not a strip
        ("export K=v", "export K", "v"),  # the name really is two words
        ("K-E-Y=v", "K-E-Y", "v"),      # os.environ accepts it; the child got it
    ])
    def test_parse_quirks_are_pinned_so_a_change_is_deliberate(
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
        """Asserted as "PATH is unchanged", not "PATH equals a fixture value":
        the real mapping is in play now, and unchanged is the actual claim."""
        mod = _load(monkeypatch, {})
        before = os.environ.get("PATH")
        env_file.write_text('PATH="/tmp/evil"\n')
        mod.sync_process_env(env_file)
        assert os.environ.get("PATH") == before


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
            {"key": "TOKEN_A", "in_file": True, "in_process_env": False,
             "equal": False, "suppressed_for_spawn": False}
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
            "suppressed_for_spawn": False,
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
    # BOTH trees (#2010 review): `routers/brain_orb.py` already spawns a
    # subprocess, so a guard that walks only `services/` is the #1965 lesson
    # ("a guard that walks only one of the two trees is not a guard") one
    # directory over.
    agent_server = _ROOT / "docker" / "base-image" / "agent_server"
    roots = [agent_server / "services", agent_server / "routers"]
    offenders = []
    for root in roots:
        for path in root.rglob("*.py"):
            if path.name == "execution_env.py":
                continue
            for num, line in enumerate(path.read_text().splitlines(), 1):
                if "env={**os.environ" in line or "**os.environ," in line:
                    offenders.append(f"{path.relative_to(_ROOT)}:{num}")
    assert not offenders, (
        "these spawn sites still snapshot the mutated process env instead of "
        f"calling build_execution_env (#1999): {offenders}"
    )


def test_the_router_call_sites_that_keep_1089_working_are_wired():
    """The AC table cites two `set_runtime_override(...)` calls as what keeps
    the #1089 token hot-reload working, and nothing covered them: deleting both
    left the suite green. Static for the same reason as the guard above — the
    behaviour they protect needs a live container to observe.
    """
    import ast

    src = (_ROOT / "docker" / "base-image" / "agent_server" / "routers"
           / "credentials.py").read_text()
    tree = ast.parse(src)
    overrides = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "set_runtime_override"
    ]
    keys = {
        n.args[0].value for n in overrides
        if n.args and isinstance(n.args[0], ast.Constant)
    }
    assert "CLAUDE_CODE_OAUTH_TOKEN" in keys, (
        "the reload-token route no longer records the rotated token as a "
        "runtime override — the next subprocess would read the stale .env "
        "value and #1089 silently stops working"
    )
    # #2114: the force-unset now iterates SUBSCRIPTION_SHADOW_KEYS (one shared
    # constant, so ANTHROPIC_AUTH_TOKEN cannot drift from ANTHROPIC_API_KEY),
    # so the key no longer appears as a Constant first-arg. Assert the loop
    # form is wired instead...
    unset_loop_over_shadow_keys = any(
        isinstance(n, ast.For)
        and isinstance(n.iter, ast.Name)
        and n.iter.id == "SUBSCRIPTION_SHADOW_KEYS"
        and any(
            isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and c.func.id == "set_runtime_override"
            and len(c.args) > 1 and isinstance(c.args[1], ast.Constant)
            and c.args[1].value is None
            for b in n.body for c in ast.walk(b)
        )
        for n in ast.walk(tree)
    )
    assert unset_loop_over_shadow_keys, (
        "the remove_api_key path no longer force-unsets the subscription-shadow "
        "keys, so a subscription switch leaves the old key applying"
    )
    # ...and that the shared constant still carries the key this guard
    # originally pinned (loaded from the spawn module — the single definition).
    spec = importlib.util.spec_from_file_location("_exec_env_1089_wiring", _MODULE)
    emod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(emod)
    assert "ANTHROPIC_API_KEY" in emod.SUBSCRIPTION_SHADOW_KEYS
    assert any(
        isinstance(n.args[1], ast.Constant) and n.args[1].value is None
        for n in overrides if len(n.args) > 1
    ) or unset_loop_over_shadow_keys, (
        "the force-unset form (value=None) is gone — only the file layer remains"
    )


# ---------------------------------------------------------------------------
# Known limitation carried by #2010 — closed by #2023 (PR #2030)
# ---------------------------------------------------------------------------

def test_update_delivers_quote_bearing_values_intact(tmp_path):
    """Was `xfail(strict=True)` while #2010 shipped with the write-only
    escaping; the marker's own reason said it flips to XPASS when #2023 lands,
    and this is that flip — the marker is gone, the assertions are unchanged.

    What it pins NOW is backward compatibility: these lines reproduce the OLD
    writer's quote-only escaping, i.e. the `.env` files already sitting on
    deployed agents' disks from before the encoding became reversible. The
    #2023 reader must deliver those values intact too, not only ones its own
    `format_env_line` produced (that round trip is test_2023's job).
    """
    import sys

    sys.path.insert(0, str(_ROOT / "docker" / "base-image"))
    from agent_server.services.execution_env import parse_env_file

    env_file = tmp_path / ".env"
    for raw in ('pa"ss', "'quoted'", 'tail"'):
        # The pre-#2023 writer, byte for byte: quote-only escaping.
        escaped = str(raw).replace('"', '\\"')
        env_file.write_text(f'K="{escaped}"\n')
        assert parse_env_file(env_file)["K"] == raw, (
            f"delivered {parse_env_file(env_file)['K']!r} for {raw!r}"
        )
