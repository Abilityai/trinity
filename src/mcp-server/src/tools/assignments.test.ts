/**
 * trinity-enterprise#500 — the role-assignment read tool.
 *
 * Pins the tool-layer contract:
 *   - `get_agent_assignments` proxies `getAgentAssignments` and returns the
 *     backend payload verbatim on success;
 *   - every failure degrades to the `enabled:false` shape rather than throwing,
 *     because a thrown error reaches the agent as an opaque transport failure it
 *     cannot reason about;
 *   - the 404 message MERGES "no assignments module here" with "no such agent /
 *     no access". That merge is the point: the backend 404 is uniform for
 *     enumeration safety (Invariant #8), so a tool that claimed to tell them
 *     apart would be claiming a distinction it does not have.
 *
 * Drives the real tool execute() with a fake TrinityClient (requireApiKey=false
 * → getClient() returns the fake directly, the same seam as
 * credential_vault.test.ts).
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createAssignmentTools } from "./assignments.js";
import { ApiError } from "../client.js";
import type { TrinityClient } from "../client.js";

type Recorded = { method: string; args: unknown[] };

/** FastAPI serializes `HTTPException(status, detail=…)` as `{"detail": …}`, and
 *  ApiError keeps that raw body after the `API error (<status>): ` prefix. */
function apiError(status: number, detail: unknown): ApiError {
  return new ApiError(status, JSON.stringify({ detail }));
}

const ROSTER = {
  agent_name: "ops-companion",
  scope: "full",
  assignments: [
    {
      user_display: "A. Smith",
      role_id: "head-of-ops",
      kind: "primary",
      drift_state: "current",
      proactive_consent: false,
    },
  ],
  mine: null,
  primary: {
    user_display: "A. Smith",
    role_id: "head-of-ops",
    kind: "primary",
    drift_state: "current",
    proactive_consent: false,
  },
};

function makeTools(calls: Recorded[], overrides: Partial<TrinityClient> = {}) {
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    getAgentAssignments: async (name: string) => {
      calls.push({ method: "getAgentAssignments", args: [name] });
      return ROSTER;
    },
    ...overrides,
  };
  return createAssignmentTools(fake as TrinityClient, false);
}

describe("ent#500 assignments — proxy shape", () => {
  it("returns the backend payload verbatim", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const out = JSON.parse(
      await tools.getAgentAssignments.execute(
        { agent_name: "ops-companion" },
        {},
      ),
    );
    assert.equal(calls[0].method, "getAgentAssignments");
    assert.deepEqual(calls[0].args, ["ops-companion"]);
    assert.deepEqual(out, ROSTER);
  });

  it("is named get_agent_assignments and takes one agent_name", () => {
    const tools = makeTools([]);
    assert.equal(tools.getAgentAssignments.name, "get_agent_assignments");
    const shape = tools.getAgentAssignments.parameters;
    assert.ok(shape, "the tool declares a zod schema");
    const parsed = shape.safeParse({ agent_name: "x" });
    assert.equal(parsed.success, true);
    assert.equal(shape.safeParse({}).success, false);
  });
});

describe("ent#500 assignments — degradation never throws", () => {
  it("404 merges 'no module here' with 'no such agent / no access'", async () => {
    const tools = makeTools([], {
      getAgentAssignments: async () => {
        throw apiError(404, "Agent not found");
      },
    });
    const out = JSON.parse(
      await tools.getAgentAssignments.execute({ agent_name: "ghost" }, {}),
    );
    assert.equal(out.enabled, false);
    assert.equal(out.agent_name, "ghost");
    // Both halves stated — the tool must not pick one and imply the other is
    // ruled out, because the backend 404 genuinely covers both.
    assert.match(out.message, /does not have the assignments module/);
    assert.match(out.message, /does not exist or is not one you can read/);
  });

  it("403 reads as a licensing message, not as 'no such agent'", async () => {
    const tools = makeTools([], {
      getAgentAssignments: async () => {
        throw apiError(
          403,
          "Enterprise feature 'assignments' is not licensed for this instance.",
        );
      },
    });
    const out = JSON.parse(
      await tools.getAgentAssignments.execute({ agent_name: "ops" }, {}),
    );
    assert.equal(out.enabled, false);
    assert.match(out.message, /not licensed/);
    assert.doesNotMatch(out.message, /does not exist/);
  });

  it("a transport failure degrades rather than throwing", async () => {
    const tools = makeTools([], {
      getAgentAssignments: async () => {
        throw new Error("socket hang up");
      },
    });
    const out = JSON.parse(
      await tools.getAgentAssignments.execute({ agent_name: "ops" }, {}),
    );
    assert.equal(out.enabled, false);
    assert.match(out.message, /could not be reached/);
    assert.match(out.detail, /socket hang up/);
  });
});
