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
  });
});

describe("ent#500 assignments — no raw error text reaches the agent ([I3])", () => {
  // The reader is a model. `message` is a curated sentence per status; the raw
  // error text is whatever the backend or the transport produced — a response
  // body, an internal URL — and the fallback branch is exactly where the
  // unexpected shape lands. None of it may be echoed.
  const LEAK = "http://backend.internal:8000/api/enterprise/assignments trace=0xDEADBEEF";

  for (const [label, err] of [
    ["an unexpected status", apiError(502, LEAK)],
    ["a 404", apiError(404, LEAK)],
    ["a 403", apiError(403, LEAK)],
    ["a transport error", new Error(LEAK)],
  ] as const) {
    it(`${label} degrades without echoing the raw error`, async () => {
      const tools = makeTools([], {
        getAgentAssignments: async () => {
          throw err;
        },
      });
      const raw = await tools.getAgentAssignments.execute({ agent_name: "ops" }, {});
      const out = JSON.parse(raw);
      assert.equal(out.enabled, false);
      assert.equal("detail" in out, false, "no raw `detail` field");
      assert.doesNotMatch(raw, /backend\.internal|DEADBEEF/);
    });
  }
});

describe("ent#500 assignments — an agent key reads only its OWN roster ([I2])", () => {
  // The description promises "an agent may only read its own roster". The
  // backend enforces it; this layer enforces it too, deliberately redundant
  // (the a2a_call.ts checkSelf precedent), so the promise does not depend on a
  // private repo remembering it.
  it("allows an agent key reading its own roster", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const out = JSON.parse(
      await tools.getAgentAssignments.execute(
        { agent_name: "ops-companion" },
        { session: { scope: "agent", agentName: "ops-companion" } as any },
      ),
    );
    assert.equal(out.agent_name, "ops-companion");
    assert.equal(calls.length, 1);
  });

  it("denies an agent key reading ANOTHER agent's roster, without a backend call", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const out = JSON.parse(
      await tools.getAgentAssignments.execute(
        { agent_name: "ops-companion" },
        { session: { scope: "agent", agentName: "sales-bot" } as any },
      ),
    );
    assert.equal(out.not_authorized, true);
    assert.equal(calls.length, 0, "a denied read must not reach the backend");
    assert.doesNotMatch(JSON.stringify(out), /A\. Smith/);
  });

  it("denies an agent key that carries no agent name (fails closed)", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const out = JSON.parse(
      await tools.getAgentAssignments.execute(
        { agent_name: "ops-companion" },
        { session: { scope: "agent" } as any },
      ),
    );
    assert.equal(out.not_authorized, true);
    assert.equal(calls.length, 0);
  });

  it("is declared an in-tool gate in the access policy, not a baseline", async () => {
    const { TOOL_ACCESS_POLICY } = await import("../access.js");
    assert.equal(TOOL_ACCESS_POLICY.get_agent_assignments.kind, "in-tool");
  });

  it("leaves user and system keys to the backend", async () => {
    for (const scope of ["user", "system"]) {
      const calls: Recorded[] = [];
      const tools = makeTools(calls);
      await tools.getAgentAssignments.execute(
        { agent_name: "ops-companion" },
        { session: { scope, agentName: scope === "system" ? "trinity-system" : undefined } as any },
      );
      assert.equal(calls.length, 1, `${scope} must reach the backend`);
    }
  });
});
