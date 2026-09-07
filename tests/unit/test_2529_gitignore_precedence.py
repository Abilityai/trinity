"""Canonical `.gitignore` precedence and honest sweep reporting (#2529).

Both writers of an agent's `.gitignore` appended, and git is last-match-wins,
so the canonical `_GITIGNORE_PATTERNS` block — appended to the END of the file
on every Push — silently overrode every `!negation` the agent wrote above it,
and `_build_rm_cached_ignored_command` then `git rm --cached`'d the files those
negations were protecting. Two confirmed field instances (2026-07-30 internal
fleet agent, causing commit `47efd80`; corbin 2026-09-02, hand-restored with
the comment *"Negation must stay LAST in this file."*).

`.env.example` is the headline casualty: compat check F-004 requires it and
`credential_requirements_service` reads it, so an agent that ships one loses it
on its first Push and then fails its own compatibility contract.

These tests exercise the REAL builders against REAL git repositories — the
defect lives in git's own last-match-wins semantics, which a mocked container
cannot express. Modelled on `test_2070_trinity_authored_paths.py`'s harness.

TWO CONSECUTIVE Pushes are the load-bearing shape: the first Push on a
pre-canonical repo appends the block and the file is *already* gone by the
second, so a single-Push assertion passes even with the bug live.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = str(_REPO / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

_ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _gs():
    try:
        import services.git_service as gs
    except Exception:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")
    return gs


def _git(cwd: Path, *args: str) -> str:
    env = dict(_ENV, HOME=str(cwd))
    out = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True
    )
    assert out.returncode == 0, f"git {' '.join(args)} failed: {out.stderr}"
    return out.stdout


def _run(cmd: str, cwd: Path) -> subprocess.CompletedProcess:
    """Run a builder's command the way the backend does — docker-py splits a
    string command with shlex, so the test does too."""
    env = dict(_ENV, HOME=str(cwd))
    return subprocess.run(
        shlex.split(cmd), cwd=cwd, env=env, capture_output=True, text=True
    )


def _make_repo(tmp_path: Path, files: dict[str, str], gitignore: str) -> Path:
    home = tmp_path / "developer"
    home.mkdir(parents=True, exist_ok=True)
    for rel, body in files.items():
        target = home / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    (home / ".gitignore").write_text(gitignore)
    _git(home, "init", "-q", ".")
    _git(home, "config", "user.email", "t@t")
    _git(home, "config", "user.name", "t")
    _git(home, "add", "-A", "-f")
    _git(home, "commit", "-qm", "init")
    return home


def _push(home: Path) -> set[str]:
    """The two commands a backend Push runs, in order. Returns tracked paths."""
    _push_sweep(home)
    return set(_git(home, "ls-files").split())


def _push_sweep(home: Path):
    """The same two commands, returning the parsed `GitignoreSweep`.

    Both probes ride on these two execs, so the report is produced by the exact
    commands the backend runs — not by a second, test-only code path.
    """
    gs = _gs()
    outs = []
    for build in (gs._build_gitignore_merge_command, gs._build_rm_cached_ignored_command):
        out = _run(build(str(home)), home)
        assert out.returncode == 0, f"command failed: {out.stderr[:400]}"
        outs.append(out.stdout)
    return gs._parse_gitignore_sweep(*outs)


def _gitignore_lines(home: Path) -> list[str]:
    return (home / ".gitignore").read_text().splitlines()


def _regions(home: Path) -> tuple[list[str], list[str], list[str]]:
    """(defaults block, user region, protected floor) as line lists."""
    gs = _gs()
    lines = _gitignore_lines(home)
    b1 = lines.index(gs._GITIGNORE_BLOCK_BEGIN)
    e1 = lines.index(gs._GITIGNORE_BLOCK_END)
    b2 = lines.index(gs._GITIGNORE_FLOOR_BEGIN)
    e2 = lines.index(gs._GITIGNORE_FLOOR_END)
    return lines[b1 + 1:e1], lines[e1 + 1:b2], lines[b2 + 1:e2]


# ---------------------------------------------------------------------------
# The reported bug (AC-5 / AC-6)
# ---------------------------------------------------------------------------

def test_env_example_survives_two_consecutive_pushes(tmp_path):
    """AC-5. `.env.example` is negated by the agent AND followed by an unrelated
    rule, so the win cannot come from merely being the file's last line."""
    home = _make_repo(
        tmp_path,
        {".env.example": "API_KEY=\n", "CLAUDE.md": "agent\n"},
        "!.env.example\nmy-scratch/\n",
    )
    assert ".env.example" in _push(home), "untracked by the FIRST Push"
    assert ".env.example" in _push(home), (
        ".env.example was untracked by the SECOND Push — the canonical block "
        "appended below the agent's negation and reversed it (#2529)"
    )


def test_claude_settings_json_negation_survives_a_push(tmp_path):
    """AC-6. #2036's rationale offered `!.claude/settings.json` as the escape
    hatch; the hatch has to survive the next append."""
    home = _make_repo(
        tmp_path,
        {".claude/settings.json": "{}\n", "CLAUDE.md": "agent\n"},
        "!.claude/settings.json\nmy-scratch/\n",
    )
    assert ".claude/settings.json" in _push(home)
    assert ".claude/settings.json" in _push(home), (
        "the #2036 negation hatch did not survive a second Push (#2529)"
    )


def test_glob_negation_survives(tmp_path):
    """corbin's own restore was `!**/.env.example` at nested depth — the exact
    line hand-added in `ac4ebaa` with the comment "Negation must stay LAST in
    this file." It must no longer have to be last."""
    home = _make_repo(
        tmp_path,
        {
            "skills/deploy/.env.example": "TOKEN=\n",
            "archive/old/.env.example": "TOKEN=\n",
            "CLAUDE.md": "agent\n",
        },
        "!**/.env.example\nmy-scratch/\n",
    )
    tracked = _push(home)
    tracked = _push(home)
    assert "skills/deploy/.env.example" in tracked
    assert "archive/old/.env.example" in tracked


