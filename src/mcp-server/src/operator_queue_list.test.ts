/**
 * trinity-enterprise#815 — a broad `list_operator_queue` is complete within
 * its limit, and says so.
 *
 * The backend used to rank the key OWNER's whole fleet, cut at `limit`, and
 * this tool then dropped the rows an agent may not see — so an agent's own
 * rows could sit below the cut and `count < limit` read as "that's all". Now:
 *   - on a broad agent-key read the tool reads its permits FIRST (strict: a
 *     Docker fault is an error, never "no peers") and passes them as
 *     `agent_names`, so the backend cuts over exactly what is delivered;
 *   - the output always carries count, total, has_more, next_cursor,
 *     next_offset, items — null plus `warnings` when completeness is not
 *     verified (version skew, a belt drop, a backend warning);
 *   - a permit set too large for one request is refused, never widened.
 *
 * Runner: built-in `node:test`. Run via:
 *   node --import tsx --test src/operator_queue_list.test.ts
 */
import { describe, it, mock } from "node:test";
import { strict as assert } from "node:assert";

import { createOperatorQueueTools } from "./tools/operator_queue.js";
import { ApiError, operatorQueueListTarget, type TrinityClient } from "./client.js";

type Row = { id: string; agent_name: string };

const agentCtx = (agentName: string) => ({ session: { scope: "agent", agentName } as any });
const scopeCtx = (scope: string) => ({ session: { scope } as any });

interface Fake {
  client: Partial<TrinityClient>;
  calls: string[];
  listParams: any[];
  permitOpts: any[];
}

function fake(opts: {
  rows?: Row[];
  body?: Record<string, unknown>;
  permitted?: string[] | Error;
}): Fake {
  const calls: string[] = [];
  const listParams: any[] = [];
  const permitOpts: any[] = [];
  const client: Partial<TrinityClient> = {
    getPermittedAgents: (async (_name: string, o?: any) => {
      calls.push("permits");
      permitOpts.push(o);
      if (opts.permitted instanceof Error) throw opts.permitted;
      return opts.permitted ?? [];
    }) as any,
    listOperatorQueue: (async (p: any) => {
      calls.push("list");
      listParams.push(p);
      const rows = opts.rows ?? [];
      return (opts.body ?? {
        items: rows,
        count: rows.length,
        total: rows.length,
        has_more: false,
        next_offset: null,
        next_cursor: null,
      }) as any;
    }) as any,
  };
  return { client, calls, listParams, permitOpts };
}

function tool(f: Fake) {
  return createOperatorQueueTools(f.client as unknown as TrinityClient, false).listOperatorQueue;
}

async function run(f: Fake, params: Record<string, unknown>, ctx: any) {
  return JSON.parse(await tool(f).execute(params as any, ctx));
}

const PAGING_KEYS = ["count", "total", "has_more", "next_cursor", "next_offset", "items"];

