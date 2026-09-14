#!/usr/bin/env python3
"""#2468 re-audit probe — diff a live agent's offered built-in tools against the audited set.

The platform deny list (``PLATFORM_DENIED_TOOLS`` in
``docker/base-image/agent_server/services/_runtime_config.py``) is a name-based
filter over an OPEN set that tracks the latest Claude Code CLI, and
``--disallowedTools`` treats an unknown name as a SILENT no-op — so a CLI bump
can rename a denied tool (re-offering its false promise) or ship a brand-new
future-promise tool, with no signal anywhere. This script is the living half of
the audit: run it against a live agent whenever the base image's CLI version
moves, and re-audit anything it flags (then bump ``AUDIT_CLI_VERSION`` beside
the tuples).

What it does:
  1. Reads the REPO's audited sets (DENIED / KEPT / AUDIT_CLI_VERSION) from
     ``_runtime_config.py`` by path (stdlib-only module, no imports needed).
  2. Asks the CONTAINER's copy for its deny list (an old image answers with a
     shorter list, or none — reported, not fatal).
  3. Runs one minimal ``claude --print`` turn inside the container — WITH the
     container's own merged ``--disallowedTools`` value, exactly as Trinity's
     two spawn sites pass it — and reads the init event's ``tools`` array.
  4. Diffs offered built-ins against the audit and prints a verdict.

Cost: one tiny model call (haiku by default, ~$0.01). Needs the agent container
running with working Claude credentials.

Usage:
    python3 scripts/dev/audit_headless_tools.py [agent-name] [--model MODEL] [--no-deny] [--json]

    --no-deny   probe WITHOUT the deny list (shows the CLI's full offering —
                use this to audit a new CLI version's complete surface)

Exit codes: 0 = clean (audit covers the offering, deny effective, version
matches) · 1 = findings (re-audit needed) · 2 = probe failed.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUNTIME_CONFIG = REPO / "docker" / "base-image" / "agent_server" / "services" / "_runtime_config.py"


def load_repo_audit():
    spec = importlib.util.spec_from_file_location("_rc_audit_probe", RUNTIME_CONFIG)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return (
        tuple(module.PLATFORM_DENIED_TOOLS),
        tuple(getattr(module, "PLATFORM_KEPT_TOOLS", ())),
        getattr(module, "AUDIT_CLI_VERSION", None),
    )


def container_deny_list(agent: str) -> tuple[list, str]:
    """The deny list as the CONTAINER's image computes it (guardrails merged)."""
    code = (
        "import sys, json; sys.path.insert(0, '/app')\n"
        "from agent_server.services._runtime_config import merged_disallowed_tools, _load_guardrails\n"
        "print(json.dumps(merged_disallowed_tools(_load_guardrails())))"
    )
    proc = subprocess.run(
        ["docker", "exec", "-u", "developer", f"agent-{agent}", "python3", "-c", code],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        # Pre-#2458 images have no merged_disallowed_tools at all. stderr opens
        # with import-time INFO logging — the exception is the LAST line.
        tail = (proc.stderr.strip().splitlines() or ["<no stderr>"])[-1]
        return [], f"container could not compute a deny list (old image?): {tail[:200]}"
    return json.loads(proc.stdout.strip()), ""


def probe_init_tools(agent: str, model: str, disallowed: list) -> dict:
    """One minimal claude --print turn; returns the parsed init event."""
    deny_arg = f"--disallowedTools {shlex.quote(','.join(disallowed))} " if disallowed else ""
    inner = (
        f'SID=$(python3 -c "import uuid;print(uuid.uuid4())"); '
        f'echo "Reply with the single word: ok" | claude --print '
        f"--output-format stream-json --verbose --dangerously-skip-permissions "
        f"--no-session-persistence --session-id \"$SID\" {deny_arg}"
        f"--mcp-config ~/.mcp.json --model {shlex.quote(model)}"
    )
    proc = subprocess.run(
        ["docker", "exec", "-u", "developer", f"agent-{agent}", "bash", "-lc", inner],
        capture_output=True, text=True, timeout=240,
    )
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            return ev
    raise RuntimeError(
        f"no init event in probe output (rc={proc.returncode}); "
        f"stderr: {proc.stderr.strip()[:300]}; stdout head: {proc.stdout[:300]}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("agent", nargs="?", default="testfix", help="agent name (container agent-<name>)")
    ap.add_argument("--model", default="claude-haiku-4-5-20251001")
    ap.add_argument("--no-deny", action="store_true", help="probe without --disallowedTools")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    denied, kept, audit_version = load_repo_audit()
    audited = set(denied) | set(kept)

    try:
        image_deny, image_note = container_deny_list(args.agent)
        probe_deny = [] if args.no_deny else image_deny
        init = probe_init_tools(args.agent, args.model, probe_deny)
    except Exception as e:  # noqa: BLE001 - a probe tool reports, it doesn't crash-trace
        print(f"PROBE FAILED: {e}", file=sys.stderr)
        return 2

    tools = init.get("tools") or []
    cli_version = init.get("claude_code_version") or "unknown"
    offered = sorted(t for t in tools if isinstance(t, str) and not t.startswith("mcp__"))

    findings = {
        # Deny asked for but still offered — CLI filter semantics changed, or name drift.
        "denied_but_offered": sorted(set(offered) & set(denied)) if probe_deny else [],
        # Offered names the audit never classified — NEW tools, audit them.
        "unaudited_offered": sorted(set(offered) - audited),
        # Audited names the CLI no longer offers (dropped/renamed) — informational,
        # but a DENIED one here means the deny entry is a silent no-op now.
        "audited_not_offered": sorted(audited - set(offered) - (set(denied) if probe_deny else set())),
        "cli_version": cli_version,
        "audit_cli_version": audit_version,
        "version_match": cli_version == audit_version,
        # Operator guardrails may ADD names; the image must carry at least the repo's denials.
        "image_deny_covers_repo": set(denied).issubset(image_deny),
        "image_note": image_note,
        "offered_builtin_count": len(offered),
        "offered_builtins": offered,
        "probed_with_deny": bool(probe_deny),
    }

    clean = (
        not findings["denied_but_offered"]
        and not findings["unaudited_offered"]
        and findings["version_match"]
        and findings["image_deny_covers_repo"]
        and not image_note
    )

    if args.as_json:
        print(json.dumps(findings, indent=2))
        return 0 if clean else 1

    print(f"agent: {args.agent} · CLI {cli_version} (audited on {audit_version}) · "
          f"{len(offered)} built-ins offered · probed {'WITH' if probe_deny else 'WITHOUT'} deny list")
    if image_note:
        print(f"  ⚠ {image_note}")
    if findings["denied_but_offered"]:
        print(f"  ✗ denied but still offered: {findings['denied_but_offered']}")
    if findings["unaudited_offered"]:
        print(f"  ✗ offered but never audited (re-audit these): {findings['unaudited_offered']}")
    if findings["audited_not_offered"]:
        print(f"  ℹ audited but not offered on this CLI: {findings['audited_not_offered']}")
    if not findings["image_deny_covers_repo"]:
        print(f"  ✗ image deny list does not cover the repo's ({sorted(set(denied) - set(image_deny))} "
              f"missing) — rebuild the base image + cold-recreate the agent")
    if not findings["version_match"]:
        print(f"  ✗ CLI version moved ({audit_version} → {cli_version}): re-run the audit, "
              f"then bump AUDIT_CLI_VERSION")
    print("CLEAN — audit covers this CLI's offering" if clean else "FINDINGS — see above")
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
