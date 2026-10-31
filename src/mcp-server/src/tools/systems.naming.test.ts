/**
 * trinity-enterprise#1030 — `naming:` on the MCP systems tools.
 *
 * `get_system_manifest` forwards an optional `naming` to the export route's
 * `?naming=` override and sends no query when it is absent (the backend then
 * infers). The descriptions are pins on top of that behaviour: they must say
 * how members are found and named, and every systems tool must fit Claude
 * Code's 2,048-character cut (#3234).
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { strict as assert } from "node:assert";
import { describe, it } from "node:test";

import { createSystemTools } from "./systems.js";
import { TrinityClient } from "../client.js";

class RecordingClient extends TrinityClient {
  calls: Array<{ method: string; path: string }> = [];
  constructor() {
    super("http://backend");
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  async request<T>(method: string, path: string): Promise<T> {
    this.calls.push({ method, path });
    return "name: acme\n" as unknown as T;
  }
}

describe("ent#1030 get_system_manifest forwards naming", () => {
  it("sends ?naming= only when the caller names a mode", async () => {
    const client = new RecordingClient();
    const tools = createSystemTools(client, false);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const exec = tools.getSystemManifest.execute as any;

    await exec({ system_name: "acme" });
    await exec({ system_name: "acme", naming: "bare" });
    await exec({ system_name: "acme", naming: "prefixed" });

    assert.deepEqual(client.calls.map((c) => c.path), [
      "/api/systems/acme/manifest",
      "/api/systems/acme/manifest?naming=bare",
      "/api/systems/acme/manifest?naming=prefixed",
    ]);
  });

  it("refuses any other mode before a request is made", () => {
    const tools = createSystemTools(new RecordingClient(), false);
    const schema = tools.getSystemManifest.parameters;
    assert.equal(schema.safeParse({ system_name: "acme", naming: "flat" }).success, false);
    assert.equal(schema.safeParse({ system_name: "acme", naming: "bare" }).success, true);
  });
});

describe("ent#1030 the systems descriptions name naming: bare", () => {
  const tools = createSystemTools(new TrinityClient("http://backend"), false);

  it("get_system_manifest says membership is by tag, not by name prefix", () => {
    const text = JSON.stringify(tools.getSystemManifest.parameters.shape.system_name.description);
    assert.doesNotMatch(text, /All agents matching/);
    assert.match(text, /tag/);
  });

  it("deploy_system and apply_system mention naming: bare", () => {
    assert.match(tools.deploySystem.description, /naming: bare/);
    assert.match(tools.deploySystem.parameters.shape.manifest.description ?? "", /naming: bare/);
    assert.match(tools.applySystem.parameters.shape.manifest.description ?? "", /naming: bare/);
    assert.match(tools.applySystem.description, /adopt/);
  });

  it("every systems tool description fits Claude Code's 2,048 cut (#3234)", () => {
    for (const tool of Object.values(tools)) {
      const d: string = (tool as { description: string }).description;
      assert.ok(d.length <= 2048, `${(tool as { name: string }).name} description is ${d.length} characters`);
    }
  });
});
