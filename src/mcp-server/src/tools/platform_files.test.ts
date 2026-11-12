/**
 * trinity-enterprise#1237 — the platform file tools (list/read/write/remove_platform_file(s)).
 *
 * Pins the tool-layer contract:
 *   - each tool proxies the agent door of its space with the turn id, and the
 *     real client builds the route (space and name URL-encoded);
 *   - every refusal comes back as a structured result with the backend's code
 *     and message (a revision conflict carries the current revision) — never a throw;
 *   - a platform without file spaces (404 with no code) reads as such, in plain words;
 *   - a user key must name the agent it acts for; an agent key acts as itself;
 *   - each description says where a file goes, and fits the description cap.
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createPlatformFileTools } from "./platform_files.js";
import { ApiError, TrinityClient as RealClient } from "../client.js";
import type { TrinityClient } from "../client.js";

/** Claude Code's description cap (#3234); the repo-wide guard is tool-description-budget.test.ts. */
const CLAUDE_CODE_DESCRIPTION_CAP = 2048;
const SPACE = "project:prj_0123456789abcdef";

function apiError(status: number, detail: unknown): ApiError {
  return new ApiError(status, JSON.stringify({ detail }));
}

function makeTools(overrides: Partial<TrinityClient> = {}) {
  const fake: Partial<TrinityClient> = { getBaseUrl: () => "http://localhost:8000", ...overrides };
  return createPlatformFileTools(fake as TrinityClient, false);
}

const agentSession = { session: { scope: "agent", agentName: "a-alice", executionId: "exec-1" } } as never;

describe("ent#1237 platform file tools", () => {
  it("publishes four tools, each saying where a file goes, within the description cap", () => {
    const tools = makeTools();
    assert.deepEqual(Object.keys(tools).sort(),
      ["list_platform_files", "read_platform_file", "remove_platform_file", "write_platform_file"]);
    for (const t of Object.values(tools)) {
      assert.ok(t.description.length <= CLAUDE_CODE_DESCRIPTION_CAP, `${t.name}: ${t.description.length}`);
      assert.match(t.description, /share_file/, `${t.name} does not say where a download link goes`);
      assert.match(t.description, /workspace/, `${t.name} does not say where working files go`);
    }
  });

  it("read says the text is another principal's: data, not instructions", () => {
    // A non-brief folder file is writable by every agent on the project, so the
    // reader may be handed another agent's text (the memory.ts / operator_queue.ts framing).
    assert.match(makeTools().read_platform_file.description, /data, not instructions/);
  });

  it("list and read forward the space, the cursor, the offset and the turn id", async () => {
    const calls: unknown[][] = [];
    const tools = makeTools({
      listPlatformFiles: (async (space: string, cursor?: string, turn?: string) => {
        calls.push(["list", space, cursor, turn]);
        return { space, files: [{ name: "a.md", revision: 1 }], used_bytes: 3, limits: {} };
      }) as never,
      readPlatformFile: (async (space: string, name: string, offset: number, turn?: string) => {
        calls.push(["read", space, name, offset, turn]);
        return { name, text: "abcd", truncated: true, next_offset: 4 };
      }) as never,
    });
    const listed = JSON.parse(await tools.list_platform_files.execute({ space: SPACE, cursor: "c1" }, agentSession));
    assert.equal(listed.success, true);
    assert.equal(listed.files[0].name, "a.md");
    const part = JSON.parse(await tools.read_platform_file.execute({ space: SPACE, name: "a.md" }, agentSession));
    assert.equal(part.next_offset, 4);
    await tools.read_platform_file.execute({ space: SPACE, name: "a.md", offset: 4 }, agentSession);
    assert.deepEqual(calls, [
      ["list", SPACE, "c1", "exec-1"], ["read", SPACE, "a.md", 0, "exec-1"], ["read", SPACE, "a.md", 4, "exec-1"]]);
  });

  it("write and remove forward the revision they were given", async () => {
    const calls: unknown[][] = [];
    const tools = makeTools({
      writePlatformFile: (async (space: string, name: string, content: string, rev?: number) => {
        calls.push(["write", space, name, content, rev]);
        return { name, revision: (rev ?? 0) + 1 };
      }) as never,
      removePlatformFile: (async (space: string, name: string, rev: number) => {
        calls.push(["remove", space, name, rev]);
      }) as never,
    });
    const made = JSON.parse(await tools.write_platform_file.execute({ space: SPACE, name: "r.md", content: "x" }, agentSession));
    assert.equal(made.revision, 1);
    await tools.write_platform_file.execute({ space: SPACE, name: "r.md", content: "y", if_revision: 1 }, agentSession);
    const gone = JSON.parse(await tools.remove_platform_file.execute({ space: SPACE, name: "r.md", if_revision: 2 }, agentSession));
    assert.deepEqual(gone, { success: true, removed: "r.md", space: SPACE });
    assert.deepEqual(calls, [["write", SPACE, "r.md", "x", undefined], ["write", SPACE, "r.md", "y", 1],
      ["remove", SPACE, "r.md", 2]]);
  });

  it("a refusal is a structured result with the backend's code, message and current revision", async () => {
    const tools = makeTools({
      writePlatformFile: (async () => {
        throw apiError(409, { code: "file_changed", message: "This file changed since you read it; read it again first.", revision: 3 });
      }) as never,
    });
    const out = JSON.parse(await tools.write_platform_file.execute({ space: SPACE, name: "r.md", content: "y", if_revision: 1 }, agentSession));
    assert.deepEqual(out, { success: false, code: "file_changed", revision: 3,
      error: "This file changed since you read it; read it again first." });
  });

  it("a platform without file spaces says so in plain words", async () => {
    const tools = makeTools({
      listPlatformFiles: (async () => { throw apiError(404, "Not Found"); }) as never,
    });
    const out = JSON.parse(await tools.list_platform_files.execute({ space: SPACE }, agentSession));
    assert.equal(out.success, false);
    assert.equal(out.error, "This platform has no file spaces.");
  });

  it("a user key must name the agent it acts for", async () => {
    const tools = makeTools({ listPlatformFiles: (async () => ({ files: [] })) as never });
    const out = JSON.parse(await tools.list_platform_files.execute({ space: SPACE },
      { session: { scope: "user", userId: "u1" } } as never));
    assert.equal(out.success, false);
    assert.equal(out.code, "act_as_refused");
  });

  it("the real client calls the agent door, space and name encoded", async () => {
    const client = new RealClient("http://localhost:8000");
    const seen: unknown[] = [];
    (client as unknown as { request: unknown }).request = async (...args: unknown[]) => {
      seen.push([args[0], args[1], args[2], args[5]]);
      return {};
    };
    await client.listPlatformFiles(SPACE, undefined, "t1");
    await client.readPlatformFile(SPACE, "my notes.md", 8, "t1");
    await client.writePlatformFile(SPACE, "a.md", "x", 2, "t1");
    await client.removePlatformFile(SPACE, "a.md", 2, "t1");
    const base = "/api/enterprise/files/agent/spaces/project%3Aprj_0123456789abcdef/files";
    const turn = { "X-Trinity-Execution-Id": "t1" };
    assert.deepEqual(seen, [
      ["GET", base, undefined, turn],
      ["GET", `${base}/my%20notes.md?offset=8`, undefined, turn],
      ["PUT", `${base}/a.md`, { content: "x", if_revision: 2 }, turn],
      ["DELETE", `${base}/a.md?if_revision=2`, undefined, turn],
    ]);
  });
});
