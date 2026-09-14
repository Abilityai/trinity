"""#2578 — the dev deploy's superproject fetch must never recurse into a submodule.

``Deploy to Dev`` went red on every commit that moved the ``src/backend/enterprise``
gitlink, and only on those (runs 33388252573 and 34142379794, attempt 1). The VM's
``git fetch origin dev`` ran with git's default ``fetch.recurseSubmodules=on-demand``:
a fetched commit that moves the gitlink of a submodule with a gitdir on the host
makes git fetch that submodule too, over its STORED remote — the SSH URL from
``.gitmodules`` — before the PAT transport in the workflow's submodule block
exists. ``Host key verification failed`` → ``Errors during submodule fetch`` →
exit 1 → ``set -e`` stops the script inside ``=== Pull ===``.

It never had a ticket because it self-heals: the superproject refs advance BEFORE
the recursion runs, so the next unrelated push finds nothing new to recurse on and
deploys the bump one push late, and a re-run of the red run is green.

Two halves, deliberately:

  1. **Static guard** over the workflow text — every ``git fetch`` / ``git pull`` /
     ``git checkout`` in the deploy script carries ``--no-recurse-submodules``
     (tokenised; comments never count), the pull keeps ``--ff-only``, and a
     STALE enterprise tree fails the run after the health check instead of
     warning: the submodule block became the ONLY sync path, and a sole sync
     path whose failure is a warning is the #2246 class one notch over.
  2. **Behavioural proof** with real git in ``tmp_path`` — a superproject +
     submodule fixture shaped like the VM (submodule populated, its remote the
     SSH URL, ``.gitmodules`` mirroring the real stanza), a fake ``ssh`` that
     records every dial and answers what the VM's ssh answers, and the
     workflow's OWN ``=== Pull ===`` block run verbatim through ``bash -e``
     against a pointer bump.

The behavioural half is built so it cannot pass independently of the file it
guards (learnings 2026-08-03): a fresh clone per test (the self-heal advances
``origin/dev``, so a shared clone would pass with or without the flags on some
random seeds), an explicit ``origin/dev == p1`` precondition, the dial log
asserted EMPTY after the block (the property is "the SSH transport is never
dialled", stronger than exit 0), and an in-clone control that rewinds the
remote-tracking ref and proves the same fixture still reproduces the class.

Nothing here touches the network: the fake ``ssh`` owns the SSH URL and every
other transport is a local file path. The environment is rebuilt from scratch
(every inherited ``GIT_*`` dropped, global/system config off, prompts off) so a
developer's real credentials can neither leak in nor be exercised.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO / ".github" / "workflows" / "deploy-dev.yml"
_GITMODULES = _REPO / ".gitmodules"

SUBMODULE = "src/backend/enterprise"
FLAG = "--no-recurse-submodules"
# The git subcommands that fetch, or update submodule working trees, on their own.
WATCHED = {"fetch", "pull", "checkout"}


# ---------------------------------------------------------------------------
# Workflow text helpers
# ---------------------------------------------------------------------------

def _text() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


def _doc(text: str | None = None) -> dict:
    return yaml.safe_load(text if text is not None else _text())


def _deploy_script(doc: dict) -> str:
    """The shell that actually runs on the dev VM (same shape as test_2204)."""
    for step in doc["jobs"]["deploy"]["steps"]:
        with_ = step.get("with") or {}
        if "script" in with_:
            return with_["script"]
    raise AssertionError("deploy job has no ssh-action step carrying a `script:`")


def _notify_script(doc: dict) -> str:
    for step in doc["jobs"]["notify-failure"]["steps"]:
        if "run" in step:
            return step["run"]
    raise AssertionError("notify-failure job has no `run:` step")


def _section(script: str, start: str, end: str) -> str:
    """Lines from the `echo "=== <start>` line up to (not including) `echo "=== <end>`."""
    lines = script.splitlines()
    try:
        a = next(i for i, l in enumerate(lines) if l.strip().startswith(f'echo "=== {start}'))
        b = next(i for i, l in enumerate(lines) if i > a and l.strip().startswith(f'echo "=== {end}'))
    except StopIteration as exc:  # pragma: no cover - a reshaped script is its own failure
        raise AssertionError(f"could not slice section {start!r}..{end!r}") from exc
    return "\n".join(lines[a:b])


def _logical_lines(block: str) -> list[str]:
    """Join backslash continuations and strip; comments are kept (callers filter)."""
    out: list[str] = []
    buf = ""
    for raw in block.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        out.append((buf + line).strip())
        buf = ""
    if buf:
        out.append(buf.strip())
    return out


def _command_lines(block: str) -> list[str]:
    return [l for l in _logical_lines(block) if l and not l.startswith("#")]


def _comment_lines(block: str) -> list[str]:
    return [l for l in _logical_lines(block) if l.startswith("#")]


def _tokens(line: str) -> list[str]:
    try:
        return shlex.split(line)
    except ValueError:
        return line.split()


def _git_invocations(line: str):
    """Yield ``(subcommand, tokens)`` for every ``git …`` invocation in a command line.

    ``git -C dir fetch``, ``git -c k=v pull``, ``sudo git fetch`` and
    ``X=$(git fetch …)`` all count; an invocation ends at a shell operator.
    """
    toks = _tokens(line)
    for i, tok in enumerate(toks):
        if tok.lstrip("$(") != "git":
            continue
        j = i + 1
        sub = None
        while j < len(toks):
            t = toks[j]
            if t in ("-c", "-C"):
                j += 2
                continue
            if t.startswith("-"):
                j += 1
                continue
            sub = t
            break
        if sub is None:
            continue
        k = j
        while k < len(toks) and toks[k] not in ("||", "&&", ";", "|"):
            k += 1
        yield sub, toks[i:k]


# ---------------------------------------------------------------------------
# The checks, as functions so the meta-test can feed them mutated input.
# ---------------------------------------------------------------------------

def check_no_git_command_recurses(script: str) -> None:
    """Every fetch/pull/checkout in the whole deploy script carries the flag.

    Scope is the entire script, not two hand-picked lines (learnings 2026-07-30):
    a future ``git fetch --tags`` reintroduces the class silently otherwise.
    ``git remote update`` fetches and has no such flag, so it must be absent.
    """
    found: set[str] = set()
    offenders: list[str] = []
    for line in _command_lines(script):
        for sub, inv in _git_invocations(line):
            if sub == "remote" and len(inv) > 2 and inv[2] == "update":
                offenders.append(f"`git remote update` fetches and cannot take {FLAG}: {line}")
            if sub in WATCHED:
                found.add(sub)
                if FLAG not in inv:
                    offenders.append(f"missing {FLAG}: {line}")
    assert not offenders, "\n".join(offenders)
    # A scan that matched nothing would pass vacuously — the 2026-08-03 class.
    # Anchored on fetch and pull only: the checkout is a belt that a later
    # simplification may legitimately drop, and its absence must not read as a
    # regression (review appendix).
    assert {"fetch", "pull"} <= found, f"the scan found only {sorted(found)} — did the Pull block change shape?"


def check_the_pull_keeps_ff_only(script: str) -> None:
    pulls = [inv for line in _command_lines(script) for sub, inv in _git_invocations(line) if sub == "pull"]
    assert pulls, "no `git pull` in the deploy script"
    for inv in pulls:
        assert "--ff-only" in inv, " ".join(inv)


def check_the_comment_names_the_class(script: str) -> None:
    """The Pull block's own comment must say WHY, so nobody 'simplifies' the flags away."""
    comments = " ".join(_comment_lines(_section(script, "Pull", "Submodule (enterprise)"))).lower()
    assert "#2578" in comments
    assert "on-demand" in comments


