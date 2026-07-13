/**
 * In-memory MCP round-trip: tool registration, schema, and call plumbing.
 * Uses the SDK's linked InMemoryTransport pair (no stdio, no network).
 */
import { test, describe, afterEach } from "node:test";
import assert from "node:assert/strict";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { createServer } from "./server.js";

const realFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = realFetch;
});

async function connectedClient(): Promise<Client> {
  const server = createServer();
  const client = new Client({ name: "test-client", version: "0.0.0" });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await Promise.all([
    client.connect(clientTransport),
    server.connect(serverTransport),
  ]);
  return client;
}

describe("trinity-docs-mcp server", () => {
  test("exposes exactly ask_trinity and get_agent_requirements", async () => {
    const client = await connectedClient();
    const { tools } = await client.listTools();
    const names = tools.map((t) => t.name).sort();
    assert.deepEqual(names, ["ask_trinity", "get_agent_requirements"]);
    const ask = tools.find((t) => t.name === "ask_trinity")!;
    assert.match(ask.description ?? "", /session_id/);
  });

  test("ask_trinity round-trip returns text content with session line", async () => {
    globalThis.fetch = (async () =>
      new Response(
        JSON.stringify({ answer: "Trinity is a platform.", state: "SUCCEEDED", session_id: "77" }),
        { status: 200 },
      )) as typeof fetch;
    const client = await connectedClient();
    const result = await client.callTool({
      name: "ask_trinity",
      arguments: { question: "What is Trinity?" },
    });
    const content = result.content as Array<{ type: string; text: string }>;
    assert.equal(result.isError ?? false, false);
    assert.equal(content[0].type, "text");
    assert.match(content[0].text, /Trinity is a platform\./);
    assert.match(content[0].text, /session_id: 77/);
  });

  test("ask_trinity surfaces endpoint failure as isError result, not a protocol error", async () => {
    globalThis.fetch = (async () => {
      throw new TypeError("fetch failed");
    }) as typeof fetch;
    const client = await connectedClient();
    const result = await client.callTool({
      name: "ask_trinity",
      arguments: { question: "q" },
    });
    assert.equal(result.isError, true);
    const content = result.content as Array<{ type: string; text: string }>;
    assert.match(content[0].text, /Could not reach/);
  });

  test("get_agent_requirements falls back gracefully when fetch fails", async () => {
    globalThis.fetch = (async () => {
      throw new TypeError("fetch failed");
    }) as typeof fetch;
    const client = await connectedClient();
    const result = await client.callTool({
      name: "get_agent_requirements",
      arguments: {},
    });
    assert.equal(result.isError ?? false, false);
    const content = result.content as Array<{ type: string; text: string }>;
    assert.match(content[0].text, /Quick Reference/);
  });
});
