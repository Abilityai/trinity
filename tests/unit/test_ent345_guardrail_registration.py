"""The guardrail hook registration must be out of the agent's write reach (ent#345).

The hooks themselves were always root-owned in `/opt/trinity/hooks/`. Their
REGISTRATION was not: it shipped to `/home/developer/.claude/settings.json`, owned
by `developer`, mode 0644, inside the git-synced working tree (HOME is the repo
root) and read live by Claude Code. So the guarded agent could edit the file that
decides whether it is guarded — one Edit call disables the bash deny-list, the file
guard, the read-only guard and the credential output-scanner for every subsequent
tool call. An agent was observed editing it autonomously during the #2036
investigation, and because the file was synced it was also settable from outside by
a push to the agent's repo.

These are Dockerfile/startup assertions rather than container tests because CI does
not build the base image; the shipped artifact is the Dockerfile, so that is what is
pinned. What cannot be asserted here — that the CLI in the built image honours the
managed path — is asserted at runtime by the startup check this file also pins.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_BASE = _ROOT / "docker" / "base-image"
_DOCKERFILE = (_BASE / "Dockerfile").read_text()
_STARTUP = (_BASE / "startup.sh").read_text()

MANAGED_PATH = "/etc/claude-code/managed-settings.json"
AGENT_SETTINGS = "/home/developer/.claude/settings.json"


def test_registration_ships_to_the_managed_path():
    assert f"COPY ./hooks/managed-settings.json {MANAGED_PATH}" in _DOCKERFILE


# Instructions only — the ent#345 block deliberately NAMES the old path in prose to
# record what moved and why, so a substring search over the whole file would fail on
# its own documentation.
_INSTRUCTIONS = "\n".join(
    line for line in _DOCKERFILE.splitlines() if not line.lstrip().startswith("#")
)


def test_no_platform_registration_lands_in_the_agents_home():
    """The bug itself: a hook registration installed into the agent's HOME.

    Also rejects the earlier filename, so a revert cannot come back quietly under
    the old name.
    """
    assert AGENT_SETTINGS not in _INSTRUCTIONS
    assert "claude-settings.json" not in _INSTRUCTIONS


def test_the_managed_file_and_its_directory_are_root_owned_and_read_only():
    """Read-only file AND non-writable directory. A writable directory is enough
    to replace the file or shadow it with a `managed-settings.d` drop-in, so the
    file mode alone would not be a boundary."""
    assert "chown -R root:root /etc/claude-code" in _DOCKERFILE
    assert "chmod 0444 /etc/claude-code/managed-settings.json" in _DOCKERFILE
    assert "chmod 0755 /etc/claude-code" in _DOCKERFILE
    # And no `--chown=developer` on the way in.
    assert f"COPY --chown=developer:developer ./hooks/managed-settings.json" not in _DOCKERFILE


def test_the_image_still_ends_as_the_non_root_user():
    """Invariant #17: the ent#345 block runs as root and must hand back."""
    tail = _DOCKERFILE[_DOCKERFILE.index(MANAGED_PATH):]
    assert "USER developer" in tail, "the root section must return to the agent user"


def test_every_registered_hook_command_exists_in_the_image():
    """A registration pointing at a script the image does not ship is a hook that
    fails on every call — fail-closed, but it turns a guardrail into an outage.
    Pins the registration against the COPY list rather than trusting both."""
    settings = json.loads((_BASE / "hooks" / "managed-settings.json").read_text())
    commands = [
        h["command"]
        for phase in settings["hooks"].values()
        for entry in phase
        for h in entry["hooks"]
    ]
    assert commands, "the registration must register something"

    for command in commands:
        script = re.search(r"(/opt/trinity/hooks/[\w.-]+\.py)", command)
        assert script, f"hook command has no /opt/trinity/hooks path: {command}"
        name = Path(script.group(1)).name
        assert (_BASE / "hooks" / name).is_file(), f"{name} is registered but not in hooks/"
        assert f"COPY ./hooks/{name} /opt/trinity/hooks/{name}" in _DOCKERFILE, (
            f"{name} is registered but never COPYed into the image"
        )


def test_the_four_guardrails_are_all_registered():
    """The set this issue is about. A registration that quietly loses one of them
    is the same defect with a smaller blast radius."""
    settings = (_BASE / "hooks" / "managed-settings.json").read_text()
    for hook in ("bash-guardrail.py", "file-guardrail.py",
                 "read-only-guard.py", "output-scanner.py"):
        assert hook in settings, f"{hook} is no longer registered"


def test_startup_verifies_the_registration_is_present_and_not_writable():
    """The one property CI cannot check — that the built image's CLI honours the
    managed path — fails SILENTLY (no hooks run, nothing in the log). So startup
    asserts what it can: the file exists and the agent user cannot write it."""
    assert "GUARDRAIL_SETTINGS=/etc/claude-code/managed-settings.json" in _STARTUP
    assert '[ ! -f "$GUARDRAIL_SETTINGS" ]' in _STARTUP
    assert '[ -w "$GUARDRAIL_SETTINGS" ]' in _STARTUP
    assert "GUARDRAILS: ERROR" in _STARTUP


def test_startup_reports_and_continues():
    """A registration problem must not become a fleet outage: the check logs, it
    does not `exit`."""
    block = _STARTUP[_STARTUP.index("GUARDRAIL_SETTINGS="):]
    block = block[: block.index("# === Scratch space")]
    assert "exit 1" not in block


def test_startup_retires_the_legacy_in_tree_copy_only_on_an_exact_match():
    """`~/.claude/settings.json` is on the DURABLE home volume, so rebuilding the
    image does not remove it from an agent that already exists — leaving a second
    hook registration whose interaction with the managed one depends on precedence
    we do not control, plus a live #2036 leak candidate.

    Removal is gated on `cmp -s` against the file we now ship: no parsing, no
    heuristics, no "looks like ours". An agent-authored settings.json differs and is
    left completely alone, which is the property that makes deleting a file in the
    agent's own HOME defensible at all.
    """
    assert "LEGACY_SETTINGS=/home/developer/.claude/settings.json" in _STARTUP
    assert 'cmp -s "$LEGACY_SETTINGS" "$GUARDRAIL_SETTINGS"' in _STARTUP
    # Guarded by BOTH files existing, so a missing managed copy cannot make the
    # comparison vacuous and delete the only registration present.
    assert '[ -f "$LEGACY_SETTINGS" ] && [ -f "$GUARDRAIL_SETTINGS" ]' in _STARTUP


def test_both_paths_stay_in_the_write_deny_lists():
    """Belt and braces, not the primary control — but a regression here would
    silently re-open the Bash route to the registration."""
    files_py = (_ROOT / "src" / "backend" / "services" / "agent_service" / "files.py").read_text()
    baseline = json.loads((_BASE / "hooks" / "guardrails-baseline.json").read_text())
    deny = baseline.get("path_deny") or []

    assert "/etc/claude-code/*" in files_py
    assert "/etc/claude-code/*" in deny
    # The old location stays denied too: pre-ent#345 agents still have the file,
    # and an agent-authored one could still carry `permissions`.
    assert ".claude/settings.json" in files_py
    assert "/home/developer/.claude/settings.json" in deny