def check_a_stale_tree_fails_the_run(script: str) -> None:
    """DIRECTION, not presence: a stale enterprise tree must fail the run.

    The flag is raised where the drift is measured (the submodule block), the
    error is printed FIRST in the registration section (it is the primary cause
    of whatever the registration greps say about the old tree), and the exit
    comes at the END of the script — after the registration greps, the #2204
    seeding check and the error check have all landed (review I1: the seeding
    line exists only in this container run's boot log, so an earlier exit loses
    it rather than delaying it). No ``DEPLOY_ALLOW_OSS_ONLY`` hatch on the stale
    path, and — as test_2246 pins — still no ``exit 1`` inside the submodule
    block itself.
    """
    lines = script.splitlines()

    def first(pred, start: int = 0) -> int:
        return next(i for i in range(start, len(lines)) if pred(lines[i]))

    def block_end(start: int) -> int:
        """Index of the bare ``fi`` closing the ``if`` at ``start`` (line-scanned:
        'classified' contains 'fi', so never split on the substring)."""
        return first(lambda l: l.strip() == "fi", start)

    init = first(lambda l: l.strip() == "ENT_STALE=0")
    warn = first(lambda l: "::warning::Enterprise submodule STALE" in l)
    assert init < warn, "ENT_STALE must be initialised before the submodule block"
    warn_block = [l.strip() for l in lines[warn:block_end(warn)]]
    assert "ENT_STALE=1" in warn_block, "the STALE branch must raise the flag"
    assert any("classify-submodule-failure.sh" in l and '"$ENT_LOG" stale' in l for l in warn_block), (
        "the STALE branch must classify the cause next to the evidence, telling the classifier the tree is stale"
    )

    health = first(lambda l: '=== Health ===' in l)
    registration = first(lambda l: "=== Enterprise registration" in l)
    assert health < registration
    gates = [i for i in range(registration, len(lines)) if '"$ENT_STALE" = "1"' in lines[i]]
    assert len(gates) >= 2, "expected an announce gate and an enforce gate after the health check"
    announce, enforce = gates[0], gates[-1]
    assert any("::error::Enterprise submodule STALE" in l for l in lines[announce:block_end(announce)]), (
        "the first ENT_STALE gate must print the ::error::"
    )
    first_registration_grep = first(
        lambda l: "grep" in l and "Trinity Enterprise registration FAILED" in l, registration
    )
    assert announce < first_registration_grep, "the STALE error is the primary cause — print it first"
    enforce_block = [l.strip() for l in lines[enforce:block_end(enforce)]]
    assert "exit 1" in enforce_block, "the last ENT_STALE gate must exit 1"
    # Review I1: the enforcement must come AFTER the #2204 seeding check's own
    # verdict — its "Failed to create agent" line exists only in this container
    # run's boot log, so an earlier exit would lose that signal, not delay it.
    seeding = first(lambda l: "=== Agent seeding check" in l, registration)
    seeding_exit = first(lambda l: l.strip() == "exit 1", seeding)
    assert enforce > seeding_exit, "the STALE exit preempts the seeding check (review I1)"
    assert not any("vars.DEPLOY_ALLOW_OSS_ONLY" in l for l in lines[enforce:block_end(enforce)]), (
        "no escape hatch on the stale path — a stale tree is never legitimate"
    )
    submodule = _section(script, "Submodule (enterprise)", "Volume ownership")
    assert "exit 1" not in submodule, "the deploy must not abort before the instance is up (#2246)"


