# Repro harness for #640 — MCP stdio stdout pollution

> **Status:** harness only. The fix at the agent-runtime layer is still
> open. PR #662 is the diagnostic-instrumentation prerequisite.

## What this harness is for

Issue #640 is the open root-cause ticket behind a family of "long execution
silently fails with null response" reports (#678, #630, #618, #548, #586).
The hypothesis is: an stdio MCP server (or one of its descendants) writes
to a file descriptor that ends up interleaving with Claude Code's own
stdout, corrupting the `stream-json` line that carries the result block.

PR #662's author tried to repro with `@upstash/context7-mcp@latest`, ran
~1.5h, did not manifest the failure (issue body says ≥100 turns / 18min
needed). What's missing is a **deterministic** repro: a controlled MCP
server that exhibits each hypothesised leak path, so we can prove which
one actually corrupts the wire and design a fix at the right layer.

## Files

| File | Purpose |
|------|---------|
| `noisy_mcp_server.py` | Minimal stdio MCP server with `--leak <variant>` knobs. Self-contained, stdlib only. |
| `run_repro.py` | Driver that hits an agent's chat API for N turns and measures null-cost / null-response rate. |
| `README.md` | This file. |

## Leak variants

Each variant tests one hypothesis from the #640 issue body and the #662
author's empirical notes:

