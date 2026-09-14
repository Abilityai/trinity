/**
 * trinity-enterprise#279 — the credential-vault runtime tools.
 *
 * Pins the tool-layer contract:
 *   - `list_available_credentials` proxies `getAvailableCredentials` and returns
 *     the `enabled:true` shape on success, degrading to the `enabled:false`
 *     shape (the `list_runnable_skills` contract) on any error rather than
 *     throwing;
 *   - `fetch_credential` proxies `fetchCredential` and returns the value on
 *     success, or the outbound-A2A `{success:false, error, ...flags}` shape on
 *     error, NEVER a throw;
 *   - the branch keys on the detail SHAPE, not the status: a dict-with-code 403
 *     (an ungranted name) and a plain-string 403 (an unlicensed build) are the
 *     same status but different situations, and the tool tells them apart.
 *
 * Drives the real tool execute() with a fake TrinityClient (requireApiKey=false
 * → getClient() returns the fake directly, the same seam as a2a_call.test.ts).
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createCredentialVaultTools } from "./credential_vault.js";
import { ApiError } from "../client.js";
import type { TrinityClient } from "../client.js";

type Recorded = { method: string; args: unknown[] };

/** FastAPI serializes `HTTPException(status, detail=…)` as `{"detail": …}`, and
 *  ApiError keeps that raw body after the `API error (<status>): ` prefix. */
function apiError(status: number, detail: unknown): ApiError {
  return new ApiError(status, JSON.stringify({ detail }));
}

function makeTools(calls: Recorded[], overrides: Partial<TrinityClient> = {}) {
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    getAvailableCredentials: async () => {
      calls.push({ method: "getAvailableCredentials", args: [] });
      return [
        { name: "openai", description: "OpenAI API key", kind: "secret" },
        { name: "db-dsn", description: null, kind: "secret" },
      ];
    },
    fetchCredential: async (body: unknown) => {
      calls.push({ method: "fetchCredential", args: [body] });
      return { name: "openai", kind: "secret", value: "sk-super-secret-value" };
    },
    ...overrides,
  };
  return createCredentialVaultTools(fake as TrinityClient, false);
}

describe("#279 credential vault — proxy shape", () => {
  it("list_available_credentials returns the enabled:true shape", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const out = JSON.parse(await tools.list_available_credentials.execute({}, {}));
    assert.equal(calls[0].method, "getAvailableCredentials");
    assert.equal(out.enabled, true);
    assert.equal(out.count, 2);
    assert.equal(out.credentials[0].name, "openai");
    // Discovery never carries a value.
    assert.equal(out.credentials[0].value, undefined);
  });

  it("fetch_credential forwards name + execution_id and returns the value", async () => {
    const calls: Recorded[] = [];
    const tools = makeTools(calls);
    const out = JSON.parse(
      await tools.fetch_credential.execute(
        { name: "openai", execution_id: "exec-1" },
        {},
      ),
    );
    assert.equal(calls[0].method, "fetchCredential");
    assert.deepEqual(calls[0].args, [{ name: "openai", execution_id: "exec-1" }]);
    assert.equal(out.success, true);
    assert.equal(out.name, "openai");
    assert.equal(out.value, "sk-super-secret-value");
  });

  it("fetch_credential requires a name", () => {
    const tools = makeTools([]);
    const parsed = (tools.fetch_credential.parameters as any).safeParse({
      execution_id: "exec-1",
    });
    assert.equal(parsed.success, false, "name must be required");
  });
});

describe("#279 — list degrades honestly, never throws", () => {
  it("404 → not available on this platform (OSS build)", async () => {
    const tools = makeTools([], {
      getAvailableCredentials: async () => {
        throw new ApiError(404, "Not Found");
      },
    });
    const out = JSON.parse(await tools.list_available_credentials.execute({}, {}));
    assert.equal(out.enabled, false);
    assert.equal(out.count, 0);
    assert.deepEqual(out.credentials, []);
    assert.match(out.message, /not available on this platform/i);
  });

  it("403 agent_key_required → an agent-context hint, NOT 'feature missing' (D25)", async () => {
    // A human operator on a user-scoped key must never be told an entitled
    // feature does not exist.
    const tools = makeTools([], {
      getAvailableCredentials: async () => {
        throw apiError(403, {
          code: "agent_key_required",
          message: "These tools act as an agent; call them from an agent context.",
        });
      },
    });
    const out = JSON.parse(await tools.list_available_credentials.execute({}, {}));
    assert.equal(out.enabled, false);
    assert.match(out.message, /agent context/i);
    assert.doesNotMatch(out.message, /not licensed|not available/i);
  });

  it("403 entitlement string → not licensed for this instance", async () => {
    const tools = makeTools([], {
      getAvailableCredentials: async () => {
        throw apiError(
          403,
          "Enterprise feature 'credential_vault' is not licensed for this instance.",
        );
      },
    });
    const out = JSON.parse(await tools.list_available_credentials.execute({}, {}));
    assert.equal(out.enabled, false);
    assert.match(out.message, /not licensed/i);
  });
});