describe("ent#815 list_operator_queue completeness", () => {
  it("M1: an agent-key page carries all six keys; count is the page length", async () => {
    const rows = [
      { id: "1", agent_name: "self" },
      { id: "2", agent_name: "peer" },
    ];
    const f = fake({
      permitted: ["peer"],
      body: { items: rows, count: 2, total: 5, has_more: true, next_offset: 2, next_cursor: "c1" },
    });
    const out = await run(f, { limit: 2 }, agentCtx("self"));
    for (const key of PAGING_KEYS) assert.ok(key in out, `missing ${key}`);
    assert.equal(out.count, 2);
    assert.equal(out.total, 5);
    assert.equal(out.has_more, true);
    assert.equal(out.next_cursor, "c1");
    assert.equal(out.next_offset, 2);
    assert.equal(out.warnings, undefined);
  });

  it("M2: the belt drops a row → total null with a warning; paging fields pass through", async () => {
    const warn = mock.method(console, "warn", () => {});
    try {
      const f = fake({
        permitted: ["peer"],
        body: {
          items: [
            { id: "1", agent_name: "self" },
            { id: "2", agent_name: "stranger" },
          ],
          count: 2, total: 9, has_more: true, next_offset: 2, next_cursor: "c9",
        },
      });
      const out = await run(f, { limit: 2 }, agentCtx("self"));
      assert.equal(out.count, 1);
      assert.deepEqual(out.items.map((i: Row) => i.id), ["1"]);
      assert.equal(out.total, null);
      assert.ok(out.warnings.some((w: string) => /withheld by your permissions/.test(w)), out.warnings);
      assert.equal(out.has_more, true);
      assert.equal(out.next_cursor, "c9");
      assert.equal(out.next_offset, 2);
      const logged = warn.mock.calls.map((c) => c.arguments.join(" ")).join("\n");
      assert.match(logged, /stranger/);
    } finally {
      warn.mock.restore();
    }
  });

  for (const scope of ["user", "system"]) {
    it(`M3: a ${scope} key never reads permits; fields pass through`, async () => {
      const f = fake({
        body: { items: [{ id: "1", agent_name: "x" }], count: 1, total: 3, has_more: true, next_offset: 1, next_cursor: null },
      });
      const out = await run(f, { limit: 1 }, scopeCtx(scope));
      assert.deepEqual(f.calls, ["list"]);
      assert.equal(f.listParams[0].agent_names, undefined);
      assert.equal(out.total, 3);
      assert.equal(out.has_more, true);
      assert.equal(out.next_offset, 1);
    });
  }

  it("M4: the description states the completeness contract within the headroom budget", () => {
    const d = tool(fake({})).description;
    for (const needle of ["has_more", "total", "null", "status=pending"]) {
      assert.ok(d.includes(needle), `description lacks ${needle}`);
    }
    assert.ok(d.length <= 1800, `description is ${d.length} chars`);
  });

  it("M5: a broad agent-key read passes {self} ∪ permitted as agent_names, permits read first", async () => {
    const f = fake({ permitted: ["peer-b", "peer-a"] });
    await run(f, { status: "pending", limit: 10 }, agentCtx("self"));
    assert.deepEqual(f.calls, ["permits", "list"]);
    assert.deepEqual([...f.listParams[0].agent_names].sort(), ["peer-a", "peer-b", "self"]);
    assert.equal(f.permitOpts[0]?.strict, true, "the broad read must use the strict permissions read");
  });

  it("M6: a scoped read of your own agent sends no agent_names and reads no permits", async () => {
    const f = fake({ rows: [{ id: "1", agent_name: "self" }] });
    await run(f, { agent_name: "self", limit: 10 }, agentCtx("self"));
    assert.deepEqual(f.calls, ["list"]);
    assert.equal(f.listParams[0].agent_names, undefined);
  });

  const failures: Array<[string, Error]> = [
    ["404", new ApiError(404, "{\"detail\":\"Agent not found\"}")],
    ["503", new ApiError(503, "{\"detail\":\"Docker could not be read\"}")],
    ["network", new TypeError("fetch failed")],
  ];
  for (const [label, error] of failures) {
    it(`M7: a failed permissions read (${label}) is permissions_unavailable, retryable, and lists nothing`, async () => {
      const f = fake({ permitted: error });
      const out = await run(f, { limit: 10 }, agentCtx("self"));
      assert.equal(out.error, "permissions_unavailable");
      assert.equal(out.retryable, true);
      assert.ok(out.cause, "cause must name what failed");
      assert.match(out.fix, /^Retry the broad listing/);
      assert.match(out.fix, /cannot tell you whether a peer already asked/);
      assert.deepEqual(f.calls, ["permits"], "no list call after a failed permissions read");
    });
  }

  it("M8: an old backend's {items, count} → paging fields present and null, with a warning", async () => {
    const f = fake({ body: { items: [{ id: "1", agent_name: "x" }], count: 1 } });
    const out = await run(f, { limit: 10 }, scopeCtx("user"));
    for (const key of ["total", "has_more", "next_cursor", "next_offset"]) {
      assert.ok(key in out, `missing ${key}`);
      assert.equal(out[key], null, key);
    }
    assert.ok(out.warnings.some((w: string) => /did not report paging fields/.test(w)), out.warnings);
  });

  it("M9: a permit set over 500 names is refused, never widened", async () => {
    const permitted = Array.from({ length: 500 }, (_, i) => `p${i}`);
    const f = fake({ permitted });
    const out = await run(f, { limit: 10 }, agentCtx("self"));
    assert.equal(out.error, "permit_set_too_large");
    assert.equal(out.retryable, false);
    assert.match(out.fix, /agent_name=/);
    assert.deepEqual(f.calls, ["permits"]);
  });

  it("M9: a request target over 8 KB with under 500 names is refused", async () => {
    const permitted = Array.from({ length: 300 }, (_, i) => `a-long-agent-name-${i}`);
    const f = fake({ permitted });
    const out = await run(f, { limit: 10 }, agentCtx("self"));
    assert.equal(out.error, "permit_set_too_large");
    assert.deepEqual(f.calls, ["permits"]);
  });

  it("M9: 500 short names under 8 KB are sent", async () => {
    const permitted: string[] = [];
    for (const letter of ["a", "b", "c", "d", "e"]) {
      for (let i = 0; i < 100; i++) permitted.push(`${letter}${i}`);
    }
    permitted.pop(); // 499 peers + self = 500 names
    const f = fake({ permitted });
    const out = await run(f, { status: "pending", limit: 100 }, agentCtx("s"));
    assert.equal(out.error, undefined, JSON.stringify(out).slice(0, 200));
    assert.equal(f.listParams[0].agent_names.length, 500);
  });

  it("M10: version skew plus a belt drop → warnings carries both messages", async () => {
    const warn = mock.method(console, "warn", () => {});
    try {
      const f = fake({
        permitted: [],
        body: {
          items: [
            { id: "1", agent_name: "self" },
            { id: "2", agent_name: "stranger" },
          ],
          count: 2,
        },
      });
      const out = await run(f, { limit: 10 }, agentCtx("self"));
      assert.equal(out.count, 1);
      assert.equal(out.total, null);
      assert.ok(out.warnings.some((w: string) => /did not report paging fields/.test(w)));
      assert.ok(out.warnings.some((w: string) => /withheld by your permissions/.test(w)));
    } finally {
      warn.mock.restore();
    }
  });

  it("M11: backend warnings reach the output with total null", async () => {
    const f = fake({
      body: {
        items: [], count: 0, total: null, has_more: true, next_offset: 10, next_cursor: null,
        warnings: ["1 item(s) on this page were withheld after the read"],
      },
    });
    const out = await run(f, { limit: 10 }, scopeCtx("system"));
    assert.equal(out.total, null);
    assert.deepEqual(out.warnings, ["1 item(s) on this page were withheld after the read"]);
  });
});

