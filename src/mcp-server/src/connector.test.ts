/**
 * ent#46 — per-agent MCP connector tools.
 *
 * Pins that the connector tools (a) take the agent from the auth context, never
 * from a tool arg, (b) advertise exactly list_playbooks/run_playbook/ask, and
 * (c) refuse to run a playbook the connector does not expose (server-side
 * enforcement, not client trust).
 *
 * Runner: built-in node:test → `node --import tsx --test src/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createConnectorTools } from "./tools/connector.js";
import type { TrinityClient } from "./client.js";
import type { McpAuthContext } from "./types.js";

function fakeClient(chats: Array<{ agent: string; message: string }>) {
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    getConnectorPlaybooks: async (_agent: string) => [
      { name: "cso", description: "audit" },
      { name: "commit" },
    ],
    chat: async (agent: string, message: string) => {
      chats.push({ agent, message });
      return { response: "ok" } as any;
    },
  };
  return fake as unknown as TrinityClient;
}

const session = (agentName?: string): McpAuthContext => ({
  userId: "owner",
  keyName: "connector-agent-1-key",
  scope: "connector",
  agentName,
});

describe("connector tools", () => {
  it("exposes exactly list_playbooks, run_playbook, ask", () => {
    const tools = createConnectorTools(fakeClient([]), false);
    const names = Object.values(tools).map((t: any) => t.name).sort();
    assert.deepEqual(names, ["ask", "list_playbooks", "run_playbook"]);
  });

  it("list_playbooks uses the bound agent from auth context", async () => {
    const tools = createConnectorTools(fakeClient([]), false);
    const out = await tools.listPlaybooks.execute({}, { session: session("agent-1") });
    const parsed = JSON.parse(out);
    assert.equal(parsed.agent, "agent-1");
    assert.deepEqual(parsed.playbooks.map((p: any) => p.name), ["cso", "commit"]);
  });

  it("run_playbook dispatches an exposed playbook to the bound agent", async () => {
    const chats: Array<{ agent: string; message: string }> = [];
    const tools = createConnectorTools(fakeClient(chats), false);
    await tools.runPlaybook.execute({ name: "cso", input: "scan repo" }, { session: session("agent-1") });
    assert.equal(chats.length, 1);
    assert.equal(chats[0].agent, "agent-1");
    assert.match(chats[0].message, /cso/);
    assert.match(chats[0].message, /scan repo/);
  });

  it("run_playbook refuses a playbook the connector does not expose", async () => {
    const chats: Array<{ agent: string; message: string }> = [];
    const tools = createConnectorTools(fakeClient(chats), false);
    const out = await tools.runPlaybook.execute({ name: "delete-everything" }, { session: session("agent-1") });
    const parsed = JSON.parse(out);
    assert.equal(parsed.error, "playbook_not_exposed");
    assert.equal(chats.length, 0); // never dispatched
  });

  it("throws if the connector key is not bound to an agent", async () => {
    const tools = createConnectorTools(fakeClient([]), false);
    await assert.rejects(
      () => tools.ask.execute({ message: "hi" }, { session: session(undefined) }),
      /not bound to an agent/
    );
  });
});