describe("#279 — fetch errors are honest structured flags, never throws", () => {
  async function run(
    err: unknown,
    overrides: Partial<TrinityClient> = {},
  ): Promise<any> {
    const tools = makeTools([], {
      fetchCredential: async () => {
        throw err;
      },
      ...overrides,
    });
    return JSON.parse(
      await tools.fetch_credential.execute({ name: "openai" }, {}),
    );
  }

  it("403 dict vault_not_granted → not_granted (deny-by-default)", async () => {
    const out = await run(
      apiError(403, {
        code: "vault_not_granted",
        message: "You are not granted access to that credential, or it does not exist.",
      }),
    );
    assert.equal(out.success, false);
    assert.equal(out.not_granted, true);
    assert.equal(out.not_entitled, undefined);
  });

  it("403 entitlement STRING → not_entitled (unlicensed build)", async () => {
    const out = await run(
      apiError(403, "Enterprise feature 'credential_vault' is not licensed."),
    );
    assert.equal(out.success, false);
    assert.equal(out.not_entitled, true);
    assert.equal(out.not_granted, undefined);
  });

  it("distinguishes the two 403 detail SHAPES — the whole point (D31)", async () => {
    // Same status, different operator situation: an ungranted NAME vs an
    // unlicensed INSTANCE. The branch keys on the detail shape, not the status.
    const granted = await run(
      apiError(403, { code: "vault_not_granted", message: "no" }),
    );
    const entitled = await run(apiError(403, "not licensed"));
    assert.equal(granted.not_granted, true);
    assert.equal(granted.not_entitled, undefined);
    assert.equal(entitled.not_entitled, true);
    assert.equal(entitled.not_granted, undefined);
  });

  it("403 dict agent_key_required → its own flag + message", async () => {
    const out = await run(
      apiError(403, {
        code: "agent_key_required",
        message: "These tools act as an agent; call them from an agent context.",
      }),
    );
    assert.equal(out.agent_key_required, true);
    assert.match(out.error, /agent context/i);
  });

  it("403 dict vault_approval_required → approval_required", async () => {
    const out = await run(
      apiError(403, { code: "vault_approval_required", message: "needs approval" }),
    );
    assert.equal(out.approval_required, true);
  });

  it("404 → not_available (OSS build)", async () => {
    const out = await run(new ApiError(404, "Not Found"));
    assert.equal(out.not_available, true);
  });

  it("429 dict vault_rate_limited → rate_limited + a retry hint", async () => {
    const out = await run(
      apiError(429, { code: "vault_rate_limited", message: "Too many credential fetches; slow down." }),
    );
    assert.equal(out.rate_limited, true);
    assert.ok(out.retry_hint, "should carry a retry hint");
  });

  it("503 vault_staging_unavailable → staging_unavailable + retryable", async () => {
    const out = await run(
      apiError(503, { code: "vault_staging_unavailable", message: "Secret staging is unavailable; the fetch was refused." }),
    );
    assert.equal(out.staging_unavailable, true);
    assert.equal(out.retryable, true);
  });

  it("503 vault_decrypt_failed → decrypt_failed + admin_action_required (NOT retryable)", async () => {
    const out = await run(
      apiError(503, { code: "vault_decrypt_failed", message: "This credential could not be decrypted. An admin must run vault rewrap." }),
    );
    assert.equal(out.decrypt_failed, true);
    assert.equal(out.admin_action_required, true);
    assert.equal(out.retryable, undefined);
  });

  it("503 vault_backend_unavailable → backend_unavailable + retryable", async () => {
    const out = await run(
      apiError(503, { code: "vault_backend_unavailable", message: "The credential backend is unavailable. Retry shortly." }),
    );
    assert.equal(out.backend_unavailable, true);
    assert.equal(out.retryable, true);
  });

  it("never throws, whatever the client does", async () => {
    const a = await run(new Error("kaboom"));
    assert.equal(a.success, false);
    // A non-ApiError still degrades to a structured failure.
    assert.ok(typeof a.error === "string");
  });

  it("value-free error path — the plaintext never rides an error message", async () => {
    // Backend VaultError.detail is value-free by construction; assert the tool
    // does not synthesize a value into the failure shape either.
    const out = await run(
      apiError(403, { code: "vault_not_granted", message: "You are not granted access." }),
    );
    assert.equal(out.value, undefined);
    assert.equal(out.success, false);
  });
});
