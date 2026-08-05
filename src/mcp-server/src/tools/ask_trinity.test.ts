/**
 * ent#328 — `ask_trinity` in the main Trinity MCP server.
 *
 * Two things are under test:
 *
 * 1. **Behavioural parity with `src/helper-mcp`.** That package's client says
 *    in its own header that it is "contract-identical with the main Trinity MCP
 *    server's ask_trinity"; this file is the other half of that claim finally
 *    existing, so the two are driven through ONE shared table of endpoint
 *    responses and must agree. Parity is on OUTPUT, not source bytes — the
 *    helper's module also carries an agent-guide fetcher this package must not
 *    grow a second copy of (`test_1713_scheduler_utils_parity.py` sets the
 *    precedent for behavioural-over-byte parity when copies legitimately
 *    differ in text).
 *
 * 2. **The tool contract** — schema, registration, and the AC that an
 *    unreachable service degrades rather than crashes.
 */
import { describe, it, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

import {
  askTrinity,
  resolveEndpoint,
  DEFAULT_ENDPOINT,
  MAX_QUESTION_CHARS,
  MAX_SESSION_ID_CHARS,
  __resetEndpointLog,
} from "./ask_trinity_client.js";
import { createDocsTools } from "./docs.js";

/**
 * The helper package is loaded through a COMPUTED specifier, not a static
 * import, and that is deliberate rather than cute.
 *
 * `tsconfig.json` sets `rootDir: ./src` and `include: src/**\/*`, so a static
 * `import "../../../helper-mcp/..."` makes `tsc` fail the whole package build
 * with TS6059 — the file is outside rootDir. The alternative fixes are worse
 * for their size: excluding `*.test.ts` from tsconfig changes build output for
 * every existing test in the package, and dropping the import turns a live
 * drift-detector into a snapshot that can agree with nothing.
 *
 * tsx resolves this at runtime; tsc cannot follow it. `any` is the honest type
 * — there is no shared declaration to lean on, which is the same reason the
 * copy exists.
 */
const helperClientPath = ["..", "..", "..", "helper-mcp", "src", "client.ts"].join("/");
const { askTrinity: helperAskTrinity } = (await import(
  new URL(helperClientPath, import.meta.url).href
)) as { askTrinity: (q: string, s?: string) => Promise<{ text: string; isError: boolean }> };

const realFetch = globalThis.fetch;

function stubFetch(handler: (url: string, init: any) => Response | Promise<Response>) {
  globalThis.fetch = (async (url: any, init: any) =>
    handler(String(url), init)) as typeof fetch;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  delete process.env.ASK_TRINITY_ENDPOINT;
  __resetEndpointLog();
});

afterEach(() => {
  globalThis.fetch = realFetch;
  delete process.env.ASK_TRINITY_ENDPOINT;
});

// ---------------------------------------------------------------------------
// Behavioural parity — one table, both implementations.
// ---------------------------------------------------------------------------

