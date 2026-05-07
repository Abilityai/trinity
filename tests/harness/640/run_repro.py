#!/usr/bin/env python3
"""Repro driver for #640 — drive N chat turns against an agent that has
the noisy MCP attached and measure how often the wire-corruption failure
mode fires.

Until PR #662 lands in dev, the agent-server doesn't expose a
`parse_failures` counter, so we measure the **observable symptom** instead:
an execution that completes with `cost_usd = None` despite running for
multiple turns. That's the exact symptom #678 hit in production.

Usage:
    run_repro.py --agent <name> --turns 50 [--token <jwt>] [--base-url <url>]

Setup is documented in tests/harness/640/README.md — you must wire
noisy_mcp_server.py into the target agent's .mcp.json BEFORE running this.

Output:
    Per-turn line of [N/M] cost=$X turn_id=… status=…
    Final summary: success rate, null-cost rate, mean turn duration.

Exit code 0 if null-cost rate < 5%, 1 otherwise — useful for CI gating
once a fix lands.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


def _request(method: str, url: str, token: str, body: Optional[Dict] = None) -> Dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode(errors="replace")[:500]
        raise SystemExit(f"HTTP {e.code} on {method} {url}: {body_txt}")


def get_admin_token(base_url: str) -> str:
    """Mint a token from local admin password if --token not provided."""
    env_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env")
    pw = None
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.startswith("ADMIN_PASSWORD="):
                    pw = line.split("=", 1)[1].strip()
                    break
    if not pw:
        raise SystemExit("ADMIN_PASSWORD not in .env and no --token passed")

    data = f"username=admin&password={pw}".encode()
    req = urllib.request.Request(f"{base_url}/api/token", data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())["access_token"]


# ---------------------------------------------------------------------------
# Drive turns
# ---------------------------------------------------------------------------


# Cheap prompts that incentivise tool use without burning many tokens.
PROMPTS = [
    "Use the echo tool to echo back the word 'one'.",
    "Echo 'two' via the echo tool.",
    "Call echo with 'three'.",
    "Echo 'four'.",
    "Echo 'five'.",
    "Echo 'six'.",
    "Echo 'seven'.",
    "Echo 'eight'.",
    "Echo 'nine'.",
    "Echo 'ten'.",
]


def run_turn(base_url: str, token: str, agent: str, prompt: str, timeout_s: int = 600) -> Dict[str, Any]:
    t0 = time.time()
    result = _request(
        "POST",
        f"{base_url}/api/agents/{agent}/chat",
        token,
        {"message": prompt, "timeout_seconds": timeout_s},
    )
    elapsed = time.time() - t0
    return {"elapsed": elapsed, "result": result}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--agent", required=True, help="Target agent name (must already be running with noisy MCP attached)")
    p.add_argument("--turns", type=int, default=50)
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--token", help="JWT (defaults to mint via .env ADMIN_PASSWORD)")
    p.add_argument("--prompt-set", default="echo", help="(reserved for future prompt variants)")
    p.add_argument("--null-cost-fail-rate", type=float, default=0.05,
                   help="Exit 1 if null-cost rate exceeds this fraction (default 5%%)")
    args = p.parse_args()

    token = args.token or get_admin_token(args.base_url)

    print(f"=== Repro #640 against agent '{args.agent}' over {args.turns} turns ===\n")

    null_cost_count = 0
    null_response_count = 0
    durations: List[float] = []
    failures: List[Dict[str, Any]] = []

    for i in range(args.turns):
        prompt = PROMPTS[i % len(PROMPTS)]
        try:
            r = run_turn(args.base_url, token, args.agent, prompt)
        except SystemExit:
            raise
        except Exception as e:
            print(f"[{i+1}/{args.turns}] EXCEPTION {type(e).__name__}: {e}")
            failures.append({"turn": i + 1, "type": "exception", "error": str(e)})
            continue

        result = r["result"]
        meta = result.get("metadata") or {}
        # /api/agents/{name}/chat puts cost / tool_count / error fields under
        # `metadata`, not at the top level. The harness measures the symptom
        # described in #678: an execution that completes with `cost_usd=None`
        # despite running through multiple turns.
        cost = meta.get("cost_usd")
        tool_count = meta.get("tool_count")
        num_turns = meta.get("num_turns")
        error_type = meta.get("error_type")
        response_text = result.get("response")
        durations.append(r["elapsed"])

        is_null_cost = cost is None
        is_null_response = not response_text

        if is_null_cost:
            null_cost_count += 1
        if is_null_response:
            null_response_count += 1

        marker = "FAIL" if (is_null_cost or is_null_response) else "ok"
        print(
            f"[{i+1}/{args.turns}] {marker:>4} cost=${cost} tools={tool_count} "
            f"turns={num_turns} response_len={len(response_text or '')} "
            f"elapsed={r['elapsed']:.1f}s err={error_type}",
            flush=True,
        )

        if is_null_cost or is_null_response:
            failures.append({
                "turn": i + 1,
                "cost": cost,
                "tool_count": tool_count,
                "num_turns": num_turns,
                "response_len": len(response_text or ""),
                "elapsed": r["elapsed"],
                "error_type": error_type,
                "error_message": meta.get("error_message"),
            })

    # ---- Summary ----------------------------------------------------------
    print()
    print("=" * 60)
    print(f"Total turns         : {args.turns}")
    print(f"Null cost           : {null_cost_count} ({null_cost_count/args.turns:.1%})")
    print(f"Null response       : {null_response_count} ({null_response_count/args.turns:.1%})")
    if durations:
        print(f"Mean turn duration  : {statistics.mean(durations):.1f}s")
        print(f"Max turn duration   : {max(durations):.1f}s")
    print()

    if failures:
        print("First 5 failures:")
        for f in failures[:5]:
            print(f"  turn {f['turn']}: {f}")

    null_rate = null_cost_count / args.turns
    if null_rate > args.null_cost_fail_rate:
        print(f"\nFAIL: null-cost rate {null_rate:.1%} exceeds threshold {args.null_cost_fail_rate:.1%}")
        return 1
    print(f"\nPASS: null-cost rate {null_rate:.1%} within threshold {args.null_cost_fail_rate:.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
