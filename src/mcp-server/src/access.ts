/**
 * Agent-access policy for the MCP tool surface (abilityai/trinity-enterprise#628).
 *
 * Before this module, "does this tool check the caller's permission edge?" was a
 * discipline: `checkAgentAccess` was called inside each tool's `execute` by
 * whoever remembered, in ten spellings across nine modules, and the tool added
 * last (`run_agent_loop`) called nothing — so an agent-scoped key could start a
 * loop on any same-owner sibling with no `agent_permissions` edge. Same class as
 * the admin gate before #1890: five patched call sites meant the GATE was wrong.
 *
 * This module makes it a mechanism with three parts:
 *
 *   1. `checkAgentEdge` — ONE implementation of the agent-scope rule (P-02):
 *      system bypasses; an agent key reaches itself and its permitted targets;
 *      a user key is passed through because the backend already scopes it; any
 *      other scope is denied (an allowlist, #2323 — never a fallthrough).
 *   2. `TOOL_ACCESS_POLICY` — every registered tool declares how it treats an
 *      agent target: `enforce` (gated here, at registration), `in-tool` (the
 *      tool resolves its target itself and gates it), `baselined` (ungated at
 *      the MCP layer; the entry names who holds the line), or `none` (no agent
 *      target). The table IS the audit ent#628 asked for, reviewed in code.
 *   3. `policyFor` — consulted by `server.ts` for every tool it registers. A
 *      tool with no row, an `enforce` row naming a parameter the tool does not
 *      declare, or a `none` row on a tool whose parameters name an agent, throws
 *      at startup. A tool added tomorrow cannot register without a row.
 *   4. `accessDenied` — the ONE serialiser for a returned denial (#2807). It
 *      stamps the per-call context so `withAudit` records the refusal; a deny
 *      site that serialises its own envelope fails `audit-denial.test.ts`.
 *
 * What this is NOT: a capability boundary. The backend resolves an agent key to
 * its owner carrying the owner's role (architecture.md Invariant #8), so the
 * REST routes behind these tools admit a sibling with no edge. Whether that
 * boundary should hold one hop down is abilityai/trinity-enterprise#629; the
 * `baselined` rows carrying that reference are its work list.
 *
 * Leaf module: imports `client.js` and `types.js` only.
 */

import { TrinityClient } from "./client.js";
import type { AgentAccessCheckResult, McpAuthContext, ToolOutcome } from "./types.js";

// ---------------------------------------------------------------------------
// Client resolution (moved from tools/chat.ts so the wrapper below and the
// tools share one implementation)
// ---------------------------------------------------------------------------

/**
 * Resolve the Trinity client for a request.
 * When requireApiKey is true, REQUIRES the MCP API key from the auth context.
 * When requireApiKey is false, uses the base client (backward compatibility).
 */
export function resolveClient(
  baseClient: TrinityClient,
  requireApiKey: boolean,
  authContext?: McpAuthContext
): TrinityClient {
  if (requireApiKey) {
    if (!authContext?.mcpApiKey) {
      throw new Error("MCP API key authentication required but no API key found in request context");
    }
    const userClient = new TrinityClient(baseClient.getBaseUrl());
    userClient.setToken(authContext.mcpApiKey);
    return userClient;
  }
  return baseClient;
}

// ---------------------------------------------------------------------------
// The gate
// ---------------------------------------------------------------------------

/** The #186 shape: one reason for "does not exist" and "not yours", no owner. */
export function uniformDenial(targetAgentName: string): AgentAccessCheckResult {
  return { allowed: false, reason: `Agent '${targetAgentName}' not found or not accessible` };
}

// ---------------------------------------------------------------------------
// The one serialiser for a returned denial (#2807)
// ---------------------------------------------------------------------------

/**
 * The subset of the tool-call context `accessDenied` writes. `session?` is
 * listed so the `{ session?: McpAuthContext }` object every tool already types
 * its context as passes TypeScript's weak-type check; the wrapper's fuller
 * `ToolCallContext` (audit.ts) is structurally a superset.
 */