const PARITY_CASES: Array<{ name: string; respond: () => Response; q: string; sid?: string }> = [
  {
    name: "plain answer",
    q: "What is Trinity?",
    respond: () => json({ answer: "A platform.", state: "SUCCEEDED", session_id: "77" }),
  },
  {
    name: "session carried through",
    q: "And schedules?",
    sid: "77",
    respond: () => json({ answer: "Cron.", state: "SUCCEEDED", session_id: "77" }),
  },
  {
    name: "silent session expiry — new id returned for an old one",
    q: "Follow-up",
    sid: "11",
    respond: () => json({ answer: "Fresh.", state: "SUCCEEDED", session_id: "99" }),
  },
  {
    name: "huge session id stays a string",
    q: "Q",
    respond: () =>
      json({ answer: "A.", state: "SUCCEEDED", session_id: 9007199254740993n.toString() }),
  },
  {
    name: "non-SUCCEEDED state with an answer",
    q: "Q",
    respond: () => json({ answer: "Partial.", state: "PARTIAL", session_id: "5" }),
  },
  {
    name: "empty answer, failed state",
    q: "Q",
    respond: () => json({ answer: "", state: "FAILED", session_id: "5" }),
  },
  {
    name: "empty answer, succeeded state",
    q: "Q",
    respond: () => json({ answer: "   ", state: "SUCCEEDED", session_id: "5" }),
  },
  {
    name: "HTTP 500 with a JSON error",
    q: "Q",
    respond: () => json({ error: "backend exploded" }, 500),
  },
  {
    name: "HTML body from a Google frontend",
    q: "Q",
    respond: () => new Response("<html>502</html>", { status: 502 }),
  },
  {
    name: "200 with a non-JSON body",
    q: "Q",
    respond: () => new Response("not json", { status: 200 }),
  },
  {
    name: "citations pass-through (field does not exist today)",
    q: "Q",
    respond: () =>
      json({ answer: "A.", state: "SUCCEEDED", session_id: "5", citations: ["docs/a.md"] }),
  },
  { name: "empty question", q: "   ", respond: () => json({ answer: "unused" }) },
  {
    name: "over-long question",
    q: "x".repeat(MAX_QUESTION_CHARS + 1),
    respond: () => json({ answer: "unused" }),
  },
  {
    name: "over-long session id",
    q: "Q",
    sid: "s".repeat(MAX_SESSION_ID_CHARS + 1),
    respond: () => json({ answer: "unused" }),
  },
];

describe("ent#328 behavioural parity with @abilityai/trinity-docs-mcp", () => {
  for (const c of PARITY_CASES) {
    it(`agrees on: ${c.name}`, async () => {
      stubFetch(() => c.respond());
      const mine = await askTrinity(c.q, c.sid);
      stubFetch(() => c.respond());
      const theirs = await helperAskTrinity(c.q, c.sid);

      assert.equal(mine.isError, theirs.isError, "isError diverged");
      assert.equal(mine.text, theirs.text, "text diverged");
    });
  }
});

// ---------------------------------------------------------------------------
// Session semantics — the part most likely to be got wrong by a reimplementation.
// ---------------------------------------------------------------------------

describe("ent#328 session handling", () => {
  it("warns when the returned session differs from the one sent", async () => {
    stubFetch(() => json({ answer: "A.", state: "SUCCEEDED", session_id: "99" }));
    const out = await askTrinity("Q", "11");
    assert.match(out.text, /previous session expired/i);
    assert.match(out.text, /session_id: 99/);
    assert.equal(out.isError, false);
  });

  it("does NOT warn on a first call with no prior session", async () => {
    stubFetch(() => json({ answer: "A.", state: "SUCCEEDED", session_id: "99" }));
    const out = await askTrinity("Q");
    assert.doesNotMatch(out.text, /previous session expired/i);
  });

  it("keeps a session id that exceeds Number.MAX_SAFE_INTEGER intact", async () => {
    // The endpoint really does return these. Parsing one as a number silently
    // corrupts it, and the corrupted id still yields HTTP 200 — the caller just
    // loses their conversation with no error.
    const big = "9007199254740993";
    stubFetch(() => json({ answer: "A.", state: "SUCCEEDED", session_id: big }));
    const out = await askTrinity("Q");
    assert.match(out.text, new RegExp(`session_id: ${big}\\b`));
  });

  it("forwards session_id in the request body only when provided", async () => {
    let seen: any;
    stubFetch((_u, init) => {
      seen = JSON.parse(init.body);
      return json({ answer: "A.", state: "SUCCEEDED", session_id: "1" });
    });
    await askTrinity("Q");
    assert.deepEqual(seen, { question: "Q" });

    await askTrinity("Q", "42");
    assert.deepEqual(seen, { question: "Q", session_id: "42" });
  });
});

// ---------------------------------------------------------------------------
// Configurability + graceful failure (explicit ACs).
// ---------------------------------------------------------------------------