def test_defaults_block_is_above_user_rules(tmp_path):
    home = _make_repo(tmp_path, {"CLAUDE.md": "a\n"}, "my-rule/\n!keep.md\n")
    _push(home)
    lines = _gitignore_lines(home)
    gs = _gs()
    assert lines.index(gs._GITIGNORE_BLOCK_END) < lines.index("my-rule/")
    assert lines.index("!keep.md") < lines.index(gs._GITIGNORE_FLOOR_BEGIN)


def test_user_rules_keep_their_order(tmp_path):
    """The user region is the original file minus managed lines, in the original
    order — a re-sort would silently change which of two of the agent's own
    rules wins."""
    home = _make_repo(
        tmp_path,
        {"CLAUDE.md": "a\n"},
        "zeta/\n# a comment\nalpha/\n!alpha/keep.md\nmiddle/\n",
    )
    _push(home)
    _, user, _ = _regions(home)
    assert user == ["zeta/", "# a comment", "alpha/", "!alpha/keep.md", "middle/"]


# ---------------------------------------------------------------------------
# The protected floor (Decisions 5-6)
# ---------------------------------------------------------------------------

def test_user_negation_cannot_un_ignore_a_credential(tmp_path):
    """A single hoisted block would turn every currently-INERT `!.env` in the
    fleet live in one Push, and the unattended 15-minute `git add -A` would then
    commit the credential to the user's own GitHub repo. The floor makes that
    structurally impossible."""
    home = _make_repo(
        tmp_path,
        {".env": "SECRET=live\n", "CLAUDE.md": "a\n"},
        "!.env\n!.env.production\n",
    )
    (home / ".env.production").write_text("SECRET=live\n")
    _push(home)
    for path in (".env", ".env.production"):
        out = subprocess.run(
            ["git", "check-ignore", "-q", path],
            cwd=home, env=dict(_ENV, HOME=str(home)), capture_output=True,
        )
        assert out.returncode == 0, f"{path} is NOT ignored — the floor was overridden"
    assert ".env" not in _push(home)


def test_a_user_negation_cannot_un_ignore_ssh_key_material(tmp_path):
    """The MIRROR of this issue's own bug, found in review and reproduced.

    Hoisting the defaults block above the user region is only safe for a default
    the agent may genuinely override. `.ssh/` is not one — `credential_paths.py`
    classifies `.ssh/id_*` as secret material — and it started this branch in
    the OVERRIDABLE region, one directory over from the `.env` case Decision 1
    reasoned about.

    The shape is the live fleet's, not a contrived one: `!.ssh` written by the
    agent, with the canonical block APPENDED below it by every pre-#2529 Push.
    Under the old append-only merge `.ssh/id_rsa` was ignored. Under a hoist
    with `.ssh/` in the top region it is not, and the unattended 15-minute
    `git add -A` commits a private key to the owner's GitHub repo.
    """
    gs = _gs()
    home = _make_repo(
        tmp_path,
        {"CLAUDE.md": "a\n"},
        "!.ssh\n" + "".join(f"{p}\n" for p in gs._GITIGNORE_PATTERNS),
    )
    (home / ".ssh").mkdir(exist_ok=True)
    (home / ".ssh" / "id_rsa").write_text("PRIVATE KEY\n")
    (home / ".ssh" / "authorized_keys").write_text("ssh-ed25519 AAAA\n")

    # Pre-merge: ignored, because the appended canonical `.ssh/` is last.
    pre = subprocess.run(
        ["git", "check-ignore", "-q", ".ssh/id_rsa"],
        cwd=home, env=dict(_ENV, HOME=str(home)), capture_output=True,
    )
    assert pre.returncode == 0, "test setup no longer reproduces the live shape"

    _push(home)

    post = subprocess.run(
        ["git", "check-ignore", "-q", ".ssh/id_rsa"],
        cwd=home, env=dict(_ENV, HOME=str(home)), capture_output=True,
    )
    assert post.returncode == 0, (
        ".ssh/id_rsa is NOT ignored after the merge — a user `!.ssh` beat the "
        "managed default, and the next `git add -A` commits a private key"
    )
    # And the surface that actually leaks: what the auto-sync loop would stage.
    _git(home, "add", "-A")
    staged = set(_git(home, "diff", "--cached", "--name-only").split())
    assert not {p for p in staged if p.startswith(".ssh/")}, (
        f"the auto-sync `git add -A` would commit {sorted(staged)}"
    )


def test_credential_bearing_defaults_are_all_in_the_floor():
    """A membership assertion, so adding a credential-bearing pattern to the
    OVERRIDABLE region is a decision someone has to make on purpose.

    The list is anchored on `services/credential_paths.py` — the platform's own
    answer to "is this secret material" — rather than on the source file's
    comment headings, which is exactly how `.ssh/` was missed: it sits under
    "Instance-specific directories", not under "Credentials".
    """
    gs = _gs()
    for pattern in (".env", ".env.*", ".mcp.json", "credentials.json",
                    "*.pem", "*.key", ".ssh/"):
        assert pattern in gs._GITIGNORE_PROTECTED, (
            f"{pattern} is credential-bearing but sits in the overridable "
            "defaults block — an agent negation would un-ignore it and the "
            "next `git add -A` would commit it"
        )


