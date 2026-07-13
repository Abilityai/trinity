/**
 * Unit tests for the docs Q&A endpoint client — mocked global fetch.
 * Run: npm test  (node --import tsx --test)
 */
import { test, describe, afterEach } from "node:test";
import assert from "node:assert/strict";
import {
  askTrinity,
  fetchAgentGuide,
  AGENT_GUIDE_FALLBACK,
  MAX_QUESTION_CHARS,
} from "./client.js";

const realFetch = globalThis.fetch;

type FetchArgs = { url: string; init: RequestInit };

function mockFetch(
  handler: (url: string, init: RequestInit) => Response | Promise<Response>,
): FetchArgs[] {
  const calls: FetchArgs[] = [];
  globalThis.fetch = (async (url: any, init: any) => {
    calls.push({ url: String(url), init });
    return handler(String(url), init);
  }) as typeof fetch;
  return calls;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  globalThis.fetch = realFetch;
  delete process.env.ASK_TRINITY_ENDPOINT;
});

describe("askTrinity", () => {
  test("happy path: returns answer and session_id; body carries question", async () => {
    const calls = mockFetch(() =>
      jsonResponse({ answer: "Agents are containers.", state: "SUCCEEDED", session_id: "42" }),
    );
    const result = await askTrinity("What are agents?");
    assert.equal(result.isError, false);
    assert.match(result.text, /Agents are containers\./);
    assert.match(result.text, /session_id: 42/);
    assert.equal(calls.length, 1);
    const body = JSON.parse(String(calls[0].init.body));
    assert.deepEqual(body, { question: "What are agents?" });
    assert.equal(calls[0].init.redirect, "error");
  });

  test("passes session_id through and does NOT warn when it matches", async () => {
    const calls = mockFetch(() =>
      jsonResponse({ answer: "Yes.", state: "SUCCEEDED", session_id: "abc" }),
    );
    const result = await askTrinity("Follow-up?", "abc");
    const body = JSON.parse(String(calls[0].init.body));
    assert.equal(body.session_id, "abc");
    assert.doesNotMatch(result.text, /session expired/);
    assert.match(result.text, /session_id: abc/);
  });

  test("silent session reset: input sid differs from returned sid → context-lost warning", async () => {
    mockFetch(() =>
      jsonResponse({ answer: "Fresh answer.", state: "SUCCEEDED", session_id: "new-777" }),
    );
    const result = await askTrinity("How do I create one?", "expired-123");
    assert.equal(result.isError, false);
    assert.match(result.text, /previous session expired/);
    assert.match(result.text, /session_id: new-777/);
  });

  test("session_id exceeding 2^53 survives as exact string", async () => {
    const big = "3056475319750407883";
    mockFetch(() =>
      jsonResponse({ answer: "ok", state: "SUCCEEDED", session_id: big }),
    );
    const result = await askTrinity("q");
    assert.match(result.text, new RegExp(`session_id: ${big}`));
  });

  test("non-200 with JSON {error} → structured error", async () => {
    mockFetch(() => jsonResponse({ error: "Missing 'question' parameter" }, 400));
    const result = await askTrinity("q");
    assert.equal(result.isError, true);
    assert.match(result.text, /HTTP 400/);
    assert.match(result.text, /Missing 'question' parameter/);
  });

  test("non-200 with HTML body → status + snippet, no crash", async () => {
    mockFetch(() => new Response("<html><body>Error: Server Error</body></html>", { status: 500 }));
    const result = await askTrinity("q");
    assert.equal(result.isError, true);
    assert.match(result.text, /HTTP 500/);
    assert.match(result.text, /<html>/);
  });

  test("200 with non-JSON body → unexpected-response error", async () => {
    mockFetch(() => new Response("upstream proxy junk", { status: 200 }));
    const result = await askTrinity("q");
    assert.equal(result.isError, true);
    assert.match(result.text, /non-JSON/);
  });

  test("state != SUCCEEDED with answer present → answer + warning", async () => {
    mockFetch(() =>
      jsonResponse({ answer: "Partial answer.", state: "FAILED", session_id: "1" }),
    );
    const result = await askTrinity("q");
    assert.equal(result.isError, false);
    assert.match(result.text, /Partial answer\./);
    assert.match(result.text, /state "FAILED"/);
  });

  test("state != SUCCEEDED without answer → error carrying state", async () => {
    mockFetch(() => jsonResponse({ answer: "", state: "BLOCKED" }));
    const result = await askTrinity("q");
    assert.equal(result.isError, true);
    assert.match(result.text, /state "BLOCKED"/);
  });

  test("empty answer with SUCCEEDED → explicit no-answer text, not empty content", async () => {
    mockFetch(() => jsonResponse({ answer: "  ", state: "SUCCEEDED", session_id: "9" }));
    const result = await askTrinity("q");
    assert.equal(result.isError, false);
    assert.match(result.text, /returned no answer/);
    assert.match(result.text, /session_id: 9/);
  });

  test("timeout → timeout-specific message", async () => {
    globalThis.fetch = (async () => {
      const err = new Error("aborted");
      err.name = "TimeoutError";
      throw err;
    }) as typeof fetch;
    const result = await askTrinity("q");
    assert.equal(result.isError, true);
    assert.match(result.text, /took longer than 50s/);
  });

  test("network error → unreachable message mentioning ASK_TRINITY_ENDPOINT", async () => {
    globalThis.fetch = (async () => {
      throw new TypeError("fetch failed");
    }) as typeof fetch;
    const result = await askTrinity("q");
    assert.equal(result.isError, true);
    assert.match(result.text, /Could not reach/);
    assert.match(result.text, /ASK_TRINITY_ENDPOINT/);
  });

  test("empty / whitespace question rejected before network", async () => {
    const calls = mockFetch(() => jsonResponse({}));
    for (const q of ["", "   ", "\n\t"]) {
      const result = await askTrinity(q);
      assert.equal(result.isError, true);
      assert.match(result.text, /empty/);
    }
    assert.equal(calls.length, 0);
  });

  test("over-cap question rejected before network with named limit", async () => {
    const calls = mockFetch(() => jsonResponse({}));
    const result = await askTrinity("x".repeat(MAX_QUESTION_CHARS + 1));
    assert.equal(result.isError, true);
    assert.match(result.text, new RegExp(String(MAX_QUESTION_CHARS)));
    assert.equal(calls.length, 0);
  });

  test("oversized session_id rejected before network", async () => {
    const calls = mockFetch(() => jsonResponse({}));
    const result = await askTrinity("q", "s".repeat(200));
    assert.equal(result.isError, true);
    assert.match(result.text, /session_id/);
    assert.equal(calls.length, 0);
  });

  test("quotes, newlines and unicode serialize correctly via JSON body", async () => {
    const calls = mockFetch(() =>
      jsonResponse({ answer: "ok", state: "SUCCEEDED" }),
    );
    const tricky = 'What about "quotes",\nnewlines and émojis 🚀?';
    await askTrinity(tricky);
    const body = JSON.parse(String(calls[0].init.body));
    assert.equal(body.question, tricky);
  });

  test("citations pass-through: renders a Sources section when the field appears", async () => {
    mockFetch(() =>
      jsonResponse({
        answer: "Cited answer.",
        state: "SUCCEEDED",
        session_id: "1",
        citations: ["docs/onboarding/01-getting-started.md", { uri: "x.txt" }],
      }),
    );
    const result = await askTrinity("q");
    assert.match(result.text, /Sources:/);
    assert.match(result.text, /01-getting-started\.md/);
    assert.match(result.text, /"uri":"x\.txt"/);
  });

  test("honors ASK_TRINITY_ENDPOINT override", async () => {
    process.env.ASK_TRINITY_ENDPOINT = "http://127.0.0.1:9/custom";
    const calls = mockFetch(() =>
      jsonResponse({ answer: "ok", state: "SUCCEEDED" }),
    );
    await askTrinity("q");
    assert.equal(calls[0].url, "http://127.0.0.1:9/custom");
  });

  test("oversized answer truncated with note", async () => {
    mockFetch(() =>
      jsonResponse({ answer: "y".repeat(70_000), state: "SUCCEEDED" }),
    );
    const result = await askTrinity("q");
    assert.match(result.text, /output truncated at 65536 characters/);
  });
});

describe("fetchAgentGuide", () => {
  test("200 → guide content", async () => {
    mockFetch(() => new Response("# Trinity Compatible Agent Guide\n\nReal content."));
    const result = await fetchAgentGuide();
    assert.equal(result.isError, false);
    assert.match(result.text, /Real content\./);
  });

  test("404 → quick-reference fallback with canonical URL, never error-only", async () => {
    mockFetch(() => new Response("Not Found", { status: 404 }));
    const result = await fetchAgentGuide();
    assert.equal(result.isError, false);
    assert.equal(result.text, AGENT_GUIDE_FALLBACK);
    assert.match(result.text, /TRINITY_COMPATIBLE_AGENT_GUIDE\.md/);
  });

  test("network failure → same fallback", async () => {
    globalThis.fetch = (async () => {
      throw new TypeError("fetch failed");
    }) as typeof fetch;
    const result = await fetchAgentGuide();
    assert.equal(result.isError, false);
    assert.equal(result.text, AGENT_GUIDE_FALLBACK);
  });
});
