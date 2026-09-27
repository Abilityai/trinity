# CSO Diff Audit — 2026-09-15 — abilityai/trinity-enterprise#628 (loop permission gate)

**Mode**: diff (all phases) · **Confidence gate**: 8/10 · **Branch**: `feature/628-loop-permission-gate` → `dev` · **Skill**: cso v1.1

## Architecture (Phase 0)
The diff lives entirely in the MCP server's tool surface (`src/mcp-server/`), the TypeScript proxy in front of the FastAPI backend. Trust boundary under audit: an **agent-scoped MCP key** (injected into every agent container as `TRINITY_MCP_API_KEY`) calling tools that target a *sibling* agent. The backend resolves that key to its owner carrying the owner's role (Invariant #8), so the MCP layer's `agent_permissions` edge (P-02) is the only place a sibling call is refused. Before this diff the refusal was a per-tool call in ten spellings; `run_agent_loop` made none.

## Attack surface (Phase 1, diff-scoped)
- New endpoints: 0 · New tools: 0 · Docker/compose: unchanged · Dependencies: unchanged
- Re-gated tools: `run_agent_loop` (registration wrapper), `get_loop_status`, `stop_loop` (resolve → gate → act)
- Shared gate consumers: `chat_with_agent`, `fan_out`, dynamic `chat_with_<slug>`, `run_agent_loop`
- Registration seam: `server.ts::addToolWithAudit` → `policyFor` for every tool; 130-row `TOOL_ACCESS_POLICY`
- CI: one offline boot-smoke step in `mcp-server-test.yml`

## Findings (Phases 2–12)
**None introduced.** 15 candidates evaluated; all refuted, excluded by rule, or pre-existing and tracked (see `filter_stats.detail` in the JSON).

Key clearances: the scope allowlist in `chat.ts` is unreachable for every non-operator scope (`OPERATOR_SCOPES` = user/agent/system; every gate caller is `operatorOnly`) and closes a fail-open; secret patterns 0 over the diff; the enterprise-disclosure PCRE replayed with a PCRE-capable engine (BSD `grep -P` errored silently on the first pass) — 0 hits; the new CI step has no event interpolation and no secrets; lockfile unchanged.

## Appendix — pre-existing, tracked
| # | Sev | Conf | Item | Tracking |
|---|-----|------|------|----------|
| A1 | HIGH | 9 | Backend REST routes are owner-equivalent for an agent key; 53 tools carry `baselined` rows for that reason, incl. `delete_agent`, `start_agent`, `stop_agent`, `set_agent_github_pat`, `export_agent_data`, `call_user`, `get_agent_auth` | abilityai/trinity-enterprise#629 (routed at the plan gate; a communicate edge ≠ a manage permission) |
| A2 | MED | 9 | Returned denials audited as successful calls; loop-id rows carry no target | abilityai/trinity#2807 |
| A3 | LOW | 8 | `none` shape check does not treat `name` as an agent target (agents.ts convention); a row is still mandatory | documented in `access.ts` |

## STRIDE (MCP server, diff-scoped)
- **Spoofing**: unchanged (key auth); an agent key can no longer act on a sibling through the loop tools.
- **Tampering**: n/a. **Repudiation**: refusals logged (`[Access Denied] …`) and audited as calls — labelled success (A2).
- **Information disclosure**: a loop-id refusal withholds the payload and the agent's name; `run_agent_loop`'s reason names only the caller-supplied target.
- **DoS**: excluded by rule. **Elevation**: the class is closed at the tool surface; residual is A1.

## Data classification (diff-scoped)
RESTRICTED: MCP keys (never logged or echoed; `resolveClient` moved verbatim). CONFIDENTIAL: loop payloads (`last_response`, `runs`, `total_cost`) — withheld on denial. INTERNAL: agent names and permission edges — echoed only where the caller supplied the name. PUBLIC (to operator scopes): tool descriptions — unchanged.

## Trend
Prior: `cso-diff-2026-09-15-2349-j10-journey` · Resolved 1 (ent#628) · Persistent 3 (ent#629, #2807, #2806) · New 0 · **Direction: improving**

## Remediation
No introduced findings; nothing to decide here. A1/A2 remain with their owners. Supply chain: the pre-existing moderate `hono` advisory (fix available) belongs to a dependabot bump.

*AI-assisted diff scan; not a substitute for a professional audit.*