def test_authored_trinity_hook_survives_a_user_wildcard(tmp_path):
    """The R2 regression, pinned: a user `*.sh` below a hoisted single block
    would beat `!.trinity/setup.sh` (trinity-enterprise#76 / #1704), and it would
    fail QUIETLY — the rm-cached pathspec still exempts the path, so nothing is
    untracked; the hook is simply never `git add`-ed again."""
    home = _make_repo(
        tmp_path,
        {".trinity/setup.sh": "#!/bin/sh\n", "CLAUDE.md": "a\n"},
        "*.sh\n*.yaml\n*.json\n",
    )
    _push(home)
    out = subprocess.run(
        ["git", "check-ignore", "-q", ".trinity/setup.sh"],
        cwd=home, env=dict(_ENV, HOME=str(home)), capture_output=True,
    )
    assert out.returncode == 1, (
        ".trinity/setup.sh is ignored — a user wildcard beat the platform's own "
        "re-include, so the hook would never be staged again"
    )
    assert ".trinity/setup.sh" in _push(home)


def test_protected_set_is_a_subset_of_the_pattern_list():
    """The floor is a PARTITION of the canonical list, not a second list. A
    pattern in the floor but not in the list would be written to the file and
    never stripped, so the merge would stop being idempotent."""
    gs = _gs()
    assert gs._GITIGNORE_PROTECTED <= set(gs._GITIGNORE_PATTERNS)
    # And the partition is total: every canonical pattern lands in exactly one
    # region.
    top = [p for p in gs._GITIGNORE_PATTERNS if p not in gs._GITIGNORE_PROTECTED]
    floor = [p for p in gs._GITIGNORE_PATTERNS if p in gs._GITIGNORE_PROTECTED]
    assert len(top) + len(floor) == len(gs._GITIGNORE_PATTERNS)
    assert set(floor) == gs._GITIGNORE_PROTECTED


def test_every_authored_trinity_path_is_protected():
    """#2070 derives the `!` re-includes from `_TRINITY_AUTHORED_PATHS`; #2529
    must derive their PROTECTION from the same tuple. A tenth authored path
    added tomorrow inherits the floor without anyone remembering to ask."""
    gs = _gs()
    for path in gs._TRINITY_AUTHORED_PATHS:
        assert f"!{path}" in gs._GITIGNORE_PROTECTED, (
            f"!{path} is not in the protected floor — a user wildcard would beat it"
        )


def test_env_example_negation_follows_the_env_pattern():
    """Order inside the floor is load-bearing: `!.env.example` must come AFTER
    `.env.*`, or the broader rule wins on the last-match."""
    gs = _gs()
    floor = [p for p in gs._GITIGNORE_PATTERNS if p in gs._GITIGNORE_PROTECTED]
    assert floor.index("!.env.example") > floor.index(".env.*")
    assert floor.index("!.mcp.json.template") > floor.index(".mcp.json")


# ---------------------------------------------------------------------------
# Merge mechanics
# ---------------------------------------------------------------------------

def test_merge_is_idempotent(tmp_path):
    """Byte-identical second run AND a clean `git status --porcelain`. The
    mtime is deliberately NOT asserted: `touch`/`: >` on an existing file is not
    the invariant, content-identity is — and the 15-minute auto-sync loop only
    cares about the latter."""
    home = _make_repo(tmp_path, {"CLAUDE.md": "a\n"}, "my-rule/\n")
    gs = _gs()
    _run(gs._build_gitignore_merge_command(str(home)), home)
    first = (home / ".gitignore").read_text()
    _git(home, "add", "-A")
    _git(home, "commit", "-qm", "merged")
    _run(gs._build_gitignore_merge_command(str(home)), home)
    assert (home / ".gitignore").read_text() == first
    assert _git(home, "status", "--porcelain") == "", "the merge manufactured drift"


def test_crlf_canonical_line_is_stripped_not_duplicated(tmp_path):
    """R7. `grep -vxF` with LF patterns does not match a CRLF copy, so a
    canonical line written by a Windows editor would survive BELOW the block and
    keep overriding the very negation this fix protects."""
    home = _make_repo(tmp_path, {"CLAUDE.md": "a\n"}, "")
    (home / ".gitignore").write_bytes(b".env.*\r\n!.env.example\r\nmy-rule/\r\n")
    _run(_gs()._build_gitignore_merge_command(str(home)), home)
    gs = _gs()
    # Byte-level, because `str.splitlines()` splits on a bare CR too and would
    # hide exactly the distinction under test.
    raw = (home / ".gitignore").read_bytes()
    body = raw.split(gs._GITIGNORE_BLOCK_END.encode())[1]
    body = body.split(gs._GITIGNORE_FLOOR_BEGIN.encode())[0]
    assert b".env.*" not in body, (
        f"a CRLF canonical line survived in the user region: {body!r}"
    )
    # Targeted: the user's OWN line keeps its CRLF. A `tr -d '\r'` fix would
    # rewrite the whole file's line endings instead.
    assert b"my-rule/\r\n" in body, f"the user's own CRLF rule was mangled: {body!r}"


def test_unreadable_gitignore_aborts_without_writing(tmp_path):
    """R6, the data-loss path. `cat <(grep ...)` takes only `cat`'s status, so
    an unreadable `.gitignore` produced a block-only temp file and the `mv` then
    destroyed the user's rules — `mv` needs DIRECTORY write permission, not
    file. Reproduced on the shipped base image with a mode-000 file."""
    home = _make_repo(tmp_path, {"CLAUDE.md": "a\n"}, "my-precious-rule/\n")
    target = home / ".gitignore"
    original = target.read_bytes()
    target.chmod(0o000)
    try:
        out = _run(_gs()._build_gitignore_merge_command(str(home)), home)
        assert out.returncode != 0, "an unreadable .gitignore must abort the merge"
    finally:
        target.chmod(0o644)
    assert target.read_bytes() == original, "the user's .gitignore was destroyed"


