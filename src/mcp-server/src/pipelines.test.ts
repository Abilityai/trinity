/**
 * Tests for #919 — agent-defined pipeline introspection MCP tools.
 *
 * Covers the pure helpers (id grammar, latest-instance selection, hardened
 * YAML parse, escalation counting) and the two tools end-to-end against a
 * mocked TrinityClient: empty case, multi-pipeline parse + latest pick,
 * zero-instance pipeline, malformed-file item isolation, get_state explicit /
 * latest / not-found / malformed, the P1 path-traversal rejection, and
 * error discrimination (stopped 400 / unreachable 503 ≠ empty/not-found).
 * Also pins that the client's JSON `request` and text `downloadAgentFile`
 * share one `_fetch` (auth + 401-retry + error mapping).
 *
 * Runner: built-in `node:test`. Run via:
 *   node --import tsx --test src/*.test.ts
 */
import { describe, it, afterEach } from "node:test";
import { strict as assert } from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import {
  createPipelineTools,
  stripExtension,
  flattenFiles,
  pickLatestInstanceNode,
  countOpenEscalations,
  summarizeDefinition,
  summarizeInstance,
  parseYamlHardened,
  httpStatusFromError,
  isNotFound,
} from "./tools/pipelines.js";
import { TrinityClient } from "./client.js";
import type { AgentFileNode } from "./types.js";

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const PIPELINES_DIR = ".trinity/pipelines";
const STATE_DIR = ".trinity/pipeline-state";

const apiErr = (status: number, detail = "") =>
  new Error(`API error (${status}): ${detail}`);

const fileNode = (
  name: string,
  path: string,
  modified?: string
): AgentFileNode => ({ name, path, type: "file", modified });

const dirNode = (name: string, children: AgentFileNode[]): AgentFileNode => ({
  name,
  path: `${STATE_DIR}/${name}`,
  type: "directory",
  children,
});

const tree = (nodes: AgentFileNode[]) => ({
  base_path: "/home/developer",
  requested_path: ".",
  tree: nodes,
  total_files: 0,
  show_hidden: true,
});

/**
 * Build the tools over a fake client whose `listAgentFiles`/`downloadAgentFile`
 * are routed by path. `requireApiKey=false` so getClient() returns the fake
 * directly. Records every download path so tests can assert the fan-out shape.
 */
function makeTools(routes: {
  list?: (path: string) => Promise<unknown>;
  download?: (path: string) => Promise<string>;
}) {
  const downloadCalls: string[] = [];
  const listCalls: string[] = [];
  const fake = {
    getBaseUrl: () => "http://x",
    setToken: () => {},
    listAgentFiles: async (_name: string, path: string) => {
      listCalls.push(path);
      if (!routes.list) throw apiErr(404, "Path not found");
      return routes.list(path);
    },
    downloadAgentFile: async (_name: string, path: string) => {
      downloadCalls.push(path);
      if (!routes.download) throw apiErr(404, "File not found");
      return routes.download(path);
    },
  };
  const tools = createPipelineTools(fake as unknown as TrinityClient, false);
  return { tools, downloadCalls, listCalls };
}

const run = async (tool: any, params: any): Promise<any> =>
  JSON.parse(await tool.execute(params, undefined));

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

