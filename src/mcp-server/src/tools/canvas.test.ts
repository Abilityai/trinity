/**
 * ent#536 — canvas tools: the default canvas, patch-by-id, and the agent-scope
 * refusal, driven through the real execute() with a fake TrinityClient
 * (requireApiKey=false → getClient() returns the fake; the reports.test.ts seam).
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createCanvasTools, DEFAULT_CANVAS_ID, filterCanvasesForAgentScope } from "./canvas.js";
import type { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

const AGENT_CTX: McpAuthContext = {
  userId: "admin",
  userEmail: "a@example.com",
  keyName: "k",
  scope: "agent",
  agentName: "worker",
  mcpApiKey: "trinity_mcp_x",
} as McpAuthContext;

const USER_CTX: McpAuthContext = {
  userId: "admin",
  userEmail: "a@example.com",
  keyName: "k",
  scope: "user",
  mcpApiKey: "trinity_mcp_x",
} as McpAuthContext;

function makeTools(calls: Array<{ method: string; args: unknown[] }>) {
  const record = (method: string) => async (...args: unknown[]) => {
    calls.push({ method, args });
    return { agent_name: "worker", canvas_id: args[1], blocks: [] };
  };
  const fake = {
    getBaseUrl: () => "http://backend",
    setToken: () => {},
    writeCanvas: record("writeCanvas"),
    patchCanvas: record("patchCanvas"),
    getCanvas: record("getCanvas"),
    listCanvases: async () => [],
    clearCanvas: record("clearCanvas"),
  } as unknown as TrinityClient;
  return createCanvasTools(fake, false);
}

describe("the default canvas", () => {
  it("is 'main' and is what set_canvas / patch_canvas / get_canvas write and read when no id is given", async () => {
    const calls: Array<{ method: string; args: unknown[] }> = [];
    const tools = makeTools(calls);
    assert.equal(DEFAULT_CANVAS_ID, "main");

    await tools.set_canvas.execute({ blocks: [{ kind: "kpi", payload: { tiles: [] } }] }, { session: AGENT_CTX });
    await tools.patch_canvas.execute({ blocks: [{ id: "b1", kind: "kpi", payload: { tiles: [] } }] }, { session: AGENT_CTX });
    await tools.get_canvas.execute({}, { session: AGENT_CTX });

    assert.deepEqual(calls.map((c) => [c.method, c.args[0], c.args[1]]), [
      ["writeCanvas", "worker", "main"],
      ["patchCanvas", "worker", "main"],
      ["getCanvas", "worker", "main"],
    ]);
  });

  it("a named canvas is honoured", async () => {
    const calls: Array<{ method: string; args: unknown[] }> = [];
    const tools = makeTools(calls);
    await tools.patch_canvas.execute(
      { canvas_id: "pipeline", blocks: [{ id: "b2", kind: "markdown", payload: { markdown: "x" } }], execution_id: "e1" },
      { session: AGENT_CTX },
    );
    assert.equal(calls[0].args[1], "pipeline");
    assert.deepEqual(calls[0].args[2], {
      blocks: [{ id: "b2", kind: "markdown", payload: { markdown: "x" } }],
      execution_id: "e1",
    });
  });
});

describe("patch_canvas", () => {
  it("passes the blocks through with their ids and reports the canvas back", async () => {
    const calls: Array<{ method: string; args: unknown[] }> = [];
    const tools = makeTools(calls);
    const out = JSON.parse(await tools.patch_canvas.execute(
      { blocks: [{ id: "b1", kind: "chart", payload: { series: [] } }] },
      { session: AGENT_CTX },
    ));
    assert.equal(out.success, true);
    assert.equal(out.canvas.canvas_id, "main");
  });

  it("requires an agent-scoped key, like every canvas write", async () => {
    const calls: Array<{ method: string; args: unknown[] }> = [];
    const tools = makeTools(calls);
    const out = JSON.parse(await tools.patch_canvas.execute(
      { blocks: [{ id: "b1", kind: "kpi", payload: {} }] },
      { session: USER_CTX },
    ));
    assert.equal(out.success, false);
    assert.match(out.error, /agent-scoped/);
    assert.equal(calls.length, 0);
  });

  it("a backend refusal (unknown id) is returned as the tool error, not thrown", async () => {
    const fake = {
      getBaseUrl: () => "http://backend",
      setToken: () => {},
      patchCanvas: async () => { throw new Error("unknown block id(s): b9 — set_canvas writes the full state"); },
    } as unknown as TrinityClient;
    const tools = createCanvasTools(fake, false);
    const out = JSON.parse(await tools.patch_canvas.execute(
      { blocks: [{ id: "b9", kind: "kpi", payload: {} }] },
      { session: AGENT_CTX },
    ));
    assert.equal(out.success, false);
    assert.match(out.error, /unknown block id/);
  });
});

describe("the kind guide", () => {
  it("advertises every canvas kind with a payload shape (the prompt is pinned against this list)", () => {
    const tools = makeTools([]);
    const shape = tools.set_canvas.parameters.shape.blocks._def.type.shape.kind;
    const kinds: string[] = shape._def.values;
    assert.deepEqual(
      [...kinds].sort(),
      ["chart", "diagram", "html", "image", "json", "kpi", "markdown", "table", "timeline"],
    );
    const guide: string = shape.description ?? "";
    for (const k of kinds) assert.ok(guide.includes(`${k} =`), `kind guide is missing ${k}`);
  });
});

describe("filterCanvasesForAgentScope", () => {
  it("keeps only the allowed agents' rows", () => {
    const rows = [{ agent_name: "worker" }, { agent_name: "other" }, {}];
    assert.deepEqual(filterCanvasesForAgentScope(rows, new Set(["worker"])), [{ agent_name: "worker" }]);
  });
});
