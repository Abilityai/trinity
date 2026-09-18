/**
 * #2807 — a RETURNED access denial is audited as a refusal.
 *
 * Every gate on the MCP tool surface returns its denial as a JSON string (the
 * envelope agents parse), and `withAudit` labelled a call by throw/no-throw —
 * so a refused chat_with_agent / chat_with_<slug> / fan_out / run_agent_loop
 * call left a row reading `success: true`. Reproduced live on 2026-09-16: the
 * refused call's row was `{"tool":"chat_with_agent","duration_ms":32,"success":true}`.
 *
 * The fix is a stamp: the deny site serialises through `access.ts::accessDenied`,
 * which writes `context.outcome = {kind: "denied", reason}` on the PER-CALL
 * context, and the wrapper reads it after `execute`. These tests capture the
 * fire-and-forget audit POST through a `globalThis.fetch` stub (the
 * `task-receipt.test.ts` idiom): `postAudit` calls `fetch` synchronously before
 * its first await, so the entry is recorded by the time the wrapped call
 * resolves. `access-wiring.test.ts` proves the same over the real transport.
 *
 * The last block is the mechanism half: a `!allowed` branch or an
 * `error: "Access denied"` envelope anywhere under src/ that bypasses the
 * helper fails here — a thirtieth deny site cannot forget to stamp.
 *
 * Runner: node:test → `node --import tsx --test src/*.test.ts`.
 */
import { describe, it, beforeEach, afterEach } from "node:test";
import { strict as assert } from "node:assert";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { configureAudit, withAudit, type ToolCallContext } from "./audit.js";
import { accessDenied, withAgentAccess } from "./access.js";
import { ApiError, type TrinityClient } from "./client.js";
import { createChatTools, runAgentChat } from "./tools/chat.js";
import { makeDedicatedChatTool } from "./tools/dynamic-agents.js";
import { createReportTools } from "./tools/reports.js";

const realFetch = globalThis.fetch;

interface AuditRow {
  details: Record<string, unknown>;
  target_id?: string;
  actor_agent_name?: string;
  mcp_scope?: string;
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** Records every audit POST; answers the permission read; rejects anything else. */
function stubFetch(permitted: string[] = []): AuditRow[] {
  const rows: AuditRow[] = [];
  globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/api/internal/audit")) {
      rows.push(JSON.parse(String(init?.body)));
      return json({ event_id: `ev_${rows.length}`, status: "logged" });
    }
    if (/\/api\/agents\/[^/]+\/permissions$/.test(url)) {
      return json({ source_agent: "alpha", permitted_agents: permitted.map((name) => ({ name })), available_agents: [] });
    }
    throw new Error(`unstubbed fetch: ${url}`);
  }) as typeof fetch;
  return rows;
}

type FakeClient = TrinityClient & { calls: string[] };

function fakeClient(permitted: boolean): FakeClient {
  const calls: string[] = [];
  const fake: Partial<TrinityClient> & { calls: string[] } = {
    calls,
    getBaseUrl: () => "http://backend.test",
    isAgentPermitted: async () => permitted,
    getAgentAccessInfo: async () => ({ owner: "u1", is_shared: true }) as any,
    chat: async (name: string) => {
      calls.push(`chat:${name}`);
      return { response: "ok" } as any;
    },
    task: async (name: string) => {
      calls.push(`task:${name}`);
      return { status: "accepted", execution_id: "ex_1" } as any;
    },
    // A report row with no owning agent: get_report refuses it fail-closed.
    getReport: async () => ({ id: "r1" }) as any,
  };
  return fake as unknown as FakeClient;
}

const agentSession = (agentName: string) =>
  ({ scope: "agent", agentName, userId: "u1", keyId: "k1", keyName: "kn", mcpApiKey: "trinity_mcp_test" }) as any;

const EDGE_REASON = (a: string, b: string) =>
  `Permission denied: Agent '${a}' is not permitted to communicate with '${b}'. Configure permissions in the Trinity UI.`;