describe("#919 pure helpers", () => {
  it("stripExtension drops the last extension", () => {
    assert.equal(stripExtension("demo.yaml"), "demo");
    assert.equal(stripExtension("inst-1.json"), "inst-1");
    assert.equal(stripExtension("noext"), "noext");
    assert.equal(stripExtension(".hidden"), ".hidden");
  });

  it("flattenFiles collects files depth-first, skipping dirs", () => {
    const nodes: AgentFileNode[] = [
      dirNode("demo", [
        fileNode("inst-1.json", "a/inst-1.json"),
        fileNode("inst-2.json", "a/inst-2.json"),
      ]),
      fileNode("top.yaml", "top.yaml"),
    ];
    assert.deepEqual(
      flattenFiles(nodes).map((f) => f.name),
      ["inst-1.json", "inst-2.json", "top.yaml"]
    );
    assert.deepEqual(flattenFiles(undefined), []);
  });

  it("pickLatestInstanceNode selects newest mtime, tie-broken by name desc", () => {
    const nodes = [
      fileNode("inst-1.json", "p/inst-1.json", "2026-01-01T00:00:00Z"),
      fileNode("inst-3.json", "p/inst-3.json", "2026-01-03T00:00:00Z"),
      fileNode("inst-2.json", "p/inst-2.json", "2026-01-02T00:00:00Z"),
    ];
    assert.equal(pickLatestInstanceNode(nodes)?.name, "inst-3.json");

    // Same mtime → lexical filename desc.
    const tied = [
      fileNode("inst-a.json", "p/inst-a.json", "2026-01-01T00:00:00Z"),
      fileNode("inst-c.json", "p/inst-c.json", "2026-01-01T00:00:00Z"),
      fileNode("inst-b.json", "p/inst-b.json", "2026-01-01T00:00:00Z"),
    ];
    assert.equal(pickLatestInstanceNode(tied)?.name, "inst-c.json");

    assert.equal(pickLatestInstanceNode([]), null);
    // Ignores non-.json entries.
    assert.equal(
      pickLatestInstanceNode([fileNode("note.txt", "p/note.txt")]),
      null
    );
  });

  it("countOpenEscalations counts open/unresolved, conservative on unknown", () => {
    assert.equal(countOpenEscalations({ escalations: [] }), 0);
    assert.equal(countOpenEscalations({}), 0);
    assert.equal(countOpenEscalations(null), 0);
    assert.equal(
      countOpenEscalations({
        escalations: [
          { status: "open" },
          { status: "resolved" },
          { resolved: false },
          { resolved: true },
          { open: true },
          { open: false },
          { note: "no status field" }, // conservative → open
        ],
      }),
      4
    );
  });

  it("summarizeDefinition is shape-tolerant", () => {
    assert.deepEqual(summarizeDefinition(null), { stages: [], stage_count: 0 });
    const s = summarizeDefinition({
      name: "Demo",
      description: "d",
      version: 2,
      stages: [{ id: "a" }, { name: "b" }, "c"],
    });
    assert.deepEqual(s.stages, ["a", "b", "c"]);
    assert.equal(s.stage_count, 3);
    assert.equal(s.name, "Demo");
  });

  it("summarizeInstance falls back to the filename id and reports escalations", () => {
    const s = summarizeInstance(
      {
        current_stage: "synthesis",
        health: "green",
        updated_at: "2026-01-02T00:00:00Z",
        escalations: [{ status: "open" }],
      },
      "inst-2"
    );
    assert.equal(s.instance_id, "inst-2");
    assert.equal(s.current_stage, "synthesis");
    assert.equal(s.health, "green");
    assert.equal(s.open_escalations, 1);

    // Prefers the in-file instance_id when present.
    assert.equal(
      summarizeInstance({ instance_id: "real" }, "fallback").instance_id,
      "real"
    );
  });

  it("parseYamlHardened parses valid YAML and rejects duplicate keys / oversize", () => {
    assert.deepEqual(parseYamlHardened("name: Demo\nstages:\n  - id: a\n"), {
      name: "Demo",
      stages: [{ id: "a" }],
    });
    assert.throws(() => parseYamlHardened("a: 1\na: 2\n")); // uniqueKeys
    assert.throws(() => parseYamlHardened("foo: [1, 2")); // malformed
    assert.throws(() => parseYamlHardened("x".repeat(256 * 1024 + 1))); // size cap
  });

  it("httpStatusFromError / isNotFound parse the client error shape", () => {
    assert.equal(httpStatusFromError(apiErr(404)), 404);
    assert.equal(httpStatusFromError(apiErr(503)), 503);
    assert.equal(httpStatusFromError(new Error("boom")), null);
    assert.equal(isNotFound(apiErr(404)), true);
    assert.equal(isNotFound(apiErr(400)), false);
    assert.equal(isNotFound(apiErr(503)), false);
  });
});

