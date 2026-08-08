/**
 * trinity-enterprise#15 — GitHub-repo import intents via MCP.
 *
 * Pins that the create_agent tool schema exposes ONLY "copy" | "clone" for
 * import_intent ("fork" is web-UI-only: MCP tool args are audit-logged and
 * fork would need a plaintext PAT arg), that the accepted value reaches the
 * backend config body, and that the create POST carries a deterministic
 * Idempotency-Key that varies with the agent name.
 *
 * Runner: built-in node:test → `node --import tsx --test src/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createAgentTools } from "./tools/agents.js";
import type { TrinityClient } from "./client.js";
import type { Agent, AgentConfig } from "./types.js";

function makeCreateTool(created: { config: AgentConfig; idempotencyKey?: string }[]) {
  const fake: Partial<TrinityClient> = {
    createAgent: async (config: AgentConfig, idempotencyKey?: string) => {
      created.push({ config, idempotencyKey });
      return { name: config.name, type: "business-assistant", status: "running" } as Agent;
    },
  };
  const tools = createAgentTools(fake as unknown as TrinityClient, false);
  return tools.createAgent;
}

describe("ent#15 create_agent import_intent schema", () => {
  it("accepts 'copy' and 'clone'", () => {
    const tool = makeCreateTool([]);
    for (const intent of ["copy", "clone"]) {
      const parsed = tool.parameters.safeParse({ name: "a1", import_intent: intent });
      assert.ok(parsed.success, `import_intent '${intent}' must be accepted`);
    }
  });

  it("rejects 'fork' (web-UI-only)", () => {
    const tool = makeCreateTool([]);
    const parsed = tool.parameters.safeParse({ name: "a1", import_intent: "fork" });
    assert.equal(parsed.success, false, "import_intent 'fork' must be rejected");
  });
});

describe("ent#15 create_agent passthrough + idempotency", () => {
  it("forwards import_intent and sends a name-dependent Idempotency-Key", async () => {
    const created: { config: AgentConfig; idempotencyKey?: string }[] = [];
    const tool = makeCreateTool(created);

    await tool.execute(
      { name: "a1", template: "github:org/repo", import_intent: "copy" },
      undefined
    );
    await tool.execute(
      { name: "a2", template: "github:org/repo", import_intent: "copy" },
      undefined
    );

    assert.equal(created.length, 2);
    assert.equal(created[0].config.import_intent, "copy");
    assert.ok(created[0].idempotencyKey, "Idempotency-Key must be derived");
    assert.notEqual(
      created[0].idempotencyKey,
      created[1].idempotencyKey,
      "two different agent names from the same repo must not share a key"
    );
  });
});