def check_the_incident_body_names_the_enterprise_stages(notify: str) -> None:
    """The auto-filed issue's First checks must send the reader to the right stage."""
    assert "Enterprise registration" in notify
    assert "trinity#2578" in notify


@pytest.fixture(scope="module")
def script() -> str:
    return _deploy_script(_doc())


def test_every_fetch_pull_and_checkout_never_recurses(script):
    check_no_git_command_recurses(script)


def test_the_pull_keeps_ff_only(script):
    check_the_pull_keeps_ff_only(script)


def test_the_comment_names_the_class(script):
    check_the_comment_names_the_class(script)


def test_a_stale_enterprise_tree_fails_the_run(script):
    check_a_stale_tree_fails_the_run(script)


def test_the_incident_body_names_the_enterprise_stages():
    check_the_incident_body_names_the_enterprise_stages(_notify_script(_doc()))


_CLASSIFIER = _REPO / "scripts" / "ci" / "classify-submodule-failure.sh"


def _classify(text: str, *args: str) -> str:
    proc = subprocess.run(
        ["bash", str(_CLASSIFIER), "-", *args], input=text, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr  # a classifier never fails the deploy it diagnoses
    return proc.stdout


def test_the_classifier_words_silence_for_the_stale_caller():
    """Review I3: the #2246 classifier now has a second caller — the STALE branch —
    where the tree IS populated, at the old pin. Its `no-output` CAUSE must not
    assert an unpopulated tree there; the transport classes read the same."""
    stale = _classify("", "stale")
    assert stale.startswith("CLASS: no-output"), stale
    assert "OLD pin" in stale and "unpopulated" not in stale, stale
    default = _classify("")
    assert default.startswith("CLASS: no-output"), default
    assert "unpopulated" in default, default
    assert _classify("Host key verification failed.\n", "stale").startswith("CLASS: host-key")


# ---------------------------------------------------------------------------
# Meta-test: the guard goes red on the pre-fix content (learnings 2026-08-05:
# "delete the real line and watch it go red"). Each mutation must CHANGE the
# file, or the meta-test would pass against an unmodified tree and prove nothing.
# ---------------------------------------------------------------------------

_FETCH = f"            git fetch {FLAG} origin dev\n"
_CHECKOUT = f"            git checkout {FLAG} dev\n"
_PULL = f"            git pull --ff-only {FLAG} origin dev\n"
_ENFORCE = (
    '            if [ "$ENT_STALE" = "1" ]; then\n'
    "              exit 1\n"
    "            fi\n"
)
# The pre-review placement of the enforcement: inside the registration branch,
# ahead of the #2204 seeding check (review I1).
_ENFORCE_EARLY = (
    '              if [ "$ENT_STALE" = "1" ]; then\n'
    "                exit 1\n"
    "              fi\n"
)
_REGISTRATION_ELSE = (
    "              fi\n"
    "            else\n"
    "              # #2246 — an unentitled dev instance is a FAILED deploy"
)


@pytest.mark.parametrize(
    "mutation,checker,target",
    [
        # Pre-fix: the fetch recurses.
        (lambda t: t.replace(_FETCH, "            git fetch origin dev\n"),
         check_no_git_command_recurses, "script"),
        # Pre-fix: the pull recurses (its own fetch, and the tree under submodule.recurse).
        (lambda t: t.replace(_PULL, "            git pull --ff-only origin dev\n"),
         check_no_git_command_recurses, "script"),
        # The belt on the checkout removed.
        (lambda t: t.replace(_CHECKOUT, "            git checkout dev\n"),
         check_no_git_command_recurses, "script"),
        # The flag present ONLY in a comment — prose must never satisfy the guard.
        (lambda t: t.replace(_FETCH, f"            # git fetch {FLAG} origin dev\n            git fetch origin dev\n"),
         check_no_git_command_recurses, "script"),
        # A second, unflagged fetch added later (the F2 class).
        (lambda t: t.replace(_CHECKOUT, _CHECKOUT + "            git fetch --tags origin\n"),
         check_no_git_command_recurses, "script"),
        # `git remote update` cannot carry the flag at all.
        (lambda t: t.replace(_CHECKOUT, _CHECKOUT + "            git remote update\n"),
         check_no_git_command_recurses, "script"),
        # --ff-only dropped from the pull.
        (lambda t: t.replace(_PULL, f"            git pull {FLAG} origin dev\n"),
         check_the_pull_keeps_ff_only, "script"),
        # T1 pre-fix: the stale tree only warns.
        (lambda t: t.replace(_ENFORCE, ""),
         check_a_stale_tree_fails_the_run, "script"),
        # T1: the flag is never raised.
        (lambda t: t.replace("                ENT_STALE=1\n", ""),
         check_a_stale_tree_fails_the_run, "script"),
        # T1: an escape hatch sneaks onto the stale path.
        (lambda t: t.replace(
            _ENFORCE,
            '            if [ "$ENT_STALE" = "1" ] && [ "${{ vars.DEPLOY_ALLOW_OSS_ONLY }}" != "true" ]; then\n'
            "              exit 1\n"
            "            fi\n"),
         check_a_stale_tree_fails_the_run, "script"),
        # Review I1: the exit moves back inside the registration branch, ahead of the seeding check.
        (lambda t: t.replace(_ENFORCE, "").replace(
            _REGISTRATION_ELSE, _REGISTRATION_ELSE.replace("            else\n", _ENFORCE_EARLY + "            else\n")),
         check_a_stale_tree_fails_the_run, "script"),
        # Review I3: the classifier is called without the `stale` tree state.
        (lambda t: t.replace('classify-submodule-failure.sh "$ENT_LOG" stale', 'classify-submodule-failure.sh "$ENT_LOG"'),
         check_a_stale_tree_fails_the_run, "script"),
        # The incident body forgets the stage again.
        (lambda t: t.replace("trinity#2578", "trinity#0000"),
         check_the_incident_body_names_the_enterprise_stages, "notify"),
    ],
    ids=[
        "fetch-recurses", "pull-recurses", "checkout-unflagged", "flag-only-in-comment",
        "second-unflagged-fetch", "remote-update", "ff-only-dropped",
        "stale-only-warns", "stale-flag-never-raised", "stale-escape-hatch",
        "stale-exit-preempts-seeding", "classifier-without-stale-state", "incident-body-stage",
    ],
)
def test_guard_rejects_pre_fix_content(mutation, checker, target):
    original = _text()
    mutated = mutation(original)
    assert mutated != original, (
        "the mutation did not change the file — the meta-test would pass "
        "against an unmodified tree and prove nothing"
    )
    doc = _doc(mutated)
    payload = _deploy_script(doc) if target == "script" else _notify_script(doc)
    with pytest.raises(AssertionError):
        checker(payload)


# ---------------------------------------------------------------------------
# Behavioural half: real git, a VM-shaped fixture, the workflow's own Pull block.
# ---------------------------------------------------------------------------

GIT = shutil.which("git")


def _require_git() -> None:
    if GIT:
        return
    if os.environ.get("GITHUB_ACTIONS"):
        pytest.fail("git is not on PATH on the CI runner — this guard would silently retire")
    pytest.skip("git is not on PATH")


def _enterprise_submodule_url() -> str:
    """The SSH URL the real .gitmodules records for the enterprise submodule."""
    lines = _GITMODULES.read_text(encoding="utf-8").splitlines()
    start = next(i for i, l in enumerate(lines) if l.strip() == f'[submodule "{SUBMODULE}"]')
    for line in lines[start + 1:]:
        if line.startswith("["):
            break
        key, _, value = line.strip().partition("=")
        if key.strip() == "url":
            return value.strip()
    raise AssertionError(f".gitmodules has no url for {SUBMODULE}")


@dataclass
class DeployVM:
    env: dict[str, str]
    ent_bare: Path
    ent_work: Path
    super_bare: Path
    super_work: Path
    vm: Path
    dial_log: Path
    ssh_url: str
    s: dict[str, str] = field(default_factory=dict)  # submodule commits, by name
    p: dict[str, str] = field(default_factory=dict)  # superproject commits, by bump name

    def git(self, *args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [GIT, *args], cwd=cwd, env=self.env, capture_output=True, text=True, timeout=60
        )
        if check:
            assert proc.returncode == 0, (
                f"git {' '.join(args)} in {cwd.name}: rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
            )
        return proc

    def rev(self, ref: str, cwd: Path) -> str:
        return self.git("rev-parse", ref, cwd=cwd).stdout.strip()

    def dials(self) -> list[str]:
        return self.dial_log.read_text().splitlines() if self.dial_log.exists() else []

    def bump(self, name: str) -> None:
        """Upstream: a new submodule commit, and a superproject commit moving the gitlink to it."""
        self.git("commit", "-q", "--allow-empty", "-m", name, cwd=self.ent_work)
        self.git("push", "-q", "origin", "HEAD:main", cwd=self.ent_work)
        sha = self.rev("HEAD", self.ent_work)
        self.s[name] = sha
        self.git("update-index", "--add", "--cacheinfo", f"160000,{sha},{SUBMODULE}", cwd=self.super_work)
        self.git("commit", "-q", "-m", f"bump {SUBMODULE} to {name}", cwd=self.super_work)
        self.git("push", "-q", "origin", "HEAD:dev", cwd=self.super_work)
        self.p[name] = self.rev("HEAD", self.super_work)


@pytest.fixture
def deploy_vm(tmp_path: Path) -> DeployVM:
    """A fresh VM-shaped fixture per test — the self-heal makes a shared one order-dependent."""
    _require_git()
    ssh_url = _enterprise_submodule_url()

    # The fake ssh: what the VM's ssh says with no known_hosts entry, plus a
    # record of every dial. Named `ssh` and paired with ssh.variant=ssh so git
    # treats it as OpenSSH and does not probe it with `-G` first.
    fake_dir = tmp_path / "fakebin"
    fake_dir.mkdir()
    fake_ssh = fake_dir / "ssh"
    dial_log = tmp_path / "ssh-dials.log"
    fake_ssh.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(dial_log))}\n"
        'echo "Host key verification failed." >&2\n'
        "exit 255\n"
    )
    fake_ssh.chmod(0o755)

    # A hermetic environment: no inherited GIT_* (a stray GIT_DIR breaks every
    # `git -C`; GIT_CONFIG_PARAMETERS/GIT_PROTOCOL_FROM_USER from a parent git
    # change transport policy), no global/system config (a developer's insteadOf
    # or credential helper must neither leak in nor be exercised), no prompts.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k == "GIT_EXEC_PATH"}
    env.update({
        "HOME": str(tmp_path),
        "LC_ALL": "C",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "trinity-test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "trinity-test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        # protocol.file.allow: git-spawned file transports (submodule update)
        # are refused by default since 2.38.1; older git ignores the key.
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "protocol.file.allow",
        "GIT_CONFIG_VALUE_0": "always",
        "GIT_CONFIG_KEY_1": "ssh.variant",
        "GIT_CONFIG_VALUE_1": "ssh",
        "GIT_SSH_COMMAND": shlex.quote(str(fake_ssh)),
    })
    vm = DeployVM(
        env=env,
        ent_bare=tmp_path / "trinity-enterprise.git",
        ent_work=tmp_path / "ent-work",
        super_bare=tmp_path / "trinity.git",
        super_work=tmp_path / "super-work",
        vm=tmp_path / "vm",
        dial_log=dial_log,
        ssh_url=ssh_url,
    )

    # Upstream submodule: bare + a working clone with s1.
    vm.git("init", "-q", "--bare", str(vm.ent_bare), cwd=tmp_path)
    vm.git("symbolic-ref", "HEAD", "refs/heads/main", cwd=vm.ent_bare)
    vm.git("clone", "-q", str(vm.ent_bare), str(vm.ent_work), cwd=tmp_path)
    vm.git("commit", "-q", "--allow-empty", "-m", "s1", cwd=vm.ent_work)
    vm.git("push", "-q", "origin", "HEAD:main", cwd=vm.ent_work)
    vm.s["s1"] = vm.rev("HEAD", vm.ent_work)

    # Upstream superproject: bare + a working clone whose p1 records the gitlink
    # s1 and the REAL .gitmodules stanza (SSH URL, `update = none` — #1443).
    vm.git("init", "-q", "--bare", str(vm.super_bare), cwd=tmp_path)
    vm.git("symbolic-ref", "HEAD", "refs/heads/dev", cwd=vm.super_bare)
    vm.git("clone", "-q", str(vm.super_bare), str(vm.super_work), cwd=tmp_path)
    (vm.super_work / ".gitmodules").write_text(
        f'[submodule "{SUBMODULE}"]\n\tpath = {SUBMODULE}\n\turl = {ssh_url}\n\tupdate = none\n'
    )
    vm.git("add", ".gitmodules", cwd=vm.super_work)
    vm.git("update-index", "--add", "--cacheinfo", f"160000,{vm.s['s1']},{SUBMODULE}", cwd=vm.super_work)
    vm.git("commit", "-q", "-m", "p1", cwd=vm.super_work)
    vm.git("push", "-q", "origin", "HEAD:dev", cwd=vm.super_work)
    vm.p["p1"] = vm.rev("HEAD", vm.super_work)

    # The "dev VM": a clone with the submodule populated the way the workflow
    # populates it — the durable `update checkout` override first (without it,
    # `update = none` skips the init with exit 0 and the assertion below fires),
    # then the per-command URL rewrite standing in for the PAT transport.
    vm.git("clone", "-q", str(vm.super_bare), str(vm.vm), cwd=tmp_path)
    vm.git("config", f"submodule.{SUBMODULE}.update", "checkout", cwd=vm.vm)
    vm.git("-c", f"url.{vm.ent_bare}.insteadOf={ssh_url}", "submodule", "update", "--init", SUBMODULE, cwd=vm.vm)
    # Exactly as on the VM: the stored remote is the SSH URL; the rewrite was per-command.
    vm.git("config", f"submodule.{SUBMODULE}.url", ssh_url, cwd=vm.vm)
    vm.git("remote", "set-url", "origin", ssh_url, cwd=vm.vm / SUBMODULE)
    assert vm.rev("HEAD", vm.vm / SUBMODULE) == vm.s["s1"]
    assert not vm.dials(), "the fixture must not dial ssh while populating"

    # Upstream moves the pointer.
    vm.bump("s2")
    assert vm.rev("origin/dev", vm.vm) == vm.p["p1"]
    return vm


