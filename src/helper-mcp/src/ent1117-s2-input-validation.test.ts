/**
 * Pins the ask_trinity input contract as the MCP SDK (>=1.32) and zod 4
 * actually deliver it, so a later dependency bump that changes the wire
 * shape fails here instead of in a client.
 *
 * Observed (both before and after the zod 3 -> 4 upgrade): an argument that
 * fails the input schema is NOT a JSON-RPC error. The SDK catches the
 * validation failure and returns a normal `result` with `isError: true`
 * whose text starts "MCP error -32602: Input validation error". The tool
 * callback (and so the docs endpoint) is never reached.
 *
 * What did change with zod 4: the advertised inputSchema no longer carries
 * `additionalProperties: false`, and the human-readable validation text is
 * worded differently. Extra keys were and still are accepted at runtime.
 */
import { test, describe, afterEach } from "node:test";
import assert from "node:assert/strict";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import type { JSONRPCMessage } from "@modelcontextprotocol/sdk/types.js";
import { createServer } from "./server.js";
import { MAX_QUESTION_CHARS } from "./client.js";

const realFetch = globalThis.fetch;
let fetchCalls = 0;

function stubEndpoint(): void {
  fetchCalls = 0;
  globalThis.fetch = (async (_url: unknown, init?: RequestInit) => {
    fetchCalls++;
    const { question } = JSON.parse(String(init?.body)) as { question: string };
    return new Response(
      JSON.stringify({ answer: `len=${question.length}`, state: "SUCCEEDED", session_id: "1" }),
      { status: 200 },
    );
  }) as typeof fetch;
}

afterEach(() => {
  globalThis.fetch = realFetch;
});

async function connectedClient(): Promise<Client> {
  const server = createServer();
  const client = new Client({ name: "ent1117-s2", version: "0.0.0" });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await Promise.all([client.connect(clientTransport), server.connect(serverTransport)]);
  return client;
}

const VALIDATION_TEXT =
  /^MCP error -32602: Input validation error: Invalid arguments for tool ask_trinity: .*\b4000\b.* at question$/;

describe("ask_trinity input contract (ent1117-s2)", () => {
  test("the limit under test is 4000 characters", () => {
    assert.equal(MAX_QUESTION_CHARS, 4000);
  });

  test("a 4001-char question returns an isError result, never reaching the endpoint", async () => {
    stubEndpoint();
    const client = await connectedClient();
    const result = await client.callTool({
      name: "ask_trinity",
      arguments: { question: "x".repeat(MAX_QUESTION_CHARS + 1) },
    });
    assert.equal(result.isError, true);
    const content = result.content as Array<{ type: string; text: string }>;
    assert.equal(content.length, 1);
    assert.equal(content[0].type, "text");
    assert.match(content[0].text, VALIDATION_TEXT);
    assert.equal(fetchCalls, 0);
  });

  test("on the wire, the 4001-char rejection is a JSON-RPC result, not a JSON-RPC error", async () => {
    stubEndpoint();
    const server = createServer();
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
    await server.connect(serverSide);
    const frames: JSONRPCMessage[] = [];
    clientSide.onmessage = (m) => frames.push(m);
    await clientSide.start();
    const waitFor = (id: number) =>
      new Promise<Record<string, unknown>>((resolve, reject) => {
        const t0 = Date.now();
        const tick = () => {
          const f = frames.find((m) => (m as { id?: unknown }).id === id);
          if (f) return resolve(f as unknown as Record<string, unknown>);
          if (Date.now() - t0 > 2000) return reject(new Error(`no response for id ${id}`));
          setTimeout(tick, 5);
        };
        tick();
      });

    await clientSide.send({
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: "2025-03-26",
        capabilities: {},
        clientInfo: { name: "raw", version: "0" },
      },
    });
    await waitFor(1);
    await clientSide.send({ jsonrpc: "2.0", method: "notifications/initialized" });
    await clientSide.send({
      jsonrpc: "2.0",
      id: 2,
      method: "tools/call",
      params: { name: "ask_trinity", arguments: { question: "x".repeat(MAX_QUESTION_CHARS + 1) } },
    });
    const frame = await waitFor(2);

    assert.equal("error" in frame, false, `expected a result frame, got ${JSON.stringify(frame)}`);
    const result = frame.result as { isError: boolean; content: Array<{ type: string; text: string }> };
    assert.equal(result.isError, true);
    assert.match(result.content[0].text, VALIDATION_TEXT);
    assert.equal(fetchCalls, 0);
  });

  test("a question of exactly 4000 chars is accepted and reaches the endpoint", async () => {
    stubEndpoint();
    const client = await connectedClient();
    const result = await client.callTool({
      name: "ask_trinity",
      arguments: { question: "x".repeat(MAX_QUESTION_CHARS) },
    });
    assert.equal(result.isError ?? false, false);
    const content = result.content as Array<{ type: string; text: string }>;
    assert.match(content[0].text, /^len=4000\n/);
    assert.equal(fetchCalls, 1);
  });

  test("unknown argument keys are accepted and not forwarded", async () => {
    stubEndpoint();
    const client = await connectedClient();
    const result = await client.callTool({
      name: "ask_trinity",
      arguments: { question: "q", bogus: 1 },
    });
    assert.equal(result.isError ?? false, false);
    assert.equal(fetchCalls, 1);
  });

  test("advertised ask_trinity inputSchema is exactly the zod 4 rendering", async () => {
    const client = await connectedClient();
    const { tools } = await client.listTools();
    const ask = tools.find((t) => t.name === "ask_trinity")!;
    // zod 3 rendered the same object plus `additionalProperties: false`.
    assert.deepEqual(ask.inputSchema, {
      $schema: "http://json-schema.org/draft-07/schema#",
      type: "object",
      properties: {
        question: {
          type: "string",
          maxLength: 4000,
          description: "The question to ask about Trinity",
        },
        session_id: {
          type: "string",
          description:
            "Opaque session token from a previous ask_trinity response, for multi-turn follow-ups",
        },
      },
      required: ["question"],
    });
  });
});