// ---------------------------------------------------------------------------
// list_agent_pipelines
// ---------------------------------------------------------------------------

describe("#919 list_agent_pipelines", () => {
  it("returns [] when the pipelines dir is absent (404)", async () => {
    const { tools } = makeTools({}); // list throws 404
    const out = await run(tools.listAgentPipelines, { agent_name: "a" });
    assert.deepEqual(out, []);
  });

  it("returns [] when the pipelines dir is empty", async () => {
    const { tools } = makeTools({
      list: async (p) =>
        p === PIPELINES_DIR ? tree([]) : Promise.reject(apiErr(404)),
    });
    const out = await run(tools.listAgentPipelines, { agent_name: "a" });
    assert.deepEqual(out, []);
  });

  it("parses N pipelines and picks each latest instance from the tree", async () => {
    const { tools, downloadCalls } = makeTools({
      list: async (p) => {
        if (p === PIPELINES_DIR)
          return tree([
            fileNode("demo.yaml", `${PIPELINES_DIR}/demo.yaml`),
            fileNode("etl.yaml", `${PIPELINES_DIR}/etl.yaml`),
          ]);
        if (p === STATE_DIR)
          return tree([
            dirNode("demo", [
              fileNode(
                "inst-1.json",
                `${STATE_DIR}/demo/inst-1.json`,
                "2026-01-01T00:00:00Z"
              ),
              fileNode(
                "inst-2.json",
                `${STATE_DIR}/demo/inst-2.json`,
                "2026-01-02T00:00:00Z"
              ),
            ]),
            dirNode("etl", []), // zero instances
          ]);
        throw apiErr(404);
      },
      download: async (p) => {
        if (p === `${PIPELINES_DIR}/demo.yaml`)
          return "name: Demo\nstages:\n  - id: a\n  - id: b\n";
        if (p === `${PIPELINES_DIR}/etl.yaml`)
          return "name: ETL\nstages:\n  - extract\n  - load\n";
        if (p === `${STATE_DIR}/demo/inst-2.json`)
          return JSON.stringify({
            instance_id: "inst-2",
            current_stage: "b",
            health: "green",
            updated_at: "2026-01-02T00:00:00Z",
            escalations: [{ status: "open" }, { status: "resolved" }],
          });
        throw apiErr(404, p);
      },
    });

    const out = await run(tools.listAgentPipelines, { agent_name: "a" });
    assert.equal(out.length, 2);

    const demo = out.find((x: any) => x.pipeline_id === "demo");
    assert.deepEqual(demo.stages, ["a", "b"]);
    assert.equal(demo.latest.instance_id, "inst-2"); // newest mtime
    assert.equal(demo.latest.current_stage, "b");
    assert.equal(demo.latest.open_escalations, 1);

    const etl = out.find((x: any) => x.pipeline_id === "etl");
    assert.deepEqual(etl.stages, ["extract", "load"]);
    assert.equal(etl.latest, null); // zero instances

    // Only the LATEST instance is downloaded — never inst-1.
    assert.ok(downloadCalls.includes(`${STATE_DIR}/demo/inst-2.json`));
    assert.ok(!downloadCalls.includes(`${STATE_DIR}/demo/inst-1.json`));
  });

  it("isolates a malformed definition as an item-level error; siblings survive", async () => {
    const { tools } = makeTools({
      list: async (p) => {
        if (p === PIPELINES_DIR)
          return tree([
            fileNode("good.yaml", `${PIPELINES_DIR}/good.yaml`),
            fileNode("bad.yaml", `${PIPELINES_DIR}/bad.yaml`),
          ]);
        if (p === STATE_DIR) return tree([]);
        throw apiErr(404);
      },
      download: async (p) => {
        if (p === `${PIPELINES_DIR}/good.yaml`) return "name: Good\nstages: []\n";
        if (p === `${PIPELINES_DIR}/bad.yaml`) return "foo: [1, 2"; // malformed
        throw apiErr(404);
      },
    });

    const out = await run(tools.listAgentPipelines, { agent_name: "a" });
    const good = out.find((x: any) => x.pipeline_id === "good");
    const bad = out.find((x: any) => x.pipeline_id === "bad");
    assert.equal(good.error, undefined);
    assert.equal(good.latest, null);
    assert.ok(bad.error, "malformed pipeline must carry an item-level error");
  });

  it("surfaces a stopped agent (400) as a real error, NOT an empty list", async () => {
    const { tools } = makeTools({
      list: async () => {
        throw apiErr(400, "Agent must be running to browse files");
      },
    });
    const out = await run(tools.listAgentPipelines, { agent_name: "a" });
    assert.equal(out.success, false);
    assert.match(out.error, /stopped or unreachable/);
  });

  it("surfaces an unreachable agent (503) as a real error", async () => {
    const { tools } = makeTools({
      list: async () => {
        throw apiErr(503, "Agent server not ready");
      },
    });
    const out = await run(tools.listAgentPipelines, { agent_name: "a" });
    assert.equal(out.success, false);
    assert.match(out.error, /stopped or unreachable/);
  });

  it("a 400 on the state-dir list (not the pipelines list) is also a real error", async () => {
    const { tools } = makeTools({
      list: async (p) => {
        if (p === PIPELINES_DIR)
          return tree([fileNode("demo.yaml", `${PIPELINES_DIR}/demo.yaml`)]);
        throw apiErr(400, "Agent must be running"); // state dir
      },
    });
    const out = await run(tools.listAgentPipelines, { agent_name: "a" });
    assert.equal(out.success, false);
  });
});

