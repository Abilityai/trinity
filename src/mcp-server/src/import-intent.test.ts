/**
 * trinity-enterprise#15 — GitHub-repo import intents via MCP.
 *
 * Pins that the create_agent tool schema exposes "copy" | "clone" | "fork" for
 * import_intent, that the accepted value reaches the backend config body, and
 * that the create POST carries a deterministic Idempotency-Key that varies with
 * the agent name.
 *
 * trinity-enterprise#1007 (item 6): fork-to-own is reachable over MCP through a
 * `fork_to_own` block of `destination_repo` + `private` — and NO `github_pat`.
 * MCP tool args are written to the audit trail, so a PAT argument would land
 * there in plaintext; the backend spends the caller's saved personal token and
 * refuses an agent key by name (FORK_PAT_REQUIRED). The body sent here is the
 * same literal `tests/unit/test_ent1007_mcp_fork_body.py` validates through the
 * backend's AgentConfig, so a key the backend would drop goes red there.
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
      return { name: config.name, status: "running" } as Agent;
    },
  };
  const tools = createAgentTools(fake as unknown as TrinityClient, false);
  return tools.createAgent;
}

describe("ent#15 create_agent import_intent schema", () => {
  it("accepts 'copy', 'clone' and 'fork'", () => {
    const tool = makeCreateTool([]);
    for (const intent of ["copy", "clone", "fork"]) {
      const parsed = tool.parameters.safeParse({ name: "a1", import_intent: intent });
      assert.ok(parsed.success, `import_intent '${intent}' must be accepted`);
    }
  });

  it("still rejects an unknown intent", () => {
    const tool = makeCreateTool([]);
    const parsed = tool.parameters.safeParse({ name: "a1", import_intent: "mirror" });
    assert.equal(parsed.success, false, "import_intent 'mirror' must be rejected");
  });
});

describe("ent#1007 create_agent fork_to_own block", () => {
  it("accepts destination_repo with and without private", () => {
    const tool = makeCreateTool([]);
    for (const block of [
      { destination_repo: "acme/sales-agent" },
      { destination_repo: "acme/sales-agent", private: false },
    ]) {
      const parsed = tool.parameters.safeParse({ name: "a1", fork_to_own: block });
      assert.ok(parsed.success, `fork_to_own ${JSON.stringify(block)} must be accepted`);
    }
  });

  it("refuses a destination that is not owner/name", () => {
    const tool = makeCreateTool([]);
    for (const bad of ["acme", "acme/a/b", "-acme/x", "../x", ""]) {
      const parsed = tool.parameters.safeParse({
        name: "a1",
        fork_to_own: { destination_repo: bad },
      });
      assert.equal(parsed.success, false, `destination_repo '${bad}' must be rejected`);
    }
  });

  it("has no github_pat parameter — MCP args are audit-logged", () => {
    const tool = makeCreateTool([]);
    const shape = (tool.parameters as any).shape.fork_to_own.unwrap().shape;
    assert.deepEqual(Object.keys(shape).sort(), ["destination_repo", "private"]);
    const parsed = tool.parameters.safeParse({
      name: "a1",
      fork_to_own: { destination_repo: "acme/x", github_pat: "ghp_secret" },
    });
    // A stray key is stripped, never forwarded.
    assert.ok(parsed.success);
    assert.equal((parsed.data as any).fork_to_own.github_pat, undefined);
  });

  it("forwards the block and intent to the backend exactly", async () => {
    const created: { config: AgentConfig; idempotencyKey?: string }[] = [];
    const tool = makeCreateTool(created);
    await tool.execute(
      {
        name: "a1",
        template: "github:org/repo",
        import_intent: "fork",
        // `false` is the value a dropped key would silently flip: the backend
        // defaults a fork to private.
        fork_to_own: { destination_repo: "acme/sales-agent", private: false },
      },
      undefined
    );
    assert.equal(created.length, 1);
    // The JSON the client posts — undefined fields drop out, as on the wire.
    const body = JSON.parse(JSON.stringify(created[0].config));
    assert.deepEqual(body, {
      name: "a1",
      template: "github:org/repo",
      import_intent: "fork",
      fork_to_own: { destination_repo: "acme/sales-agent", private: false },
    });
  });

  it("names the fork rules in the tool description", () => {
    const tool = makeCreateTool([]);
    for (const needle of ["fork_to_own", "FORK_PAT_REQUIRED", "FORK_TO_OWN_REQUIRED"]) {
      assert.ok(tool.description.includes(needle), `description must mention ${needle}`);
    }
    assert.ok(!/only in the web UI/.test(JSON.stringify((tool.parameters as any).shape.import_intent.description)));
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
