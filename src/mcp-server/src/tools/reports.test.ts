/**
 * #1538 — agent report read-back (list_reports / get_report).
 *
 * The tools are thin proxies over already-access-controlled REST endpoints, so
 * what needs pinning is the layer the BACKEND cannot enforce: an agent-scoped
 * key resolves to its OWNER, so the backend scopes reads to everything the owner
 * can see — broader than the calling agent's permits. The MCP layer narrows that
 * to {self} ∪ permitted, exactly as list_operator_queue does (#1104).
 *
 * Drives the real tool execute() with a fake TrinityClient (requireApiKey=false
 * → getClient() returns the fake directly, same seam as git.test.ts).
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createReportTools, filterReportsForAgentScope, stripAudienceFromReports } from "./reports.js";
import type { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

const AGENT_CTX: McpAuthContext = {
  userId: "admin",   // McpAuthContext.userId is the OWNER USERNAME, not a numeric id
  userEmail: "a@example.com",
  keyName: "k",
  scope: "agent",
  agentName: "worker",
  mcpApiKey: "trinity_mcp_x",
} as McpAuthContext;

const USER_CTX: McpAuthContext = {
  userId: "admin",   // McpAuthContext.userId is the OWNER USERNAME, not a numeric id
  userEmail: "a@example.com",
  keyName: "k",
  scope: "user",
  mcpApiKey: "trinity_mcp_x",
} as McpAuthContext;

function makeTools(overrides: Partial<TrinityClient> = {}) {
  const fake = {
    getBaseUrl: () => "http://backend",
    setToken: () => {},
    getPermittedAgents: async () => [],
    listReports: async () => [],
    listAgentReports: async () => [],
    getReport: async () => ({}),
    ...overrides,
  } as unknown as TrinityClient;
  return createReportTools(fake, false);
}

const summary = (id: string, agent: string) => ({
  id,
  agent_name: agent,
  report_type: "recon.weekly",
  title: `t-${id}`,
  created_at: "2026-07-28T00:00:00Z",
});

describe("filterReportsForAgentScope", () => {
  it("keeps only reports whose agent is in the allowed set", () => {
    const rows = [summary("1", "worker"), summary("2", "sibling"), summary("3", "friend")];
    const kept = filterReportsForAgentScope(rows, new Set(["worker", "friend"]));
    assert.deepEqual(kept.map((r) => r.id), ["1", "3"]);
  });

  it("returns nothing when the allowed set is empty", () => {
    assert.deepEqual(filterReportsForAgentScope([summary("1", "worker")], new Set()), []);
  });
});

describe("list_reports", () => {
  it("narrows a broad listing to {self} ∪ permitted for an agent key", async () => {
    // The backend answered with the OWNER's accessible agents — wider than the
    // calling agent's permits. Without the MCP-layer narrowing, 'stranger' leaks.
    const tools = makeTools({
      getPermittedAgents: async () => ["friend"],
      listReports: async () =>
        [summary("1", "worker"), summary("2", "friend"), summary("3", "stranger")] as never,
    });
    const out = JSON.parse(
      await tools.listReports.execute({ limit: 50, offset: 0 }, { session: AGENT_CTX }),
    );
    assert.deepEqual(out.reports.map((r: { agent_name: string }) => r.agent_name), [
      "worker",
      "friend",
    ]);
    assert.equal(out.count, 2);
  });

  it("does not narrow for a user-scoped key", async () => {
    // A user key is already scoped by the backend to that user's accessible
    // agents; re-filtering here would hide reports the user legitimately sees.
    const tools = makeTools({
      getPermittedAgents: async () => {
        throw new Error("must not be consulted for a user key");
      },
      listReports: async () => [summary("1", "a"), summary("2", "b")] as never,
    });
    const out = JSON.parse(
      await tools.listReports.execute({ limit: 50, offset: 0 }, { session: USER_CTX }),
    );
    assert.equal(out.count, 2);
  });

  it("denies a scoped listing of a non-permitted sibling", async () => {
    const tools = makeTools({ getPermittedAgents: async () => ["friend"] });
    const out = JSON.parse(
      await tools.listReports.execute(
        { agent_name: "stranger", limit: 50, offset: 0 },
        { session: AGENT_CTX },
      ),
    );
    assert.equal(out.error, "Access denied");
    assert.match(out.reason, /does not have permission/);
  });

  it("allows an agent to list its own reports", async () => {
    const tools = makeTools({
      listAgentReports: async (name: string) => [summary("1", name)] as never,
    });
    const out = JSON.parse(
      await tools.listReports.execute(
        { agent_name: "worker", limit: 50, offset: 0 },
        { session: AGENT_CTX },
      ),
    );
    assert.equal(out.count, 1);
    assert.equal(out.reports[0].agent_name, "worker");
  });

  it("uses the per-agent route when scoped and the fleet route when broad", async () => {
    const called: string[] = [];
    const tools = makeTools({
      listAgentReports: async () => {
        called.push("agent");
        return [] as never;
      },
      listReports: async () => {
        called.push("fleet");
        return [] as never;
      },
    });
    await tools.listReports.execute({ agent_name: "worker", limit: 5, offset: 0 }, { session: AGENT_CTX });
    await tools.listReports.execute({ limit: 5, offset: 0 }, { session: USER_CTX });
    assert.deepEqual(called, ["agent", "fleet"]);
  });
});

describe("get_report", () => {
  it("returns the payload for an accessible report", async () => {
    const tools = makeTools({
      getReport: async () => ({ id: "r1", agent_name: "worker", payload: { rows: [1, 2] } }),
    });
    const out = JSON.parse(
      await tools.getReport.execute({ report_id: "r1" }, { session: AGENT_CTX }),
    );
    assert.deepEqual(out.payload, { rows: [1, 2] });
  });

  it("hides a non-permitted sibling's report behind the SAME not-found shape", async () => {
    // The backend answers 404 (not 403) so an id can't be probed for existence.
    // An agent key is narrower than its owner, so the re-check here must not
    // widen that disclosure into "exists but forbidden".
    const tools = makeTools({
      getPermittedAgents: async () => [],
      getReport: async () => ({ id: "r9", agent_name: "stranger", payload: { secret: true } }),
    });
    const out = JSON.parse(
      await tools.getReport.execute({ report_id: "r9" }, { session: AGENT_CTX }),
    );
    assert.equal(out.error, "Report not found");
    assert.equal(out.payload, undefined);
    assert.equal(out.agent_name, undefined);
  });
});

describe("list_reports — filters that the backend narrows (#1838 review)", () => {
  it("rejects a window the backend would silently coerce", () => {
    // _VALID_HOURS = {0,1,6,24,168,720}; anything else becomes 168 server-side,
    // so `hours: 48` would answer with 7 days and no signal the window was
    // ignored. The schema refuses it instead.
    const tools = makeTools();
    const schema = tools.listReports.parameters;
    assert.equal(schema.safeParse({ hours: 48 }).success, false);
    assert.equal(schema.safeParse({ hours: 24 }).success, true);
    assert.equal(schema.safeParse({ hours: 0 }).success, true);
  });

  it("sends hours/search down the per-agent route too (#1539)", async () => {
    // This assertion is inverted from what it pinned before #1539: the
    // per-agent endpoint took report_type + paging only, so the tool dropped
    // the window and the search term whenever a caller scoped to one agent.
    // Now both routes carry the same filters and the caveat is gone from the
    // tool description.
    let seen: Record<string, unknown> | undefined;
    const tools = makeTools({
      listAgentReports: async (_n: string, p: Record<string, unknown>) => {
        seen = p;
        return [] as never;
      },
    });
    await tools.listReports.execute(
      { agent_name: "worker", hours: 24, search: "x", limit: 10, offset: 0 },
      { session: AGENT_CTX },
    );
    assert.equal(seen?.hours, 24);
    assert.equal(seen?.search, "x");
  });
});

describe("get_report — fail closed (#1838 review)", () => {
  it("refuses a response with no agent_name for an agent key", async () => {
    // Without an owner the access re-check cannot run; handing over the payload
    // would mean an agent key receiving a body nobody gated for it.
    const tools = makeTools({
      getReport: async () => ({ id: "r1", payload: { secret: true } }),
    });
    const out = JSON.parse(
      await tools.getReport.execute({ report_id: "r1" }, { session: AGENT_CTX }),
    );
    assert.equal(out.error, "Report not found");
    assert.equal(out.payload, undefined);
  });

  it("still serves a user-scoped key, which the backend already gated", async () => {
    const tools = makeTools({
      getReport: async () => ({ id: "r1", payload: { ok: true } }),
    });
    const out = JSON.parse(
      await tools.getReport.execute({ report_id: "r1" }, { session: USER_CTX }),
    );
    assert.deepEqual(out.payload, { ok: true });
  });
});
describe("stripAudienceFromReports (ent#365 review)", () => {
  it("removes the client address a report was produced for", () => {
    const [out] = stripAudienceFromReports([
      { id: "r1", agent_name: "atlas", title: "Weekly", addressed_to: "client@example.com" },
    ]);
    assert.equal("addressed_to" in out, false);
    // Everything an agent legitimately reads survives.
    assert.equal((out as { id: string }).id, "r1");
    assert.equal((out as { agent_name: string }).agent_name, "atlas");
  });

  it("leaves an operator-only report untouched", () => {
    const rows = [{ id: "r2", agent_name: "atlas", title: "Ops" }];
    assert.deepEqual(stripAudienceFromReports(rows), rows);
  });

  it("accepts an interface, not just an index-signature type", () => {
    // The first version constrained on `Record<string, unknown>`, which a
    // TypeScript `interface` cannot satisfy — it has no implicit index
    // signature — so `tsc` failed on `ReportSummary[]` and the MCP server did
    // not build. This is the regression, expressed in the type system.
    interface Row {
      id: string;
      agent_name: string;
      addressed_to?: string | null;
    }
    const rows: Row[] = [{ id: "r3", agent_name: "atlas", addressed_to: "c@x.test" }];
    const [out] = stripAudienceFromReports(rows);
    assert.equal("addressed_to" in out, false);
  });

  it("does not mutate the caller's rows", () => {
    const row = { id: "r4", agent_name: "atlas", addressed_to: "c@x.test" };
    stripAudienceFromReports([row]);
    assert.equal(row.addressed_to, "c@x.test");
  });
});
