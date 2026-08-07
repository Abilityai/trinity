/**
 * #2027 — two edge cases in `ask_trinity`, found by an `/edge-cases` pass over
 * ent#328 (#1981).
 *
 * 1. `truncate` cut on UTF-16 code units, so a boundary landing inside a
 *    surrogate pair left an unpaired surrogate — ill-formed Unicode that a
 *    strict UTF-8 encoder or JSON-RPC client replaces with U+FFFD or rejects.
 *
 * 2. The context-loss warning lived inside `if (responseSessionId)`, so a 200
 *    that answered but omitted `session_id` produced no warning at all: the
 *    caller silently lost its conversation context, which is precisely what
 *    that warning exists to prevent. The "different id returned" branch was
 *    covered; the neighbouring "no id returned" one was not.
 *
 * Both fixes are mirrored into `src/helper-mcp/src/client.ts`, which carries a
 * byte-identical copy of the same code — the existing parity suite compares the
 * two on OUTPUT, so they have to move together.
 */

import { describe, it, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

import {
  askTrinity,
  MAX_OUTPUT_CHARS,
  __resetEndpointLog,
} from "./ask_trinity_client.js";

const realFetch = globalThis.fetch;

function stubJson(body: unknown, status = 200): void {
  globalThis.fetch = (async () =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    })) as typeof fetch;
}

/** Any unpaired surrogate anywhere in the string. */
function hasLoneSurrogate(text: string): boolean {
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = text.charCodeAt(i + 1);
      if (!(next >= 0xdc00 && next <= 0xdfff)) return true;
      i++;
    } else if (code >= 0xdc00 && code <= 0xdfff) {
      return true;
    }
  }
  return false;
}

beforeEach(() => {
  delete process.env.ASK_TRINITY_ENDPOINT;
  __resetEndpointLog();
});

afterEach(() => {
  globalThis.fetch = realFetch;
});

describe("#2027 truncation never splits a surrogate pair", () => {
  it("cuts before an astral char that straddles the boundary", async () => {
    // 😀 (U+1F600) occupies the code units at MAX-1 and MAX, so a naive
    // slice(0, MAX) keeps only its high half.
    const answer = "a".repeat(MAX_OUTPUT_CHARS - 1) + "\u{1F600}" + "tail";
    stubJson({ answer, state: "SUCCEEDED" });

    const out = await askTrinity("q");

    assert.equal(hasLoneSurrogate(out.text), false, "output carries an unpaired surrogate");
    assert.match(out.text, /output truncated at/);
  });

  it("still reports the ORIGINAL limit after stepping back", async () => {
    // The message must describe the configured limit, not the adjusted cut —
    // an off-by-one there would be a confusing thing to hand a reader.
    const answer = "a".repeat(MAX_OUTPUT_CHARS - 1) + "\u{1F600}" + "tail";
    stubJson({ answer, state: "SUCCEEDED" });

    const out = await askTrinity("q");

    assert.match(out.text, new RegExp(`output truncated at ${MAX_OUTPUT_CHARS} characters`));
  });

  it("keeps the whole pair when it sits just inside the boundary", async () => {
    // Both halves fit: nothing should be dropped, so the emoji survives intact.
    const answer = "a".repeat(MAX_OUTPUT_CHARS - 2) + "\u{1F600}" + "tail";
    stubJson({ answer, state: "SUCCEEDED" });

    const out = await askTrinity("q");

    assert.equal(hasLoneSurrogate(out.text), false);
    assert.ok(out.text.includes("\u{1F600}"), "a fully-fitting pair was dropped");
  });

  it("is unchanged for a BMP-only answer at the same length", async () => {
    // Guards against the step-back firing when it should not: a plain answer
    // must still be cut at exactly MAX.
    const answer = "a".repeat(MAX_OUTPUT_CHARS + 50);
    stubJson({ answer, state: "SUCCEEDED" });

    const out = await askTrinity("q");

    assert.equal(out.text.split("\n\n[output truncated")[0].length, MAX_OUTPUT_CHARS);
  });

  it("leaves a short answer completely alone", async () => {
    stubJson({ answer: "short \u{1F600} answer", state: "SUCCEEDED" });
    const out = await askTrinity("q");
    assert.ok(out.text.startsWith("short \u{1F600} answer"));
    assert.ok(!out.text.includes("output truncated"));
  });

  it("also protects the error-path truncation", async () => {
    // `truncate(rawText, 200)` handles arbitrary upstream bytes, so it is the
    // likelier of the two to meet an astral char at the boundary.
    const body = "x".repeat(199) + "\u{1F600}" + "more";
    globalThis.fetch = (async () =>
      new Response(body, { status: 500, headers: { "Content-Type": "text/html" } })) as typeof fetch;

    const out = await askTrinity("q");

    assert.equal(out.isError, true);
    assert.equal(hasLoneSurrogate(out.text), false);
  });
});

describe("#2027 a session-less response is not a silent context loss", () => {
  it("warns when a session was sent and none came back", async () => {
    stubJson({ answer: "an answer", state: "SUCCEEDED" });

    const out = await askTrinity("q", "session-the-caller-had");

    assert.match(out.text, /no session_id/);
    assert.match(out.text, /context was lost/);
    assert.equal(out.isError, false, "a lost session is a warning, not a failure");
  });

  it("still warns when a DIFFERENT session comes back", async () => {
    stubJson({ answer: "an answer", session_id: "new-one", state: "SUCCEEDED" });

    const out = await askTrinity("q", "old-one");

    assert.match(out.text, /previous session expired/);
    assert.match(out.text, /session_id: new-one/);
  });

  it("says nothing when the same session comes back", async () => {
    stubJson({ answer: "an answer", session_id: "same", state: "SUCCEEDED" });

    const out = await askTrinity("q", "same");

    assert.ok(!out.text.includes("context was lost"));
    assert.match(out.text, /session_id: same/);
  });

  it("says nothing on a first call with no session at all", async () => {
    stubJson({ answer: "an answer", state: "SUCCEEDED" });

    const out = await askTrinity("q");

    assert.ok(!out.text.includes("context was lost"));
    assert.ok(!out.text.includes("session_id:"));
  });

  it("does not claim a new session was started when none was", async () => {
    // The two cases need different wording: "a new session was started" is
    // false when the response carried no id, and telling a caller to keep
    // using a session that does not exist is worse than saying nothing.
    stubJson({ answer: "an answer", state: "SUCCEEDED" });

    const out = await askTrinity("q", "gone");

    assert.ok(!out.text.includes("a new session was started"));
    assert.ok(!out.text.includes("session_id:"), "no id to hand back");
  });
});