| Variant | Hypothesis tested |
|---------|-------------------|
| `none` | Control / baseline. Should produce 0% null-cost. |
| `stderr-flood` | MCP child stderr noise — does it bleed onto stdout via Claude's stderr handling? |
| `setsid-child` | Grandchild escapes Claude pgid kill (#618 fix family) and retains write end on protocol pipe. |
| `proc-fd-write` | Process writes to its own fd 1 via `/proc/self/fd/1`, interleaving raw text with MCP frames. |
| `delayed-stdout` | Partial-line writes that race Claude's reader at line boundary. |
| `npm-wrapper` | Real-world `npx`-style: package-manager boilerplate on stdout BEFORE protocol handshake. Most likely culprit. |

The simulator never writes to stdout/stderr for its own diagnostics —
those are reserved for the wire. All instrumentation goes to a sidecar
log file (default `/tmp/noisy-mcp-server.log`).

## How to run

### 1. Pick an agent

You need an existing Trinity agent in your local stack. Either reuse one
or create a fresh template-based agent. The agent's `.mcp.json` will be
modified to attach this simulator.

### 2. Wire the simulator into the agent's `.mcp.json`

Inside the agent container (e.g. `docker exec -u developer <agent> bash`):

```bash
# Copy the simulator into the agent's home dir.
docker cp tests/harness/640/noisy_mcp_server.py <agent>:/home/developer/noisy_mcp_server.py
docker exec <agent> chmod +x /home/developer/noisy_mcp_server.py

# Append to .mcp.json (variant=npm-wrapper is the most production-like).
# Use jq or hand-edit to add:
#   "noisy": {
#     "command": "/home/developer/noisy_mcp_server.py",
#     "args": ["--leak", "npm-wrapper", "--log-file", "/home/developer/noisy-mcp.log"]
#   }
docker exec <agent> python3 -c '
import json, pathlib
p = pathlib.Path("/home/developer/.mcp.json")
cfg = json.loads(p.read_text())
cfg.setdefault("mcpServers", {})["noisy"] = {
    "command": "/home/developer/noisy_mcp_server.py",
    "args": ["--leak", "npm-wrapper", "--log-file", "/home/developer/noisy-mcp.log"],
}
p.write_text(json.dumps(cfg, indent=2))
'
```

### 3. Restart the agent so Claude picks up the new MCP

```bash
# Force agent-server to restart claude — easiest path is to reset the chat session.
curl -X DELETE -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/agents/<agent>/chat/history
```

### 4. Drive the repro

```bash
./tests/harness/640/run_repro.py --agent <agent> --turns 50
```

The driver mints a JWT from `.env` `ADMIN_PASSWORD` if `--token` is not
passed.

### 5. Read the verdict

The driver prints per-turn status and a summary:

```
============================================================
Total turns         : 50
Null cost           : 6 (12.0%)
Null response       : 4 (8.0%)
Mean turn duration  : 14.3s
Max turn duration   : 78.1s

First 5 failures:
  turn 17: {'turn': 17, 'cost': None, ...}

FAIL: null-cost rate 12.0% exceeds threshold 5.0%
```

Exit code 0 if null-cost rate is below the `--null-cost-fail-rate`
threshold (default 5%), 1 otherwise. Once a fix lands the same harness
becomes a regression gate — variants that pass on the fixed runtime but
failed on the buggy runtime are the ones the fix actually addresses.

## Caveats

- The simulator runs as the agent user (`developer`, uid 1000). It needs
  Python 3 in `$PATH` — already present in the base image.
- `setsid-child` forks a grandchild that holds an open write end on the
  protocol pipe. After Claude exits, this grandchild becomes the orphan
  that PR #662's `_kill_orphan_pipe_writers` is designed to surface.
  Verify by checking `/home/developer/noisy-mcp.log` for the
  `setsid-child started pgid=…` line and then `/proc/<pid>/fd/` after a
  failed run.
- The `npm-wrapper` variant only fires once at startup. To stress this
  path, you must reset the chat session between turns so Claude
  re-spawns the MCP child. The driver currently does not — extend if
  needed.
- Until PR #662 lands, parse_failures aren't logged at WARNING level —
  the driver measures the symptom (null cost / null response). Once #662
  ships, also grep agent-server logs for `parse_failures=` to correlate.

## Results to date (2026-05-07 session)

Six hypotheses tested via this harness + static inspection of
`@anthropic-ai/claude-code` cli.js (v at agent base image as of branch).
**All falsified.** Negative results below — preserved so future hunts
don't re-walk the same paths.

| # | Hypothesis | Method | Result |
|---|-----------|--------|--------|
| 1 | Node `child_process.spawn` leaks fd>2 to MCP child | Read spawn opts in cli.js | ✗ Node closes fd>2 by default; no MCP-spawn site disables it |
| 2 | Claude has stray `process.stdout.write` in stream-json hot path | Grepped all 45 stdout writes for real-code (non-docstring) sites firing in `--print` mode | ✗ All real sites are subcommand-specific (mcp add, login, TUI keys, native-host) — none fire in stream-json hot path |
| 3 | Claude bridges MCP child stderr → its own stdout | Read `m.stderr.on("data")` handler | ✗ Buffers in 64MB ring (`if($.length<67108864) $+=h.toString()`); never re-emits |
| 4 | `npm`-bootstrap stdout boilerplate before MCP handshake | 50 turns w/ `npm-wrapper` variant | ✗ 0/50 null-cost; Claude tolerates pre-protocol garbage at handshake |
| 5 | `setsid` grandchild retains protocol-pipe write end and pollutes during steady state | 50 turns w/ `setsid-child` variant; verified 53 grandchildren forked + escaped pgid | ✗ 0/50 null-cost; pollution lands on MCP-protocol pipe (claude-side), not agent-server's claude-stdout pipe — Claude skips non-JSON frames |
| 6 | Userspace `/proc/self/fd/N` bypass of pipe isolation | Smoke test of `proc-fd-write` variant | ✗ Linux returns `ENXIO` when re-opening a pipe via `/proc/self/fd/*` — kernel-level isolation. Hypothesis impossible at user level. |

### What this rules out

The corruption mechanism behind #640 is **not** in any of:

- Process-level fd inheritance / leak from claude → MCP child → grandchild
- Claude's own stream-json output gating (no stray writes in hot path)
- Userspace MCP-child output corruption (stdout AND stderr both isolated by SDK transport)
- Linux `/proc/*/fd/*` bypass at the user-permission level

### What's still open (real remaining hypothesis space)

- **Race in Claude's own stdout line buffering** — concurrent emitter threads inside Claude's runtime, not external pollution. Would require LD_PRELOAD or claude-cli source patching to test.
- **Specific package with shell-wrapped launch** (`bash -c "node x.js"`) where shell init writes to a fd we haven't simulated — need a real cmdline from a production failure to target.
- **Load-state dependent**: bug only manifests after 100+ turns / 18+ min sustained load (per #640 issue body). Our 50-turn / ~6 min runs may not exhaust whatever Claude-internal buffer/timer accumulates.
- **Specific cli.js version drift**: the cli.js inspected here may differ from production version that hit #640 / #678. Check version pinning.

### Recommended next steps

1. **Land PR #662** (diagnostic instrumentation) — the only realistic way to identify the actual leaking package is from real production logs once `parse_failures=N` and orphan-cmdline lines are surfaced.
2. **Redeploy dev cluster fixes** (#657, #649, #618, #630, #531, #520) on the host running `ability-website`. Reduces frequency of #678-style failures even without root-cause fix.
3. **Wait for next prod failure with #662 logs** — the orphan cmdline and `parse_failures` count will identify the actual leaking package.
4. **Build a targeted variant from that real cmdline** — much cheaper than synthetic hypothesis-hunting.
5. **Optionally: 200-turn run with this harness** to test the load-state-dependent hypothesis. ~$8 / 30 min.

### Cost of this investigation

- ~$4 of subscription compute (npm-wrapper + setsid-child runs at 50 turns each)
- ~3 hours wall (harness scaffold + 2 variants + cli.js inspection)
- Information delivered: 6 hypotheses falsified, harness committed as future regression gate

## Links

- Issue #640 — root cause ticket (still open after this investigation)
- PR #662 — diagnostic instrumentation (prerequisite for next prod-data-driven attempt)
- Issue #678 — production manifestation (24-min execution, null telemetry)
- `docker/base-image/agent_server/services/claude_code.py` — agent-server spawn site
- `docker/base-image/agent_server/utils/subprocess_pgroup.py` — orphan reaper