describe("ent#328 endpoint configuration", () => {
  it("defaults to the public Cloud Function", () => {
    assert.equal(resolveEndpoint(), DEFAULT_ENDPOINT);
  });

  it("honours ASK_TRINITY_ENDPOINT so self-hosted installs can redirect it", () => {
    process.env.ASK_TRINITY_ENDPOINT = "https://internal.example/ask";
    assert.equal(resolveEndpoint(), "https://internal.example/ask");
  });

  it("ignores a whitespace-only override rather than POSTing to nowhere", () => {
    process.env.ASK_TRINITY_ENDPOINT = "   ";
    assert.equal(resolveEndpoint(), DEFAULT_ENDPOINT);
  });

  it("actually POSTs to the override", async () => {
    process.env.ASK_TRINITY_ENDPOINT = "https://internal.example/ask";
    let url = "";
    stubFetch((u) => {
      url = u;
      return json({ answer: "A.", state: "SUCCEEDED", session_id: "1" });
    });
    await askTrinity("Q");
    assert.equal(url, "https://internal.example/ask");
  });
});

describe("ent#328 graceful failure", () => {
  it("returns structured text on a network error, never throws", async () => {
    stubFetch(() => {
      throw new TypeError("fetch failed");
    });
    const out = await askTrinity("Q");
    assert.equal(out.isError, true);
    assert.match(out.text, /Could not reach the Trinity docs service/);
  });

  it("names the timeout distinctly from a network failure", async () => {
    stubFetch(() => {
      const e = new Error("timed out");
      e.name = "TimeoutError";
      throw e;
    });
    const out = await askTrinity("Q");
    assert.equal(out.isError, true);
    assert.match(out.text, /took longer than/);
  });

  it("refuses to follow a redirect", async () => {
    let init: any;
    stubFetch((_u, i) => {
      init = i;
      return json({ answer: "A.", state: "SUCCEEDED", session_id: "1" });
    });
    await askTrinity("Q");
    assert.equal(init.redirect, "error");
  });

  it("bounds the request with a timeout signal", async () => {
    let init: any;
    stubFetch((_u, i) => {
      init = i;
      return json({ answer: "A.", state: "SUCCEEDED", session_id: "1" });
    });
    await askTrinity("Q");
    assert.ok(init.signal, "no AbortSignal — a hung endpoint would hang the tool call");
  });
});

// ---------------------------------------------------------------------------
// Tool registration.
// ---------------------------------------------------------------------------

describe("ent#328 tool registration", () => {
  it("is exposed by createDocsTools alongside get_agent_requirements", () => {
    const tools = createDocsTools() as Record<string, any>;
    const names = Object.values(tools).map((t: any) => t.name);
    assert.deepEqual(names.sort(), ["ask_trinity", "get_agent_requirements"]);
  });

  it("takes question required + session_id optional", () => {
    const tools = createDocsTools() as Record<string, any>;
    const schema = tools.askTrinity.parameters;
    assert.equal(schema.safeParse({ question: "hi" }).success, true);
    assert.equal(schema.safeParse({ question: "hi", session_id: "7" }).success, true);
    assert.equal(schema.safeParse({}).success, false, "question must be required");
    assert.equal(
      schema.safeParse({ question: "hi", session_id: 7 }).success,
      false,
      "session_id must be a string — the ids exceed MAX_SAFE_INTEGER",
    );
  });

  it("returns text rather than throwing when the service is unreachable", async () => {
    stubFetch(() => {
      throw new TypeError("fetch failed");
    });
    const tools = createDocsTools() as Record<string, any>;
    const out = await tools.askTrinity.execute({ question: "Q" });
    assert.equal(typeof out, "string");
    assert.match(out, /Could not reach the Trinity docs service/);
  });

  it("mentions session_id in its description so a caller knows follow-ups exist", () => {
    const tools = createDocsTools() as Record<string, any>;
    assert.match(tools.askTrinity.description, /session_id/);
  });
});