// ---------------------------------------------------------------------------
// get_agent_pipeline_state
// ---------------------------------------------------------------------------

describe("#919 get_agent_pipeline_state", () => {
  it("reads an explicit instance directly (no list call)", async () => {
    const { tools, listCalls } = makeTools({
      download: async (p) => {
        assert.equal(p, `${STATE_DIR}/demo/inst-2.json`);
        return JSON.stringify({ instance_id: "inst-2", current_stage: "b" });
      },
    });
    const out = await run(tools.getAgentPipelineState, {
      agent_name: "a",
      pipeline_id: "demo",
      instance_id: "inst-2",
    });
    assert.equal(out.success, true);
    assert.equal(out.pipeline_id, "demo");
    assert.equal(out.instance_id, "inst-2");
    assert.equal(out.state.current_stage, "b");
    assert.deepEqual(listCalls, []); // explicit instance never lists
  });

  it("resolves the latest instance when instance_id is omitted", async () => {
    const { tools } = makeTools({
      list: async (p) => {
        assert.equal(p, `${STATE_DIR}/demo`);
        return tree([
          fileNode(
            "inst-1.json",
            `${STATE_DIR}/demo/inst-1.json`,
            "2026-01-01T00:00:00Z"
          ),
          fileNode(
            "inst-2.json",
            `${STATE_DIR}/demo/inst-2.json`,
            "2026-01-02T00:00:00Z"
          ),
        ]);
      },
      download: async (p) => {
        assert.equal(p, `${STATE_DIR}/demo/inst-2.json`);
        return JSON.stringify({ current_stage: "done" });
      },
    });
    const out = await run(tools.getAgentPipelineState, {
      agent_name: "a",
      pipeline_id: "demo",
    });
    assert.equal(out.success, true);
    assert.equal(out.instance_id, "inst-2");
    assert.equal(out.state.current_stage, "done");
  });

  it("returns a clean not-found when the pipeline state dir is absent (404)", async () => {
    const { tools } = makeTools({
      list: async () => {
        throw apiErr(404, "Path not found");
      },
    });
    const out = await run(tools.getAgentPipelineState, {
      agent_name: "a",
      pipeline_id: "nope",
    });
    assert.equal(out.success, false);
    assert.match(out.error, /No state found/);
  });

  it("returns not-found when the pipeline dir exists but has no instances", async () => {
    const { tools } = makeTools({
      list: async () => tree([]),
    });
    const out = await run(tools.getAgentPipelineState, {
      agent_name: "a",
      pipeline_id: "demo",
    });
    assert.equal(out.success, false);
    assert.match(out.error, /no instances/);
  });

  it("returns not-found when an explicit instance is missing (404 download)", async () => {
    const { tools } = makeTools({
      download: async () => {
        throw apiErr(404, "File not found");
      },
    });
    const out = await run(tools.getAgentPipelineState, {
      agent_name: "a",
      pipeline_id: "demo",
      instance_id: "missing",
    });
    assert.equal(out.success, false);
    assert.match(out.error, /not found/);
  });

  it("returns a clean error (not a throw) on malformed JSON", async () => {
    const { tools } = makeTools({
      download: async () => "{ not valid json",
    });
    const out = await run(tools.getAgentPipelineState, {
      agent_name: "a",
      pipeline_id: "demo",
      instance_id: "inst-1",
    });
    assert.equal(out.success, false);
    assert.match(out.error, /not valid JSON/);
  });

  it("discriminates a stopped agent (400) from not-found", async () => {
    const { tools } = makeTools({
      download: async () => {
        throw apiErr(400, "Agent must be running to download files");
      },
    });
    const out = await run(tools.getAgentPipelineState, {
      agent_name: "a",
      pipeline_id: "demo",
      instance_id: "inst-1",
    });
    assert.equal(out.success, false);
    assert.match(out.error, /Could not read state/);
    assert.doesNotMatch(out.error, /not found/);
  });
});

