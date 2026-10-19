/**
 * trinity-enterprise#926 — every backend call a tool makes carries its turn.
 *
 * Real HTTP: a backend that records the X-Trinity-Execution-Id of each request,
 * and the real TrinityClient called through the real withAudit wrapper.
 */
import { strict as assert } from "node:assert";
import { after, before, describe, it } from "node:test";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";

import { TrinityClient } from "./client.js";
import { withAudit } from "./audit.js";
import { currentTurn, runInTurn } from "./turn-context.js";

describe("ent#926 the turn rides on every backend call", () => {
  let backend: Server;
  let base: string;
  const seen: Array<{ url: string; turn: string | undefined }> = [];

  before(async () => {
    backend = createServer((req, res) => {
      seen.push({ url: req.url ?? "", turn: req.headers["x-trinity-execution-id"] as string | undefined });
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end("{}");
    });
    await new Promise<void>((r) => backend.listen(0, "127.0.0.1", () => r()));
    base = `http://127.0.0.1:${(backend.address() as AddressInfo).port}`;
  });
  after(() => backend.close());

  const client = () => {
    const c = new TrinityClient(base);
    c.setToken("agent-key");
    return c;
  };
  const tool = (fn: () => Promise<unknown>) =>
    withAudit("t", async () => { await fn(); return "ok"; });

  it("a call no client method forwarded before now carries it (setReminder, createAgentSchedule)", async () => {
    seen.length = 0;
    await tool(async () => {
      await client().setReminder("atlas", { message: "m", fire_at: "2030-01-01T00:00:00Z" } as never);
      await client().createAgentSchedule("atlas", { name: "n" } as never);
    })({}, { session: { executionId: "exec-share-1" } as never });
    assert.deepEqual(seen.map((s) => s.turn), ["exec-share-1", "exec-share-1"]);
  });

  it("no turn outside a tool call, and none for a session without one", async () => {
    seen.length = 0;
    await client().listAgents();
    await tool(() => client().listAgents())({}, { session: {} as never });
    assert.deepEqual(seen.map((s) => s.turn), [undefined, undefined]);
  });

  it("an id of the wrong shape is not forwarded", async () => {
    seen.length = 0;
    await tool(() => client().listAgents())({}, { session: { executionId: "bad id\r\nx" } as never });
    assert.equal(seen[0].turn, undefined);
  });

  it("a method that sets the header itself keeps its own value", async () => {
    seen.length = 0;
    await tool(() => client().raiseAsk("atlas", { title: "t" } as never, "exec-explicit"))(
      {}, { session: { executionId: "exec-ambient" } as never });
    assert.equal(seen[0].turn, "exec-explicit");
  });

  it("two concurrent tool calls never see each other's turn", async () => {
    seen.length = 0;
    const slow = (id: string) => tool(async () => {
      await new Promise((r) => setTimeout(r, 20));
      await client().listAgents();
    })({}, { session: { executionId: id } as never });
    await Promise.all([slow("exec-a"), slow("exec-b")]);
    assert.deepEqual(seen.map((s) => s.turn).sort(), ["exec-a", "exec-b"]);
    assert.equal(currentTurn(), undefined);
    assert.equal(runInTurn("x-1", () => currentTurn()), "x-1");
  });
});
