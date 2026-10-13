#!/usr/bin/env python3
"""PreToolUse hook for `Skill`, `Agent` and `Task`: gated skills
(trinity-enterprise#752). The bootstrap.

Registered by /etc/claude-code/managed-settings.d/50-skill-gate.json, exec form:

    /usr/bin/env -i /usr/local/bin/python3 -I -S /opt/trinity/hooks/skill-gate.py

The logic is in `_skill_gate.py`. This file exists so that nothing that can go
wrong there — an import error, a syntax error, a crash — ends the process with
exit 1, which Claude Code treats as "let the tool run". Whatever happens, the
exit is 0 or 2, and when the module cannot decide, the root-owned marker does:
present (this agent has gates) refuses, absent allows.

`--self-test` is the image build's smoke: it imports the module WITHOUT the
catch-all, so a broken file fails the build instead of shipping.
"""
import os
import sys

HOOKS_DIR = "/opt/trinity/hooks"
# Must equal `_skill_gate.MARKER` / `_skill_gate.COULD_NOT_CHECK` (unit-tested):
# this copy decides when that module cannot be loaded at all.
MARKER = "/opt/trinity/skill-gates-active"
COULD_NOT_CHECK = (
    "Trinity could not be reached to check whether this skill needs approval, so it was not "
    "run. Do not carry out its steps another way; tell whoever asked that it could not be checked."
)


def _fallback() -> int:
    if os.path.lexists(MARKER):
        try:
            sys.stderr.write(COULD_NOT_CHECK + "\n")
        except BaseException:  # noqa: BLE001
            pass
        return 2
    return 0


def _run() -> int:
    try:
        sys.path.insert(0, HOOKS_DIR)
        import _skill_gate
        code = _skill_gate.main(sys.argv[1:])
    except BaseException:  # noqa: BLE001 — never exit 1: the marker decides
        return _fallback()
    return code if code in (0, 2) else _fallback()


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.path.insert(0, HOOKS_DIR)
        import _skill_gate
        sys.exit(_skill_gate.self_test())
    exit_code = _run()
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except BaseException:  # noqa: BLE001
        pass
    os._exit(exit_code)
