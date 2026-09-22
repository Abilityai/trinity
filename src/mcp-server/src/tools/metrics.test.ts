/**
 * `record_metrics` / `refresh_metric_definitions` (trinity-enterprise#478)
 *
 * The tool is a thin proxy, so what is worth testing is the two things it
 * decides by itself: WHO may call it, and what a backend failure MEANS to the
 * agent reading the result. The second is the load-bearing one — a tool that
 * tells an agent to retry a permanently unstorable batch turns one bad point
 * into an infinite loop.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  classifyMetricError,
  createMetricsTools,
  extractPointErrors,
  extractRetryAfter,
} from "./metrics.js";
import { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

const AGENT_AUTH: McpAuthContext = {
  scope: "agent",
  agentName: "metrics-agent",
  mcpApiKey: "trinity_mcp_test",
} as McpAuthContext;

function fakeClient(behaviour: {
  record?: (agent: string, body: unknown) => Promise<unknown>;
  refresh?: (agent: string) => Promise<unknown>;
  read?: (agent: string, options: unknown) => Promise<unknown>;
  objectives?: (agent: string) => Promise<unknown>;
}): TrinityClient {
  const client = new TrinityClient("http://backend:8000");
  (client as unknown as Record<string, unknown>).recordMetrics =
    behaviour.record ??
    (async (agent: string) => ({
      success: true,
      agent_name: agent,
      recorded: 1,
      deduplicated: 0,
      replayed: false,
      points: [{ index: 0, ts: "2026-09-22T08:00:00.000000Z", idempotency_key: "a".repeat(64) }],
    }));
  (client as unknown as Record<string, unknown>).refreshMetricDefinitions =
    behaviour.refresh ?? (async () => ({ created: ["cycles"], unchanged: 0 }));
  (client as unknown as Record<string, unknown>).getAgentMetrics =
    behaviour.read ??
    (async (agent: string) => ({
      agent_name: agent,
      declared: true,
      stale_rule: "2x cadence",
      window: { kind: "auto", since: "2026-09-21T12:00:00Z", until: "2026-09-22T12:00:00Z" },
      metrics: [
        {
          name: "revenue",
          latest: { value: 10, ts: "2026-09-22T11:59:00Z", dims: null },
          stale: false,
          freshness: "fresh",
          series: [{ dims: null, buckets: [{ ts: "2026-09-22T11:59:00Z", value: 10 }] }],
        },
      ],
      findings: [],
      findings_evaluated_at: null,
    }));
  (client as unknown as Record<string, unknown>).getAgentObjectives =
    behaviour.objectives ??
    (async (agent: string) => ({
      agent_name: agent,
      generated_at: "2026-09-22T12:00:00Z",
      stale_rule: "2x cadence",
      role: { id: "revenue-lead", path: "canon/roles/revenue-lead.yaml" },
      canon_root: "canon",
      unavailable: null,
      source: { template: "read", objectives_dir: "read" },
      objectives: [
        {
          id: "q4-close-rate",
          owned: true,
          supporting: false,
          metrics: [
            {
              name: "close_rate",
              target: 35,
              actual: 30,
              stale: false,
              freshness: "fresh",
              gap: { status: "behind", delta: -5, reason: null },
              finding: null,
            },
          ],
        },
      ],
      findings: [],
      summary: { objectives: 1, metrics: 1, behind: 1 },
      message: null,
    }));
  return client;
}

function tools(behaviour: Parameters<typeof fakeClient>[0] = {}) {
  return createMetricsTools(fakeClient(behaviour), false);
}

const ONE_POINT = { points: [{ metric: "cycles", value: 1 }] };

// ---------------------------------------------------------------------------
// Who may call it
// ---------------------------------------------------------------------------

test("record_metrics refuses a non-agent key without calling the backend", async () => {
  let called = false;
  const t = tools({
    record: async () => {
      called = true;
      return {};
    },
  });

  const raw = await t.recordMetrics.execute(ONE_POINT, {
    session: { scope: "user" } as McpAuthContext,
  });
  const result = JSON.parse(raw as string);

  assert.equal(result.success, false);
  assert.match(result.error, /agent-scoped API key/);
  assert.equal(called, false, "a refusal must not reach the network");
});

test("record_metrics records as the key's own agent, with no target parameter", async () => {
  let seenAgent = "";
  const t = tools({
    record: async (agent) => {
      seenAgent = agent;
      return {
        success: true,
        agent_name: agent,
        recorded: 1,
        deduplicated: 0,
        replayed: false,
        points: [],
      };
    },
  });

  await t.recordMetrics.execute(ONE_POINT, { session: AGENT_AUTH });
  assert.equal(seenAgent, "metrics-agent");
  assert.ok(
    !("agent_name" in t.recordMetrics.parameters.shape),
    "an agent-acting tool must expose no agent target to spoof",
  );
});

test("refresh_metric_definitions is agent-scoped too", async () => {
  const t = tools();
  const raw = await t.refreshMetricDefinitions.execute({}, {
    session: { scope: "system" } as McpAuthContext,
  });
  assert.equal(JSON.parse(raw as string).success, false);
});

// ---------------------------------------------------------------------------
// The happy path reports three separate counts
// ---------------------------------------------------------------------------

test("a successful batch reports recorded, deduplicated and replayed separately", async () => {
  const t = tools({
    record: async (agent) => ({
      success: true,
      agent_name: agent,
      recorded: 2,
      deduplicated: 1,
      replayed: false,
      points: [],
    }),
  });

  const result = JSON.parse(
    (await t.recordMetrics.execute(ONE_POINT, { session: AGENT_AUTH })) as string,
  );

  assert.equal(result.recorded, 2);
  assert.equal(result.deduplicated, 1);
  assert.equal(result.replayed, false);
});

test("a replayed batch says so rather than claiming a fresh write", async () => {
  const t = tools({
    record: async (agent) => ({
      success: true,
      agent_name: agent,
      recorded: 1,
      deduplicated: 0,
      replayed: true,
      points: [],
    }),
  });

  const result = JSON.parse(
    (await t.recordMetrics.execute(ONE_POINT, { session: AGENT_AUTH })) as string,
  );
  assert.equal(result.replayed, true);
});

// ---------------------------------------------------------------------------
// Failure classification — never throws, and retryable means retryable
// ---------------------------------------------------------------------------

test("the tool returns a failure rather than throwing it", async () => {
  const t = tools({
    record: async () => {
      throw new Error("Request failed: 503 metric_store_unavailable");
    },
  });

  const result = JSON.parse(
    (await t.recordMetrics.execute(ONE_POINT, { session: AGENT_AUTH })) as string,
  );
  assert.equal(result.success, false);
  assert.equal(result.retryable, true);
});

test("a store outage is retryable and a rejected batch is not", () => {
  assert.equal(classifyMetricError("503 metric_store_unavailable").retryable, true);
  assert.equal(classifyMetricError("500 metric_store_rejected_batch").retryable, false);
});

test("the two 429s are distinguished, because the remedies differ", () => {
  const rate = classifyMetricError("429 Metric recording rate limit exceeded");
  assert.equal(rate.rate_limited, true);
  assert.equal(rate.quota_exceeded, undefined);

  const quota = classifyMetricError("429 daily_point_cap_exceeded");
  assert.equal(quota.quota_exceeded, true);
  assert.equal(quota.rate_limited, undefined);
});

test("an in-flight duplicate is a conflict worth retrying", () => {
  const flags = classifyMetricError("409 A batch with this idempotency key is already in progress");
  assert.equal(flags.in_flight, true);
  assert.equal(flags.retryable, true);
});

test("an invalid batch is flagged invalid, not retryable", () => {
  const flags = classifyMetricError('422 {"reason":"invalid_points"}');
  assert.equal(flags.invalid, true);
  assert.equal(flags.retryable, undefined);
});

test("a uniform 404 on this dep-gated route reads as not authorized", () => {
  assert.equal(classifyMetricError("404 Agent not found").not_authorized, true);
  assert.equal(classifyMetricError("403 may only record as itself").not_authorized, true);
});

test("a retry-after is surfaced so the agent knows when to come back", () => {
  assert.equal(extractRetryAfter('429 ... "Retry-After": 3600'), 3600);
  assert.equal(extractRetryAfter("503 no header here"), undefined);
});

// ---------------------------------------------------------------------------
// Per-point errors reach the agent verbatim
// ---------------------------------------------------------------------------

test("the per-point reason codes are surfaced so the agent can self-correct", async () => {
  const body =
    '422 {"reason":"invalid_points","errors":[{"index":0,"metric":"cycles",' +
    '"code":"metric_undeclared","message":"not declared","hint":"declare it"}]}';
  const t = tools({
    record: async () => {
      throw new Error(body);
    },
  });

  const result = JSON.parse(
    (await t.recordMetrics.execute(ONE_POINT, { session: AGENT_AUTH })) as string,
  );

  assert.equal(result.invalid, true);
  assert.equal(result.errors.length, 1);
  assert.equal(result.errors[0].code, "metric_undeclared");
  assert.equal(result.errors[0].hint, "declare it");
});

test("an unparseable error body degrades to no detail rather than throwing", () => {
  assert.equal(extractPointErrors("503 service unavailable"), undefined);
  assert.equal(extractPointErrors('422 {"reason":"invalid_points","errors":'), undefined);
});

// ---------------------------------------------------------------------------
// The description teaches the contract the agent has to satisfy
// ---------------------------------------------------------------------------

test("the description names the remedy for an undeclared metric", () => {
  // The hint the backend returns names this tool; a description that did not
  // would leave the agent told to do something it cannot find.
  assert.match(tools().recordMetrics.description, /refresh_metric_definitions/);
});

test("the description states the identity rule and how to correct a point", () => {
  const description = tools().recordMetrics.description;
  assert.match(description, /deduplicated/);
  assert.match(description, /CORRECTION is a new ts/);
});

test("the batch size limit the description quotes is the one the schema enforces", async () => {
  const t = tools();
  assert.match(t.recordMetrics.description, /1000 points per call/);

  const tooMany = {
    points: Array.from({ length: 1001 }, () => ({ metric: "cycles", value: 1 })),
  };
  const parsed = t.recordMetrics.parameters.safeParse(tooMany);
  assert.equal(parsed.success, false);
});

test("a point requires a metric and a value and nothing else", () => {
  const schema = tools().recordMetrics.parameters;
  assert.equal(schema.safeParse({ points: [{ metric: "cycles", value: 1 }] }).success, true);
  assert.equal(schema.safeParse({ points: [{ metric: "cycles" }] }).success, false);
  assert.equal(schema.safeParse({ points: [] }).success, false);
});

test("dimension values are strings, so one label cannot fork into four series", () => {
  const schema = tools().recordMetrics.parameters;
  assert.equal(
    schema.safeParse({ points: [{ metric: "c", value: 1, dims: { region: "eu" } }] }).success,
    true,
  );
  assert.equal(
    schema.safeParse({ points: [{ metric: "c", value: 1, dims: { region: 3 } }] }).success,
    false,
  );
});


// ---------------------------------------------------------------------------
// get_metrics (ent#479) — the read half
// ---------------------------------------------------------------------------

test("get_metrics refuses a non-agent key without calling the backend", async () => {
  let called = false;
  const t = tools({
    read: async () => {
      called = true;
      return {};
    },
  });
  const result = JSON.parse(
    (await t.getMetrics.execute({}, { session: { scope: "user" } as McpAuthContext })) as string,
  );

  assert.equal(result.success, false);
  assert.match(result.error, /agent-scoped API key/);
  assert.equal(called, false);
});

test("get_metrics reads the CALLING agent — there is no target to spoof", async () => {
  let seen = "";
  const t = tools({
    read: async (agent) => {
      seen = agent;
      return { agent_name: agent, metrics: [] };
    },
  });
  await t.getMetrics.execute({}, { session: AGENT_AUTH });

  assert.equal(seen, "metrics-agent");
  // ent#80 is the grant that would add one; until then the schema has no
  // agent parameter at all, so a prompt-injected "read the other agent's
  // revenue" has nothing to bind to.
  assert.equal(t.getMetrics.parameters.safeParse({ agent_name: "victim" }).success, true);
  assert.equal(
    Object.keys(t.getMetrics.parameters.shape).includes("agent_name"),
    false,
  );
});

test("get_metrics returns the route body verbatim — the shape IS the contract", async () => {
  const result = JSON.parse(
    (await tools().getMetrics.execute({}, { session: AGENT_AUTH })) as string,
  );

  assert.equal(result.success, true);
  assert.equal(result.stale_rule, "2x cadence");
  assert.equal(result.metrics[0].freshness, "fresh");
  assert.equal(result.metrics[0].latest.value, 10);
  assert.equal(result.findings_evaluated_at, null);
});

test("get_metrics forwards every query knob", async () => {
  let seen: Record<string, unknown> = {};
  const t = tools({
    read: async (_agent, options) => {
      seen = options as Record<string, unknown>;
      return {};
    },
  });
  await t.getMetrics.execute(
    { metric: "revenue", window: "7d", since: "2026-09-01T00:00:00Z", include_retired: true },
    { session: AGENT_AUTH },
  );

  assert.equal(seen.metric, "revenue");
  assert.equal(seen.window, "7d");
  assert.equal(seen.since, "2026-09-01T00:00:00Z");
  assert.equal(seen.include_retired, true);
});

test("an undeclared metric comes back named, with the remedy, not as a retry", async () => {
  const t = tools({
    read: async () => {
      throw new Error('422 {"reason":"metric_undeclared","message":"not declared"}');
    },
  });
  const result = JSON.parse(
    (await t.getMetrics.execute({ metric: "nope" }, { session: AGENT_AUTH })) as string,
  );

  assert.equal(result.success, false);
  assert.equal(result.undeclared, true);
  assert.match(result.hint, /refresh_metric_definitions/);
  assert.notEqual(result.retryable, true);
});

test("get_metrics never throws, whatever the backend does", async () => {
  for (const message of ["503 store down", "500 boom", "403 nope", "429 slow down"]) {
    const t = tools({
      read: async () => {
        throw new Error(message);
      },
    });
    const result = JSON.parse(
      (await t.getMetrics.execute({}, { session: AGENT_AUTH })) as string,
    );
    assert.equal(result.success, false, message);
  }
});

test("a store outage is retryable and a permanent failure is not", async () => {
  const outage = tools({
    read: async () => {
      throw new Error("503 metric_store_unavailable");
    },
  });
  const permanent = tools({
    read: async () => {
      throw new Error("500 internal error");
    },
  });

  assert.equal(
    JSON.parse((await outage.getMetrics.execute({}, { session: AGENT_AUTH })) as string)
      .retryable,
    true,
  );
  assert.equal(
    JSON.parse((await permanent.getMetrics.execute({}, { session: AGENT_AUTH })) as string)
      .retryable,
    false,
  );
});

test("the window enum the schema accepts is the one the backend accepts", () => {
  const schema = tools().getMetrics.parameters;
  for (const window of ["auto", "24h", "7d", "30d", "90d"]) {
    assert.equal(schema.safeParse({ window }).success, true, window);
  }
  assert.equal(schema.safeParse({ window: "last-tuesday" }).success, false);
});

test("the description teaches what stale means and that the series is bounded", () => {
  const description = tools().getMetrics.description;
  assert.match(description, /2x its declared cadence/);
  assert.match(description, /no_cadence/);
  assert.match(description, /no_points/);
  assert.match(description, /buckets/);
});

// ---------------------------------------------------------------------------
// get_objectives (ent#666)
// ---------------------------------------------------------------------------

test("get_objectives refuses a non-agent key without calling the backend", async () => {
  let called = false;
  const t = tools({
    objectives: async () => {
      called = true;
      return {};
    },
  });
  const result = JSON.parse(
    (await t.getObjectives.execute({}, { session: { scope: "user" } as McpAuthContext })) as string,
  );

  assert.equal(result.success, false);
  assert.match(result.error, /agent-scoped API key/);
  assert.equal(called, false);
});

test("get_objectives takes no agent parameter — it reads the caller's own", async () => {
  let asked: string | undefined;
  const t = tools({
    objectives: async (agent: string) => {
      asked = agent;
      return { agent_name: agent, objectives: [] };
    },
  });
  await t.getObjectives.execute({}, { session: AGENT_AUTH });

  assert.equal(asked, "metrics-agent");
  assert.deepEqual(Object.keys(t.getObjectives.parameters.shape ?? {}), []);
});

test("get_objectives passes the backend body through verbatim — the shape IS the contract", async () => {
  const result = JSON.parse(
    (await tools().getObjectives.execute({}, { session: AGENT_AUTH })) as string,
  );

  assert.equal(result.success, true);
  assert.equal(result.stale_rule, "2x cadence");
  const row = result.objectives[0].metrics[0];
  assert.deepEqual(row.gap, { status: "behind", delta: -5, reason: null });
  assert.equal(row.target, 35);
  assert.equal(row.actual, 30);
  assert.equal(result.summary.behind, 1);
});

test("get_objectives never throws, whatever the backend does", async () => {
  for (const message of ["503 store down", "500 boom", "403 nope", "429 slow down", "404"]) {
    const t = tools({
      objectives: async () => {
        throw new Error(message);
      },
    });
    const result = JSON.parse(
      (await t.getObjectives.execute({}, { session: AGENT_AUTH })) as string,
    );
    assert.equal(result.success, false, message);
  }
});

test("get_objectives maps a store outage as retryable and a 403/404 as not authorized", async () => {
  const outage = tools({
    objectives: async () => {
      throw new Error("503 metric_store_unavailable");
    },
  });
  const denied = tools({
    objectives: async () => {
      throw new Error("403 Agent-scoped key may only read its own objectives");
    },
  });

  assert.equal(
    JSON.parse((await outage.getObjectives.execute({}, { session: AGENT_AUTH })) as string)
      .retryable,
    true,
  );
  assert.equal(
    JSON.parse((await denied.getObjectives.execute({}, { session: AGENT_AUTH })) as string)
      .not_authorized,
    true,
  );
});

test("the description teaches position-not-pace, what stale means, and the undeclared fix", () => {
  const description = tools().getObjectives.description;
  assert.match(description, /NEVER pace/);
  assert.match(description, /on_target/);
  assert.match(description, /off_target/);
  assert.match(description, /DO NOT ACT ON THIS NUMBER/);
  assert.match(description, /refresh_metric_definitions/);
  assert.match(description, /metric_not_declared_here/);
  // The whole point of the issue: one join, not a second one derived here.
  assert.match(description, /do not re-derive a gap/);
});
