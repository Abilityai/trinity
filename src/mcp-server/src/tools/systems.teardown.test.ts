/**
 * ent#454 — `teardown_system` tool contract.
 *
 * Four things live in tool CODE (not the backend, not the model) and are
 * therefore pinned here:
 *
 * 1. **`dry_run` defaults to TRUE.** The asymmetry with `deploy_system` (which
 *    defaults false) is the whole safety property of this tool: an unwanted
 *    preview costs a round trip, an unwanted execute costs a deleted fleet. An
 *    `|| false` typo would silently invert it, and nothing else would notice.
 * 2. **The verb, the gated path, and the confirmed set in the BODY.** A DELETE
 *    body is unusual enough that a well-meaning refactor could move `agents`
 *    into the query string, which would break the server's time-of-check /
 *    time-of-use re-validation.
 * 3. **It never throws.** A thrown error reaches the agent as an opaque
 *    transport failure it cannot reason about, so every refusal comes back as a
 *    parseable object.
 * 4. **The three refusals stay distinct.** 404 (no such build), 403 (not
 *    licensed / not human) and 503 (unverified membership — retryable) are
 *    different operator situations; flattening them is how a human on a user
 *    key gets told a licensed feature "doesn't exist".
 *
 * Drives the real tool execute() with a fake TrinityClient
 * (requireApiKey=false → getClient() returns the fake directly — the
 * agents.deploy.test.ts / git.test.ts seam).
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createSystemTools } from "./systems.js";
import { ApiError } from "../client.js";
import type { TrinityClient } from "../client.js";

interface RecordedCall {
  method: string;
  path: string;
  body: unknown;
}

function makeTools(calls: RecordedCall[], respond?: () => unknown) {
  const fake = {
    getBaseUrl: () => "http://backend",
    setToken: () => {},
    request: async (method: string, path: string, body: unknown) => {
      calls.push({ method, path, body });
      if (respond) return respond();
      return { status: "preview", system_name: "acme", dry_run: true };
    },
  } as unknown as TrinityClient;
  return createSystemTools(fake, false);
}

// `new ApiError(status, body)` — that argument order, and the real `body`
// field, are the contract the tool reads. Constructing it by hand rather than
// hand-rolling a `{status}` object is the point: a fake that carried only a
// status would pass while the tool's body parsing was broken.
function failWith(status: number, body: string) {
  return () => {
    throw new ApiError(status, body);
  };
}

describe("teardown_system — the request it actually sends", () => {
  it("previews by default, without being asked", async () => {
    const calls: RecordedCall[] = [];
    const tools = makeTools(calls);

    await tools.teardownSystem.execute({ system_name: "acme" }, {});

    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, "DELETE");
    assert.equal(
      calls[0].path,
      "/api/enterprise/system-teardown/acme?dry_run=true",
      "dry_run must default to TRUE — the opposite of deploy_system"
    );
  });

  it("executes only when dry_run is explicitly false", async () => {
    const calls: RecordedCall[] = [];
    const tools = makeTools(calls);

    await tools.teardownSystem.execute(
      { system_name: "acme", dry_run: false, agents: ["acme-web", "acme-db"] },
      {}
    );

    assert.ok(calls[0].path.endsWith("?dry_run=false"));
    assert.deepEqual(calls[0].body, {
      agents: ["acme-web", "acme-db"],
      strict: false,
    });
  });

  it("sends the confirmed set in the BODY, never the query string", async () => {
    const calls: RecordedCall[] = [];
    const tools = makeTools(calls);

    await tools.teardownSystem.execute(
      { system_name: "acme", dry_run: false, agents: ["acme-web"] },
      {}
    );

    assert.ok(
      !calls[0].path.includes("acme-web"),
      "the confirmed set is the TOCTOU seam the server re-validates; it belongs "
        + "in the body"
    );
    assert.deepEqual((calls[0].body as { agents: unknown }).agents, ["acme-web"]);
  });

  it("distinguishes 'remove every member' from 'remove nothing'", async () => {
    const calls: RecordedCall[] = [];
    const tools = makeTools(calls);

    await tools.teardownSystem.execute({ system_name: "acme", dry_run: false }, {});
    assert.equal(
      (calls[0].body as { agents: unknown }).agents,
      null,
      "omitting `agents` must send null (= all current members), not [] — an "
        + "empty array is 'confirm nothing', which the server rejects as a 400"
    );

    await tools.teardownSystem.execute(
      { system_name: "acme", dry_run: false, agents: [] },
      {}
    );
    assert.deepEqual((calls[1].body as { agents: unknown }).agents, []);
  });

  it("url-encodes the system name", async () => {
    const calls: RecordedCall[] = [];
    const tools = makeTools(calls);
    await tools.teardownSystem.execute({ system_name: "a/b c" }, {});
    assert.ok(calls[0].path.startsWith("/api/enterprise/system-teardown/a%2Fb%20c?"));
  });

  it("passes strict through", async () => {
    const calls: RecordedCall[] = [];
    const tools = makeTools(calls);
    await tools.teardownSystem.execute(
      { system_name: "acme", dry_run: false, strict: true },
      {}
    );
    assert.equal((calls[0].body as { strict: unknown }).strict, true);
  });
});

describe("teardown_system — it degrades, it does not throw", () => {
  it("reports an OSS build as unavailable, not as a failure", async () => {
    const tools = makeTools([], failWith(404, '{"detail":"Not Found"}'));
    const out = JSON.parse(
      await tools.teardownSystem.execute({ system_name: "acme" }, {})
    );
    assert.equal(out.success, false);
    assert.equal(out.not_available, true);
    // The actionable alternative has to be in the message: the agent can still
    // remove agents one at a time.
    assert.match(out.error, /DELETE \/api\/agents/);
  });

  it("reports a 403 as a licensing/human-only refusal", async () => {
    const tools = makeTools(
      [],
      failWith(403, '{"detail":"Enterprise feature \'system_teardown\' is not licensed"}')
    );
    const out = JSON.parse(
      await tools.teardownSystem.execute({ system_name: "acme" }, {})
    );
    assert.equal(out.not_permitted, true);
    assert.equal(out.not_available, undefined, "403 is not 404 — do not conflate");
  });

  it("marks an unverified-membership refusal RETRYABLE and says nothing was removed", async () => {
    const tools = makeTools(
      [],
      failWith(503, '{"detail":"System membership could not be verified"}')
    );
    const out = JSON.parse(
      await tools.teardownSystem.execute({ system_name: "acme", dry_run: false }, {})
    );
    assert.equal(out.membership_unverified, true);
    assert.equal(out.retryable, true);
    assert.match(out.error, /[Nn]othing was removed/);
  });

  it("keeps a `failed` report that arrives as a 500 — it is a result, not an error", async () => {
    const report = {
      status: "failed",
      system_name: "acme",
      members: [{ name: "acme-web", outcome: "failed", reason: "docker daemon gone" }],
    };
    const tools = makeTools([], failWith(500, JSON.stringify(report)));
    const out = JSON.parse(
      await tools.teardownSystem.execute({ system_name: "acme", dry_run: false }, {})
    );
    assert.equal(out.status, "failed");
    assert.equal(
      out.members[0].reason,
      "docker daemon gone",
      "a naive catch throws away the per-member reasons, which are the only "
        + "actionable output of a failed teardown"
    );
  });

  it("still returns an object for an unrecognised failure", async () => {
    const tools = makeTools([], () => {
      throw new Error("socket hang up");
    });
    const out = JSON.parse(
      await tools.teardownSystem.execute({ system_name: "acme" }, {})
    );
    assert.equal(out.success, false);
    assert.match(out.detail, /socket hang up/);
  });
});

describe("teardown_system — what the description promises the model", () => {
  const tools = makeTools([]);
  const d: string = tools.teardownSystem.description;

  it("is named as destructive and preview-first", () => {
    assert.match(d, /DESTRUCTIVE/);
    assert.match(d, /dry_run defaults to TRUE/);
  });

  it("tells the model to switch on status, not the HTTP code", () => {
    assert.match(d, /switch on `status`/);
    assert.match(d, /never the HTTP code/);
  });

  it("warns that `prefix` evidence needs a human", () => {
    assert.match(d, /evidence/);
    assert.match(d, /NAME ONLY/);
  });

  it("states the recovery window AND that ghosts have none", () => {
    assert.match(d, /180 days/);
    assert.match(d, /NO recovery window/);
  });

  it("states it is human-only, so a model does not retry an agent-key 403", () => {
    assert.match(d, /HUMAN caller/);
  });

  it("warns off list_systems as a name source", () => {
    assert.match(
      tools.teardownSystem.parameters.shape.system_name.description ?? "",
      /list_systems/,
      "GET /api/systems groups by the last hyphen and can report a name that is "
        + "not a real system — feeding it here would target nothing, or worse"
    );
  });
});