def test_nul_byte_gitignore_does_not_lose_user_rules(tmp_path):
    """R11. Without `-a`, grep prints "binary file matches" and emits ZERO lines
    while exiting ZERO — so even an explicit status check passes and the whole
    user region is silently dropped."""
    home = _make_repo(tmp_path, {"CLAUDE.md": "a\n"}, "")
    (home / ".gitignore").write_bytes(b"my-rule/\n\x00binary\nkeep-me/\n")
    _run(_gs()._build_gitignore_merge_command(str(home)), home)
    body = (home / ".gitignore").read_bytes()
    assert b"my-rule/" in body and b"keep-me/" in body, (
        "the user region was dropped on a NUL-bearing .gitignore"
    )


def test_empty_pattern_is_rejected_at_import():
    """The `grep -F -f` wipe guard. An empty entry in the strip list matches
    every blank line with `-x` and every line without it."""
    gs = _gs()
    for line in gs._GITIGNORE_MANAGED_LINES:
        assert line, "an empty managed line would delete the user's blank lines"
        assert "\n" not in line and "\r" not in line, (
            f"managed line {line!r} smuggles an extra pattern into the strip list"
        )


def test_file_without_trailing_newline_is_not_glued(tmp_path):
    """The old `echo p >> .gitignore` glued its first pattern onto the last line
    of a file with no trailing newline."""
    home = _make_repo(tmp_path, {"CLAUDE.md": "a\n"}, "")
    (home / ".gitignore").write_bytes(b"my-rule/")  # no trailing newline
    _run(_gs()._build_gitignore_merge_command(str(home)), home)
    _, user, _ = _regions(home)
    assert user == ["my-rule/"], f"line-gluing regression: {user}"


def test_merge_creates_a_missing_gitignore(tmp_path):
    home = _make_repo(tmp_path, {"CLAUDE.md": "a\n"}, "")
    (home / ".gitignore").unlink()
    _run(_gs()._build_gitignore_merge_command(str(home)), home)
    top, user, floor = _regions(home)
    assert user == [] and ".env" in floor and "content/" in top


# ---------------------------------------------------------------------------
# Sweep + reporting (AC-1, AC-4, and Eugene's revised criteria 2 and 3)
# ---------------------------------------------------------------------------

def test_removed_paths_are_reported(tmp_path):
    home = _make_repo(
        tmp_path,
        {"errors.log": "boom\n", "cache.db": "x\n", "CLAUDE.md": "a\n"},
        "",
    )
    sweep = _push_sweep(home)
    assert set(sweep.removed) == {"errors.log", "cache.db"}
    # Idempotent: the second Push has nothing left to report.
    assert _push_sweep(home).removed == ()


def test_unignored_paths_are_reported(tmp_path):
    """Criterion 3. A user's negation ABOVE their own canonical duplicate is
    INVERTED by the rebuild: the file becomes tracked and the same Push's
    `git add -A` commits it. The issue author ruled explicitly against blocking
    here — "a frozen repo is harder to notice because nothing changes. Proceed
    and report" — so this field is the whole control."""
    home = _make_repo(tmp_path, {"CLAUDE.md": "a\n"}, "!keep.log\n*.log\n")
    (home / "keep.log").write_text("keep\n")
    # Before the rebuild the user's own `*.log` sits BELOW the negation and wins.
    assert subprocess.run(
        ["git", "check-ignore", "-q", "keep.log"],
        cwd=home, env=dict(_ENV, HOME=str(home)), capture_output=True,
    ).returncode == 0
    sweep = _push_sweep(home)
    assert "keep.log" in sweep.unignored, (
        f"the inverted duplicate was not reported: {sweep}"
    )


def test_shadowed_negations_are_reported(tmp_path):
    """Criterion 2. The two residuals #2529 knowingly leaves — a negation under
    a dir-form canonical pattern, and a negation the protected floor refuses —
    are the ONLY thing this field exists to say out loud."""
    home = _make_repo(
        tmp_path,
        {"content/keep.md": "k\n", "CLAUDE.md": "a\n"},
        "!content/keep.md\n!.env\n!my-own.txt\nmy-own.txt\n",
    )
    sweep = _push_sweep(home)
    assert "!content/keep.md -> content/" in sweep.shadowed
    assert "!.env -> .env" in sweep.shadowed
    # The agent's own rule defeating its own negation is the agent's business,
    # not ours to explain.
    assert not any("my-own.txt" in entry for entry in sweep.shadowed), sweep.shadowed


def test_check_ignore_verdict_comes_from_the_pattern_not_the_exit_code(tmp_path):
    """THE trap. `git check-ignore -v` exits 0 even when the deciding rule is
    itself a NEGATION — i.e. even when the path is not ignored. A verdict read
    off the exit code would report every honoured negation as shadowed."""
    home = _make_repo(tmp_path, {".env.example": "K=\n", "CLAUDE.md": "a\n"}, "")
    _push(home)
    # `--no-index` mirrors the production probe: without it a TRACKED path is
    # never reported at all, which would make the report blind to exactly the
    # negations that are currently working.
    plain = subprocess.run(
        ["git", "check-ignore", "--no-index", ".env.example"],
        cwd=home, env=dict(_ENV, HOME=str(home)), capture_output=True, text=True,
    )
    verbose = subprocess.run(
        ["git", "check-ignore", "-v", "--no-index", ".env.example"],
        cwd=home, env=dict(_ENV, HOME=str(home)), capture_output=True, text=True,
    )
    assert plain.returncode == 1, "not ignored — that is the whole point"
    assert verbose.returncode == 0, (
        "the trap has changed shape; re-check `_shadowed_negations`"
    )
    assert ":!.env.example\t" in verbose.stdout
    # And the parser gets it right.
    assert _gs()._shadowed_negations([verbose.stdout.strip()]) == ()


