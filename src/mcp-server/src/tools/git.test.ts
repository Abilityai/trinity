/**
 * #905 — git MCP tools.
 *
 * Pins the tool-layer contract for the 6 direct git tools:
 *   - requestId: minted per call, stamped on context.requestId (→ audit row)
 *     AND forwarded to the client method (→ X-Request-ID → joinable backend row).
 *   - agent-to-agent gate: an agent-scoped key calling a non-permitted sibling
 *     is denied at the MCP layer (backend resolves to owner, can't see this).
 *   - conflict surfacing: a 409 ApiError becomes a structured conflict object
 *     with verbatim X-Conflict-Type/-Class and a chat_with_agent hint.
 *   - get_git_log clamps limit to 1–100 (agent server shells `git log -N`).
 *
 * Drives the real tool execute() with a fake TrinityClient (requireApiKey=false
 * → getClient() returns the fake directly, same seam as operator_queue.test.ts).
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createGitTools } from "./git.js";
import { ApiError, type TrinityClient } from "../client.js";
import type { ToolCallContext } from "../audit.js";
import type { McpAuthContext } from "../types.js";

type Recorded = { method: string; args: unknown[]; requestId?: string };

function makeTools(
  calls: Recorded[],
  overrides: Partial<TrinityClient> = {},
) {
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    getPermittedAgents: async () => [],
    getGitStatus: async (name: string, requestId?: string) => {
      calls.push({ method: "getGitStatus", args: [name], requestId });
      return { branch: "main" };
    },
    gitSync: async (name: string, body: unknown, requestId?: string) => {
      calls.push({ method: "gitSync", args: [name, body], requestId });
      return { success: true };
    },
    getGitLog: async (name: string, limit: number, requestId?: string) => {
      calls.push({ method: "getGitLog", args: [name, limit], requestId });
      return [];
    },
    gitPull: async (name: string, strategy: string, requestId?: string) => {
      calls.push({ method: "gitPull", args: [name, strategy], requestId });
      return { success: true };
    },
    getGitSyncState: async (name: string, requestId?: string) => {
      calls.push({ method: "getGitSyncState", args: [name], requestId });
      return { last_sync_status: "success" };
    },
    resetToMainPreserveState: async (name: string, requestId?: string) => {
      calls.push({ method: "resetToMainPreserveState", args: [name], requestId });
      return { success: true };
    },
    ...overrides,
  };
  return createGitTools(fake as TrinityClient, false);
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

describe("#905 git tools — requestId minted, stamped, and forwarded", () => {
  it("stamps context.requestId and forwards the SAME id to the client", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const context: ToolCallContext = {};

    await tools.getGitStatus.execute({ agent_name: "alpha" }, context);

    assert.equal(calls.length, 1);
    assert.match(String(context.requestId), UUID_RE);
    // The id forwarded to the backend call equals the one on the audit context.
    assert.equal(calls[0].requestId, context.requestId);
  });
});

describe("#905 git tools — agent-to-agent permission gate", () => {
  it("denies an agent-scoped key calling a non-permitted sibling", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls); // getPermittedAgents → []
    const authContext: McpAuthContext = {
      userId: "u1",
      keyName: "k",
      scope: "agent",
      agentName: "caller",
    };

    const out = await tools.gitSync.execute(
      { agent_name: "other" },
      { session: authContext },
    );

    const parsed = JSON.parse(out);
    assert.equal(parsed.error, "Access denied");
    assert.equal(calls.length, 0); // never reached the backend
  });

  it("allows an agent-scoped key acting on itself", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const authContext: McpAuthContext = {
      userId: "u1",
      keyName: "k",
      scope: "agent",
      agentName: "self",
    };

    await tools.getGitStatus.execute({ agent_name: "self" }, { session: authContext });
    assert.equal(calls.length, 1);
    assert.equal(calls[0].args[0], "self");
  });
});

describe("#905 git tools — conflict surfacing", () => {
  it("turns a 409 ApiError into a structured conflict with a chat_with_agent hint", async () => {
    const calls: Recorded[] = [];
    const headers = new Headers({
      "X-Conflict-Type": "diverged_history",
      "X-Conflict-Class": "merge",
    });
    const tools = makeTools(calls, {
      gitSync: async () => {
        throw new ApiError(409, "conflict detail", headers);
      },
    });

    const out = await tools.gitSync.execute({ agent_name: "alpha" }, {});
    const parsed = JSON.parse(out);

    assert.equal(parsed.error, "conflict");
    assert.equal(parsed.status, 409);
    assert.equal(parsed.conflict_type, "diverged_history");
    assert.equal(parsed.conflict_class, "merge");
    assert.match(parsed.hint, /chat_with_agent/);
  });

  it("surfaces a non-409 error as a plain { error }", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls, {
      gitPull: async () => {
        throw new ApiError(400, "bad request", new Headers());
      },
    });
    const out = await tools.gitPull.execute({ agent_name: "alpha" }, {});
    const parsed = JSON.parse(out);
    assert.ok(parsed.error.includes("400"));
    assert.equal(parsed.status, undefined);
  });
});

describe("#905 git tools — get_git_log limit clamp", () => {
  it("clamps an over-max limit down to 100", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    await tools.getGitLog.execute({ agent_name: "alpha", limit: 9999 }, {});
    assert.equal(calls[0].args[1], 100);
  });

  it("clamps a below-min limit up to 1", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    await tools.getGitLog.execute({ agent_name: "alpha", limit: 0 }, {});
    assert.equal(calls[0].args[1], 1);
  });

  it("defaults to 10 when limit omitted", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    await tools.getGitLog.execute({ agent_name: "alpha" }, {});
    assert.equal(calls[0].args[1], 10);
  });
});
