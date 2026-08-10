/**
 * #2035 condition 1 — `Mcp-Session-Id` must not reach the log in full.
 *
 * Two kinds of test here, and they answer different questions:
 *
 *  - the pure-function and wrapper cases pin what redaction DOES;
 *  - `installed dependencies` pins the ASSUMPTION redaction rests on — that
 *    every log site able to emit a session id carries the `[mcp-proxy]` prefix
 *    the wrapper keys on. That assumption lives in someone else's package, so a
 *    dependency bump can invalidate it silently. Scanning the installed bundle
 *    turns that into a red build.
 *
 * The third guard is behavioural and deliberately lives elsewhere: the transport
 * suite drives a real client and asserts its actual session id never appears on
 * stdout. Neither file substitutes for the other — this one would still pass if
 * the wrapper were never installed.
 */
import { strict as assert } from "node:assert";
import { describe, it } from "node:test";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  MCP_PROXY_LOG_PREFIX,
  REDACTION_KEEP_CHARS,
  REDACTION_MARKER,
  installLogRedaction,
  redactMcpProxyArgs,
  redactUuids,
} from "./log-redaction.js";

const SESSION_ID = "ec975d4b-77f8-43bc-a855-b4f05c2d4fb6";
const TRUNCATED = `ec975d4b${REDACTION_MARKER}`;

describe("#2035 redactUuids", () => {
  it("truncates a UUID and marks it", () => {
    assert.equal(redactUuids(SESSION_ID), TRUNCATED);
  });

  it("keeps enough prefix to correlate two lines for one connection", () => {
    // The whole reason for truncating rather than dropping: an operator must
    // still be able to join the SSE line to the delete line.
    assert.equal(redactUuids(SESSION_ID).slice(0, REDACTION_KEEP_CHARS), "ec975d4b");
  });

  it("redacts every occurrence, not just the first", () => {
    const other = "11111111-2222-3333-4444-555555555555";
    const out = redactUuids(`a ${SESSION_ID} b ${other} c`);
    assert.ok(!out.includes(SESSION_ID));
    assert.ok(!out.includes(other));
  });

  it("matches uppercase hex", () => {
    assert.ok(!redactUuids(SESSION_ID.toUpperCase()).includes("-77F8-"));
  });

  it("leaves text with no UUID untouched", () => {
    assert.equal(redactUuids("[mcp-proxy] received delete request"), "[mcp-proxy] received delete request");
  });
});

describe("#2035 redactMcpProxyArgs", () => {
  it("redacts the single-template form", () => {
    const [line] = redactMcpProxyArgs([
      `${MCP_PROXY_LOG_PREFIX} establishing new SSE stream for session ID ${SESSION_ID}`,
    ]) as string[];
    assert.ok(!line.includes(SESSION_ID));
    assert.ok(line.includes(TRUNCATED));
  });

  it("redacts a trailing argument, not only the first", () => {
    // `console.log("[mcp-proxy] …for session", sessionId)` — the delete path
    // passes the id as a SEPARATE argument, so first-argument-only redaction
    // would miss it entirely.
    const out = redactMcpProxyArgs([
      `${MCP_PROXY_LOG_PREFIX} received delete request for session`,
      SESSION_ID,
    ]) as string[];
    assert.equal(out[1], TRUNCATED);
  });

  it("leaves non-mcp-proxy lines alone", () => {
    // Scope is narrow on purpose: ids Trinity logs deliberately (execution ids,
    // the anonymous correlation id) keep their full value.
    const args = [`MCP anonymous session (#848 inline auth): ${SESSION_ID}`];
    assert.deepEqual(redactMcpProxyArgs(args), args);
  });

  it("passes through a non-string first argument", () => {
    const args = [{ some: "object" }, SESSION_ID];
    assert.deepEqual(redactMcpProxyArgs(args), args);
  });
});

