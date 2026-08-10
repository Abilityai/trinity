/**
 * trinity-enterprise#160 — A2A control MCP tools.
 *
 * Pins the tool-layer contract for the 7 A2A management tools:
 *   - each tool proxies the matching TrinityClient method with the right args;
 *   - honest gating: an unentitled 403 ("not licensed") and an OSS-only 404
 *     become a structured { success:false, not_entitled/not_found } — never a
 *     silent success;
 *   - human-only 403 (agent-scoped key on a mutation) → { human_only:true };
 *   - outbound credentials are NEVER echoed in tool output;
 *   - set_a2a_inbound_allowlist with neither add nor remove is rejected client-side.
 *
 * Drives the real tool execute() with a fake TrinityClient (requireApiKey=false
 * → getClient() returns the fake directly, same seam as git.test.ts).
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createA2ATools } from "./a2a.js";
import type { TrinityClient } from "../client.js";

type Recorded = { method: string; args: unknown[] };

function makeTools(calls: Recorded[], overrides: Partial<TrinityClient> = {}) {
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    getA2AConfig: async (name: string) => {
      calls.push({ method: "getA2AConfig", args: [name] });
      return { agent_name: name, a2a_exposed: false, inbound_allowlist: [], outbound_endpoints: [] };
    },
    setA2AExposure: async (name: string, enabled: boolean) => {
      calls.push({ method: "setA2AExposure", args: [name, enabled] });
      return { agent_name: name, a2a_exposed: enabled };
    },
    getA2ACard: async (name: string) => {
      calls.push({ method: "getA2ACard", args: [name] });
      return { name, protocolVersion: "1.0" };
    },
    updateA2AInboundAllowlist: async (name: string, body: unknown) => {
      calls.push({ method: "updateA2AInboundAllowlist", args: [name, body] });
      return { agent_name: name, inbound_allowlist: ["did:a"] };
    },
    registerA2AEndpoint: async (name: string, body: unknown) => {
      calls.push({ method: "registerA2AEndpoint", args: [name, body] });
      // The backend NEVER returns the credential — only has_credentials.
      return { id: "ep1", agent_name: name, name: (body as { name: string }).name, url: (body as { url: string }).url, has_credentials: true };
    },
    listA2AEndpoints: async (name: string) => {
      calls.push({ method: "listA2AEndpoints", args: [name] });
      return [{ id: "ep1", name: "partner", url: "https://x/a2a", has_credentials: true }];
    },
    removeA2AEndpoint: async (name: string, id: string) => {
      calls.push({ method: "removeA2AEndpoint", args: [name, id] });
      return { status: "removed", endpoint_id: id };
    },
    ...overrides,
  };
  return createA2ATools(fake as TrinityClient, false);
}

describe("ent#160 A2A tools — proxy the right client method", () => {
  it("get_agent_a2a_config proxies getA2AConfig", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const out = JSON.parse(await tools.get_agent_a2a_config.execute({ agent_name: "bot" }, {}));
    assert.equal(calls[0].method, "getA2AConfig");
    assert.deepEqual(calls[0].args, ["bot"]);
    assert.equal(out.success, true);
    assert.equal(out.config.agent_name, "bot");
  });

  it("set_agent_a2a_exposure proxies setA2AExposure(name, enabled)", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const out = JSON.parse(await tools.set_agent_a2a_exposure.execute({ agent_name: "bot", enabled: true }, {}));
    assert.deepEqual(calls[0], { method: "setA2AExposure", args: ["bot", true] });
    assert.equal(out.config.a2a_exposed, true);
  });

  it("get_agent_a2a_card proxies the OSS served-card endpoint", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const out = JSON.parse(await tools.get_agent_a2a_card.execute({ agent_name: "bot" }, {}));
    assert.equal(calls[0].method, "getA2ACard");
    assert.equal(out.card.protocolVersion, "1.0");
  });

  it("set_a2a_inbound_allowlist forwards add/remove", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    await tools.set_a2a_inbound_allowlist.execute({ agent_name: "bot", add: ["did:a"], remove: ["did:b"] }, {});
    assert.equal(calls[0].method, "updateA2AInboundAllowlist");
    assert.deepEqual(calls[0].args[1], { add: ["did:a"], remove: ["did:b"] });
  });

  it("list_a2a_endpoints + remove_a2a_endpoint proxy correctly", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    await tools.list_a2a_endpoints.execute({ agent_name: "bot" }, {});
    const rm = JSON.parse(await tools.remove_a2a_endpoint.execute({ agent_name: "bot", endpoint_id: "ep1" }, {}));
    assert.equal(calls[0].method, "listA2AEndpoints");
    assert.deepEqual(calls[1], { method: "removeA2AEndpoint", args: ["bot", "ep1"] });
    assert.equal(rm.result.status, "removed");
  });
});

describe("ent#160 A2A tools — credentials never echoed", () => {
  it("register_a2a_endpoint returns has_credentials, never the secret", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const raw = await tools.register_a2a_endpoint.execute(
      { agent_name: "bot", name: "partner", url: "https://x/a2a", credentials: "SUPER-SECRET-XYZ" },
      {},
    );
    // The credential is forwarded to the client (which encrypts it) ...
    assert.equal((calls[0].args[1] as { credentials?: string }).credentials, "SUPER-SECRET-XYZ");
    // ... but MUST NOT appear anywhere in the tool's returned payload.
    assert.equal(raw.includes("SUPER-SECRET-XYZ"), false);
    const out = JSON.parse(raw);
    assert.equal(out.endpoint.has_credentials, true);
    assert.equal("credentials" in out.endpoint, false);
  });
});

describe("ent#160 A2A tools — honest gating", () => {
  it("empty allow-list update is rejected client-side (no backend call)", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const out = JSON.parse(await tools.set_a2a_inbound_allowlist.execute({ agent_name: "bot" }, {}));
    assert.equal(out.success, false);
    assert.equal(out.invalid, true);
    assert.equal(calls.length, 0);
  });

  it("unentitled 403 → not_entitled (never silent success)", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls, {
      getA2AConfig: async () => {
        throw new Error("Request failed: 403 - Enterprise feature 'a2a' is not licensed for this instance.");
      },
    });
    const out = JSON.parse(await tools.get_agent_a2a_config.execute({ agent_name: "bot" }, {}));
    assert.equal(out.success, false);
    assert.equal(out.not_entitled, true);
  });

  it("OSS-only 404 → not_found", async () => {
    const tools = makeTools([], {
      getA2AConfig: async () => {
        throw new Error("Request failed: 404 - Not Found");
      },
    });
    const out = JSON.parse(await tools.get_agent_a2a_config.execute({ agent_name: "bot" }, {}));
    assert.equal(out.success, false);
    assert.equal(out.not_found, true);
  });

  it("human-only 403 (agent-scoped key on a mutation) → human_only", async () => {
    const tools = makeTools([], {
      setA2AExposure: async () => {
        throw new Error("Request failed: 403 - This operation is human-only; agent-scoped keys cannot perform it");
      },
    });
    const out = JSON.parse(await tools.set_agent_a2a_exposure.execute({ agent_name: "bot", enabled: true }, {}));
    assert.equal(out.success, false);
    assert.equal(out.human_only, true);
  });
});
