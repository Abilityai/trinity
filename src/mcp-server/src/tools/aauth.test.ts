/**
 * trinity-enterprise#878 — `request_person_token`.
 *
 * Pins the tool-layer contract:
 *   - self-acting, AGENT scope only: no `agent_name` parameter, the agent
 *     comes from the key; user-, system- and unscoped keys are refused without
 *     a round-trip (the backend would 403 them anyway);
 *   - the backend's `issued` / `pending` / `refused` shapes pass through with
 *     `next_step`, and a token-shaped field never does;
 *   - errors become honest structured flags (`aauth_disabled` on 404 — taste
 *     T3: advertised but inert when the flag is off), and the tool never throws.
 *
 * Drives the real tool execute() with a fake TrinityClient (requireApiKey=false
 * → getClient() returns the fake directly, the a2a_call.test.ts seam).
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createAAuthTools } from "./aauth.js";
import { ApiError } from "../client.js";
import type { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

type Recorded = { method: string; args: unknown[] };

const ISSUED = {
  success: true, outcome: "issued", jti: "j-1", aud: "https://res.example", sub: "directed",
  issuer: "https://ps.example", exp: 1800003600, cached: false, next_step: "present it",
};
const PENDING = {
  success: true, outcome: "pending", aud: "https://res.example", issuer: "https://ps.example",
  interaction: {
    url: "https://ps.example/auth", code: "ABCD-1234", link: "https://ps.example/auth?code=ABCD-1234",
    pending_handle: "h".repeat(32), retry_after: 5, status: "pending", expires_at: 1800001800,
  },
  next_step: "relay privately to your owner",
};

function makeTools(calls: Recorded[], reply: unknown = ISSUED) {
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    requestPersonToken: async (name: string, body: unknown) => {
      calls.push({ method: "requestPersonToken", args: [name, body] });
      if (reply instanceof Error) throw reply;
      return reply;
    },
  };
  return createAAuthTools(fake as TrinityClient, false);
}

function ctx(over: Partial<McpAuthContext>): { session: McpAuthContext } {
  return {
    session: {
      userId: "admin", userEmail: "a@example.com", keyName: "k", mcpApiKey: "trinity_mcp_x", ...over,
    } as McpAuthContext,
  };
}

const AGENT = ctx({ scope: "agent", agentName: "bot" });

describe("ent#878 request_person_token — shape", () => {
  it("declares resource and pending_handle, and NO agent_name", () => {
    const tools = makeTools([]);
    const shape = tools.request_person_token.parameters.shape as Record<string, unknown>;
    assert.ok(shape.resource && shape.pending_handle && shape.wait);
    assert.equal(shape.agent_name, undefined);
    assert.equal(tools.request_person_token.parameters.safeParse({ resource: "https://res.example", pending_handle: "short" }).success, false);
    assert.equal(tools.request_person_token.parameters.safeParse({ resource: "https://res.example", wait: 16 }).success, false);
  });

  it("tells the agent to relay the link privately to its OWNER, and what aauth_disabled means", () => {
    const d = makeTools([]).request_person_token.description;
    assert.match(d, /PRIVATELY to your owner/i);
    assert.match(d, /whoever approves first/i);
    assert.match(d, /ask_operator/);
    assert.match(d, /aauth_disabled/);
    assert.ok(d.length <= 2048, `description is ${d.length} chars`);
  });
});

describe("ent#878 request_person_token — self-acting gate", () => {
  it("acts as the key's agent and forwards every parameter with trigger_source=mcp", async () => {
    const calls: Recorded[] = [];
    const out = JSON.parse(await makeTools(calls).request_person_token.execute(
      { resource: "https://res.example", pending_handle: "h".repeat(32), wait: 3, execution_id: "exec-1" },
      AGENT,
    ));
    assert.deepEqual(calls[0].args, ["bot", {
      resource: "https://res.example", pending_handle: "h".repeat(32), wait: 3,
      execution_id: "exec-1", trigger_source: "mcp",
    }]);
    assert.equal(out.outcome, "issued");
    assert.equal(out.agent, "bot");
    assert.equal(out.jti, "j-1");
  });

  it("prefers the session's execution id over the parameter", async () => {
    const calls: Recorded[] = [];
    await makeTools(calls).request_person_token.execute(
      { resource: "https://res.example", execution_id: "manual" },
      ctx({ scope: "agent", agentName: "bot", executionId: "exec-9" }),
    );
    assert.equal((calls[0].args[1] as { execution_id?: string }).execution_id, "exec-9");
  });

  for (const [label, session] of [
    ["a user-scoped key", ctx({ scope: "user" })],
    ["a system-scoped key", ctx({ scope: "system", agentName: "trinity-system" })],
    ["an agent key naming no agent", ctx({ scope: "agent" })],
    ["no session", undefined],
  ] as const) {
    it(`refuses ${label} without a round-trip`, async () => {
      const calls: Recorded[] = [];
      const out = JSON.parse(await makeTools(calls).request_person_token.execute(
        { resource: "https://res.example" }, session as { session?: McpAuthContext } | undefined,
      ));
      assert.equal(out.success, false);
      assert.equal(out.not_authorized, true);
      assert.match(out.reason, /agent-scoped key/);
      assert.deepEqual(calls, []);
    });
  }
});

describe("ent#878 request_person_token — outcomes and flags", () => {
  it("passes a pending outcome through with link, code, handle and next_step", async () => {
    const out = JSON.parse(await makeTools([], PENDING).request_person_token.execute(
      { resource: "https://res.example" }, AGENT,
    ));
    assert.equal(out.outcome, "pending");
    assert.equal(out.interaction.link, "https://ps.example/auth?code=ABCD-1234");
    assert.equal(out.interaction.pending_handle, "h".repeat(32));
    assert.match(out.next_step, /owner/);
  });

  it("never passes a token-shaped field through, even if the backend grew one", async () => {
    const out = JSON.parse(await makeTools([], { ...ISSUED, person_token: "eyJ.x.y", token: "eyJ.a.b" })
      .request_person_token.execute({ resource: "https://res.example" }, AGENT));
    assert.equal(out.person_token, undefined);
    assert.equal(out.token, undefined);
    assert.ok(!JSON.stringify(out).includes("eyJ"));
  });

  it("passes a refused outcome through as the backend's 200 body", async () => {
    const refused = { success: false, outcome: "refused", reason: "denied", message: "declined", next_step: "stop" };
    const out = JSON.parse(await makeTools([], refused).request_person_token.execute(
      { resource: "https://res.example" }, AGENT,
    ));
    assert.equal(out.success, false);
    assert.equal(out.reason, "denied");
  });

  const cases: Array<[number, string, Record<string, unknown>]> = [
    [404, '{"detail":"Not found"}', { not_found: true, aauth_disabled: true }],
    [403, '{"detail":{"reason":"aauth_agent_only","message":"own key only"}}', { not_authorized: true, agent_only: true, reason: "aauth_agent_only" }],
    [409, '{"detail":{"reason":"person_server_unconfigured","message":"set it"}}', { person_server_unconfigured: true, reason: "person_server_unconfigured" }],
    [422, '{"detail":{"reason":"invalid_resource","message":"host only"}}', { invalid: true }],
    [429, "slow", { rate_limited: true }],
    [502, '{"detail":{"reason":"person_server_unreachable","message":"down"}}', { person_server_unreachable: true }],
    [503, '{"detail":{"reason":"state_unavailable","message":"redis"}}', { state_unavailable: true }],
  ];
  for (const [status, body, expect] of cases) {
    it(`maps HTTP ${status} to honest flags and never throws`, async () => {
      const out = JSON.parse(await makeTools([], new ApiError(status, body, new Headers()))
        .request_person_token.execute({ resource: "https://res.example" }, AGENT));
      assert.equal(out.success, false);
      for (const [k, v] of Object.entries(expect)) assert.equal(out[k], v, k);
    });
  }

  it("reports a gateway abort as possibly_pending", async () => {
    const out = JSON.parse(await makeTools([], new Error("This operation was aborted"))
      .request_person_token.execute({ resource: "https://res.example" }, AGENT));
    assert.equal(out.timeout, true);
    assert.equal(out.possibly_pending, true);
  });
});
