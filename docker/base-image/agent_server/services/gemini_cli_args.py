"""Gemini CLI argument surface for the pinned gemini-cli (#2971).

The base image installs ``@google/gemini-cli@GEMINI_CLI_VERSION``. Every flag
the Gemini runtime passes is built here, so the one function the runtime uses
is also the one the image build smoke-tests against the real binary.

Verified against gemini-cli 0.62.0 (``gemini --help`` and live argument
parses, 2026-10-01):

* No ``--system-prompt`` and no ``--max-turns`` — the strict parser exits 1
  with ``Unknown arguments``. The only system-prompt mechanism is
  ``GEMINI_SYSTEM_MD``, a *full replacement* of the built-in prompt (tool-use
  and safety rules included), so the platform prompt is instead prepended to
  the turn input, as the Codex runtime does. There is no per-run turn cap
  (``model.maxSessionTurns`` is a settings-file value shared by every
  concurrent run); runs are bounded by the wall-clock timeout.
* Without workspace trust the CLI exits 55 and downgrades ``--yolo`` to the
  ``default`` approval mode. ``--skip-trust`` trusts the working directory for
  that run only. It is a flag rather than ``GEMINI_CLI_TRUST_WORKSPACE`` so a
  release that renames it fails this module's smoke instead of silently
  reverting to untrusted.
* ``--resume <uuid>`` resumes that exact session; a missing one exits 42 with
  ``Error resuming session``. Bare ``--resume`` means "latest", which is shared
  with headless runs — never emitted.

No intra-package imports: the image build runs this file directly
(``python3 gemini_cli_args.py --smoke``) right after the agent server is
copied in.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from typing import List, Optional

# Must match the Dockerfile pin; tests/unit/test_2971_gemini_runtime.py
# asserts it.
GEMINI_CLI_VERSION = "0.62.0"

# Stderr marker of a `--resume <id>` whose session file does not exist.
RESUME_MISSING_MARKER = "Error resuming session"


def _base_argv(model: Optional[str]) -> List[str]:
    cmd = ["gemini", "--output-format", "stream-json", "--yolo", "--skip-trust"]
    if model:
        cmd += ["--model", model]
    return cmd


def build_chat_argv(model: Optional[str], resume_session_id: Optional[str]) -> List[str]:
    """Chat turn. ``resume_session_id`` must be the chat's OWN session id."""
    cmd = _base_argv(model)
    if resume_session_id:
        cmd += ["--resume", resume_session_id]
    return cmd


def build_headless_argv(
    model: Optional[str], allowed_tools: Optional[List[str]] = None
) -> List[str]:
    """Headless (task) turn — always a fresh session."""
    cmd = _base_argv(model)
    for tool in allowed_tools or []:
        cmd += ["--allowed-tools", tool]
    return cmd


def compose_prompt(system_prompt: Optional[str], prompt: str) -> str:
    """Prepend the platform system prompt to the turn input (no CLI flag)."""
    if system_prompt:
        return f"{system_prompt}\n\n---\n\n{prompt}"
    return prompt


# ---------------------------------------------------------------------------
# Build-time smoke: a real argument parse of every argv shape the runtime uses.
# ---------------------------------------------------------------------------

_SMOKE_SESSION = "00000000-0000-4000-8000-000000000000"


def _smoke_cases():
    """(label, argv, expected exit code). ``--list-sessions`` makes the CLI
    parse the full argv and exit without contacting the API; a ``--resume``
    against an empty store exits 42 *after* parsing succeeded."""
    return [
        ("chat-cold", build_chat_argv("gemini-3-flash", None), 0),
        ("chat-resume", build_chat_argv("gemini-3-flash", _SMOKE_SESSION), 42),
        ("headless", build_headless_argv("gemini-3-flash", ["read_file"]), 0),
    ]


def smoke(gemini_bin: str = "gemini") -> List[str]:
    """Run each argv shape against ``gemini_bin``; return failure strings."""
    failures: List[str] = []
    with tempfile.TemporaryDirectory(prefix="gemini-smoke-") as home:
        work = os.path.join(home, "work")
        os.makedirs(work)
        env = dict(os.environ)
        env.update({"HOME": home, "GEMINI_API_KEY": "smoke-not-a-real-key"})
        for label, argv, expected in _smoke_cases():
            cmd = [gemini_bin] + argv[1:] + ["--list-sessions"]
            try:
                proc = subprocess.run(
                    cmd, cwd=work, env=env, stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, timeout=120,
                )
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{label}: {type(exc).__name__}: {exc}")
                continue
            if proc.returncode != expected:
                head = (proc.stderr or proc.stdout or "").strip()[:400]
                failures.append(
                    f"{label}: exit {proc.returncode} (expected {expected}) "
                    f"argv={argv[1:]} :: {head}"
                )
    return failures


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--smoke", action="store_true", required=True)
    parser.add_argument("--gemini", default="gemini")
    args = parser.parse_args(argv)
    failures = smoke(args.gemini)
    for f in failures:
        print(f"[gemini-smoke] FAIL {f}", file=sys.stderr)
    if not failures:
        print(f"[gemini-smoke] ok: {len(_smoke_cases())} argv shapes parsed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
