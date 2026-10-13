/**
 * trinity-enterprise#160 + abilityai/trinity-enterprise#761 — A2A control MCP tools.
 *
 * Pins the tool-layer contract for the 7 A2A management tools:
 *   - each tool proxies the matching TrinityClient method with the right args;
 *   - the four INBOUND/card tools keep their entitlement-aware mapper: an
 *     unentitled 403 ("not licensed") and an OSS-only 404 become a structured
 *     { success:false, not_entitled/not_found } — never a silent success;
 *   - the three OUTBOUND control tools (register/list/remove) target the
 *     platform-wide outbound endpoint store under /api/settings, which carries
 *     no entitlement on any build, so `not_entitled` must be UNREACHABLE for
 *     them even when a 403 body mentions "a2a" (ent#761);
 *   - human-only 403 (agent-scoped key on a mutation) → { human_only:true };
 *   - outbound credentials are NEVER echoed in tool output;
 *   - set_a2a_inbound_allowlist with neither add nor remove is rejected client-side.
 *
 * Drives the real tool execute() with a fake TrinityClient (requireApiKey=false
 * → getClient() returns the fake directly, same seam as git.test.ts). Error
 * fixtures throw a real `ApiError`, because the outbound mapper branches on
 * `.status` — a bare `new Error("…403…")` would leave every flag dead and the
 * test would pass against a mapper that does nothing.
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";
import * as fs from "node:fs";

import { createA2ATools } from "./a2a.js";
import { ApiError, type TrinityClient } from "../client.js";
import { TOOL_ACCESS_POLICY } from "../access.js";

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
    registerA2AEndpoint: async (body: { name: string; url: string }) => {
      calls.push({ method: "registerA2AEndpoint", args: [body] });
      // The backend NEVER returns the credential — only has_credentials.
      return {
        endpoint: { id: "ep1", name: body.name, url: body.url, has_credentials: true },
        enabled: true,
      };
    },
    listA2AEndpoints: async () => {
      calls.push({ method: "listA2AEndpoints", args: [] });
      return {
        endpoints: [{ id: "ep1", name: "partner", url: "https://x/a2a", has_credentials: true }],
        enabled: true,
      };
    },
    removeA2AEndpoint: async (ref: string) => {
      calls.push({ method: "removeA2AEndpoint", args: [ref] });
      return { success: true, removed: ref };
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
});

describe("ent#761 — the three outbound control tools target the OSS endpoint store", () => {
  it("register proxies the settings route with the body only (no agent name)", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const out = JSON.parse(
      await tools.register_a2a_endpoint.execute({ name: "partner", url: "https://x/a2a" }, {}),
    );
    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, "registerA2AEndpoint");
    // One argument: the request body. The store is platform-wide, so there is
    // no agent to scope the write to.
    assert.equal(calls[0].args.length, 1);
    assert.deepEqual(calls[0].args[0], {
      name: "partner",
      url: "https://x/a2a",
      credentials: undefined,
      clear_credentials: undefined,
      // #3185: absent, not defaulted — the store infers the kind from the value,
      // and an `api_key` sent here would override that inference with a guess.
      credential_kind: undefined,
    });
    assert.equal(out.success, true);
    assert.equal(out.endpoint.name, "partner");
  });

  it("an ignored agent_name does not change the write (platform-wide registry)", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    await tools.register_a2a_endpoint.execute({ agent_name: "whoever", name: "partner", url: "https://x/a2a" }, {});
    assert.deepEqual(calls[0].args[0], {
      name: "partner",
      url: "https://x/a2a",
      credentials: undefined,
      clear_credentials: undefined,
      credential_kind: undefined,
    });
  });

  it("register forwards clear_credentials", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    await tools.register_a2a_endpoint.execute(
      { name: "partner", url: "https://x/a2a", clear_credentials: true },
      {},
    );
    assert.equal((calls[0].args[0] as { clear_credentials?: boolean }).clear_credentials, true);
  });

  it("register rejects clear_credentials together with credentials (no backend call)", async () => {
    // The store would accept the request and silently DROP the supplied secret,
    // leaving the operator believing a credential is stored.
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const out = JSON.parse(
      await tools.register_a2a_endpoint.execute(
        { name: "partner", url: "https://x/a2a", credentials: "TOK", clear_credentials: true },
        {},
      ),
    );
    assert.equal(out.success, false);
    assert.equal(out.invalid, true);
    assert.equal(calls.length, 0);
  });

  it("register surfaces outbound_enabled and hints when the kill switch is off", async () => {
    const tools = makeTools([], {
      registerA2AEndpoint: async () => ({ endpoint: { id: "ep1", name: "partner" }, enabled: false }),
    });
    const out = JSON.parse(await tools.register_a2a_endpoint.execute({ name: "partner", url: "https://x/a2a" }, {}));
    assert.equal(out.success, true);
    assert.equal(out.outbound_enabled, false);
    assert.match(out.hint, /a2a_outbound_enabled/);
  });

  it("register adds no hint when outbound calling is on", async () => {
    const tools = makeTools([]);
    const out = JSON.parse(await tools.register_a2a_endpoint.execute({ name: "partner", url: "https://x/a2a" }, {}));
    assert.equal(out.outbound_enabled, true);
    assert.equal("hint" in out, false);
  });

  it("list returns endpoints + outbound_enabled and takes no agent argument", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const out = JSON.parse(await tools.list_a2a_endpoints.execute({}, {}));
    assert.deepEqual(calls[0], { method: "listA2AEndpoints", args: [] });
    assert.equal(out.success, true);
    assert.equal(out.endpoints[0].name, "partner");
    assert.equal(out.outbound_enabled, true);
  });

  it("list reports outbound_enabled false without failing (kill switch off)", async () => {
    const tools = makeTools([], {
      listA2AEndpoints: async () => ({ endpoints: [], enabled: false }),
    });
    const out = JSON.parse(await tools.list_a2a_endpoints.execute({}, {}));
    assert.equal(out.success, true);
    assert.equal(out.outbound_enabled, false);
    assert.deepEqual(out.endpoints, []);
  });

  it("remove proxies by ref and flattens the result to { removed }", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const out = JSON.parse(await tools.remove_a2a_endpoint.execute({ endpoint_id: "ep1" }, {}));
    assert.deepEqual(calls[0], { method: "removeA2AEndpoint", args: ["ep1"] });
    assert.equal(out.success, true);
    assert.equal(out.removed, "ep1");
    // Not the nested { success, result: { success, removed } } of the old shape.
    assert.equal("result" in out, false);
  });

  it("remove accepts a bare ref but rejects an empty one at the schema", () => {
    const tools = makeTools([]);
    assert.equal(tools.remove_a2a_endpoint.parameters.safeParse({ endpoint_id: "ep1" }).success, true);
    // "" is not a reference to anything; the store would 404 on it.
    assert.equal(tools.remove_a2a_endpoint.parameters.safeParse({ endpoint_id: "" }).success, false);
  });

  it("the client methods never target the entitlement-gated enterprise router", () => {
    // Source pin (the `tool-visibility.test.ts` shape): the three methods are
    // the seam this issue moves, and a silent revert to /api/enterprise/a2a/
    // would still pass every fake-driven assertion above.
    const src = fs.readFileSync(new URL("../client.ts", import.meta.url), "utf8");
    for (const method of ["registerA2AEndpoint", "listA2AEndpoints", "removeA2AEndpoint"]) {
      const start = src.indexOf(`async ${method}(`);
      assert.ok(start > 0, `${method} not found in client.ts`);
      const body = src.slice(start, start + 600);
      const end = body.indexOf("\n  }");
      const impl = end > 0 ? body.slice(0, end) : body;
      assert.ok(
        impl.includes("/api/settings/a2a-endpoints"),
        `${method} must target /api/settings/a2a-endpoints`,
      );
      assert.equal(
        impl.includes("/api/enterprise/a2a"),
        false,
        `${method} must not target the entitlement-gated enterprise router`,
      );
    }
  });
});

describe("ent#160 A2A tools — credentials never echoed", () => {
  it("register_a2a_endpoint returns has_credentials, never the secret", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const raw = await tools.register_a2a_endpoint.execute(
      { name: "partner", url: "https://x/a2a", credentials: "SUPER-SECRET-XYZ" },
      {},
    );
    // The credential is forwarded to the client (which encrypts it) ...
    assert.equal((calls[0].args[0] as { credentials?: string }).credentials, "SUPER-SECRET-XYZ");
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
        throw new ApiError(403, "Enterprise feature 'a2a' is not licensed for this instance.");
      },
    });
    const out = JSON.parse(await tools.get_agent_a2a_config.execute({ agent_name: "bot" }, {}));
    assert.equal(out.success, false);
    assert.equal(out.not_entitled, true);
  });

  it("OSS-only 404 → not_found", async () => {
    const tools = makeTools([], {
      getA2AConfig: async () => {
        throw new ApiError(404, "Not Found");
      },
    });
    const out = JSON.parse(await tools.get_agent_a2a_config.execute({ agent_name: "bot" }, {}));
    assert.equal(out.success, false);
    assert.equal(out.not_found, true);
  });

  it("human-only 403 (agent-scoped key on a mutation) → human_only", async () => {
    const tools = makeTools([], {
      setA2AExposure: async () => {
        throw new ApiError(403, "This operation is human-only; agent-scoped keys cannot perform it");
      },
    });
    const out = JSON.parse(await tools.set_agent_a2a_exposure.execute({ agent_name: "bot", enabled: true }, {}));
    assert.equal(out.success, false);
    assert.equal(out.human_only, true);
  });
});

describe("ent#761 — an outbound failure is never reported as not_entitled", () => {
  // The entitlement-aware mapper infers `not_entitled` from the body text: any
  // 403 whose body matches /\ba2a\b/ trips it. The outbound routes carry no
  // entitlement on ANY build, so an operator told to buy a licence for an auth
  // error has been actively misinformed. Each fixture below uses a 403/404 body
  // that mentions "a2a" ON PURPOSE — swapping the outbound mapper back to the
  // entitlement-aware one turns these red.

  it("a 403 whose body names the a2a setting maps to not_authorized, not not_entitled", async () => {
    for (const tool of ["register_a2a_endpoint", "list_a2a_endpoints", "remove_a2a_endpoint"] as const) {
      const thrower = async () => {
        throw new ApiError(403, "Admin access required to change the a2a outbound endpoints");
      };
      const tools = makeTools([], {
        registerA2AEndpoint: thrower,
        listA2AEndpoints: thrower,
        removeA2AEndpoint: thrower,
      });
      const out = JSON.parse(
        await tools[tool].execute({ name: "partner", url: "https://x/a2a", endpoint_id: "ep1" } as never, {}),
      );
      assert.equal(out.success, false, tool);
      assert.equal(out.not_entitled, undefined, `${tool} must not claim an entitlement gate`);
      assert.equal(out.not_authorized, true, tool);
    }
  });

  it("a human-only 403 mentioning a2a maps to human_only, not not_entitled", async () => {
    const tools = makeTools([], {
      registerA2AEndpoint: async () => {
        throw new ApiError(403, "This operation is human-only; agent-scoped keys cannot perform it (a2a)");
      },
    });
    const out = JSON.parse(await tools.register_a2a_endpoint.execute({ name: "p", url: "https://x/a2a" }, {}));
    assert.equal(out.human_only, true);
    assert.equal(out.not_entitled, undefined);
    assert.equal(out.not_authorized, undefined);
  });

  it("an unknown ref → not_found + endpoint_not_found, never not_entitled", async () => {
    const tools = makeTools([], {
      removeA2AEndpoint: async () => {
        throw new ApiError(404, "A2A endpoint 'ghost' not found");
      },
    });
    const out = JSON.parse(await tools.remove_a2a_endpoint.execute({ endpoint_id: "ghost" }, {}));
    assert.equal(out.success, false);
    assert.equal(out.not_found, true);
    assert.equal(out.endpoint_not_found, true);
    assert.equal(out.not_entitled, undefined);
  });

  it("a 422 from the store's validation → invalid, never not_entitled", async () => {
    const tools = makeTools([], {
      registerA2AEndpoint: async () => {
        throw new ApiError(422, "Endpoint URL must be https and must not resolve to a private address");
      },
    });
    const out = JSON.parse(await tools.register_a2a_endpoint.execute({ name: "p", url: "http://10.0.0.1" }, {}));
    assert.equal(out.success, false);
    assert.equal(out.invalid, true);
    assert.equal(out.not_entitled, undefined);
  });
});

describe("ent#761 AC3 — tool descriptions carry no entitlement claim on the outbound three", () => {
  it("the three outbound descriptions never promise a licence", () => {
    const tools = makeTools([]);
    for (const name of ["register_a2a_endpoint", "list_a2a_endpoints", "remove_a2a_endpoint"] as const) {
      const d = tools[name].description;
      assert.equal(/enterprise feature/i.test(d), false, `${name} still claims an enterprise feature`);
      assert.equal(/licen[cs]ed/i.test(d), false, `${name} still mentions licensing`);
      assert.equal(/not_entitled/.test(d), false, `${name} still advertises not_entitled`);
      assert.match(d, /human-only/i, `${name} must state the human-only tier`);
    }
  });

  it("the inbound/exposure tools still state their entitlement", () => {
    const tools = makeTools([]);
    for (const name of ["get_agent_a2a_config", "set_agent_a2a_exposure", "set_a2a_inbound_allowlist"] as const) {
      assert.match(tools[name].description, /not_entitled/, `${name} must keep its entitlement claim`);
    }
  });

  it("agent_name is optional on the three outbound tools and described as ignored", () => {
    const tools = makeTools([]);
    for (const name of ["register_a2a_endpoint", "list_a2a_endpoints", "remove_a2a_endpoint"] as const) {
      const shape = (tools[name].parameters as unknown as { shape: Record<string, { description?: string }> }).shape;
      const ok = tools[name].parameters.safeParse(
        name === "remove_a2a_endpoint"
          ? { endpoint_id: "ep1" }
          : name === "register_a2a_endpoint"
            ? { name: "p", url: "https://x/a2a" }
            : {},
      );
      assert.equal(ok.success, true, `${name} must accept a call with no agent_name`);
      assert.match(
        shape.agent_name.description ?? "",
        /ignored/i,
        `${name}'s agent_name must say it is ignored on this build`,
      );
    }
  });

  it("get_agent_a2a_config signposts which list the runtime resolves", async () => {
    const tools = makeTools([]);
    const out = JSON.parse(await tools.get_agent_a2a_config.execute({ agent_name: "bot" }, {}));
    assert.match(out.outbound_endpoints_note, /list_a2a_endpoints/);
    assert.match(tools.get_agent_a2a_config.description, /list_a2a_endpoints/);
  });

  it("call_a2a_agent no longer says list_a2a_endpoints reads a different store", () => {
    const src = fs.readFileSync(new URL("./a2a_call.ts", import.meta.url), "utf8");
    assert.equal(
      /list_a2a_endpoints` reads a different/.test(src),
      false,
      "the two tools now address the same registry",
    );
    assert.ok(src.includes("`list_a2a_endpoints` (operator-only) shows the registered names."));
  });

  it("the access ledger describes the outbound three as OSS settings routes", () => {
    // Reads the live policy table, not its source text: the rows are what
    // `server.ts` registers against, and the ledger lying is worse than none.
    for (const name of ["register_a2a_endpoint", "list_a2a_endpoints", "remove_a2a_endpoint"]) {
      const row = TOOL_ACCESS_POLICY[name];
      assert.ok(row, `${name} has no ledger row`);
      const prose = row.kind === "in-tool" ? row.how : row.kind === "baselined" ? row.owner : "";
      assert.equal(
        /enterprise route/.test(prose),
        false,
        `${name}'s ledger row still names an enterprise route`,
      );
      assert.match(prose, /human-only/, `${name}'s ledger row must name the real gate`);
    }
    assert.match(
      (TOOL_ACCESS_POLICY.register_a2a_endpoint as { owner: string }).owner,
      /OSS settings route/,
    );
  });
});

describe("#736 F8 / ent#761 — agent-scoped keys on the A2A reads", () => {
  // `get_agent_a2a_config` keeps the `{self} ∪ permitted` gate: the backend
  // resolves an agent-scoped key to its OWNER and checks owner access, so on a
  // single-owner install any agent could otherwise enumerate a SIBLING's
  // config. `list_a2a_endpoints` no longer needs it — the store it reads is
  // platform-wide and its route refuses every agent principal, so the honest
  // answer is a client-side human-only refusal with no round trip.

  it("get_agent_a2a_config denies a non-permitted sibling", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls, {
      getPermittedAgents: async (agent: string) => {
        calls.push({ method: "getPermittedAgents", args: [agent] });
        return ["allowed"];
      },
    });
    const out = JSON.parse(
      await tools.get_agent_a2a_config.execute(
        { agent_name: "victim" },
        { session: { scope: "agent", agentName: "attacker" } as any },
      ),
    );
    assert.equal(out.success, false);
    assert.equal(out.not_authorized, true);
    assert.equal(
      calls.filter((c) => c.method === "getA2AConfig").length,
      0,
      "a denied read must not reach the backend",
    );
  });

  it("get_agent_a2a_config allows self and an explicitly permitted target", async () => {
    for (const target of ["attacker", "allowed"]) {
      const calls: Recorded[] = [];
      const tools = makeTools(calls, { getPermittedAgents: async () => ["allowed"] });
      const out = JSON.parse(
        await tools.get_agent_a2a_config.execute(
          { agent_name: target },
          { session: { scope: "agent", agentName: "attacker" } as any },
        ),
      );
      assert.equal(out.success, true, `${target} should be reachable`);
    }
  });

  it("list_a2a_endpoints refuses an agent-scoped key client-side, with no round trip", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const out = JSON.parse(
      await tools.list_a2a_endpoints.execute({}, { session: { scope: "agent", agentName: "bot" } as any }),
    );
    assert.equal(out.success, false);
    assert.equal(out.human_only, true);
    assert.equal(out.not_entitled, undefined);
    // Not even its OWN name gets through: the route is human-only, not
    // self-scoped, so a permission lookup would be theatre.
    assert.equal(calls.length, 0);
  });

  it("list_a2a_endpoints does not gate user- or system-scoped keys", async () => {
    for (const scope of ["user", "system"]) {
      const tools = makeTools([]);
      const out = JSON.parse(await tools.list_a2a_endpoints.execute({}, { session: { scope } as any }));
      assert.equal(out.success, true, scope);
    }
  });
});

describe("abilityai/trinity#3185 — credential_kind on register_a2a_endpoint", () => {
  it("passes an explicit kind through to the settings route", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    await tools.register_a2a_endpoint.execute(
      {
        name: "partner",
        url: "https://x/a2a",
        credentials: "TOK",
        credential_kind: "payment_token",
      },
      {},
    );
    assert.equal((calls[0].args[0] as { credential_kind?: string }).credential_kind,
                 "payment_token");
  });

  it("refuses a kind together with clear_credentials, and places no call", async () => {
    // Contradictory instructions about one slot. Refused here as well as at the
    // route so the caller is never told a payment token is registered when the
    // clear emptied the slot.
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const out = JSON.parse(
      await tools.register_a2a_endpoint.execute(
        {
          name: "partner",
          url: "https://x/a2a",
          clear_credentials: true,
          credential_kind: "payment_token",
        },
        {},
      ),
    );
    assert.equal(out.success, false);
    assert.equal(out.invalid, true);
    assert.equal(calls.length, 0);
  });

  it("relays the store's single-use warning instead of swallowing it", async () => {
    // An x402 v3 token authorises ONE settlement. Dropped here, the operator's
    // second call reads as a mystery 402 on an endpoint that just worked.
    const tools = makeTools([], {
      registerA2AEndpoint: async () => ({
        endpoint: { id: "ep1", name: "partner", has_credentials: true,
                    credential_kind: "payment_token", credential_single_use: true },
        enabled: true,
        hint: "This payment token authorises a single settlement: …",
      }),
    });
    const out = JSON.parse(
      await tools.register_a2a_endpoint.execute(
        { name: "partner", url: "https://x/a2a", credentials: "TOK" }, {},
      ),
    );
    assert.equal(out.endpoint.credential_kind, "payment_token");
    assert.equal(out.endpoint.credential_single_use, true);
    assert.match(out.credential_hint, /single settlement/);
    // The kill-switch hint keeps its own key — two different warnings must not
    // overwrite each other.
    assert.equal("hint" in out, false);
  });

  it("the description tells an operator when to pass payment_token", async () => {
    const tools = makeTools([]);
    const text = tools.register_a2a_endpoint.description;
    assert.match(text, /credential_kind/);
    assert.match(text, /payment_token/);
    assert.match(text, /inferred/i);
  });
});
