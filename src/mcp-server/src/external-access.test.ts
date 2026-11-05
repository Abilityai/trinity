/**
 * ent#1101 — off-box `/mcp` is refused while external MCP access is off.
 *
 * `classifyPeer` is the peer rule (loopback and the agent network minus its
 * gateway are on-box; everything else is off-box). `ExternalAccessGate` reads
 * the backend's verdict with a TTL and keeps serving the last one through a
 * backend blip; with no verdict obtainable it answers "unverified".
 */
import { strict as assert } from "node:assert";
import { describe, it } from "node:test";
import { readFileSync } from "node:fs";

import { AGENT_NETWORK_SUBNET, classifyPeer, ExternalAccessGate } from "./external-access.js";

describe("ent#1101 classifyPeer", () => {
  const table: Array<[string | undefined, "on-box" | "off-box"]> = [
    ["127.0.0.1", "on-box"],
    ["::1", "on-box"],
    ["::ffff:127.0.0.1", "on-box"],
    ["172.28.0.5", "on-box"],
    ["172.28.200.9", "on-box"],
    ["::ffff:172.28.0.9", "on-box"],
    ["172.28.0.1", "off-box"], // the bridge gateway: a host-forwarded raw-port caller
    ["::ffff:172.28.0.1", "off-box"],
    ["172.29.0.7", "off-box"],
    ["172.30.0.2", "off-box"],
    ["172.30.0.3", "off-box"],
    ["10.0.0.4", "off-box"],
    ["203.0.113.9", "off-box"],
    ["fd00::9", "off-box"],
    ["garbage", "off-box"],
    ["", "off-box"],
    [undefined, "off-box"],
  ];
  for (const [addr, want] of table) {
    it(`${String(addr)} → ${want}`, () => assert.equal(classifyPeer(addr), want));
  }

  it("the subnet literal is docker-compose.yml's trinity-agent-network subnet", () => {
    const compose = readFileSync(new URL("../../../docker-compose.yml", import.meta.url), "utf8");
    const block = compose.slice(compose.indexOf("name: trinity-agent-network"));
    const m = /subnet:\s*([0-9./]+)/.exec(block);
    assert.ok(m, "docker-compose.yml declares the agent network subnet");
    assert.equal(AGENT_NETWORK_SUBNET, m![1]);
  });
});

type Reply = { status: number; body: unknown } | Error;

function fakeFetch(replies: Reply[], calls: Array<{ url: string; headers: Record<string, string> }> = []) {
  return Object.assign(
    async (url: string, init: any) => {
      calls.push({ url, headers: init?.headers ?? {} });
      const r = replies.length > 1 ? replies.shift()! : replies[0];
      if (r instanceof Error) throw r;
      return { ok: r.status >= 200 && r.status < 300, status: r.status, json: async () => r.body } as any;
    },
    { calls }
  );
}

describe("ent#1101 ExternalAccessGate", () => {
  const on = { status: 200, body: { enabled: true, source: "settings" } };
  const off = { status: 200, body: { enabled: false, source: "default" } };

  it("is unverified until the backend has answered, then carries the verdict", async () => {
    let t = 1_000;
    const fetch = fakeFetch([off]);
    const gate = new ExternalAccessGate({ trinityApiUrl: "http://backend:8000", internalSecret: "s", fetch, now: () => t });
    assert.deepEqual(gate.cached(), undefined);
    assert.equal((await gate.verdict()).kind, "off");
    assert.equal(fetch.calls[0].url, "http://backend:8000/api/internal/mcp-external-access");
    assert.equal(fetch.calls[0].headers["X-Internal-Secret"], "s");
    assert.equal(gate.cached()?.kind, "off");
  });

  it("prefetch retries a bounded number of times and never throws", async () => {
    const fetch = fakeFetch([new Error("ECONNREFUSED"), new Error("ECONNREFUSED"), on]);
    const gate = new ExternalAccessGate({ trinityApiUrl: "http://b", internalSecret: "s", fetch, now: () => 0, retryDelayMs: 0, retries: 5 });
    await gate.prefetch();
    assert.equal(gate.cached()?.kind, "on");
    assert.equal(fetch.calls.length, 3);
  });

  it("a prefetch that never reaches the backend leaves the gate unverified, naming the reason", async () => {
    const fetch = fakeFetch([new Error("ECONNREFUSED")]);
    const gate = new ExternalAccessGate({ trinityApiUrl: "http://b", internalSecret: "s", fetch, now: () => 0, retryDelayMs: 0, retries: 2 });
    await gate.prefetch();
    const v = await gate.verdict();
    assert.equal(v.kind, "unverified");
    assert.match((v as any).reason, /backend not reachable yet/);
    assert.equal(fetch.calls.length, 4, "3 prefetch attempts (2 retries), then the verdict() call");
  });

  it("caches for the TTL, then asks again (a toggle flip lands within one TTL)", async () => {
    let t = 0;
    const fetch = fakeFetch([off, on]);
    const gate = new ExternalAccessGate({ trinityApiUrl: "http://b", internalSecret: "s", fetch, now: () => t, ttlMs: 10_000 });
    assert.equal((await gate.verdict()).kind, "off");
    t = 9_999;
    assert.equal((await gate.verdict()).kind, "off");
    assert.equal(fetch.calls.length, 1, "within the TTL the cache answers");
    t = 10_001;
    assert.equal(gate.cached(), undefined, "stale: no synchronous answer");
    assert.equal((await gate.verdict()).kind, "on");
    assert.equal(fetch.calls.length, 2);
  });

  it("serves the last verdict through a backend blip (stale-while-error), never flipping to unverified", async () => {
    let t = 0;
    const fetch = fakeFetch([on, new Error("socket hang up"), { status: 502, body: {} }]);
    const gate = new ExternalAccessGate({ trinityApiUrl: "http://b", internalSecret: "s", fetch, now: () => t, ttlMs: 10 });
    assert.equal((await gate.verdict()).kind, "on");
    t = 11;
    assert.equal((await gate.verdict()).kind, "on", "the error keeps the stale verdict");
    t = 22;
    assert.equal((await gate.verdict()).kind, "on", "a non-2xx keeps it too");
    assert.equal(fetch.calls.length, 3);
  });

  it("with INTERNAL_API_SECRET empty it is unverified and never calls the backend", async () => {
    const fetch = fakeFetch([on]);
    const gate = new ExternalAccessGate({ trinityApiUrl: "http://b", internalSecret: "", fetch, now: () => 0 });
    await gate.prefetch();
    const v = await gate.verdict();
    assert.equal(v.kind, "unverified");
    assert.match((v as any).reason, /INTERNAL_API_SECRET is not set/);
    assert.equal(fetch.calls.length, 0);
  });

  it("a malformed body is an error, not a verdict", async () => {
    const fetch = fakeFetch([{ status: 200, body: { nope: 1 } }]);
    const gate = new ExternalAccessGate({ trinityApiUrl: "http://b", internalSecret: "s", fetch, now: () => 0 });
    assert.equal((await gate.verdict()).kind, "unverified");
  });
});
