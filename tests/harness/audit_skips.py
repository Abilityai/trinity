#!/usr/bin/env python3
"""Skip audit — an unexplained skip fails the run (#2080).

A skipped test is indistinguishable from a passing one in every summary line
pytest prints, so a suite that skips silently reports green while covering
nothing. That is exactly how the tiers this issue is about (Postgres/Alembic,
agent-dependent) came to never run locally: each announced itself only as
`s` in a dot-line nobody reads.

This reads the `-rs` short summary from every tier log and fails on any skip
whose reason is not on the allowlist below. The allowlist is DELIBERATELY a
list of reasons rather than of test ids: a reason describes a *condition* (no
Slack credentials configured) that is legitimately absent on a developer
machine, while a test id would let any new skip inherit an old justification.

Adding an entry is a decision someone has to write down — which is the point.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Reasons that are legitimately environment-dependent on a developer machine.
# Each entry is matched as a case-insensitive SUBSTRING of the reason pytest
# prints, and each must say WHY the condition is acceptable rather than a bug.
ALLOWED_SKIP_REASONS: tuple[tuple[str, str], ...] = (
    # Genuinely external third-party credentials. Trinity cannot provision
    # these, and a run on a laptop must not require a real Slack workspace or
    # a paid Twilio number.
    ("slack", "needs a real Slack workspace + bot token"),
    ("twilio", "needs a real Twilio account (paid, external)"),
    ("whatsapp", "rides the Twilio account above"),
    ("elevenlabs", "needs a paid ElevenLabs key"),
    ("gemini", "needs a Google AI Studio key"),
    ("anthropic api key", "needs a real Anthropic key (billable)"),
    # #2336: the J03 first-turn journey asserts REAL model output, which needs a
    # provider key — and every PR-triggered workflow here is deliberately
    # credential-free, because `pull_request` exposes repository secrets to fork
    # PRs while running the PR's own shell (integration-nightly.yml states the
    # full reasoning). The journey runs on a developer's stack and in the
    # nightly; the per-PR gate runs the credential-free lifecycle journey, which
    # is where the 2026-08-14 regression actually was.
    ("journey needs a real provider key", "credential-bound journey; see #2336"),
    ("openai", "needs a real OpenAI key (billable)"),
    ("nevermined", "needs the Nevermined testnet + a funded wallet"),
    # Platform/interpreter conditionals that are correct, not missing coverage.
    ("requires python", "interpreter-version conditional"),
    ("only on linux", "platform conditional"),
    ("docker not available", "the tier that needs Docker reports its own failure"),
    # Enterprise submodule is optional and absent on OSS clones by design
    # (.gitmodules update=none, #1443).
    ("enterprise", "private submodule not mounted — optional by design"),
    ("private submodule", "same condition, as `test_2068_alembic_heads_guard` words it"),
    ("backend venv required", "import-guard on a module the tier does not own"),
)

_SKIP_RE = re.compile(r"^SKIPPED\s+\[\s*\d+\s*\]\s*(?P<loc>\S+):\s*(?P<reason>.*)$")
# pytest also prints the short form: "SKIPPED [1] path:line: reason"
_SKIP_RE_ALT = re.compile(r"^SKIPPED\s+(?P<loc>\S+)\s*[-:]\s*(?P<reason>.*)$")


def _allowed(reason: str) -> bool:
    low = reason.lower()
    return any(token in low for token, _ in ALLOWED_SKIP_REASONS)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: audit_skips.py <log-dir>", file=sys.stderr)
        return 2
    log_dir = Path(argv[1])
    logs = sorted(log_dir.glob("*.log"))
    if not logs:
        # No logs at all means no tier produced output — the audit cannot
        # certify anything, and certifying nothing as "clean" is the failure
        # mode this whole file exists to prevent.
        print(f"  no tier logs in {log_dir} — nothing to audit, refusing to pass")
        return 1

    offenders: list[tuple[str, str, str]] = []
    total_skips = 0
    for log in logs:
        for line in log.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("SKIPPED"):
                continue
            m = _SKIP_RE.match(line) or _SKIP_RE_ALT.match(line)
            if not m:
                continue
            total_skips += 1
            reason = m.group("reason").strip()
            if not _allowed(reason):
                offenders.append((log.stem, m.group("loc"), reason))

    if offenders:
        print(f"  {len(offenders)} unallowlisted skip(s) of {total_skips} total:")
        seen: set[str] = set()
        for tier, loc, reason in offenders:
            key = f"{tier}:{reason}"
            if key in seen:
                continue
            seen.add(key)
            print(f"    [{tier}] {loc}\n        reason: {reason}")
        print()
        print("  A skip is a test that did NOT run. Either make it run, or add the")
        print("  reason to ALLOWED_SKIP_REASONS in tests/harness/audit_skips.py with")
        print("  a note saying why the condition is acceptable.")
        return 1

    print(f"  {total_skips} skip(s), all on the allowlist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