@pytest.mark.parametrize(
    "pattern_form,ignore_rule,path,survives",
    [
        # File-form: the negation is effective, so the sweep must not touch it.
        ("file-form", "*.log", "errors.log", True),
        # Dir-form: git never descends into an excluded directory, so the
        # negation is inert AT ANY POSITION and the file IS swept. Pinned as
        # known-and-surfaced rather than silently absent.
        ("dir-form", "content/", "content/keep.md", False),
    ],
)
def test_negation_covered_path_is_not_swept(
    tmp_path, pattern_form, ignore_rule, path, survives
):
    """AC-1 rule (b), parametrised over BOTH canonical pattern forms so it
    cannot pass on a file-form fixture and thereby certify a guarantee that is
    false for the 23 dir-form patterns."""
    home = _make_repo(tmp_path, {path: "x\n", "CLAUDE.md": "a\n"}, f"!{path}\n")
    sweep = _push_sweep(home)
    tracked = set(_git(home, "ls-files").split())
    if survives:
        assert path in tracked, f"an effective negation did not protect {path}"
        assert path not in sweep.removed
    else:
        assert path not in tracked
        assert path in sweep.removed
        assert f"!{path} -> {ignore_rule}" in sweep.shadowed, (
            "a dir-form residual must at least be REPORTED"
        )


def test_no_pre_tracked_path_is_swept_except_declared(tmp_path):
    """AC-1 is a UNIVERSAL, and a single-fixture test proves a strictly weaker
    property. Snapshot the whole tracked set before and after, over a repo
    spanning both regions, a dir-form parent, effective negations and genuine
    #462/#1596 targets, and assert `before - after == set(removed)` exactly."""
    files = {
        "CLAUDE.md": "a\n",
        "src/app.py": "x\n",
        ".env.example": "K=\n",              # protected negation
        ".claude/settings.json": "{}\n",     # user negation
        ".claude/skills/s/SKILL.md": "s\n",  # untouched by any rule
        "keep.db": "x\n",                    # user negation over *.db
        "node_modules/pkg/index.js": "x\n",  # #1596 target — MUST be swept
        "errors.log": "x\n",                 # #462 target — MUST be swept
        "content/keep.md": "k\n",            # dir-form residual — swept
        ".trinity/setup.sh": "#!/bin/sh\n",  # platform-authored — MUST survive
    }
    home = _make_repo(
        tmp_path, files,
        "!.claude/settings.json\n!keep.db\n!content/keep.md\nmy-scratch/\n",
    )
    before = set(_git(home, "ls-files").split())
    sweep = _push_sweep(home)
    after = set(_git(home, "ls-files").split())

    assert before - after == set(sweep.removed), (
        "the sweep untracked paths it did not report (or vice versa): "
        f"unreported={sorted((before - after) - set(sweep.removed))} "
        f"overreported={sorted(set(sweep.removed) - (before - after))}"
    )
    # #462 / #1596 purpose survives — rule (a) would have deleted it.
    assert "errors.log" in sweep.removed
    assert "node_modules/pkg/index.js" in sweep.removed
    # Effective negations hold.
    for path in (".env.example", ".claude/settings.json", "keep.db",
                 ".trinity/setup.sh", ".claude/skills/s/SKILL.md", "src/app.py"):
        assert path in after, f"{path} was untracked despite an effective negation"


def test_462_purpose_survives(tmp_path):
    """A NEWLY added runtime file must still be untracked — that is #462's
    entire reason to exist, and rule (a) as literally worded would delete it."""
    home = _make_repo(tmp_path, {"CLAUDE.md": "a\n"}, "")
    _push(home)
    for rel in (".cache/foo", "errors.log"):
        target = home / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n")
    _git(home, "add", "-A")
    assert ".cache/foo" not in _git(home, "ls-files").split()
    assert "errors.log" not in _git(home, "ls-files").split()


def test_marker_parse_survives_interleaved_stderr():
    """`container_exec_run` does NOT pass `demux=True`, so stdout and stderr
    arrive as ONE blob. A begin/end region parser would swallow a stray
    `grep: ...` line as a payload path; a per-line tag cannot."""
    gs = _gs()
    merge = (
        "TRINITY-2529-untracked-before: draft.md\n"
        "grep: .gitignore: Permission denied\n"
    )
    sweep_out = (
        "grep: warning: stray stderr line\n"
        "TRINITY-2529-removed: errors.log\n"
        "fatal: something unrelated\n"
        "TRINITY-2529-untracked-after: draft.md\n"
        "TRINITY-2529-untracked-after: new-thing.txt\n"
        "TRINITY-2529-shadow: .gitignore:13:content/\tcontent/keep.md\n"
        "warning: yet more noise\n"
    )
    sweep = gs._parse_gitignore_sweep(merge, sweep_out)
    assert sweep.removed == ("errors.log",)
    assert sweep.unignored == ("new-thing.txt",)
    assert sweep.shadowed == ("!content/keep.md -> content/",)


def test_coerce_sweep_accepts_a_magicmock():
    """`test_ent123_tokenless_clone.py` patches `_migrate_workspace_gitignore`
    with a bare `AsyncMock()`, and a `MagicMock` landing in a `List[str]`
    response field fails Pydantic validation at the very return that test
    asserts on. This is required, not defensive noise."""
    from unittest.mock import MagicMock

    gs = _gs()
    assert gs._coerce_sweep(MagicMock()) == gs.GitignoreSweep()
    assert gs._coerce_sweep(None) == gs.GitignoreSweep()
    real = gs.GitignoreSweep(removed=("a",))
    assert gs._coerce_sweep(real) is real


def test_summary_line_and_commit_message():
    """AC-4's commit-message half. Best-effort by construction — `git rm
    --cached` only STAGES — so the shape is asserted, not the delivery."""
    gs = _gs()
    empty = gs.GitignoreSweep()
    assert empty.summary_line() == ""
    assert gs._augment_commit_message("my message", empty) == "my message"

    sweep = gs.GitignoreSweep(removed=tuple(f"f{i}" for i in range(25)))
    augmented = gs._augment_commit_message("my message", sweep)
    assert augmented.startswith("my message\n\n")
    assert "untracked 25 file(s)" in augmented
    assert "- f0" in augmented and "- f19" in augmented
    assert "- f20" not in augmented
    assert "and 5 more" in augmented

    # No caller message: the agent server's own `Trinity sync: <ts>` default is
    # reproduced, because supplying a message at all suppresses it.
    assert gs._augment_commit_message(None, sweep).startswith("Trinity sync: ")


