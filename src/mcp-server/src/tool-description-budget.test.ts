/**
 * #3234 — every tool description fits Claude Code's 2,048-character cap.
 *
 * Claude Code shows a model only the first `CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH`
 * characters (default 2048) of an MCP tool description and appends "… [truncated]".
 * Parameter descriptions are not cut. The cut happens in the CLIENT: this server
 * publishes the full text, so nothing on Trinity's side shows a description is
 * losing its tail — abilityai/trinity-enterprise#568 found `chat_with_agent`'s
 * routing guidance had been invisible to every model. No Trinity image sets that
 * variable, and an operator's own Claude Code is not ours to configure, so the
 * budget has to hold at the source.
 *
 * This lists what the server actually PUBLISHES — a real `createServer` in key mode,
 * a real MCP client over the streamable-HTTP transport (the `access-wiring.test.ts`
 * shape), a USER-scoped key, which sees the widest tool set (agent and connector
 * sessions list subsets of the same tools) — and fails naming every tool over the
 * cap with its length.
 *
 * Runner: node:test → `node --import tsx --test src/*.test.ts`.
 */
import { strict as assert } from "node:assert";
import { after, before, describe, it } from "node:test";
import { createServer as createHttpServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

import { createServer } from "./server.js";

/** Claude Code's default `CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH`. */
export const CLAUDE_CODE_DESCRIPTION_CAP = 2048;

/**
 * Over the cap today, owned by an in-flight change — tool → [length now, owner].
 * Shrink-only: an entry may not grow, and an entry that fits the cap FAILS so it
 * is removed with the fix that shortened it. Never add one to make a new tool pass.
 */
const PENDING: Record<string, [number, string]> = {};

describe("#3234 published tool descriptions fit Claude Code's description cap", () => {
  let backend: Server;
  let mcpServer: { stop: () => Promise<void> };
  let mcpUrl: URL;

  before(async () => {
    backend = createHttpServer((req, res) => {
      req.resume();
      req.on("end", () => {
        res.writeHead(200, { "Content-Type": "application/json" });
        if (req.url === "/api/mcp/validate") {
          return res.end(JSON.stringify({
            valid: true, key_id: "key-user-1", user_id: "owner",
            user_email: "owner@example.com", key_name: "budget", scope: "user",
          }));
        }
        return res.end("{}");
      });
    });
    await new Promise<void>((r) => backend.listen(0, "127.0.0.1", () => r()));
    const backendPort = (backend.address() as AddressInfo).port;

    const probe = createHttpServer();
    await new Promise<void>((r) => probe.listen(0, "127.0.0.1", () => r()));
    const mcpPort = (probe.address() as AddressInfo).port;
    await new Promise<void>((r) => probe.close(() => r()));

    const { server } = await createServer({
      trinityApiUrl: `http://127.0.0.1:${backendPort}`,
      requireApiKey: true,
      port: mcpPort,
    });
    await server.start({ transportType: "httpStream", httpStream: { port: mcpPort, host: "127.0.0.1" } });
    mcpServer = server;
    mcpUrl = new URL(`http://127.0.0.1:${mcpPort}/mcp`);
  });

  after(async () => {
    await mcpServer?.stop();
    await new Promise<void>((r) => backend.close(() => r()));
  });

  it("no published tool description exceeds 2,048 characters", async () => {
    const client = new Client({ name: "budget-3234", version: "1.0.0" });
    await client.connect(new StreamableHTTPClientTransport(mcpUrl, {
      requestInit: { headers: { Authorization: "Bearer trinity_mcp_test_user_key" } },
    }));
    try {
      const { tools } = await client.listTools();
      // A listing that silently shrank would pass vacuously — the server publishes ~150.
      assert.ok(tools.length > 100, `only ${tools.length} tools listed — the census is not seeing the server`);
      for (const name of ["deploy_local_agent", "get_objectives"]) {
        assert.ok(tools.some((t) => t.name === name), `${name} is not published — the issue's two tools must be in the census`);
      }
      const over = tools
        .map((t) => ({ name: t.name, length: (t.description ?? "").length }))
        .filter((t) => t.length > CLAUDE_CODE_DESCRIPTION_CAP)
        .sort((a, b) => b.length - a.length);
      for (const [name, [pinned, owner]] of Object.entries(PENDING)) {
        const now = over.find((t) => t.name === name);
        assert.ok(now, `${name} now fits the cap — remove its PENDING entry (${owner})`);
        assert.ok(now.length <= pinned, `${name} grew to ${now.length} characters (pinned ${pinned}, ${owner}) — PENDING is shrink-only`);
      }
      assert.deepEqual(
        over.filter((t) => !(t.name in PENDING)),
        [],
        `tool description(s) over Claude Code's ${CLAUDE_CODE_DESCRIPTION_CAP}-character cap — the client cuts the tail, ` +
          `so the model never reads it. Move detail into parameter descriptions (not cut) and put the rule ` +
          `a model must act on first:\n` +
          over.map((t) => `  ${t.name}: ${t.length} characters`).join("\n"),
      );
    } finally {
      await client.close();
    }
  });
});
