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
 * What did change with zod 4:
 * - the advertised inputSchema no longer carries `additionalProperties: false`;
 * - the human-readable validation text is worded differently;
 * - zod 4 counts the question's length in Unicode code points, zod 3 counted
 *   UTF-16 code units. For text with characters outside the Basic
 *   Multilingual Plane (emoji, for example) the schema cap therefore admits
 *   more, and the cap the caller hits is client.ts's UTF-16 guard, with its
 *   own wording. Nothing rejected before is accepted now, and the endpoint is
 *   still never reached.
 * Extra keys were and still are accepted at runtime, and are not forwarded.
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
let lastBody: Record<string, unknown> | undefined;

function stubEndpoint(): void {
  fetchCalls = 0;
  lastBody = undefined;
  globalThis.fetch = (async (_url: unknown, init?: RequestInit) => {
    fetchCalls++;
    lastBody = JSON.parse(String(init?.body)) as Record<string, unknown>;
    const { question } = lastBody as { question: string };
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

const VALIDATION_PREFIX =
  "MCP error -32602: Input validation error: Invalid arguments for tool ask_trinity: ";

function onlyText(result: Record<string, unknown>): string {
  const content = result.content as Array<{ type: string; text: string }>;
  assert.equal(content.length, 1);
  assert.equal(content[0].type, "text");
  return content[0].text;
}

// U+1F600: one code point, two UTF-16 code units.
const ASTRAL = "\u{1F600}";

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
    assert.deepEqual(lastBody, { question: "q" });
  });

  // Each argument set fails the input schema, so the SDK must answer with the
  // -32602 validation result naming the offending field, and the tool (and so
  // the docs endpoint) must never run. A schema that coerced or loosened
  // either field would forward these to the endpoint instead.
  const invalidArguments: Array<{ name: string; args: Record<string, unknown>; field: string }> = [
    { name: "question is a number", args: { question: 5 }, field: "question" },
    { name: "question is null", args: { question: null }, field: "question" },
    { name: "question is an array", args: { question: ["a"] }, field: "question" },
    { name: "question is missing", args: {}, field: "question" },
    { name: "session_id is null", args: { question: "q", session_id: null }, field: "session_id" },
    { name: "session_id is a number", args: { question: "q", session_id: 5 }, field: "session_id" },
  ];
  for (const { name, args, field } of invalidArguments) {
    test(`invalid arguments (${name}) return the -32602 validation result and never reach the endpoint`, async () => {
      stubEndpoint();
      const client = await connectedClient();
      const result = await client.callTool({ name: "ask_trinity", arguments: args });
      assert.equal(result.isError, true);
      const text = onlyText(result);
      assert.ok(text.startsWith(VALIDATION_PREFIX), `unexpected text: ${text}`);
      assert.ok(text.endsWith(` at ${field}`), `expected the error to name ${field}: ${text}`);
      assert.equal(fetchCalls, 0);
    });
  }

  test("astral text: 4001 UTF-16 units in 2001 code points passes the schema and is stopped by the client guard", async () => {
    stubEndpoint();
    const client = await connectedClient();
    const question = ASTRAL.repeat(MAX_QUESTION_CHARS / 2) + "a";
    assert.equal(question.length, MAX_QUESTION_CHARS + 1);
    assert.equal([...question].length, MAX_QUESTION_CHARS / 2 + 1);
    const result = await client.callTool({ name: "ask_trinity", arguments: { question } });
    assert.equal(result.isError, true);
    assert.equal(
      onlyText(result),
      "The question is 4001 characters — the limit is 4000. Shorten it and try again.",
    );
    assert.equal(fetchCalls, 0);
  });

  test("astral text: 4000 UTF-16 units is accepted and reaches the endpoint", async () => {
    stubEndpoint();
    const client = await connectedClient();
    const question = ASTRAL.repeat(MAX_QUESTION_CHARS / 2);
    const result = await client.callTool({ name: "ask_trinity", arguments: { question } });
    assert.equal(result.isError ?? false, false);
    assert.match(onlyText(result), /^len=4000\n/);
    assert.equal(fetchCalls, 1);
  });

  test("astral text: 4001 code points is rejected by the schema", async () => {
    stubEndpoint();
    const client = await connectedClient();
    const result = await client.callTool({
      name: "ask_trinity",
      arguments: { question: ASTRAL.repeat(MAX_QUESTION_CHARS + 1) },
    });
    assert.equal(result.isError, true);
    assert.match(onlyText(result), VALIDATION_TEXT);
    assert.equal(fetchCalls, 0);
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