// ---------------------------------------------------------------------------
// [P1 SECURITY] id grammar rejects path traversal BEFORE any download
// ---------------------------------------------------------------------------

describe("#919 [P1] path-traversal id validation (zod, pre-download)", () => {
  const { tools } = makeTools({
    list: async () => {
      throw new Error("LIST MUST NOT BE CALLED");
    },
    download: async () => {
      throw new Error("DOWNLOAD MUST NOT BE CALLED");
    },
  });
  const schema = tools.getAgentPipelineState.parameters;

  it("accepts safe ids", () => {
    for (const id of ["demo", "demo_1", "demo-1", "demo.v2", "ABC123"]) {
      assert.equal(
        schema.safeParse({ agent_name: "a", pipeline_id: id }).success,
        true,
        `expected '${id}' to be accepted`
      );
    }
  });

  it("rejects traversal / separator ids on pipeline_id and instance_id", () => {
    const bad = [
      "../../.env",
      "..",
      "a/b",
      "a/../b",
      ".ssh/id_rsa",
      "%2e%2e%2fetc",
      "a%2Fb",
      "",
      "foo bar",
    ];
    for (const id of bad) {
      assert.equal(
        schema.safeParse({ agent_name: "a", pipeline_id: id }).success,
        false,
        `expected pipeline_id '${id}' to be rejected`
      );
      assert.equal(
        schema.safeParse({
          agent_name: "a",
          pipeline_id: "ok",
          instance_id: id,
        }).success,
        false,
        `expected instance_id '${id}' to be rejected`
      );
    }
  });
});

// ---------------------------------------------------------------------------
// Schema-fixture: the docs/schemas/*.json contract is real and its required
// fields match what the tools depend on. Dependency-free (no ajv): we read
// each schema, assert it parses + declares draft 2020-12, pin its `required`
// set, and prove representative fixtures (the E2E fixtures) carry those fields.
// ---------------------------------------------------------------------------