def test_with_sweep_makes_the_failure_path_honest():
    """R5. The router raises `HTTPException(detail=result.message)` on 409/400
    and keeps NOTHING else, so a structured field alone is dead on exactly the
    paths where the index mutation already happened."""
    gs = _gs()
    from database import GitSyncResult

    sweep = gs.GitignoreSweep(removed=("a", "b"), unignored=("c",), shadowed=("d",))
    out = gs._with_sweep(GitSyncResult(success=False, message="Sync conflict"), sweep)
    assert out.removed_paths == ["a", "b"]
    assert out.unignored_paths == ["c"]
    assert out.shadowed_negations == ["d"]
    assert "untracked 2 file(s)" in out.message
    assert out.message.startswith("Sync conflict")

    # An empty sweep leaves the message exactly as it was.
    clean = gs._with_sweep(GitSyncResult(success=True, message="Synced"), gs.GitignoreSweep())
    assert clean.message == "Synced"
    assert clean.removed_paths == []


def test_creation_merge_on_a_pristine_bundled_template_shows_no_drift(tmp_path):
    """R12 / #953, where it actually bites. #1908 ships the 14 bundled templates
    with a `.gitignore` byte-identical to what the platform writes, which is the
    zero-drift property that let #953 stop `startup.sh` producing `M .gitignore`
    against `origin/main`.

    A two-region merge over a template that merely CONTAINED the canonical
    patterns yields `M .gitignore` (39+/37-) on every template-derived agent and
    tears the template's `# GENERATED, do not hand-edit` header off its content.
    The templates are therefore regenerated as the merge's own fixed point —
    proven here against the real shipped bytes, not a synthetic seed.
    """
    template = _REPO / "config" / "agent-templates" / "dd-lead" / ".gitignore"
    home = _make_repo(tmp_path, {"CLAUDE.md": "a\n"}, template.read_text())
    _run(_gs()._build_gitignore_merge_command(str(home)), home)
    porcelain = _git(home, "status", "--porcelain")
    assert porcelain.strip() == "", (
        f"the merge manufactured drift on a pristine bundled template:\n{porcelain}"
    )


def test_managed_lines_are_all_stripped_by_the_builder():
    """The strip list and the shadow oracle are the SAME set. If the builder
    ever stopped stripping a line the oracle still calls "managed", that line
    would accumulate in the user region on every Push."""
    gs = _gs()
    command = gs._build_gitignore_merge_command("/home/developer")
    for line in gs._GITIGNORE_MANAGED_LINES:
        assert shlex.quote(line) in command or line in command, (
            f"managed line {line!r} is not in the merge command's strip list"
        )


def test_untracked_alert_is_budgeted_not_a_direct_create():
    """#1677. The sibling `git_bloat`/`sync_failing` emitters are direct creates
    because their cadence is the 60-second platform poller's. This one fires
    from `sync_to_github`, which an AGENT can drive — `git_sync` is an MCP tool
    an agent-scoped key may call on itself, and a `git add -f <ignored>` + sync
    loop yields a fresh `removed` set every time against a timestamped (not
    idempotent) id. That is the `_alert_skill_not_found` (#1410) shape the budget
    seam exists for, so it routes through `create_bounded_alert` and its id
    prefix is reserved against agent pre-creation (the C2 suppression class).
    """
    import services.operator_queue_service as oqs

    assert "gitignore_untracked" in oqs._BUDGETED_ALERT_TYPES
    assert "gitignore-untracked-" in oqs._RESERVED_ID_PREFIXES