export interface DenyCallContext {
  session?: McpAuthContext;
  outcome?: ToolOutcome;
}

/**
 * Serialise a denial envelope AND record that this call was refused.
 *
 * Every gate on this surface RETURNS its denial — the JSON the caller reads is
 * the contract (agents parse it), so throwing was never an option — but
 * `withAudit` labels a call by throw/no-throw, so a returned denial used to be
 * audited as `success: true` (#2807). The fix is a stamp: this helper writes
 * `context.outcome = { kind: "denied", reason }` on the PER-CALL context object
 * (FastMCP builds one per `execute`; #905 already stamps `requestId` there) and
 * the wrapper reads it after `execute`. The envelope is serialised exactly as
 * before — same keys, same order, same `null, 2` — so nothing a caller parses
 * changes.
 *
 * `auditReason` is for compound denials whose caller-facing reason is
 * deliberately uniform (`Loop '<id>' not found or not accessible`, `Report not
 * found`): the operator's admin-only row may carry the internal reason the site
 * already logs. Never stamp `context.session` — that object is shared by every
 * call on the session (`verify_login` relies on it).
 */
export function accessDenied(
  context: DenyCallContext | undefined,
  envelope: Record<string, unknown>,
  auditReason?: string
): string {
  if (context) {
    const reason =
      auditReason ??
      (typeof envelope.reason === "string" ? envelope.reason : undefined) ??
      (typeof envelope.error === "string" ? envelope.error : undefined) ??
      "Access denied";
    context.outcome = { kind: "denied", reason };
  }
  return JSON.stringify(envelope, null, 2);
}

/**
 * The agent-scope permission edge (P-02), decided at the MCP layer.
 *
 * - no auth context → allowed (dev mode installs no `authenticate`; the backend gates)
 * - `system` → allowed (Phase 11.1: the system agent talks to everyone)
 * - `agent` → itself, or a target with an `agent_permissions` edge from it
 *   (`GET /api/agents/{caller}/permissions`, which the client reads FAIL-CLOSED:
 *   an unreadable list is an empty list). A key row with no agent name is
 *   denied, not promoted to the user rule.
 * - `user` → allowed HERE: the backend already scopes a user key to what its
 *   owner can access (`AuthorizedAgent*` / `assert_agent_access`), and it does so
 *   by ROLE and per-user grant, which this layer cannot see (#2824).
 * - anything else → denied. `mcp_api_keys.scope` is free text; a scope nobody
 *   has invented yet is least-privileged, never `user` (#2323, types.ts).
 */
export async function checkAgentEdge(
  client: TrinityClient,
  authContext: McpAuthContext | undefined,
  targetAgentName: string
): Promise<AgentAccessCheckResult> {
  if (!authContext) {
    return { allowed: true };
  }
  if (authContext.scope === "system") {
    console.log(`[System Agent Access] ${authContext.agentName || "system"} -> ${targetAgentName} (bypassing permissions)`);
    return { allowed: true };
  }
  if (authContext.scope === "agent") {
    const callerAgentName = authContext.agentName;
    if (!callerAgentName) {
      return uniformDenial(targetAgentName);
    }
    if (callerAgentName === targetAgentName) {
      return { allowed: true };
    }
    if (await client.isAgentPermitted(callerAgentName, targetAgentName)) {
      return { allowed: true };
    }
    return {
      allowed: false,
      reason:
        `Permission denied: Agent '${callerAgentName}' is not permitted to communicate with '${targetAgentName}'. ` +
        `Configure permissions in the Trinity UI.`,
    };
  }
  if (authContext.scope === "user") {
    return { allowed: true };
  }
  return uniformDenial(targetAgentName);
}

// ---------------------------------------------------------------------------
// The policy table
// ---------------------------------------------------------------------------