describe("#919 schema fixtures match the tool contract", () => {
  const readSchema = (name: string) =>
    JSON.parse(
      readFileSync(
        fileURLToPath(new URL(`../../../docs/schemas/${name}`, import.meta.url)),
        "utf8"
      )
    );
  const defSchema = readSchema("agent-pipeline.schema.json");
  const stateSchema = readSchema("agent-pipeline-state.schema.json");

  it("both schemas parse and declare JSON Schema draft 2020-12", () => {
    assert.match(defSchema.$schema, /draft\/2020-12/);
    assert.match(stateSchema.$schema, /draft\/2020-12/);
  });

  it("the state schema requires exactly the fields the tools read", () => {
    // These five drive latest-instance selection + the health summary. If a
    // future edit drops one, this fails — the schema is the contract.
    for (const f of [
      "instance_id",
      "current_stage",
      "health",
      "updated_at",
      "escalations",
    ]) {
      assert.ok(
        stateSchema.required.includes(f),
        `state schema must require '${f}'`
      );
    }
    assert.ok(defSchema.required.includes("stages"), "def schema must require 'stages'");
  });

  it("the E2E fixtures satisfy the schemas' required sets", () => {
    const defFixture = parseYamlHardened(
      "id: demo\nname: Demo\nstages:\n  - id: collect\n  - id: synthesize\n"
    ) as Record<string, unknown>;
    const stateFixture = {
      instance_id: "inst-1",
      pipeline_id: "demo",
      current_stage: "synthesize",
      health: "green",
      updated_at: "2026-06-26T12:00:00Z",
      escalations: [],
    };

    for (const f of defSchema.required) assert.ok(f in defFixture, `def fixture missing '${f}'`);
    for (const f of stateSchema.required)
      assert.ok(f in stateFixture, `state fixture missing '${f}'`);

    // And the tools actually consume the fixtures correctly.
    assert.deepEqual(summarizeDefinition(defFixture).stages, ["collect", "synthesize"]);
    assert.equal(summarizeInstance(stateFixture, "x").open_escalations, 0);
  });
});

// ---------------------------------------------------------------------------
// Client transport: JSON `request` + text `downloadAgentFile` share `_fetch`
// ---------------------------------------------------------------------------

describe("#919 client _fetch shared by JSON + text paths", () => {
  const realFetch = globalThis.fetch;
  afterEach(() => {
    globalThis.fetch = realFetch;
  });

  function stubFetch(
    handler: (url: string, init: any) => { status: number; body: string; contentType?: string }
  ) {
    globalThis.fetch = (async (url: any, init: any) => {
      const r = handler(String(url), init);
      return new Response(r.body, {
        status: r.status,
        headers: { "content-type": r.contentType ?? "application/json" },
      });
    }) as typeof fetch;
  }

  it("both paths attach the bearer token and hit the right endpoints", async () => {
    const seen: Array<{ url: string; auth: string | undefined }> = [];
    stubFetch((url, init) => {
      seen.push({ url, auth: init?.headers?.Authorization });
      if (url.includes("/files/download"))
        return { status: 200, body: "stage: synth", contentType: "text/plain" };
      return {
        status: 200,
        body: JSON.stringify(tree([])),
        contentType: "application/json",
      };
    });

    const client = new TrinityClient("http://backend:8000");
    client.setToken("tok-123");

    const listed = await client.listAgentFiles("ag", ".trinity/pipelines", true);
    assert.equal(listed.total_files, 0); // JSON-decoded

    const text = await client.downloadAgentFile("ag", ".trinity/pipelines/x.yaml");
    assert.equal(text, "stage: synth"); // text-decoded, NOT JSON.parse'd

    assert.equal(seen.length, 2);
    assert.ok(seen.every((s) => s.auth === "Bearer tok-123"));
    assert.ok(seen[0].url.includes("/api/agents/ag/files?path="));
    assert.ok(seen[1].url.includes("/api/agents/ag/files/download?path="));
  });

  it("both paths map a non-2xx to the same `API error (NNN)` throw", async () => {
    stubFetch(() => ({ status: 404, body: "Path not found" }));
    const client = new TrinityClient("http://backend:8000");
    client.setToken("tok");

    await assert.rejects(
      () => client.listAgentFiles("ag", ".trinity/pipelines", true),
      /API error \(404\)/
    );
    await assert.rejects(
      () => client.downloadAgentFile("ag", ".trinity/pipelines/x.yaml"),
      /API error \(404\)/
    );
  });
});