describe("#2807 a returned denial is audited as a refusal", () => {
  let rows: AuditRow[];

  beforeEach(() => {
    configureAudit({ apiUrl: "http://audit.test", secret: "test-secret" });
    rows = stubFetch();
  });
  afterEach(() => {
    globalThis.fetch = realFetch;
  });

  const chatTool = (client: TrinityClient) =>
    withAudit<{ agent_name: string; message: string }>("chat_with_agent", (p, ctx) =>
      runAgentChat(client, false, false, p.agent_name, { message: p.message }, ctx)
    );

  it("chat_with_agent: the row says refused, and the caller's JSON is byte-identical", async () => {
    const client = fakeClient(false);
    const out = await chatTool(client)({ agent_name: "bravo", message: "hi" }, { session: agentSession("alpha") });

    assert.equal(
      out,
      JSON.stringify({ error: "Access denied", reason: EDGE_REASON("alpha", "bravo"), caller: "alpha", target: "bravo" }, null, 2)
    );
    assert.equal(rows.length, 1, "exactly one audit row");
    assert.equal(rows[0].details.tool, "chat_with_agent");
    assert.equal(rows[0].details.success, false, `the refusal was audited as a success: ${JSON.stringify(rows[0].details)}`);
    assert.equal(rows[0].details.denied, true);
    assert.equal(rows[0].details.error, EDGE_REASON("alpha", "bravo"));
    assert.equal(rows[0].target_id, "bravo");
    assert.equal(rows[0].actor_agent_name, "alpha");
    assert.equal(rows[0].mcp_scope, "agent");
    assert.deepEqual(client.calls, [], "nothing was dispatched");
  });

  it("a dedicated chat_with_<slug> tool: refused and labelled, with the bound target on the row", async () => {
    const tool = makeDedicatedChatTool(fakeClient(false), false, false, "secret-bot", "chat_with_secret_bot", "desc");
    const wrapped = withAudit(tool.name, tool.execute, "secret-bot");
    const out = JSON.parse(await wrapped({ message: "hi" } as any, { session: agentSession("alpha") }));

    assert.equal(out.error, "Access denied");
    assert.equal(rows[0].details.tool, "chat_with_secret_bot");
    assert.equal(rows[0].details.success, false);
    assert.equal(rows[0].details.denied, true);
    assert.equal(rows[0].target_id, "secret-bot");
  });

  it("fan_out in key mode (a real client over the stubbed permission read): refused and labelled", async () => {
    // requireApiKey=true is the production shape: fan_out reads the session only
    // then, and resolveClient mints a real TrinityClient whose permission read
    // the fetch stub answers with an empty edge list.
    const tools = createChatTools(fakeClient(true), true, false);
    const wrapped = withAudit("fan_out", tools.fanOut.execute);
    const out = await wrapped(
      { agent_name: "bravo", tasks: [{ id: "t1", message: "x" }] } as any,
      { session: agentSession("alpha") }
    );

    assert.equal(out, JSON.stringify({ error: "Access denied", reason: EDGE_REASON("alpha", "bravo") }, null, 2));
    const row = rows.find((r) => r.details.tool === "fan_out");
    assert.ok(row, `no audit row for fan_out among ${JSON.stringify(rows)}`);
    assert.equal(row.details.success, false);
    assert.equal(row.details.denied, true);
    assert.equal(row.details.error, EDGE_REASON("alpha", "bravo"));
    assert.equal(row.target_id, "bravo");
  });

  it("get_report: the uniform 'Report not found' refusal is a refusal on the row, carrying the internal reason", async () => {
    const tools = createReportTools(fakeClient(false), false);
    const wrapped = withAudit("get_report", tools.getReport.execute);
    const out = JSON.parse(await wrapped({ report_id: "r1" } as any, { session: agentSession("alpha") }));

    assert.equal(out.error, "Report not found");
    assert.equal(rows[0].details.success, false);
    assert.equal(rows[0].details.denied, true);
    assert.match(String(rows[0].details.error), /no agent_name/);
  });

  it("run_agent_loop through the ent#628 enforce wrapper: refused, labelled, execute never ran", async () => {
    let ran = 0;
    const gated = withAgentAccess(
      "run_agent_loop",
      async () => {
        ran++;
        return "{}";
      },
      { kind: "enforce", param: "agent_name" },
      () => fakeClient(false)
    );
    const wrapped = withAudit("run_agent_loop", gated);
    const out = JSON.parse(await wrapped({ agent_name: "bravo", message: "m" }, { session: agentSession("alpha") }));

    assert.equal(out.error, "Access denied");
    assert.equal(ran, 0);
    assert.equal(rows[0].details.success, false);
    assert.equal(rows[0].details.denied, true);
    assert.equal(rows[0].target_id, "bravo");
  });

  it("a permitted call carries no marker, and the stamp never lands on the shared session object", async () => {
    const session = agentSession("alpha");
    const client = fakeClient(false);
    const wrapped = chatTool(client);

    await wrapped({ agent_name: "bravo", message: "hi" }, { session }); // refused
    client.isAgentPermitted = async () => true;
    await wrapped({ agent_name: "bravo", message: "hi again" }, { session }); // permitted: fresh per-call context, same session

    assert.equal(rows.length, 2);
    assert.equal(rows[0].details.denied, true);
    assert.equal(rows[1].details.success, true, `the permitted call inherited a stale refusal: ${JSON.stringify(rows[1].details)}`);
    assert.equal(rows[1].details.denied, undefined);
    assert.equal(rows[1].details.error, undefined);
    assert.equal(session.outcome, undefined, "the stamp leaked onto the session object");
    assert.deepEqual(client.calls, ["chat:bravo"]);
  });

  it("a direct call with no context object is still labelled", async () => {
    const wrapped = withAudit<{ agent_name: string }>("t", async (_p, ctx) =>
      accessDenied(ctx, { error: "Access denied", reason: "nope" })
    );
    const out = await wrapped({ agent_name: "bravo" });

    assert.equal(out, JSON.stringify({ error: "Access denied", reason: "nope" }, null, 2));
    assert.equal(rows[0].details.denied, true);
    assert.equal(rows[0].details.success, false);
    assert.equal(rows[0].details.error, "nope");
  });

  it("a plain throw is a failure, not a refusal; a thrown backend 403 is a refusal; a stamp survives a throw", async () => {
    const boom = withAudit("t", async () => {
      throw new Error("boom");
    });
    await assert.rejects(() => boom({} as any, {}), /boom/);
    assert.equal(rows[0].details.success, false);
    assert.equal(rows[0].details.denied, undefined);

    const forbidden = withAudit("t", async () => {
      throw new ApiError(403, "forbidden");
    });
    await assert.rejects(() => forbidden({} as any, {}), /403/);
    assert.equal(rows[1].details.success, false);
    assert.equal(rows[1].details.denied, true);

    const stampedThrow = withAudit("t", async (_p, ctx) => {
      accessDenied(ctx, { error: "Access denied", reason: "r" });
      throw new Error("after");
    });
    await assert.rejects(() => stampedThrow({} as any, {}), /after/);
    assert.equal(rows[2].details.denied, true);
    assert.equal(rows[2].details.error, "after");
  });

  it("accessDenied: auditReason overrides the row reason, the envelope is untouched, no context is tolerated", () => {
    const ctx: ToolCallContext = {};
    const env = { success: false, error: "Access denied", reason: "Loop 'x' not found or not accessible", hint: "h" };
    const out = accessDenied(ctx, env, EDGE_REASON("a", "b"));

    assert.equal(out, JSON.stringify(env, null, 2));
    assert.deepEqual(ctx.outcome, { kind: "denied", reason: EDGE_REASON("a", "b") });
    assert.equal(accessDenied(undefined, { error: "Report not found" }), JSON.stringify({ error: "Report not found" }, null, 2));
    const c2: ToolCallContext = {};
    accessDenied(c2, { error: "Report not found" });
    assert.equal(c2.outcome?.reason, "Report not found");
  });
});