describe("#2035 installLogRedaction", () => {
  const makeConsole = () => {
    const seen: unknown[][] = [];
    const sink = {
      log: (...a: unknown[]) => seen.push(a),
      info: (...a: unknown[]) => seen.push(a),
      warn: (...a: unknown[]) => seen.push(a),
      error: (...a: unknown[]) => seen.push(a),
      debug: (...a: unknown[]) => seen.push(a),
    };
    return { sink, seen };
  };

  it("redacts through every wrapped method", () => {
    const { sink, seen } = makeConsole();
    installLogRedaction(sink);
    for (const m of ["log", "info", "warn", "error", "debug"] as const) {
      sink[m](`${MCP_PROXY_LOG_PREFIX} session ID ${SESSION_ID}`);
    }
    assert.equal(seen.length, 5);
    for (const [line] of seen) {
      assert.ok(!String(line).includes(SESSION_ID), `leaked: ${line}`);
      assert.ok(String(line).includes(TRUNCATED));
    }
  });

  it("is idempotent — a second install does not stack wrappers", () => {
    const { sink, seen } = makeConsole();
    installLogRedaction(sink);
    const afterFirst = sink.log;
    installLogRedaction(sink);
    assert.equal(sink.log, afterFirst);
    sink.log(`${MCP_PROXY_LOG_PREFIX} session ID ${SESSION_ID}`);
    // Double-wrapping would redact the already-redacted string a second time —
    // harmless here, but it also means N wrappers per createServer() call.
    assert.equal(seen.length, 1);
    assert.ok(String(seen[0][0]).includes(TRUNCATED));
  });
});

// ---------------------------------------------------------------------------
// The assumption guard (#2035 condition 1)
// ---------------------------------------------------------------------------
//
// Redaction keys on the `[mcp-proxy]` prefix so it can stay narrow. That is a
// claim about an INSTALLED DEPENDENCY, not about our code, so it is checked
// against the bundle that actually runs rather than trusted. A bump that adds
// an unprefixed session-id log site — in mcp-proxy or in fastmcp, which today
// logs none — fails here instead of shipping a credential to `/data/logs`.

const MCP_SERVER_ROOT = fileURLToPath(new URL("..", import.meta.url));

/** Every bundled JS file shipped by a package's `dist/`. */
function bundleFiles(pkg: string): string[] {
  const root = join(MCP_SERVER_ROOT, "node_modules", pkg, "dist");
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir)) {
      const path = join(dir, entry);
      if (statSync(path).isDirectory()) walk(path);
      else if (/\.(js|mjs|cjs)$/.test(path)) out.push(path);
    }
  };
  walk(root);
  return out;
}

/**
 * Argument text of every `console.*` call in `source`, by paren balance.
 *
 * Crude but sufficient: these bundles contain no console call whose arguments
 * carry unbalanced parens inside a string, and over-capturing would only make
 * the guard stricter, never looser.
 */
function consoleCallArgs(source: string): string[] {
  const calls: string[] = [];
  const re = /console\.(?:log|info|warn|error|debug)\(/g;
  let match: RegExpExecArray | null;
  while ((match = re.exec(source))) {
    const start = match.index + match[0].length;
    let i = start;
    let depth = 1;
    while (i < source.length && depth > 0) {
      const c = source[i];
      if (c === "(") depth++;
      else if (c === ")") depth--;
      i++;
    }
    calls.push(source.slice(start, i - 1));
  }
  return calls;
}

describe("#2035 installed dependencies still match the redaction assumption", () => {
  const sessionLogSites = (pkg: string) =>
    bundleFiles(pkg).flatMap((file) =>
      consoleCallArgs(readFileSync(file, "utf8"))
        .filter((args) => /session/i.test(args))
        .map((args) => ({ file, args }))
    );

  it("finds the known mcp-proxy session-id log sites", () => {
    // A floor, so the guard cannot pass vacuously if the scan stops matching
    // (renamed chunk, changed call shape) and silently finds nothing.
    const sites = sessionLogSites("mcp-proxy");
    assert.ok(
      sites.length >= 3,
      `expected >= 3 mcp-proxy session log sites, found ${sites.length}. ` +
        `Either the bundle layout changed or the scan stopped matching — ` +
        `re-verify before relaxing this.`
    );
  });

  it("every session-id log site carries the prefix redaction keys on", () => {
    for (const pkg of ["mcp-proxy", "fastmcp"]) {
      for (const { file, args } of sessionLogSites(pkg)) {
        const firstArg = args.trimStart();
        assert.ok(
          firstArg.startsWith(`\`${MCP_PROXY_LOG_PREFIX}`) ||
            firstArg.startsWith(`"${MCP_PROXY_LOG_PREFIX}`) ||
            firstArg.startsWith(`'${MCP_PROXY_LOG_PREFIX}`),
          `${pkg} logs a session id from a site redaction does not cover:\n` +
            `  ${file}\n  ${args.slice(0, 200)}\n` +
            `Widen log-redaction.ts (or redact unconditionally) before shipping.`
        );
      }
    }
  });
});
