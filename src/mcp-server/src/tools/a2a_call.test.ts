/**
 * abilityai/trinity#736 — the outbound A2A runtime tools.
 *
 * Pins the tool-layer contract:
 *   - `call_a2a_agent` / `get_a2a_task` proxy the matching client method with
 *     the right args;
 *   - the target is an endpoint NAME and there is no URL parameter to pass;
 *   - `dedup_label` is required by the schema (its absence is the C2 replay
 *     bug: the guard keys on the endpoint and conversation, never the message);
 *   - the agent-scoped gate is SELF-ONLY, matching the backend exactly — not
 *     `{self} ∪ permitted`, which would deny a strict subset of what the
 *     backend denies and therefore block nothing while costing a round-trip;
 *   - errors become honest structured flags, and the tools NEVER throw;
 *   - a client abort reports `possibly_delivered`, because the credentialed
 *     call may well have completed on the remote.
 *
 * Drives the real tool execute() with a fake TrinityClient (requireApiKey=false
 * → getClient() returns the fake directly, the same seam as a2a.test.ts).
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createA2ACallTools } from "./a2a_call.js";
import { ApiError } from "../client.js";
import type { TrinityClient } from "../client.js";

type Recorded = { method: string; args: unknown[] };

function makeTools(calls: Recorded[], overrides: Partial<TrinityClient> = {}) {
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    callA2AAgent: async (name: string, body: unknown) => {
      calls.push({ method: "callA2AAgent", args: [name, body] });
      return { state: "completed", text: "the peer answered", task_id: "t-1", endpoint: "partner" };
    },
    getA2ATask: async (name: string, body: unknown) => {
      calls.push({ method: "getA2ATask", args: [name, body] });
      return { state: "working", task_id: "t-1", endpoint: "partner" };
    },
    getPermittedAgents: async (agent: string) => {
      calls.push({ method: "getPermittedAgents", args: [agent] });
      return ["sibling"];
    },
    ...overrides,
  };
  return createA2ACallTools(fake as TrinityClient, false);
}

const CALL_ARGS = {
  agent_name: "bot",
  endpoint: "partner",
  message: "delegate this",
  dedup_label: "step-1",
};

describe("#736 outbound tools — proxy shape", () => {
  it("call_a2a_agent forwards every parameter to the backend", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const out = JSON.parse(
      await tools.call_a2a_agent.execute(
        { ...CALL_ARGS, context_id: "c-1", task_id: "t-0", execution_id: "exec-1" },
        {},
      ),
    );
    assert.equal(calls[0].method, "callA2AAgent");
    assert.deepEqual(calls[0].args, [
      "bot",
      {
        endpoint: "partner",
        message: "delegate this",
        dedup_label: "step-1",
        context_id: "c-1",
        task_id: "t-0",
        execution_id: "exec-1",
      },
    ]);
    assert.equal(out.success, true);
    assert.equal(out.state, "completed");
    assert.equal(out.text, "the peer answered");
  });

  it("get_a2a_task forwards the endpoint and task id", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const out = JSON.parse(
      await tools.get_a2a_task.execute(
        { agent_name: "bot", endpoint: "partner", task_id: "t-1" },
        {},
      ),
    );
    assert.equal(calls[0].method, "getA2ATask");
    assert.deepEqual(calls[0].args, ["bot", { endpoint: "partner", task_id: "t-1" }]);
    assert.equal(out.state, "working");
  });
});

describe("#736 — the schema is the security boundary", () => {
  it("declares `endpoint` and NO url parameter", () => {
    const tools = makeTools([]);
    const shape = (tools.call_a2a_agent.parameters as any).shape;
    assert.ok(shape.endpoint, "endpoint must be a parameter");
    assert.equal(shape.agent_card_url, undefined, "a URL parameter is the SSRF primitive #736 removes");
    assert.equal(shape.url, undefined);
  });

  it("declares no `stream` parameter", () => {
    // A parameter that is accepted and silently does not stream is a lie in a
    // schema agents read.
    const tools = makeTools([]);
    const shape = (tools.call_a2a_agent.parameters as any).shape;
    assert.equal(shape.stream, undefined);
  });

  it("requires dedup_label", () => {
    const tools = makeTools([]);
    const parsed = (tools.call_a2a_agent.parameters as any).safeParse({
      agent_name: "bot",
      endpoint: "partner",
      message: "hi",
    });
    assert.equal(parsed.success, false, "dedup_label must be required — see the C2 replay bug");
  });

  it("tells the agent, in the description, that the target must be pre-registered", () => {
    const tools = makeTools([]);
    assert.match(tools.call_a2a_agent.description, /PRE-REGISTERED/);
    assert.match(tools.call_a2a_agent.description, /cannot supply a URL/);
  });
});

describe("#736 — agent-scoped gate is SELF-ONLY", () => {
  it("allows an agent key calling as itself", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const out = JSON.parse(
      await tools.call_a2a_agent.execute(CALL_ARGS, {
        session: { scope: "agent", agentName: "bot" } as any,
      }),
    );
    assert.equal(out.success, true);
  });

  it("denies an agent key calling as a DIFFERENT agent", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const out = JSON.parse(
      await tools.call_a2a_agent.execute(CALL_ARGS, {
        session: { scope: "agent", agentName: "other" } as any,
      }),
    );
    assert.equal(out.success, false);
    assert.equal(out.not_authorized, true);
    assert.equal(calls.length, 0, "denied calls must not reach the backend");
  });

  it("denies a PERMITTED sibling too — which is the point", async () => {
    // The backend route is self-only: an agent may spend only its OWN agent's
    // registered credential. A `{self} ∪ permitted` gate here would allow
    // `sibling` through only for the backend to 403 it, i.e. it would deny a
    // strict subset of what the backend denies — an inert check costing a
    // getPermittedAgents round-trip per call.
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const out = JSON.parse(
      await tools.call_a2a_agent.execute(
        { ...CALL_ARGS, agent_name: "sibling" },
        { session: { scope: "agent", agentName: "bot" } as any },
      ),
    );
    assert.equal(out.success, false);
    assert.equal(
      calls.filter((c) => c.method === "getPermittedAgents").length,
      0,
      "self-only must not consult the permission matrix at all",
    );
  });

  it("does not gate user- or system-scoped keys", async () => {
    for (const scope of ["user", "system"]) {
      const calls: Recorded[] = [];
      const tools = makeTools(calls);
      const out = JSON.parse(
        await tools.call_a2a_agent.execute(CALL_ARGS, { session: { scope } as any }),
      );
      assert.equal(out.success, true, `${scope} scope should pass to the backend`);
    }
  });

  it("gates get_a2a_task identically", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const out = JSON.parse(
      await tools.get_a2a_task.execute(
        { agent_name: "bot", endpoint: "partner", task_id: "t" },
        { session: { scope: "agent", agentName: "other" } as any },
      ),
    );
    assert.equal(out.not_authorized, true);
    assert.equal(calls.length, 0);
  });
});

describe("#736 — errors are honest structured flags, never throws", () => {
  const cases: Array<[number, string, string]> = [
    [403, "forbidden", "not_authorized"],
    [409, "duplicate in flight", "duplicate_in_flight"],
    [429, "rate limited", "rate_limited"],
    [502, "remote said no", "remote_error"],
    [504, "timed out", "timeout"],
    [422, "bad body", "invalid"],
  ];

  for (const [status, body, flag] of cases) {
    it(`maps HTTP ${status} → ${flag}`, async () => {
      const tools = makeTools([], {
        callA2AAgent: async () => {
          throw new ApiError(status, body);
        },
      });
      const out = JSON.parse(await tools.call_a2a_agent.execute(CALL_ARGS, {}));
      assert.equal(out.success, false);
      assert.equal(out[flag], true);
    });
  }

  it("distinguishes an unknown endpoint from a disabled feature — both are 404", async () => {
    // The two 404s need different operator actions: "register an endpoint"
    // versus "ask an admin to turn the feature on".
    const unknown = makeTools([], {
      callA2AAgent: async () => {
        throw new ApiError(404, JSON.stringify({ detail: { reason: "endpoint_not_found" } }));
      },
    });
    const off = makeTools([], {
      callA2AAgent: async () => {
        throw new ApiError(404, "Not found");
      },
    });
    const a = JSON.parse(await unknown.call_a2a_agent.execute(CALL_ARGS, {}));
    const b = JSON.parse(await off.call_a2a_agent.execute(CALL_ARGS, {}));
    assert.equal(a.endpoint_not_found, true);
    assert.equal(a.outbound_disabled, undefined);
    assert.equal(b.outbound_disabled, true);
  });

  it("reports possibly_delivered on a client abort", async () => {
    // We abort BEFORE the MCP gateway would, so the credentialed outbound call
    // may well have completed on the remote. An agent told only "failed" would
    // retry a side effect it already caused.
    const tools = makeTools([], {
      callA2AAgent: async () => {
        const e = new Error("The operation was aborted");
        e.name = "AbortError";
        throw e;
      },
    });
    const out = JSON.parse(await tools.call_a2a_agent.execute(CALL_ARGS, {}));
    assert.equal(out.timeout, true);
    assert.equal(out.possibly_delivered, true);
  });

  it("never throws, whatever the client does", async () => {
    const tools = makeTools([], {
      callA2AAgent: async () => {
        throw new Error("kaboom");
      },
      getA2ATask: async () => {
        throw new Error("kaboom");
      },
    });
    const a = await tools.call_a2a_agent.execute(CALL_ARGS, {});
    const b = await tools.get_a2a_task.execute(
      { agent_name: "bot", endpoint: "p", task_id: "t" }, {},
    );
    assert.equal(JSON.parse(a).success, false);
    assert.equal(JSON.parse(b).success, false);
  });
});

// ===========================================================================
// abilityai/trinity#3185 — the payment outcomes reach the agent as flags
//
// The backend answers a priced remote with HTTP 402 and
// `detail = {reason, message, payment, remote_status?, task_id?}`, and keeps a
// remote 403 / a refused token on 502 with a `reason`. What is pinned here is
// the mapper: the agent must be able to tell "a person has to buy this" from
// "the remote is down and a retry may work", because one of those is retryable
// and the other is a side effect nobody can spend their way out of.
// ===========================================================================
describe("#3185 payment outcomes", () => {
  const PAYMENT = {
    summary: { plan_id: "plan_42", scheme: "exact", credits_per_request: 1 },
    x402: { x402Version: 2, accepts: [{ scheme: "exact", planId: "plan_42" }] },
    truncated: false,
  };

  const failing = (status: number, body: string) =>
    makeTools([], {
      callA2AAgent: async () => {
        throw new ApiError(status, body);
      },
      getA2ATask: async () => {
        throw new ApiError(status, body);
      },
    });

  it("a 402 is payment_required, carries the requirements, and says do_not_retry", async () => {
    const tools = failing(402, JSON.stringify({
      detail: {
        reason: "payment_required",
        message: "This agent charges 1 credit per request.",
        payment: PAYMENT,
        remote_status: 402,
        task_id: "t-402",
      },
    }));
    const out = JSON.parse(await tools.call_a2a_agent.execute(CALL_ARGS, {}));
    assert.equal(out.success, false);
    assert.equal(out.payment_required, true);
    assert.equal(out.do_not_retry, true);
    assert.equal(out.task_id, "t-402");
    assert.equal(out.message, "This agent charges 1 credit per request.");
    assert.deepEqual(out.payment, PAYMENT);
    // Not a transport failure and not an access denial — those lead the agent
    // to retry or to re-route, and neither can ever succeed here.
    assert.equal(out.remote_error, undefined);
    assert.equal(out.not_authorized, undefined);
  });

  it("a 402 with a non-JSON body is still flagged payment_required", async () => {
    // A proxy or CDN in front of the remote answers its own 402 page. The status
    // is the fact; the body is a courtesy. Flagging only the parsable case would
    // hand the agent an opaque failure for the commonest deployment shape.
    const tools = failing(402, "<html>Payment Required</html>");
    const out = JSON.parse(await tools.call_a2a_agent.execute(CALL_ARGS, {}));
    assert.equal(out.payment_required, true);
    assert.equal(out.do_not_retry, true);
    assert.equal(out.payment, undefined);
  });

  it("a 502 naming rpc_forbidden is remote_forbidden, not a payment problem", async () => {
    const tools = failing(502, JSON.stringify({
      detail: { reason: "rpc_forbidden", message: "Remote refused", remote_status: 403 },
    }));
    const out = JSON.parse(await tools.call_a2a_agent.execute(CALL_ARGS, {}));
    assert.equal(out.remote_error, true);
    assert.equal(out.remote_forbidden, true);
    assert.equal(out.payment_rejected, undefined);
    assert.equal(out.payment_required, undefined);
  });

  it("a 502 naming payment_rejected is terminal, not a retryable remote error", async () => {
    // The stored token was refused. Retrying spends the same token against the
    // same refusal; an operator has to top up or re-paste.
    const tools = failing(502, JSON.stringify({
      detail: { reason: "payment_rejected", message: "Payment verification failed" },
    }));
    const out = JSON.parse(await tools.call_a2a_agent.execute(CALL_ARGS, {}));
    assert.equal(out.remote_error, true);
    assert.equal(out.payment_rejected, true);
    assert.equal(out.do_not_retry, true);
    assert.equal(out.remote_forbidden, undefined);
  });

  it("an ordinary 502 keeps today's single flag", async () => {
    const tools = failing(502, JSON.stringify({ detail: "Remote call failed" }));
    const out = JSON.parse(await tools.call_a2a_agent.execute(CALL_ARGS, {}));
    assert.equal(out.remote_error, true);
    assert.equal(out.remote_forbidden, undefined);
    assert.equal(out.payment_rejected, undefined);
    assert.equal(out.do_not_retry, undefined);
  });

  it("the poll path maps a 402 identically", async () => {
    // A priced task polled with no token answers the same way as the call that
    // started it; a poll that read as a plain failure would have the agent
    // keep polling a task that will never progress.
    const tools = failing(402, JSON.stringify({
      detail: { reason: "payment_required", message: "pay up", payment: PAYMENT },
    }));
    const out = JSON.parse(await tools.get_a2a_task.execute(
      { agent_name: "bot", endpoint: "partner", task_id: "t-1" }, {},
    ));
    assert.equal(out.payment_required, true);
    assert.equal(out.do_not_retry, true);
  });

  it("the description tells the agent to relay a payment_required once and stop", async () => {
    // The flag is only half the contract: an agent that reads `do_not_retry` but
    // was never told who CAN act will try a different endpoint instead.
    const tools = makeTools([]);
    const text = tools.call_a2a_agent.description;
    assert.match(text, /payment_required/);
    assert.match(text, /do not retry/i);
    assert.match(text, /person/i);
    assert.match(text, /task_id/);
  });
});
