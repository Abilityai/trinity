/**
 * trinity-enterprise#878 — `request_person_token`; trinity-enterprise#879 —
 * `call_aauth_resource` (the second half of this file).
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
    callAAuthResource: async (name: string, body: unknown) => {
      calls.push({ method: "callAAuthResource", args: [name, body] });
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

// =========================================================================== //
// ent#879 — call_aauth_resource
// =========================================================================== //
const RESULT = {
  success: true, outcome: "result", resource: "https://res.example", method: "POST", path: "/v1/search_entities",
  status: 200, body: "{\"entities\":[]}", content_type: "application/json", truncated: false,
  budget: { cost: 2, balance: 38, unit: "credits" }, token_kind: "auth", challenges: ["auth-token"],
  replayed: false, next_step: "The resource answered",
};
const INTERACTION = {
  success: true, outcome: "interaction_required", resource: "https://res.example", challenges: ["auth-token"],
  replayed: false,
  interaction: {
    pending_handle: "h".repeat(32), retry_after: 5, requirement: "interaction", at: "person_server",
    url: "https://ps.example/auth", code: "WXYZ-5678", link: "https://ps.example/auth?code=WXYZ-5678", expires_at: 1,
  },
  next_step: "Relay `interaction.link` PRIVATELY to your OWNER",
};
const CALL = { resource: "https://res.example", method: "POST" as const, path: "/v1/search_entities",
               body: { FEATURES: [{ NAME_FULL: "x" }] }, justification: "demo", dedup_label: "check-1" };

describe("ent#879 call_aauth_resource — shape", () => {
  it("declares the call fields and pending_handle, and NO agent_name; call XOR resume", () => {
    const tools = makeTools([]);
    const schema = tools.call_aauth_resource.parameters;
    // zod 3 wraps a refined object in ZodEffects (`_def.schema`); zod 4 keeps `.shape` on the object.
    const anySchema = schema as unknown as { shape?: Record<string, unknown>; _def?: { schema?: { shape: Record<string, unknown> } } };
    const shape = anySchema.shape ?? anySchema._def?.schema?.shape ?? {};
    for (const k of ["resource", "method", "path", "body", "justification", "dedup_label", "pending_handle", "wait"]) {
      assert.ok(shape[k], k);
    }
    assert.equal(shape.agent_name, undefined);
    assert.equal(schema.safeParse(CALL).success, true);
    assert.equal(schema.safeParse({ pending_handle: "h".repeat(32) }).success, true);
    assert.equal(schema.safeParse({ pending_handle: "h".repeat(32), wait: 3 }).success, true);
    // XOR: a handle with call fields, a call without its required fields, bad paths / methods / handles
    for (const bad of [
      { ...CALL, pending_handle: "h".repeat(32) },
      { pending_handle: "h".repeat(32), resource: "https://res.example" },
      { resource: "https://res.example", path: "/v1/x" },                       // no dedup_label
      { ...CALL, path: "v1/x" }, { ...CALL, path: "/v1/x?y=1" }, { ...CALL, path: "/v1/%41" },
      { ...CALL, method: "PUT" }, { ...CALL, wait: 3 }, { pending_handle: "short" },
      { ...CALL, body: ["not", "an", "object"] }, { ...CALL, justification: "j".repeat(501) },
    ]) {
      assert.equal(schema.safeParse(bad).success, false, JSON.stringify(bad).slice(0, 80));
    }
  });

  it("names the actor and the retry rules, under the description cap", () => {
    const d = makeTools([]).call_aauth_resource.description;
    assert.match(d, /PRIVATELY to your owner/i);
    assert.match(d, /whoever approves first/i);
    assert.match(d, /ask_operator/);
    assert.match(d, /do_not_retry/);
    assert.match(d, /possibly_executed/);
    assert.match(d, /dedup_label/);
    assert.match(d, /aauth_disabled/);
    assert.ok(d.length <= 2048, `description is ${d.length} chars`);
  });
});

describe("ent#879 call_aauth_resource — self-acting gate", () => {
  it("acts as the key's agent, defaults method to POST and body to null, forwards trigger_source=mcp", async () => {
    const calls: Recorded[] = [];
    const out = JSON.parse(await makeTools(calls, RESULT).call_aauth_resource.execute(
      { resource: "https://res.example", path: "/v1/whoami", dedup_label: "who", execution_id: "exec-1" },
      ctx({ scope: "agent", agentName: "bot", executionId: "exec-9" }),
    ));
    assert.deepEqual(calls[0].args, ["bot", {
      resource: "https://res.example", method: "POST", path: "/v1/whoami", body: null, justification: undefined,
      dedup_label: "who", execution_id: "exec-9", trigger_source: "mcp",
    }]);
    assert.equal(out.outcome, "result");
    assert.equal(out.agent, "bot");
    assert.equal(out.token_kind, "auth");
  });

  it("sends a resume as the handle ALONE (the stored request runs, never caller arguments)", async () => {
    const calls: Recorded[] = [];
    await makeTools(calls, RESULT).call_aauth_resource.execute(
      { pending_handle: "h".repeat(32), wait: 2 }, AGENT,
    );
    assert.deepEqual(calls[0].args, ["bot", {
      pending_handle: "h".repeat(32), wait: 2, execution_id: undefined, trigger_source: "mcp",
    }]);
  });

  for (const [label, session] of [
    ["a user-scoped key", ctx({ scope: "user" })],
    ["a system-scoped key", ctx({ scope: "system", agentName: "trinity-system" })],
    ["an agent key naming no agent", ctx({ scope: "agent" })],
    ["no session", undefined],
  ] as const) {
    it(`refuses ${label} without a round-trip`, async () => {
      const calls: Recorded[] = [];
      const out = JSON.parse(await makeTools(calls).call_aauth_resource.execute(
        CALL, session as { session?: McpAuthContext } | undefined,
      ));
      assert.equal(out.success, false);
      assert.equal(out.not_authorized, true);
      assert.match(out.reason, /call_aauth_resource acts as the calling agent/);
      assert.deepEqual(calls, []);
    });
  }
});

describe("ent#879 call_aauth_resource — outcomes and refusals", () => {
  it("passes a result through with status, body, budget, token_kind and challenges", async () => {
    const out = JSON.parse(await makeTools([], RESULT).call_aauth_resource.execute(CALL, AGENT));
    assert.equal(out.success, true);
    assert.equal(out.status, 200);
    assert.equal(out.body, "{\"entities\":[]}");
    assert.deepEqual(out.budget, { cost: 2, balance: 38, unit: "credits" });
    assert.deepEqual(out.challenges, ["auth-token"]);
  });

  it("passes interaction_required through with link, code, handle and the owner-naming next_step", async () => {
    const out = JSON.parse(await makeTools([], INTERACTION).call_aauth_resource.execute(CALL, AGENT));
    assert.equal(out.outcome, "interaction_required");
    assert.equal(out.interaction.link, "https://ps.example/auth?code=WXYZ-5678");
    assert.equal(out.interaction.pending_handle, "h".repeat(32));
    assert.match(out.next_step, /OWNER/);
  });

  it("passes denied / expired through as the backend's 200 body with do_not_retry", async () => {
    const denied = { success: false, outcome: "denied", reason: "denied", detail: "declined", do_not_retry: true,
                     challenges: ["auth-token"], replayed: false, next_step: "stop" };
    const out = JSON.parse(await makeTools([], denied).call_aauth_resource.execute(CALL, AGENT));
    assert.equal(out.outcome, "denied");
    assert.equal(out.do_not_retry, true);
  });

  it("never passes a token-shaped field through, even if the backend grew one", async () => {
    const out = JSON.parse(await makeTools([], { ...RESULT, token: "eyJ.a.b", auth_token: "eyJ.c.d",
                                                 person_token: "eyJ.e.f", resource_token: "eyJ.g.h" })
      .call_aauth_resource.execute(CALL, AGENT));
    for (const k of ["token", "auth_token", "person_token", "resource_token"]) assert.equal(out[k], undefined, k);
    assert.ok(!JSON.stringify(out).includes("eyJ"));
  });

  const cases: Array<[number, string, Record<string, unknown>, boolean]> = [
    [404, '{"detail":"Not found"}', { aauth_disabled: true, not_found: true }, true],
    [403, '{"detail":{"reason":"aauth_agent_only","message":"own key only"}}', { agent_only: true, not_authorized: true }, true],
    [403, '{"detail":{"reason":"resource_not_allowed","message":"not listed","resource":"https://res.example"}}',
      { resource_not_allowed: true, not_authorized: true }, true],
    [402, '{"detail":{"reason":"payment_required","message":"pay"}}', { payment_required: true }, true],
    [409, '{"detail":{"reason":"person_server_unconfigured","message":"set it"}}', { person_server_unconfigured: true }, true],
    [409, '{"detail":{"reason":"effect_in_progress","message":"running"}}', { in_progress: true }, false],
    [422, '{"detail":{"reason":"invalid_path","message":"bad"}}', { invalid: true }, true],
    [429, '{"detail":{"reason":"rate_limited","message":"slow","retry_after":7}}', { rate_limited: true }, false],
    [502, '{"detail":{"reason":"unsupported_requirement:x","message":"?","challenges":["x"]}}', { peer_unusable: true }, true],
    [503, '{"detail":{"reason":"state_unavailable","message":"redis"}}', { state_unavailable: true }, false],
    [504, '{"detail":{"reason":"timeout","message":"slow"}}', { timeout: true }, false],
    [504, '{"detail":{"reason":"timeout","message":"slow","possibly_executed":true}}', { timeout: true }, true],
  ];
  for (const [status, body, expect, doNotRetry] of cases) {
    it(`maps HTTP ${status} ${JSON.parse(body).detail?.reason ?? ""} to a refusal outcome and never throws`, async () => {
      const out = JSON.parse(await makeTools([], new ApiError(status, body, new Headers()))
        .call_aauth_resource.execute(CALL, AGENT));
      assert.equal(out.success, false);
      assert.equal(out.outcome, "refusal");
      assert.equal(out.refusal.http_status, status);
      assert.equal(out.do_not_retry, doNotRetry, "do_not_retry");
      assert.equal(typeof out.next_step, "string");
      for (const [k, v] of Object.entries(expect)) assert.equal(out[k], v, k);
    });
  }

  it("carries retry_after, challenges, resource and possibly_executed into the refusal block", async () => {
    const out = JSON.parse(await makeTools([], new ApiError(504,
      '{"detail":{"reason":"timeout","message":"slow","possibly_executed":true,"resource":"https://res.example","challenges":["auth-token"]}}',
      new Headers())).call_aauth_resource.execute(CALL, AGENT));
    assert.equal(out.refusal.possibly_executed, true);
    assert.equal(out.refusal.resource, "https://res.example");
    assert.deepEqual(out.refusal.challenges, ["auth-token"]);
    assert.match(out.next_step, /may have executed/i);
    const slow = JSON.parse(await makeTools([], new ApiError(429,
      '{"detail":{"reason":"rate_limited","message":"slow","retry_after":7}}', new Headers()))
      .call_aauth_resource.execute(CALL, AGENT));
    assert.equal(slow.refusal.retry_after, 7);
  });

  it("reports a gateway abort as a retryable refusal with possibly_pending", async () => {
    const out = JSON.parse(await makeTools([], new Error("This operation was aborted"))
      .call_aauth_resource.execute(CALL, AGENT));
    assert.equal(out.outcome, "refusal");
    assert.equal(out.timeout, true);
    assert.equal(out.possibly_pending, true);
    assert.equal(out.do_not_retry, false);
  });
});