export type ToolAccessPolicy =
  /** `server.ts` wraps `execute`: `params[param]`, when present, must pass `checkAgentEdge`. */
  | { kind: "enforce"; param: string }
  /** The tool resolves its own target (an id, a bound agent, a module-local gate) and gates it itself. */
  | { kind: "in-tool"; how: string }
  /** Ungated at the MCP layer. `owner` names the issue or the backend fence that holds the line. */
  | { kind: "baselined"; owner: string }
  /** No agent target. Checked against the tool's parameters at registration. */
  | { kind: "none"; why: string };

/**
 * Parameter names that address an agent. A `none` row on a tool declaring one
 * of these fails registration. `name` is deliberately absent: it names an agent
 * only in `agents.ts` and a playbook, room, credential or subscription
 * elsewhere — those tools carry explicit rows instead.
 */
export const AGENT_TARGET_PARAMS: ReadonlySet<string> = new Set([
  "agent_name",
  "agent",
  "agents",
  "target_agent",
  "source_agent",
]);

const ENT629 =
  "abilityai/trinity-enterprise#629 — the backend route is owner-equivalent for an agent key (Invariant #8); MCP gate pending that ruling";
const ADMIN_ONLY =
  "backend rejects agent principals (require_admin / assert_admin / reject_agent_principal, #1890)";
const TEARDOWN_HUMAN_ONLY =
  "backend fence: the gated route requires role 'creator' AND a HUMAN caller — reject_agent_principal plus a credential-kind refusal, because one call removes N agents without delete_agent's per-agent spawn-scope check (abilityai/trinity-enterprise#454)";
const REMINDER_SELF_GATE = "backend self-gate: reminders.py::_self_gate refuses an agent key naming another agent";
const SKILL_MANAGER_FENCE =
  "backend fence: routers/skills.py get_skill_managed_agent_by_name refuses an agent key whose agent does not hold the skills.manage capability — on a sibling AND on itself (abilityai/trinity-enterprise#596)";
const CONNECTOR_SCOPE = "connector scope — the key is bound to one agent; backend _enforce_connector_scope (ent#46)";
const ROOMS_SERVICE = "room membership is the rooms service's decision (ent#169, ent#443), not a per-agent permission edge";
const EVENT_EDGE = "backend gates by agent_permissions edge itself (event_subscriptions.py, uniform 403)";
const CHAT_GATE = "chat.ts checkAgentAccess → checkAgentEdge for agent/system scope, its own user-scope rule otherwise";
const SCHEDULES_GATE = "schedules.ts checkAgentAccess: reads are {self} ∪ permitted, writes are self-only";
const EXECUTIONS_GATE = "executions.ts checkAgentAccess ({self} ∪ permitted)";
const REPORTS_GATE = "reports.ts checkAgentAccess ({self} ∪ permitted)";
const OPERATOR_QUEUE_GATE = "operator_queue.ts checkAgentAccess ({self} ∪ permitted)";
const GIT_GATE = "git.ts `run` wrapper → checkAgentAccess ({self} ∪ permitted)";
const A2A_GATE = "a2a.ts checkAgentAccess ({self} ∪ permitted)";
const A2A_CALL_SELF = "a2a_call.ts checkSelf — self-only by design (an agent spends only its own endpoint credential)";
const AGENTS_INLINE = "agents.ts inline getPermittedAgents filter ({self} ∪ permitted)";
const LOOP_RESOLVE =
  "resolves the loop's agent (GET /api/loops/{id}) then checkAgentEdge; compound uniform denial (ent#628)";

/**
 * One row per registered static tool, keyed by tool NAME (a module rename moves
 * nothing here). Dynamic tools (`chat_with_<slug>`, #846) pass their policy to
 * `registerDynamicTool` explicitly. The `baselined` rows referencing ent#629
 * are that issue's work list; flip a row to `enforce` when the ruling lands.
 */
