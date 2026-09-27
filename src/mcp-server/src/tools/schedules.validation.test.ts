/**
 * #2759 — the post-execution validation config (VALIDATE-001) is readable over
 * MCP but was not writable: `list_agent_schedules` / `get_agent_schedule`
 * return `validation_enabled`, `validation_prompt` and
 * `validation_timeout_seconds`, while `create_agent_schedule` and
 * `update_agent_schedule` accepted none of them. Both tool schemas are strict,
 * so the fields could not be smuggled through either.
 *
 * Drives the real tool execute() with a fake TrinityClient (requireApiKey=false
 * → getClient() returns the fake directly, same seam as reports.test.ts).
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createScheduleTools } from "./schedules.js";
import type { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

const CTX: McpAuthContext = {
  userId: "admin",
  userEmail: "a@example.com",
  keyName: "k",
  scope: "user",
  mcpApiKey: "trinity_mcp_x",
} as McpAuthContext;

const SCHEDULE = {
  id: "sch_1",
  agent_name: "worker",
  name: "Daily",
  cron_expression: "0 9 * * *",
  message: "go",
  enabled: true,
  timezone: "UTC",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  timeout_seconds: 900,
};

function makeTools() {
  const seen: { create?: Record<string, unknown>; update?: Record<string, unknown> } = {};
  const fake = {
    getBaseUrl: () => "http://backend",
    setToken: () => {},
    getPermittedAgents: async () => [],
    createAgentSchedule: async (_agent: string, payload: Record<string, unknown>) => {
      seen.create = payload;
      return SCHEDULE;
    },
    updateAgentSchedule: async (
      _agent: string,
      _id: string,
      payload: Record<string, unknown>,
    ) => {
      seen.update = payload;
      return SCHEDULE;
    },
  } as unknown as TrinityClient;
  return { tools: createScheduleTools(fake, false), seen };
}

const BASE_CREATE = {
  agent_name: "worker",
  name: "Daily",
  cron_expression: "0 9 * * *",
  message: "go",
};

describe("#2759 create_agent_schedule carries the validation config", () => {
  it("sends all three fields to the backend", async () => {
    const { tools, seen } = makeTools();

    await tools.createAgentSchedule.execute(
      {
        ...BASE_CREATE,
        validation_enabled: true,
        validation_prompt: "check the numbers add up",
        validation_timeout_seconds: 300,
      },
      { session: CTX },
    );

    assert.equal(seen.create?.validation_enabled, true);
    assert.equal(seen.create?.validation_prompt, "check the numbers add up");
    assert.equal(seen.create?.validation_timeout_seconds, 300);
  });

  it("leaves the safe default alone when they are omitted", async () => {
    // The accept control. Validation is not free -- it adds an execution per
    // run -- so a schedule created without asking for it must not get it.
    const { tools, seen } = makeTools();

    await tools.createAgentSchedule.execute({ ...BASE_CREATE }, { session: CTX });

    assert.equal(seen.create?.validation_enabled, undefined);
    assert.equal(seen.create?.validation_prompt, undefined);
    assert.equal(seen.create?.validation_timeout_seconds, undefined);
  });

  it("declares the fields in its parameter schema", () => {
    // The schemas are strict (additionalProperties: false), so a field the
    // schema does not name cannot reach execute() at all -- which is why the
    // pass-through above is only half the fix.
    const { tools } = makeTools();
    const shape = tools.createAgentSchedule.parameters.shape as Record<string, unknown>;

    assert.ok(shape.validation_enabled, "validation_enabled missing from the create schema");
    assert.ok(shape.validation_prompt, "validation_prompt missing from the create schema");
    assert.ok(
      shape.validation_timeout_seconds,
      "validation_timeout_seconds missing from the create schema",
    );
  });

  it("rejects a timeout outside the documented 30-600 range", () => {
    const { tools } = makeTools();
    const schema = tools.createAgentSchedule.parameters;

    for (const bad of [29, 601, 0, -1]) {
      const parsed = schema.safeParse({ ...BASE_CREATE, validation_timeout_seconds: bad });
      assert.equal(parsed.success, false, `${bad} should be rejected`);
    }
    for (const good of [30, 120, 600]) {
      const parsed = schema.safeParse({ ...BASE_CREATE, validation_timeout_seconds: good });
      assert.equal(parsed.success, true, `${good} should be accepted`);
    }
  });

  it("names the cost of enabling validation in the tool description", () => {
    // The operator decision on #1573 is that a FAIL records a status and
    // raises an alert rather than retrying. A caller choosing to enable this
    // is buying one extra execution per successful run, and the schema is
    // the only place they will read that.
    const { tools } = makeTools();
    // Read the description through the accessor, not JSON.stringify: zod 4
    // (dev) no longer serialises `description` into the schema JSON.
    const described = tools.createAgentSchedule.parameters.shape.validation_enabled.description ?? "";

    assert.match(described, /execution/i);
    assert.match(described, /not retry|does NOT retry/i);
  });
});

describe("#2759 update_agent_schedule carries the validation config", () => {
  const BASE_UPDATE = { agent_name: "worker", schedule_id: "sch_1" };

  it("sends the fields that were passed", async () => {
    const { tools, seen } = makeTools();

    await tools.updateAgentSchedule.execute(
      { ...BASE_UPDATE, validation_enabled: true, validation_timeout_seconds: 240 },
      { session: CTX },
    );

    assert.equal(seen.update?.validation_enabled, true);
    assert.equal(seen.update?.validation_timeout_seconds, 240);
  });

  it("omits what was not passed, so a stored value survives", async () => {
    // The handler's exclude_unset contract: an omitted field must not be sent
    // as undefined/null and clear what is stored.
    const { tools, seen } = makeTools();

    await tools.updateAgentSchedule.execute(
      { ...BASE_UPDATE, validation_enabled: false },
      { session: CTX },
    );

    assert.equal(seen.update?.validation_enabled, false);
    assert.ok(!("validation_prompt" in (seen.update ?? {})));
    assert.ok(!("validation_timeout_seconds" in (seen.update ?? {})));
  });

  it("can turn validation off, not only on", async () => {
    const { tools, seen } = makeTools();

    await tools.updateAgentSchedule.execute(
      { ...BASE_UPDATE, validation_enabled: false },
      { session: CTX },
    );

    assert.equal(seen.update?.validation_enabled, false);
  });

  it("rejects a timeout outside the documented 30-600 range", () => {
    const { tools } = makeTools();
    const schema = tools.updateAgentSchedule.parameters;

    assert.equal(schema.safeParse({ ...BASE_UPDATE, validation_timeout_seconds: 29 }).success, false);
    assert.equal(schema.safeParse({ ...BASE_UPDATE, validation_timeout_seconds: 601 }).success, false);
    assert.equal(schema.safeParse({ ...BASE_UPDATE, validation_timeout_seconds: 120 }).success, true);
  });
});