_EXPR = re.compile(r"\$\{\{[^}]*\}\}")


def _pull_block(text: str | None = None) -> str:
    """The workflow's `=== Pull ===` block as runnable shell: comments dropped,
    continuations joined, `${{ … }}` expressions replaced by a literal."""
    section = _section(_deploy_script(_doc(text)), "Pull", "Submodule (enterprise)")
    return _EXPR.sub("test-run", "\n".join(_command_lines(section))) + "\n"


def _run_pull_block(vm: DeployVM, tmp_path: Path) -> subprocess.CompletedProcess:
    script = tmp_path / "pull-block.sh"
    script.write_text(_pull_block())
    return subprocess.run(
        ["bash", "-e", str(script)], cwd=vm.vm, env=vm.env, capture_output=True, text=True, timeout=120
    )


def test_the_class_reproduces_and_self_heals(deploy_vm):
    """The fixture has teeth: the PRE-fix command fails the way production did.

    A failure HERE means the fixture no longer reproduces the class — re-examine
    the fixture — not that the workflow regressed. Without this control the
    survival test below could pass against a fixture that never recurses.
    """
    vm = deploy_vm
    proc = vm.git("fetch", "origin", "dev", cwd=vm.vm, check=False)
    assert proc.returncode != 0, "a plain fetch survived the pointer bump — the fixture no longer recurses"
    assert "Errors during submodule fetch" in proc.stderr, proc.stderr
    assert "Host key verification failed" in proc.stderr, proc.stderr
    assert vm.dials(), "the fake ssh was never dialled — something else answered the SSH URL"
    # The refs advanced anyway: this is the self-heal that hid the class.
    assert vm.rev("origin/dev", vm.vm) == vm.p["s2"]
    again = vm.git("fetch", "origin", "dev", cwd=vm.vm, check=False)
    assert again.returncode == 0, again.stderr
    assert vm.rev("HEAD", vm.vm) == vm.p["p1"], "nothing was deployed either time"