export const TOOL_ACCESS_POLICY: Readonly<Record<string, ToolAccessPolicy>> = {
  // --- agents.ts ---
  list_agents: { kind: "in-tool", how: AGENTS_INLINE },
  get_agent: { kind: "baselined", owner: ENT629 },
  get_agent_info: { kind: "in-tool", how: AGENTS_INLINE },
  get_agent_compatibility_report: { kind: "in-tool", how: AGENTS_INLINE },
  create_agent: { kind: "none", why: "creates the agent; `name` is the new agent's name, not a target" },
  rename_agent: { kind: "baselined", owner: ADMIN_ONLY },
  delete_agent: { kind: "baselined", owner: ENT629 },
  start_agent: { kind: "baselined", owner: ENT629 },
  stop_agent: { kind: "baselined", owner: ENT629 },
  list_templates: { kind: "none", why: "no agent target" },
  get_credential_status: { kind: "baselined", owner: ENT629 },
  inject_credentials: { kind: "baselined", owner: ADMIN_ONLY },
  export_credentials: { kind: "baselined", owner: ADMIN_ONLY },
  import_credentials: { kind: "baselined", owner: ADMIN_ONLY },
  export_agent_data: { kind: "baselined", owner: ENT629 },
  import_agent_data: { kind: "baselined", owner: ENT629 },
  get_credential_encryption_key: { kind: "none", why: "no agent target" },
  get_agent_ssh_access: { kind: "baselined", owner: ADMIN_ONLY },
  deploy_local_agent: { kind: "none", why: "creates the agent; `name` is the new agent's name, not a target" },
  initialize_github_sync: { kind: "baselined", owner: ENT629 },
  get_agent_github_pat_status: { kind: "baselined", owner: ENT629 },
  set_agent_github_pat: { kind: "baselined", owner: ENT629 },
  // --- chat.ts ---
  chat_with_agent: { kind: "in-tool", how: CHAT_GATE },
  get_chat_history: { kind: "baselined", owner: ENT629 },
  get_agent_logs: { kind: "baselined", owner: ENT629 },
  fan_out: { kind: "in-tool", how: CHAT_GATE },
  // --- systems.ts ---
  deploy_system: { kind: "none", why: "a system manifest, not an agent" },
  // NOT `none`: unlike its siblings this tool does name agents (`agents`, the
  // confirmed removal set) and deletes them. `agents` IS in
  // AGENT_TARGET_PARAMS, so a `none` row here would not merely have said
  // something false — `policyFor` throws on it at startup, and the server
  // would refuse to boot. The shape check is what makes this row load-bearing
  // rather than decorative.
  teardown_system: { kind: "baselined", owner: TEARDOWN_HUMAN_ONLY },
  list_systems: { kind: "none", why: "no agent target" },
  restart_system: { kind: "none", why: "a system name, not an agent" },
  get_system_manifest: { kind: "none", why: "a system name, not an agent" },
  // --- docs.ts ---
  get_agent_requirements: { kind: "none", why: "no agent target" },
  ask_trinity: { kind: "none", why: "no agent target" },
  // --- skills.ts ---
  list_skills: { kind: "none", why: "no agent target" },
  get_skill: { kind: "none", why: "a skill name, not an agent" },
  get_skills_library_status: { kind: "none", why: "no agent target" },
  assign_skill_to_agent: { kind: "baselined", owner: SKILL_MANAGER_FENCE },
  set_agent_skills: { kind: "baselined", owner: SKILL_MANAGER_FENCE },
  sync_agent_skills: { kind: "baselined", owner: SKILL_MANAGER_FENCE },
  get_agent_skills: { kind: "baselined", owner: ENT629 },
  run_skill: { kind: "none", why: "runs on the calling agent; a skill name, not an agent" },
  list_runnable_skills: { kind: "none", why: "no agent target" },
  // --- schedules.ts ---
  list_agent_schedules: { kind: "in-tool", how: SCHEDULES_GATE },
  create_agent_schedule: { kind: "in-tool", how: SCHEDULES_GATE },
  get_agent_schedule: { kind: "in-tool", how: SCHEDULES_GATE },
  update_agent_schedule: { kind: "in-tool", how: SCHEDULES_GATE },
  delete_agent_schedule: { kind: "in-tool", how: SCHEDULES_GATE },
  toggle_agent_schedule: { kind: "in-tool", how: SCHEDULES_GATE },
  trigger_agent_schedule: { kind: "in-tool", how: SCHEDULES_GATE },
  get_schedule_executions: { kind: "in-tool", how: SCHEDULES_GATE },
  // --- tags.ts ---
  list_tags: { kind: "none", why: "no agent target" },
  get_agent_tags: { kind: "baselined", owner: ENT629 },
  tag_agent: { kind: "baselined", owner: ENT629 },
  untag_agent: { kind: "baselined", owner: ENT629 },
  set_agent_tags: { kind: "baselined", owner: ENT629 },
  // --- notifications.ts ---
  send_notification: { kind: "none", why: "no agent target" },
  // --- reports.ts ---
  report: { kind: "none", why: "self-published; the backend self-gates the path agent (#918)" },
  record_metrics: { kind: "none", why: "self-recorded; the backend self-gates the path agent (ent#478)" },
  refresh_metric_definitions: { kind: "none", why: "self-scoped; reconciles the calling agent's own template (ent#478)" },
  get_metrics: { kind: "none", why: "self-scoped read; the backend self-gates the path agent (ent#479)" },
  get_objectives: { kind: "none", why: "self-scoped read; the backend self-gates the path agent (ent#666)" },
  list_reports: { kind: "in-tool", how: REPORTS_GATE },
  get_report: { kind: "in-tool", how: "reports.ts resolves the report's agent, then " + REPORTS_GATE },
  // --- canvas.ts ---
  set_canvas: { kind: "none", why: "the calling agent's own canvas (ent#438)" },
  patch_canvas: { kind: "none", why: "the calling agent's own canvas (ent#438)" },
  get_canvas: { kind: "none", why: "the calling agent's own canvas (ent#438)" },
  list_canvases: { kind: "none", why: "the calling agent's own canvases (ent#438)" },
  clear_canvas: { kind: "none", why: "the calling agent's own canvas (ent#438)" },
  // --- files.ts ---
  share_file: { kind: "none", why: "the calling agent's own public folder (FILES-001)" },
  // --- pipelines.ts ---
  list_agent_pipelines: { kind: "baselined", owner: ENT629 },
  get_agent_pipeline_state: { kind: "baselined", owner: ENT629 },
  // --- subscriptions.ts ---
  register_subscription: { kind: "none", why: "`name` is the subscription's name, not an agent" },
  list_subscriptions: { kind: "none", why: "no agent target" },
  assign_subscription: { kind: "baselined", owner: ENT629 },
  clear_agent_subscription: { kind: "baselined", owner: ENT629 },
  get_agent_auth: { kind: "baselined", owner: ENT629 + "; the secrets branch is assert_admin" },
  delete_subscription: { kind: "none", why: "a subscription name, not an agent" },
  // --- monitoring.ts ---
  get_fleet_health: { kind: "none", why: "no agent target" },
  get_agent_health: { kind: "baselined", owner: ENT629 },
  trigger_health_check: { kind: "baselined", owner: ENT629 },
  // --- nevermined.ts ---
  configure_nevermined: { kind: "baselined", owner: ENT629 },
  get_nevermined_config: { kind: "baselined", owner: ENT629 },
  toggle_nevermined: { kind: "baselined", owner: ADMIN_ONLY },
  get_nevermined_payments: { kind: "baselined", owner: ADMIN_ONLY },
  // --- executions.ts ---
  list_recent_executions: { kind: "in-tool", how: EXECUTIONS_GATE },
  get_execution_result: { kind: "in-tool", how: EXECUTIONS_GATE },
  get_fan_out_result: { kind: "in-tool", how: EXECUTIONS_GATE },
  get_agent_activity_summary: { kind: "in-tool", how: EXECUTIONS_GATE },
  search_executions: {
    kind: "baselined",
    owner:
      "backend rejects agent principals on the enterprise execution-search route (reject_agent_principal, abilityai/trinity-enterprise#653); the tool's own canAccess allow-lists system/user scope",
  },
  // --- events.ts ---
  emit_event: { kind: "none", why: "emits as the calling agent (EVT-001)" },
  subscribe_to_event: { kind: "baselined", owner: EVENT_EDGE },
  list_event_subscriptions: { kind: "none", why: "the calling agent's own subscriptions" },
  delete_event_subscription: { kind: "none", why: "a subscription id owned by the calling agent" },
  // --- channels.ts ---
  list_channel_groups: {
    kind: "baselined",
    owner: ENT629 + "; the Slack branch already rejects agent principals (reject_agent_principal), the Telegram branch does not",
  },
  send_group_message: { kind: "baselined", owner: ENT629 },
  // --- messages.ts ---
  send_message: { kind: "baselined", owner: ENT629 },
  // --- voice.ts ---
  send_voice_reply: { kind: "baselined", owner: ENT629 + "; effect-guarded per execution (#1084)" },
  // --- memory.ts ---
  write_user_memory: { kind: "baselined", owner: ENT629 },
  // --- decisions.ts (ent#638) --- the target is only ever the seat's own agent
  record_decision: { kind: "enforce", param: "agent_name" },
  list_seat_decisions: { kind: "enforce", param: "agent_name" },
  // --- loops.ts ---
  run_agent_loop: { kind: "enforce", param: "agent_name" },
  get_loop_status: { kind: "in-tool", how: LOOP_RESOLVE },
  stop_loop: { kind: "in-tool", how: LOOP_RESOLVE },
  // --- reminders.ts ---
  set_reminder: { kind: "baselined", owner: REMINDER_SELF_GATE },
  list_reminders: { kind: "baselined", owner: REMINDER_SELF_GATE },
  cancel_reminder: { kind: "baselined", owner: REMINDER_SELF_GATE },
  // --- voip.ts ---
  call_user: { kind: "baselined", owner: ENT629 + "; server-gated + rate-limited (VOIP-001)" },
  // --- operator_queue.ts ---
  list_operator_queue: { kind: "in-tool", how: OPERATOR_QUEUE_GATE },
  get_operator_queue_item: { kind: "in-tool", how: "operator_queue.ts resolves the item's agent, then " + OPERATOR_QUEUE_GATE },
  respond_to_operator_queue: {
    kind: "in-tool",
    how:
      "operator_queue.ts resolves the item's agent, then " + OPERATOR_QUEUE_GATE +
      "; the backend then refuses every key but a person's (reject_non_person_principal, trinity-enterprise#611)",
  },
  get_my_ask: { kind: "none", why: "self-acting: the agent comes from the key (resolveActingAgent); the backend re-checks identity (trinity-enterprise#611)" },
  // --- git.ts ---
  get_git_status: { kind: "in-tool", how: GIT_GATE },
  git_sync: { kind: "in-tool", how: GIT_GATE },
  get_git_log: { kind: "in-tool", how: GIT_GATE },
  git_pull: { kind: "in-tool", how: GIT_GATE },
  get_git_sync_state: { kind: "in-tool", how: GIT_GATE },
  reset_to_main_preserve_state: { kind: "in-tool", how: GIT_GATE },
  // --- rooms.ts ---
  create_room: { kind: "baselined", owner: ROOMS_SERVICE },
  list_rooms: { kind: "none", why: "no agent target" },
  read_room: { kind: "none", why: "a room id; membership is the rooms service's (ent#169)" },
  post_to_room: { kind: "none", why: "a room id; membership is the rooms service's (ent#169)" },
  close_room: { kind: "none", why: "a room id; membership is the rooms service's (ent#169)" },
  // --- a2a.ts ---
  get_agent_a2a_config: { kind: "in-tool", how: A2A_GATE },
  set_agent_a2a_exposure: { kind: "baselined", owner: ADMIN_ONLY + "; enterprise route, human-only" },
  get_agent_a2a_card: { kind: "baselined", owner: ENT629 },
  set_a2a_inbound_allowlist: { kind: "baselined", owner: ADMIN_ONLY + "; enterprise route, human-only" },
  register_a2a_endpoint: { kind: "baselined", owner: ADMIN_ONLY + "; enterprise route, human-only" },
  list_a2a_endpoints: { kind: "in-tool", how: A2A_GATE },
  remove_a2a_endpoint: { kind: "baselined", owner: ADMIN_ONLY + "; enterprise route, human-only" },
  // --- a2a_call.ts ---
  call_a2a_agent: { kind: "in-tool", how: A2A_CALL_SELF },
  get_a2a_task: { kind: "in-tool", how: A2A_CALL_SELF },
  // --- credential_vault.ts ---
  list_available_credentials: { kind: "none", why: "the calling agent's own grants (ent#279)" },
  fetch_credential: { kind: "none", why: "`name` is a credential; the backend scopes it to the calling agent (ent#279)" },
  // --- assignments.ts --- get_agent_assignments is FENCED (not registered; 0.9.5 F1) —
  // the totality test forbids a row for an unregistered tool. Restore with the registration:
  //   get_agent_assignments: { kind: "baselined", owner: ENT629 + "; the route's 404 is uniform (ent#500)" },
  // --- connector.ts (connector / anonymous tiers) ---
  list_playbooks: { kind: "baselined", owner: CONNECTOR_SCOPE },
  run_playbook: { kind: "baselined", owner: CONNECTOR_SCOPE },
  ask: { kind: "baselined", owner: CONNECTOR_SCOPE },
  // --- auth.ts (#848 inline auth, anonymous tier) ---
  request_login: { kind: "none", why: "no agent target" },
  verify_login: { kind: "none", why: "no agent target" },
};

