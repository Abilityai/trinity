/**
 * abilityai/trinity-enterprise#1117 — `git_sync` refuses a `paths` element that
 * git would read as an option.
 *
 * An agent created on an older base image stages `paths` with `git add <paths>`
 * (no `--`), so `paths: ["-f", ".env"]` force-stages the gitignored credentials
 * file and the same sync pushes it. The tool schema refuses any element that
 * starts with `-`, naming it, so the call never reaches the backend. A dash
 * anywhere else (`sub/-x.md`) is an ordinary path.
 *
 * Validation is driven through `parameters["~standard"].validate`, the exact
 * seam FastMCP calls before `execute` (a failure becomes an InvalidParams
 * error whose text is `<path>: <message>` per issue).
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createGitTools } from "./git.js";
import type { TrinityClient } from "../client.js";

type Issue = { message: string; path?: ReadonlyArray<PropertyKey | { key: PropertyKey }> };

function makeTools(calls: unknown[][]) {
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    getPermittedAgents: async () => [],
    gitSync: async (name: string, body: unknown) => {
      calls.push([name, body]);
      return { success: true };
    },
  };
  return createGitTools(fake as TrinityClient, false);
}

async function validate(args: unknown): Promise<{ issues?: ReadonlyArray<Issue>; value?: unknown }> {
  const tools = makeTools([]);
  // FastMCP's own call: tool.parameters["~standard"].validate(arguments)
  return (await (tools.gitSync.parameters as any)["~standard"].validate(args)) as {
    issues?: ReadonlyArray<Issue>;
    value?: unknown;
  };
}

describe("ent#1117 git_sync — a paths element starting with '-' is a schema error", () => {
  for (const [paths, bad] of [
    [["-f", ".env"], "-f"],
    [[".env", "--force"], "--force"],
    [["notes.md", "--pathspec-from-file=/etc/passwd"], "--pathspec-from-file=/etc/passwd"],
    [["-"], "-"],
  ] as const) {
    it(`rejects ${JSON.stringify(paths)} and names '${bad}'`, async () => {
      const result = await validate({ agent_name: "alpha", paths });
      assert.ok(result.issues && result.issues.length > 0, "expected a schema error");
      const messages = result.issues.map((i) => i.message).join(" | ");
      assert.ok(messages.includes(`'${bad}'`), `the error must name the element: ${messages}`);
      // The issue points at the offending element, so FastMCP renders `paths.<i>: ...`.
      const index = (paths as readonly string[]).indexOf(bad);
      assert.ok(
        result.issues.some((i) => {
          const keys = (i.path ?? []).map((p) => (typeof p === "object" ? p.key : p));
          return keys[0] === "paths" && keys[1] === index;
        }),
        `issue path must be paths.${index}: ${JSON.stringify(result.issues)}`,
      );
    });
  }

  it("accepts a dash anywhere but the first character", async () => {
    const paths = ["sub/-x.md", "a-b/c-d.txt", "./-leading-dash-spelt-safely.md", "x-"];
    const result = await validate({ agent_name: "alpha", paths });
    assert.equal(result.issues, undefined, JSON.stringify(result.issues));
    assert.deepEqual((result.value as { paths: string[] }).paths, paths);
  });

  it("characterization: omitting paths still validates (sync everything)", async () => {
    const result = await validate({ agent_name: "alpha" });
    assert.equal(result.issues, undefined, JSON.stringify(result.issues));
  });

  it("execute forwards an accepted paths list unchanged", async () => {
    const calls: unknown[][] = [];
    const tools = makeTools(calls);
    await tools.gitSync.execute({ agent_name: "alpha", paths: ["sub/-x.md"] }, {});
    assert.deepEqual(calls, [["alpha", { message: undefined, paths: ["sub/-x.md"], strategy: undefined }]]);
  });
});