@pytest.mark.parametrize(
    "submodule_recurse", [None, "true"], ids=["default-config", "submodule.recurse=true"]
)
def test_the_workflow_pull_block_survives_a_pointer_bump(deploy_vm, tmp_path, submodule_recurse):
    """The workflow's OWN Pull block, verbatim, against a bump: exit 0, gitlink
    moved, submodule tree untouched, the SSH transport never dialled."""
    vm = deploy_vm
    if submodule_recurse:
        vm.git("config", "submodule.recurse", submodule_recurse, cwd=vm.vm)
    assert vm.rev("origin/dev", vm.vm) == vm.p["p1"]
    assert not vm.dials()

    proc = _run_pull_block(vm, tmp_path)
    assert proc.returncode == 0, f"the Pull block failed on a pointer bump:\n{proc.stdout}\n{proc.stderr}"
    assert vm.rev("HEAD", vm.vm) == vm.p["s2"]
    assert vm.rev(f"HEAD:{SUBMODULE}", vm.vm) == vm.s["s2"], "the gitlink must move"
    assert vm.rev("HEAD", vm.vm / SUBMODULE) == vm.s["s1"], "syncing the tree is the submodule block's job"
    assert not vm.dials(), f"the Pull block dialled ssh: {vm.dials()}"

    # In-clone control: run the un-fixed command in the SAME clone — the
    # detector must still depend on its input. Both the branch AND the
    # remote-tracking ref go back to p1: git's on-demand walk is
    # `<new tips> --not <every local ref before the fetch>`, so a commit that
    # any local ref already holds is never inspected for gitlink changes.
    # (That exclusion IS the self-heal: once the failed fetch has advanced
    # origin/dev, no later fetch ever recurses on that bump again.)
    vm.git("reset", "-q", "--hard", "--no-recurse-submodules", vm.p["p1"], cwd=vm.vm)
    vm.git("update-ref", "refs/remotes/origin/dev", vm.p["p1"], cwd=vm.vm)
    control = vm.git("fetch", "origin", "dev", cwd=vm.vm, check=False)
    assert control.returncode != 0 and "Errors during submodule fetch" in control.stderr, control.stderr
    assert vm.dials(), "the un-fixed fetch must dial the fake ssh"