// ---------------------------------------------------------------------------
// Registration-time assertion + the enforce wrapper
// ---------------------------------------------------------------------------

export class ToolAccessPolicyError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ToolAccessPolicyError";
  }
}

/** The parameter names a tool declares, read from its zod object schema. */
export function toolParamNames(tool: { parameters?: unknown }): string[] {
  const shape = (tool.parameters as { shape?: Record<string, unknown> } | undefined)?.shape;
  return shape ? Object.keys(shape) : [];
}

/**
 * The policy a tool registers under, or a thrown `ToolAccessPolicyError`.
 * Called by `server.ts` for EVERY tool it registers — static ones look their
 * name up in `TOOL_ACCESS_POLICY`, dynamic ones pass `explicit`. The three
 * shape checks make the table say something the code can contradict.
 */
export function policyFor(
  tool: { name: string; parameters?: unknown },
  explicit?: ToolAccessPolicy
): ToolAccessPolicy {
  const policy = explicit ?? TOOL_ACCESS_POLICY[tool.name];
  if (!policy) {
    throw new ToolAccessPolicyError(
      `tool '${tool.name}' has no entry in TOOL_ACCESS_POLICY (src/access.ts). Every tool declares how it treats an ` +
        `agent target: enforce:{param} (gated at registration), in-tool:<how>, baselined:<owner>, or none:<why>.`
    );
  }
  const params = toolParamNames(tool);
  if (policy.kind === "enforce" && !params.includes(policy.param)) {
    throw new ToolAccessPolicyError(
      `tool '${tool.name}' is enforce:{param:"${policy.param}"} but declares no such parameter (has: ${params.join(", ") || "none"})`
    );
  }
  if (policy.kind === "none") {
    const targets = params.filter((p) => AGENT_TARGET_PARAMS.has(p));
    if (targets.length > 0) {
      throw new ToolAccessPolicyError(
        `tool '${tool.name}' is declared none (no agent target) but its parameters name an agent: ${targets.join(", ")}`
      );
    }
  }
  return policy;
}