@pytest.mark.asyncio
async def test_untracked_alert_names_the_paths_and_never_raises():
    from unittest.mock import AsyncMock, patch

    gs = _gs()
    sweep = gs.GitignoreSweep(
        removed=tuple(f"f{i}" for i in range(25)),
        shadowed=("!content/keep.md -> content/",),
    )
    with patch(
        "services.operator_queue_service.create_bounded_alert", new=AsyncMock()
    ) as create:
        await gs._emit_gitignore_untracked_alert("alpha", sweep)
    assert create.await_count == 1
    agent_name, item = create.await_args.args
    assert agent_name == "alpha"
    assert item["type"] == "gitignore_untracked"
    assert item["id"].startswith("gitignore-untracked-alpha-")
    assert item["context"]["removed_count"] == 25
    assert len(item["context"]["removed_paths"]) == 20  # capped
    assert item["priority"] == "high"  # a removal is a confirmed destructive act
    assert item["context"]["shadowed_negations"] == ["!content/keep.md -> content/"]

    # An empty sweep files nothing at all.
    with patch(
        "services.operator_queue_service.create_bounded_alert", new=AsyncMock()
    ) as create:
        await gs._emit_gitignore_untracked_alert("alpha", gs.GitignoreSweep())
    assert create.await_count == 0

    # And an alerting failure never reaches the Push.
    with patch(
        "services.operator_queue_service.create_bounded_alert",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        await gs._emit_gitignore_untracked_alert("alpha", sweep)


@pytest.mark.asyncio
async def test_a_newly_unignored_path_is_reported_on_every_surface():
    """The reporting gate is `changed_tracking`, not `removed`.

    Found in review: the same rebuild that stops a managed default reversing an
    agent negation can also newly UN-ignore a path, and the SAME Push's
    `git add -A` then commits it. Gating every surface on `removed` alone made
    that addition exactly as silent as the deletions #2529 exists to end — and
    an addition is the worse half, because it is already in the remote's
    history and may need a credential rotated rather than a file restored.
    """
    from unittest.mock import AsyncMock, patch

    gs = _gs()
    from database import GitSyncResult

    sweep = gs.GitignoreSweep(unignored=(".ssh/id_rsa", ".ssh/authorized_keys"))
    assert sweep.changed_tracking is True
    assert not sweep.removed  # the point: nothing was untracked

    # 1. the one-line summary that rides on `message` (the only failure-path surface)
    assert "newly un-ignored 2 path(s)" in sweep.summary_line()
    assert "untracked" not in sweep.summary_line()

    # 2. the commit message that CARRIES the addition
    msg = gs._augment_commit_message("my message", sweep)
    assert "+ .ssh/id_rsa" in msg and "newly un-ignored 2 path(s)" in msg

    # 3. the structured response fields
    out = gs._with_sweep(GitSyncResult(success=True, message="Synced"), sweep)
    assert out.unignored_paths == [".ssh/id_rsa", ".ssh/authorized_keys"]
    assert "newly un-ignored" in out.message

    # 4. the durable operator-queue entry — the surface that outlives the session
    with patch(
        "services.operator_queue_service.create_bounded_alert", new=AsyncMock()
    ) as create:
        await gs._emit_gitignore_untracked_alert("alpha", sweep)
    assert create.await_count == 1
    _, item = create.await_args.args
    assert item["context"]["unignored_paths"] == [".ssh/id_rsa", ".ssh/authorized_keys"]
    assert item["context"]["unignored_count"] == 2
    assert item["context"]["removed_count"] == 0
    assert "previously gitignored" in item["title"]
    assert "ROTATE" in item["question"]
    # `medium`, not `high`: the `unignored` probe is `after - before` across two
    # execs against a live container, so a file the agent's own session creates
    # in that window lands here too. A removal is confirmed and keeps `high`.
    assert item["priority"] == "medium"

    # `shadowed` alone is NOT a change this Push made — standing advice about the
    # file, not an event. It must not file an alert on every single Push.
    advice_only = gs.GitignoreSweep(shadowed=("!content/keep.md -> content/",))
    assert advice_only.changed_tracking is False
    assert advice_only.summary_line() == ""
    with patch(
        "services.operator_queue_service.create_bounded_alert", new=AsyncMock()
    ) as create:
        await gs._emit_gitignore_untracked_alert("alpha", advice_only)
    assert create.await_count == 0


# ---------------------------------------------------------------------------
# The other writers of an agent's `.gitignore` (Decision 8: the coverage frame
# is the FILE, not the three merge call sites)
# ---------------------------------------------------------------------------

def test_data_paths_append_lands_in_the_user_region_and_does_not_churn(tmp_path):
    """`materialize_data_paths` (#1169) appends through the generic
    `_build_gitignore_append_command`, i.e. to the END of the file — which after
    #2529 is BELOW the protected floor. Harmless (they are positive ignores
    matching agent-declared data dirs, so they shadow nothing the floor
    protects) and SELF-CORRECTING: they are not managed lines, so the next merge
    carries them into the user region with everything else. The property that
    matters is that neither step duplicates them, or the 15-minute auto-sync
    loop would re-commit a growing `.gitignore` forever."""
    gs = _gs()
    home = _make_repo(tmp_path, {"CLAUDE.md": "a\n"}, "")
    _run(gs._build_gitignore_merge_command(str(home)), home)
    paths = ["data/", "datasets/big/"]
    _run(gs._build_gitignore_append_command(str(home), paths), home)
    _, user_before, _ = _regions(home)
    # Appended past the floor on this pass...
    assert user_before[-2:] != paths
    assert (home / ".gitignore").read_text().rstrip().endswith("datasets/big/")

    _run(gs._build_gitignore_merge_command(str(home)), home)
    _, user, _ = _regions(home)
    assert user == paths, f"data paths did not land in the user region: {user}"

    # Idempotent across both writers, in both orders.
    _run(gs._build_gitignore_append_command(str(home), paths), home)
    _run(gs._build_gitignore_merge_command(str(home)), home)
    _, user, _ = _regions(home)
    assert user == paths, f"a data path was duplicated: {user}"


def test_a_user_line_matching_a_canonical_pattern_is_deduped_not_doubled(tmp_path):
    """A user who typed a canonical pattern themselves keeps ONE copy of it —
    ours, in the managed block. The honest cost, documented in the agent guide:
    if they had written `*.log` / `!important.log` / `*.log`, only the negation
    survives below the block, which WEAKENS an ignore they meant. It can never
    untrack something new, the floor covers the cases where weakening would be
    dangerous, and `shadowed_negations` reports the rest."""
    home = _make_repo(tmp_path, {"CLAUDE.md": "a\n"}, "*.log\n!important.log\n*.log\nmine/\n")
    _run(_gs()._build_gitignore_merge_command(str(home)), home)
    top, user, _ = _regions(home)
    assert top.count("*.log") == 1
    assert user == ["!important.log", "mine/"]


def test_a_previously_swept_file_comes_back_and_is_reported(tmp_path):
    """Revised criterion 3, in the shape the field incident actually had.

    An earlier Push swept four `.env.example` files: still on disk, gone from the
    index, and IGNORED — so nothing re-added them, and the issue's own follow-up
    names the second-order property, *"un-ignoring a file does not re-track it."*
    Three of four were restored by hand; one sat untracked for two days.

    After this fix the agent's negation is effective again, so on the next Push
    the file is newly un-ignored and still untracked — which is exactly what
    `unignored_paths` reports, and exactly what would have made that sweep a
    same-day finding instead of a two-month-old one. The same Push's own
    `git add -A` then re-tracks it.
    """
    home = _make_repo(tmp_path, {"CLAUDE.md": "a\n"}, "!.env.example\nmy-scratch/\n")
    # The state a previous Push left behind: on disk, untracked, and ignored
    # because the canonical block was appended BELOW the negation.
    (home / ".env.example").write_text("API_KEY=\n")
    (home / ".gitignore").write_text("!.env.example\nmy-scratch/\n.env\n.env.*\n")
    assert subprocess.run(
        ["git", "check-ignore", "-q", ".env.example"],
        cwd=home, env=dict(_ENV, HOME=str(home)), capture_output=True,
    ).returncode == 0, "fixture is wrong — the file must start out ignored"

    sweep = _push_sweep(home)
    assert ".env.example" in sweep.unignored, (
        f"the swept file's return was not reported: {sweep}"
    )
    # And the agent's own `git add -A` now picks it up.
    _git(home, "add", "-A")
    assert ".env.example" in _git(home, "ls-files").split()


def test_removed_is_reported_only_after_the_rm_actually_ran():
    """Order inside the sweep command, pinned. The `$ignored` list is captured
    BEFORE the `git rm --cached`, so echoing it before the rm would name files
    that are still tracked whenever the rm fails — a false alarm on the one
    surface (the operator-queue entry) whose whole job is to be trusted. Echoing
    it after means a failed rm aborts the `&&` chain and reports nothing, which
    is what this code did before #2529 anyway."""
    gs = _gs()
    script = shlex.split(gs._build_rm_cached_ignored_command("/home/developer"))[2]
    rm_at = script.index("git rm --cached")
    report_at = script.index(gs._SWEEP_TAG_REMOVED)
    assert rm_at < report_at, (
        "the removed-paths report is emitted BEFORE the git rm — a failed sweep "
        "would then be reported as a successful one"
    )
    # And both probes that describe the POST-sweep world come after it too.
    assert rm_at < script.index(gs._SWEEP_TAG_AFTER)
    assert rm_at < script.index(gs._SWEEP_TAG_SHADOW)


# ---------------------------------------------------------------------------
# Probe bounds — `container_exec_run` reads the whole exec output as one blob
# ---------------------------------------------------------------------------

def test_probe_lists_are_line_capped_in_container():
    """Both probed sets are unbounded in exactly the cases this code exists for:
    the untracked probe runs BEFORE the block is written on a pre-canonical
    agent (so `$HOME` answers with all of `.local/lib/.../site-packages`), and
    `$ignored` is five figures on the #1596 population — an agent with a
    committed `node_modules/`, the 44 GB repos that motivated those patterns."""
    gs = _gs()
    cap = gs._SWEEP_PROBE_LINE_CAP
    merge = shlex.split(gs._build_gitignore_merge_command("/home/developer"))[2]
    sweep = shlex.split(gs._build_rm_cached_ignored_command("/home/developer"))[2]
    # cap+1 on the two set-difference operands: a full cap+1 lines is how
    # truncation announces itself.
    assert f"head -n {cap + 1}" in merge
    assert f"head -n {cap + 1}" in sweep
    # The removed list is capped at the cap itself, and its exact count is
    # emitted separately so the report is never an undercount.
    assert f"head -n {cap}" in sweep
    assert gs._SWEEP_TAG_REMOVED_COUNT in sweep


def test_truncated_probe_suppresses_unignored_rather_than_inventing_it():
    """`unignored` is a SET DIFFERENCE, so a truncated operand manufactures
    entries that are only "new" because the other side was cut off. Drop the
    field instead — it is advisory, and a fiction is worse than a gap."""
    gs = _gs()
    cap = gs._SWEEP_PROBE_LINE_CAP
    merge = "".join(f"{gs._SWEEP_TAG_BEFORE}b{i}\n" for i in range(cap + 1))
    sweep = "".join(f"{gs._SWEEP_TAG_AFTER}a{i}\n" for i in range(10))
    assert gs._parse_gitignore_sweep(merge, sweep).unignored == ()

    # Just under the cap, it is computed normally.
    merge = "".join(f"{gs._SWEEP_TAG_BEFORE}b{i}\n" for i in range(cap))
    assert gs._parse_gitignore_sweep(merge, sweep).unignored == tuple(
        sorted(f"a{i}" for i in range(10))
    )


def test_removed_count_is_exact_even_when_the_list_is_capped():
    """A capped list must never become an undercounted claim about how many
    files a Push untracked — the number is what an operator acts on."""
    gs = _gs()
    sweep_out = (
        f"{gs._SWEEP_TAG_REMOVED_COUNT}41234\n"
        + "".join(f"{gs._SWEEP_TAG_REMOVED}p{i}\n" for i in range(2000))
    )
    sweep = gs._parse_gitignore_sweep("", sweep_out)
    assert len(sweep.removed) == 2000
    assert sweep.removed_total == 41234
    assert "untracked 41234 file(s)" in sweep.summary_line()
    assert "and 41214 more" in gs._augment_commit_message("m", sweep)

    # A missing or unparseable count falls back to the list length — a wrong
    # count is worse than a conservative one.
    fallback = gs._parse_gitignore_sweep(
        "", f"{gs._SWEEP_TAG_REMOVED_COUNT}not-a-number\n{gs._SWEEP_TAG_REMOVED}p1\n"
    )
    assert fallback.removed_total == 1


def test_probe_tags_are_prefix_distinct():
    """`_tagged_output_lines` matches by `startswith`, so a tag that is a prefix
    of another silently absorbs its lines. `removed` vs `removed-count` is one
    character away from exactly that."""
    gs = _gs()
    tags = [
        gs._SWEEP_TAG_BEFORE, gs._SWEEP_TAG_AFTER,
        gs._SWEEP_TAG_REMOVED, gs._SWEEP_TAG_REMOVED_COUNT, gs._SWEEP_TAG_SHADOW,
    ]
    assert len(set(tags)) == len(tags)
    for a in tags:
        for b in tags:
            if a is not b:
                assert not b.startswith(a), f"{b!r} is absorbed by {a!r}"