def test_the_submodule_block_syncs_the_pin(deploy_vm, tmp_path, script):
    """After the Pull block, the submodule block's form (its URL rewrite standing
    in for the PAT transport) brings the tree to the recorded pin."""
    vm = deploy_vm
    assert _run_pull_block(vm, tmp_path).returncode == 0
    submodule = _section(script, "Submodule (enterprise)", "Volume ownership")
    assert "insteadOf=git@github.com:" in submodule
    assert f"submodule update --init --recursive {SUBMODULE}" in submodule
    assert f"git config submodule.{SUBMODULE}.update checkout" in submodule

    vm.git("config", f"submodule.{SUBMODULE}.update", "checkout", cwd=vm.vm)
    vm.git(
        "-c", f"url.{vm.ent_bare}.insteadOf={vm.ssh_url}",
        "submodule", "update", "--init", "--recursive", SUBMODULE, cwd=vm.vm,
    )
    assert vm.rev("HEAD", vm.vm / SUBMODULE) == vm.rev(f"HEAD:{SUBMODULE}", vm.vm) == vm.s["s2"]
    assert not vm.dials()


def test_a_failed_sync_leaves_a_stale_tree_and_cannot_wedge_the_next_deploy(deploy_vm, tmp_path):
    """An expired PAT on a bump: the tree stays at the old pin (the STALE state
    the workflow now fails on), and the NEXT push's Pull block still runs —
    `git stash push` ignores a drifted gitlink and the ff pull moves it."""
    vm = deploy_vm
    assert _run_pull_block(vm, tmp_path).returncode == 0
    failed = vm.git("submodule", "update", "--init", "--recursive", SUBMODULE, cwd=vm.vm, check=False)
    assert failed.returncode != 0 and vm.dials(), failed.stderr
    assert vm.rev(f"HEAD:{SUBMODULE}", vm.vm) == vm.s["s2"]
    assert vm.rev("HEAD", vm.vm / SUBMODULE) == vm.s["s1"]  # exactly the STALE condition

    vm.dial_log.unlink()
    vm.bump("s3")
    proc = _run_pull_block(vm, tmp_path)
    assert proc.returncode == 0, f"a stale tree wedged the next deploy:\n{proc.stdout}\n{proc.stderr}"
    assert vm.rev("HEAD", vm.vm) == vm.p["s3"]
    assert vm.rev(f"HEAD:{SUBMODULE}", vm.vm) == vm.s["s3"]
    assert not vm.dials()