describe("ent#815 list_operator_queue cursor walk (opt-in)", () => {
  it("M12: cursor start / a token pass through; no cursor sends none and offset 0", async () => {
    const f = fake({});
    // Through the published schema, as the MCP framework calls the tool — so
    // `offset`'s default of 0 applies exactly as it does for a real caller.
    const parse = (p: Record<string, unknown>) => (tool(f).parameters as any).parse(p);
    await run(f, parse({ cursor: "start", limit: 5 }), scopeCtx("user"));
    await run(f, parse({ cursor: "tok-abc", limit: 5 }), scopeCtx("user"));
    await run(f, parse({ limit: 5 }), scopeCtx("user"));
    assert.equal(f.listParams[0].cursor, "start");
    assert.equal(f.listParams[1].cursor, "tok-abc");
    assert.equal(f.listParams[2].cursor, undefined);
    assert.equal(f.listParams[2].offset, 0);
    assert.match(operatorQueueListTarget(f.listParams[0]), /[?&]cursor=start(&|$)/);
    assert.doesNotMatch(operatorQueueListTarget(f.listParams[2]), /cursor=/);
  });

  it("M13: a two-page walk carries next_cursor and re-reads and re-sends permits each page", async () => {
    const permits = [["peer-a", "peer-b"], ["peer-a"]];
    const calls: string[] = [];
    const sent: any[] = [];
    let page = 0;
    const client: Partial<TrinityClient> = {
      getPermittedAgents: (async (_n: string, o?: any) => {
        calls.push(`permits:${o?.strict}`);
        return permits[Math.min(calls.filter((c) => c.startsWith("permits")).length - 1, 1)];
      }) as any,
      listOperatorQueue: (async (p: any) => {
        calls.push("list");
        sent.push(p);
        page += 1;
        return {
          items: [{ id: `i${page}`, agent_name: "self" }], count: 1, total: 2,
          has_more: page === 1, next_offset: null, next_cursor: page === 1 ? "next-1" : null,
        } as any;
      }) as any,
    };
    const t = createOperatorQueueTools(client as unknown as TrinityClient, false).listOperatorQueue;
    const first = JSON.parse(await t.execute({ cursor: "start", limit: 1 } as any, agentCtx("self")));
    const second = JSON.parse(
      await t.execute({ cursor: first.next_cursor, limit: 1 } as any, agentCtx("self")),
    );
    assert.equal(first.next_cursor, "next-1");
    assert.equal(sent[1].cursor, "next-1");
    assert.deepEqual(calls, ["permits:true", "list", "permits:true", "list"]);
    assert.deepEqual([...sent[0].agent_names].sort(), ["peer-a", "peer-b", "self"]);
    assert.deepEqual([...sent[1].agent_names].sort(), ["peer-a", "self"]);
    assert.equal(second.has_more, false);
    assert.equal(second.next_cursor, null);
  });

  it("M9: just over 8 KB once a 250-byte cursor is counted, under 500 names → refused", async () => {
    // 468 short names: just under 8 KB on their own, just over with the cursor.
    const permitted = Array.from({ length: 468 }, (_, i) => `${"abcde"[i % 5]}x${Math.floor(i / 5)}`);
    const params = { limit: 100, cursor: "c".repeat(250) };
    const withoutCursor = Buffer.byteLength(
      operatorQueueListTarget({ limit: 100, offset: 0, agent_names: ["s", ...permitted] }), "utf8");
    assert.ok(withoutCursor <= 8192 && withoutCursor > 8192 - 250, `fixture is ${withoutCursor} bytes`);
    const f = fake({ permitted });
    const out = await run(f, params, agentCtx("s"));
    assert.equal(out.error, "permit_set_too_large");
    assert.deepEqual(f.calls, ["permits"]);
  });

  it("M4: the description teaches the walk", () => {
    const t = tool(fake({}));
    assert.ok(t.description.includes("next_cursor"));
    assert.ok(t.description.includes('cursor="start"'));
    assert.ok(t.description.length <= 1800, `description is ${t.description.length} chars`);
    const shape = (t.parameters as any).shape;
    assert.ok(shape.cursor, "the tool takes a cursor");
    assert.match(shape.offset.description, /legacy paging; prefer cursor/);
  });
});