/** The subset of the tool-call context this wrapper reads. */
interface AccessCallContext {
  session?: McpAuthContext;
}

/**
 * Wrap a tool's `execute` so that, when the caller supplies the target
 * parameter, `checkAgentEdge` runs BEFORE the tool does anything. An absent
 * parameter passes through untouched — the tool's own resolution applies (an
 * agent key defaults to itself; a user key is told the parameter is required).
 *
 * The denial is RETURNED, in the same envelope every other gate on this surface
 * returns, so callers that read `success` and callers that read `error` both
 * see it. `accessDenied` stamps the call context so `withAudit` records the
 * refusal as one (#2807) — the envelope bytes are unchanged.
 */
export function withAgentAccess<P extends Record<string, unknown>>(
  toolName: string,
  execute: (params: P, context?: AccessCallContext) => Promise<string>,
  policy: Extract<ToolAccessPolicy, { kind: "enforce" }>,
  resolve: (context?: AccessCallContext) => TrinityClient
): (params: P, context?: AccessCallContext) => Promise<string> {
  return async (params: P, context?: AccessCallContext) => {
    const target = params?.[policy.param];
    if (typeof target !== "string" || !target) {
      return execute(params, context);
    }
    const authContext = context?.session;
    const access = await checkAgentEdge(resolve(context), authContext, target);
    if (!access.allowed) {
      const caller = authContext?.agentName || authContext?.userId || "unknown";
      console.log(`[Access Denied] ${toolName}: ${caller} -> ${target}: ${access.reason}`);
      return accessDenied(context, { success: false, error: "Access denied", reason: access.reason, caller, target });
    }
    return execute(params, context);
  };
}

