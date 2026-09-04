/**
 * #2060 — deploy_local_agent tool contract.
 *
 * Two things live in tool CODE (not backend, not model-controlled) and are
 * therefore pinned here:
 *
 * 1. `require_manifest: true` is set unconditionally in the POST body — the
 *    MCP surface gets the integrity contract whether or not the calling model
 *    read the description.
 * 2. The Idempotency-Key is deterministic over the ARGUMENTS (same args ⇒
 *    same key, so a transport retry replays instead of forking a second
 *    version). Explicitly NOT claiming cross-rebuild determinism: a re-run
 *    packaging pipeline produces new gzip bytes ⇒ a new key by design.
 *
 * Drives the real tool execute() with a fake TrinityClient
 * (requireApiKey=false → getClient() returns the fake directly, the
 * git.test.ts / reports.test.ts seam).
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createAgentTools } from "./agents.js";
import { deriveMcpIdempotencyKey } from "./chat.js";
import type { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

const CTX: McpAuthContext = {
  userId: "admin", // McpAuthContext.userId is the OWNER USERNAME, not a numeric id
  userEmail: "a@example.com",
  keyName: "k",
  scope: "user",
  mcpApiKey: "trinity_mcp_x",
} as McpAuthContext;

// A tiny valid base64 payload (content is irrelevant — the fake client never
// decodes it; the backend owns archive validation).
const ARCHIVE = Buffer.from("not-really-a-tarball").toString("base64");

interface RecordedCall {
  method: string;
  path: string;
  body: Record<string, unknown>;
  extraHeaders?: Record<string, string>;
}

function makeTools(calls: RecordedCall[]) {
  const fake = {
    getBaseUrl: () => "http://backend",
    setToken: () => {},
    request: async (
      method: string,
      path: string,
      body: Record<string, unknown>,
      _isRetry?: boolean,
      _requestId?: string,
      extraHeaders?: Record<string, string>
    ) => {
      calls.push({ method, path, body, extraHeaders });
      return { status: "success", verified: true };
    },
  } as unknown as TrinityClient;
  return createAgentTools(fake, false);
}

describe("deploy_local_agent", () => {
  it("sets require_manifest: true in the POST body unconditionally", async () => {
    const calls: RecordedCall[] = [];
    const tools = makeTools(calls);

    await tools.deployLocalAgent.execute(
      { archive: ARCHIVE, name: "my-agent" },
      { session: CTX }
    );

    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, "POST");
    assert.equal(calls[0].path, "/api/agents/deploy-local");
    assert.equal(calls[0].body.require_manifest, true);
    assert.equal(calls[0].body.archive, ARCHIVE);
    assert.equal(calls[0].body.name, "my-agent");
  });

  it("sends a deterministic Idempotency-Key derived from the arguments", async () => {
    const calls: RecordedCall[] = [];
    const tools = makeTools(calls);

    await tools.deployLocalAgent.execute(
      { archive: ARCHIVE, name: "my-agent" },
      { session: CTX }
    );
    await tools.deployLocalAgent.execute(
      { archive: ARCHIVE, name: "my-agent" },
      { session: CTX }
    );

    const k1 = calls[0].extraHeaders?.["Idempotency-Key"];
    const k2 = calls[1].extraHeaders?.["Idempotency-Key"];
    assert.ok(k1, "first call carries an Idempotency-Key");
    assert.equal(k1, k2, "identical args must derive an identical key");
    assert.equal(
      k1,
      deriveMcpIdempotencyKey([CTX.userId, "deploy_local_agent", "my-agent", ARCHIVE])
    );
  });

  it("different archive bytes derive a different key (a rebuilt tar is a new deploy)", async () => {
    const calls: RecordedCall[] = [];
    const tools = makeTools(calls);
    const otherArchive = Buffer.from("rebuilt-tarball-new-mtime").toString("base64");

    await tools.deployLocalAgent.execute(
      { archive: ARCHIVE, name: "my-agent" },
      { session: CTX }
    );
    await tools.deployLocalAgent.execute(
      { archive: otherArchive, name: "my-agent" },
      { session: CTX }
    );

    assert.notEqual(
      calls[0].extraHeaders?.["Idempotency-Key"],
      calls[1].extraHeaders?.["Idempotency-Key"]
    );
  });

  it("refuses an empty or non-base64 archive before any backend call", async () => {
    const calls: RecordedCall[] = [];
    const tools = makeTools(calls);

    await assert.rejects(
      () => tools.deployLocalAgent.execute({ archive: "" }, { session: CTX }),
      /Archive is required/
    );
    await assert.rejects(
      () =>
        tools.deployLocalAgent.execute(
          { archive: "!!! not base64 !!!" },
          { session: CTX }
        ),
      /base64/
    );
    assert.equal(calls.length, 0);
  });

  it("mentions the embedded manifest contract and the CLI escape in the description", () => {
    const tools = makeTools([]);
    const desc = tools.deployLocalAgent.description;
    assert.ok(desc.includes(".trinity-manifest.json"));
    assert.ok(desc.includes("COPYFILE_DISABLE"));
    assert.ok(desc.includes("MANIFEST_DRIFT"));
    assert.ok(/trinity deploy/.test(desc), "directs large agents at the CLI");
    assert.ok(/token-bound/.test(desc), "states the honest token ceiling");
  });
});