// ---------------------------------------------------------------------------
// The mechanism half: no deny site under src/ can bypass the helper
// ---------------------------------------------------------------------------

describe("#2807 the label is a mechanism: no deny site bypasses accessDenied", () => {
  const SRC = dirname(fileURLToPath(import.meta.url));
  const files: string[] = [];
  const walk = (dir: string) => {
    for (const name of readdirSync(dir)) {
      const p = join(dir, name);
      if (statSync(p).isDirectory()) walk(p);
      else if (p.endsWith(".ts") && !p.endsWith(".test.ts") && !p.endsWith(".d.ts")) files.push(p);
    }
  };
  walk(SRC);
  const rel = (f: string) => f.slice(SRC.length + 1);
  const DENY_CHECK = /if \(!\s*\w+(?:\.\w+)*\.allowed\)/g;

  /** Same-file helpers whose body reaches accessDenied (a2a.ts `denied`, loops.ts `denyUnlessAccessible`). */
  const localDenyHelpers = (src: string): string[] => {
    const names: string[] = [];
    for (const chunk of src.split(/\n(?=  (?:const|function) )/)) {
      const m = chunk.match(/^  (?:const|function) (\w+)/);
      if (!m) continue;
      const own = chunk.split("\n  return {")[0];
      if (own.includes("accessDenied(")) names.push(m[1]);
    }
    return names;
  };

  /** The braced block or the single statement that follows a deny check. */
  const blockAfter = (src: string, idx: number): string => {
    let i = idx;
    while (src[i] === " ") i++;
    if (src[i] === "{") {
      let depth = 0;
      for (let j = i; j < src.length; j++) {
        if (src[j] === "{") depth++;
        else if (src[j] === "}" && --depth === 0) return src.slice(i, j + 1);
      }
      return src.slice(i);
    }
    const end = src.indexOf(";", i);
    return src.slice(i, end === -1 ? undefined : end + 1);
  };

  it("every `!allowed` branch reaches accessDenied, directly or through a same-file helper that does", () => {
    const offenders: string[] = [];
    for (const f of files) {
      const src = readFileSync(f, "utf8");
      const helpers = localDenyHelpers(src);
      for (const m of src.matchAll(DENY_CHECK)) {
        const block = blockAfter(src, m.index! + m[0].length);
        const ok = block.includes("accessDenied(") || helpers.some((h) => block.includes(`${h}(`));
        if (!ok) offenders.push(`${rel(f)}:${src.slice(0, m.index).split("\n").length}`);
      }
    }
    assert.deepEqual(
      offenders,
      [],
      `a denial branch that does not stamp the call context — it would be audited as a success:\n${offenders.join("\n")}`
    );
  });

  it('every `error: "Access denied"` envelope is serialised by accessDenied, never by a bare JSON.stringify', () => {
    const offenders: string[] = [];
    for (const f of files) {
      const lines = readFileSync(f, "utf8").split("\n");
      lines.forEach((line, i) => {
        if (!/error:\s*"Access denied"/.test(line)) return;
        const window = lines.slice(Math.max(0, i - 4), i + 1).join("\n");
        const last = Math.max(window.lastIndexOf("accessDenied("), window.lastIndexOf("JSON.stringify("));
        if (last === -1 || window.slice(last).startsWith("JSON.stringify(")) offenders.push(`${rel(f)}:${i + 1}`);
      });
    }
    assert.deepEqual(offenders, [], `an "Access denied" envelope that bypasses accessDenied:\n${offenders.join("\n")}`);
  });

  it("the guard sees the surface it guards, and is not vacuous", () => {
    assert.ok(files.some((f) => f.endsWith("/tools/chat.ts")));
    assert.ok(files.some((f) => f.endsWith("/access.ts")));
    const branches = files.reduce((n, f) => n + (readFileSync(f, "utf8").match(DENY_CHECK) ?? []).length, 0);
    assert.ok(branches >= 20, `expected the deny branches to be visible to the guard, found ${branches}`);
  });
});