/**
 * The agent a SELF-ACTING tool acts as (#2975).
 *
 * `report`, the canvas tools and the metrics tools take no target parameter on
 * purpose — they act as the caller, so there is nothing to spoof and the
 * identity has to come from the key. Each resolved it itself with
 * `scope === "agent" && agentName`, which refused the platform's own
 * `trinity-system`: its key is `scope: "system"` (`system_agent_service`
 * mints it agent-scoped and then flips the scope), and #1816 makes that
 * permanent — the orchestrator's key is never re-minted as `agent`, so
 * "issue it an agent-scoped key instead" is not available. The system agent
 * could read everything and publish nothing: its daily fleet-health report and
 * its canvas both fell back to files and the operator queue.
 *
 * ONE home for the rule, for the ent#628 reason the permission edge has one:
 * ten spellings across nine modules is how the tenth ships with the old rule.
 *
 * It stays an ALLOWLIST over `mcp_api_keys.scope` — a free-text column with no
 * CHECK constraint (#1854, #2323), so a denylist is open at the top:
 *
 *   - `agent`  → the calling agent. Unchanged.
 *   - `system` → the agent the key was minted FOR, and only when the key
 *     carries one. The name comes from the key row, never from a parameter,
 *     so this widens identity by exactly zero: a system key already reaches
 *     every agent's data on the read surfaces.
 *   - everything else — `user`, `connector`, `portal_delegate`, `ops`,
 *     `anonymous`, and whatever ships next — is refused. `connector` is the
 *     one worth naming: it carries an `agentName` too (it is bound to one
 *     agent), and it is an END USER's consumption key. Letting it through
 *     would let a client publish reports as the agent serving them.
 *
 * A `system` key with no `agentName` is refused as well: there is no identity
 * to attribute the write to, and inventing one is exactly the spoof these
 * tools are shaped to prevent.
 */
export const SELF_ACTING_SCOPES: ReadonlySet<string> = new Set(["agent", "system"]);

export function resolveActingAgent(
  authContext: McpAuthContext | undefined,
  what: string,
): string {
  const scope = authContext?.scope;
  const agentName = authContext?.agentName;
  if (scope !== undefined && SELF_ACTING_SCOPES.has(scope) && agentName) {
    return agentName;
  }
  throw new Error(
    `${what}: this call requires a key that carries an agent identity — an ` +
      `agent-scoped key, or the platform orchestrator's system-scoped key. ` +
      `This key is ${scope ? `'${scope}'-scoped` : "unscoped"}` +
      `${agentName ? "" : " and names no agent"}.`,
  );
}
