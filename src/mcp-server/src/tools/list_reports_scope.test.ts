/**
 * #3300 — a broad list_reports under an agent key is cut over the rows it keeps.
 *
 * The tool read one page of `GET /api/reports` — ranked over everything the
 * key could reach and cut at `limit` — and only then dropped the rows outside
 * {self} ∪ permitted. Rows the agent may see could sit below the cut, so a
 * short page was not evidence that nothing else existed. Now (the
 * trinity-enterprise#815 shape, as list_operator_queue does) the tool reads its
 * permits FIRST and sends them as `agent_names`, so the backend narrows in SQL
 * before the limit. The post-filter stays as a belt, and a row it drops is
 * reported, never a silently shorter page.
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createReportTools, LIST_REPORTS_BELT_WARNING } from "./reports.js";
import type { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

const AGENT_CTX = {
  userId: "admin", userEmail: "a@example.com", keyName: "k",
  scope: "agent", agentName: "worker", mcpApiKey: "trinity_mcp_x",
} as McpAuthContext;
const USER_CTX = {
  userId: "admin", userEmail: "a@example.com", keyName: "k",
  scope: "user", mcpApiKey: "trinity_mcp_x",
} as McpAuthContext;

const summary = (id: string, agent: string) => ({
  id, agent_name: agent, report_type: "t", title: `t-${id}`, created_at: "2026-10-30T00:00:00Z",
});

type ListCall = Record<string, unknown> & { agent_names?: string[] };

function makeTools(opts: {
  permitted?: string[] | (() => Promise<string[]>);
  rows?: ReturnType<typeof summary>[];
  calls?: ListCall[];
  strictSeen?: boolean[];
}) {
  const fake = {
    getBaseUrl: () => "http://backend",
    setToken: () => {},
    getPermittedAgents: async (_a: string, o: { strict?: boolean } = {}) => {
      opts.strictSeen?.push(!!o.strict);
      return typeof opts.permitted === "function" ? opts.permitted() : (opts.permitted ?? []);
    },
    listReports: async (p: ListCall) => {
      opts.calls?.push(p);
      return (opts.rows ?? []) as never;
    },
    listAgentReports: async (_n: string, p: ListCall) => {
      opts.calls?.push(p);
      return (opts.rows ?? []) as never;
    },
    getReport: async () => ({}),
  } as unknown as TrinityClient;
  return createReportTools(fake, false);
}

const run = async (tools: ReturnType<typeof makeTools>, args: Record<string, unknown>, session: McpAuthContext) =>
  JSON.parse(await tools.listReports.execute({ limit: 50, offset: 0, ...args } as never, { session }));

describe("#3300 a broad agent-key listing narrows before the limit", () => {
  it("sends {self} ∪ permitted as agent_names, read strictly first", async () => {
    const calls: ListCall[] = [];
    const strictSeen: boolean[] = [];
    await run(makeTools({ permitted: ["friend"], calls, strictSeen }), {}, AGENT_CTX);
    assert.deepEqual([...(calls[0].agent_names ?? [])].sort(), ["friend", "worker"]);
    assert.deepEqual(strictSeen, [true]);
  });

  it("a page the backend already narrowed is returned as before, with no extra keys", async () => {
    const out = await run(
      makeTools({ permitted: ["friend"], rows: [summary("1", "worker"), summary("2", "friend")] }),
      {}, AGENT_CTX,
    );
    assert.deepEqual(Object.keys(out).sort(), ["count", "reports"]);
    assert.equal(out.count, 2);
  });

  it("a row the belt drops is reported, not a silently shorter page", async () => {
    const out = await run(
      makeTools({ permitted: ["friend"], rows: [summary("1", "worker"), summary("2", "stranger")] }),
      {}, AGENT_CTX,
    );
    assert.deepEqual(out.reports.map((r: { agent_name: string }) => r.agent_name), ["worker"]);
    assert.equal(out.count, 1);
    assert.equal(out.withheld, 1);
    assert.deepEqual(out.warnings, [LIST_REPORTS_BELT_WARNING]);
  });

  it("a failed permissions read is an error, never an empty peer set", async () => {
    const calls: ListCall[] = [];
    const out = await run(
      makeTools({ permitted: async () => { throw new Error("docker down"); }, calls }),
      {}, AGENT_CTX,
    );
    assert.equal(out.error, "permissions_unavailable");
    assert.equal(out.retryable, true);
    assert.equal(calls.length, 0, "nothing is listed over a guessed permit set");
  });

  it("a permit set the backend would refuse is refused before sending", async () => {
    const calls: ListCall[] = [];
    const many = Array.from({ length: 500 }, (_, i) => `peer-${i}`);
    const out = await run(makeTools({ permitted: many, calls }), {}, AGENT_CTX);
    assert.equal(out.error, "permit_set_too_large");
    assert.equal(calls.length, 0);
  });

  it("a user key and a scoped listing send no agent_names", async () => {
    const calls: ListCall[] = [];
    await run(makeTools({ calls }), {}, USER_CTX);
    await run(makeTools({ permitted: ["friend"], calls }), { agent_name: "worker" }, AGENT_CTX);
    assert.equal(calls.length, 2);
    assert.equal(calls[0].agent_names, undefined);
    assert.equal(calls[1].agent_names, undefined);
  });
});
