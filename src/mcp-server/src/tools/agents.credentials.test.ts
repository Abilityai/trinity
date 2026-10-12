/**
 * #3435 — the three credential-portability tools say who may call them.
 *
 * `export_credentials` / `import_credentials` sit behind the backend's
 * `get_owned_agent_by_name` + `reject_agent_principal` (owner or admin, human
 * only); `get_credential_encryption_key` behind `require_admin` (admin, human
 * only). An agent-scoped key can never pass either gate, so:
 *
 *   - each tool carries its own `canAccess` ALLOW-list `{user, system}` — what
 *     is advertised equals what the backend admits, and an unknown scope fails
 *     closed (#848);
 *   - a backend 403 comes back as a typed `{success:false, …}` envelope stamped
 *     `denied` on the audit row (#2807) instead of a thrown string;
 *   - 400 / 404 / 503 stay thrown, unchanged (ruling TD-4).
 *
 * Drives the real tool execute() with a fake TrinityClient.
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it, beforeEach, afterEach } from "node:test";
import { strict as assert } from "node:assert";

import { createAgentTools } from "./agents.js";
import { ApiError, type TrinityClient } from "../client.js";
import { configureAudit, withAudit } from "../audit.js";

type Tool = {
  name: string;
  description: string;
  canAccess?: (auth: unknown) => boolean;
  execute: (a: unknown, c: unknown) => Promise<string>;
};

const TOOLS = ["export_credentials", "import_credentials", "get_credential_encryption_key"] as const;
const HUMAN_ONLY = "This operation is human-only; agent-scoped keys cannot perform it";

function build(answer: () => Promise<unknown>) {
  const calls: string[] = [];
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    exportCredentials: async (name: string) => { calls.push(`export:${name}`); return answer() as never; },
    importCredentials: async (name: string) => { calls.push(`import:${name}`); return answer() as never; },
    getEncryptionKey: async () => { calls.push("key"); return answer() as never; },
  };
  const all = createAgentTools(fake as TrinityClient, false) as unknown as Record<string, Tool>;
  const byName = Object.fromEntries(Object.values(all).map((t) => [t.name, t])) as Record<string, Tool>;
  return { byName, calls };
}

const args = (tool: string) => (tool === "get_credential_encryption_key" ? {} : { name: "acme-bot" });

describe("#3435 credential tools: advertisement", () => {
  const { byName } = build(async () => ({}));

  it("each tool declares its own canAccess", () => {
    for (const t of TOOLS) assert.equal(typeof byName[t].canAccess, "function", `${t} has no canAccess`);
  });

  it("is listed for user and system keys — what the backend admits", () => {
    for (const t of TOOLS) {
      for (const scope of ["user", "system"]) {
        assert.equal(byName[t].canAccess!({ scope }), true, `${t} must be listed for ${scope}`);
      }
    }
  });

  it("is hidden from agent, connector, anonymous, an unknown scope and no scope (fails closed)", () => {
    for (const t of TOOLS) {
      for (const auth of [{ scope: "agent" }, { scope: "connector" }, { scope: "anonymous" },
        { scope: "portal_delegate" }, { scope: "some_future_tier" }, {}, undefined, null]) {
        assert.equal(byName[t].canAccess!(auth), false, `${t} must be hidden for ${JSON.stringify(auth)}`);
      }
    }
  });

  it("each description names who may call it, and stays under the 2,048 cap", () => {
    for (const t of TOOLS) {
      assert.match(byName[t].description, /agent-scoped keys? (?:can never|cannot)/i, `${t}: no caller class`);
      assert.match(byName[t].description, /user-scoped or system-scoped key/i, `${t}: must name both admitted key scopes`);
      assert.ok(byName[t].description.length <= 2048, `${t}: ${byName[t].description.length} chars`);
    }
    assert.match(byName.get_credential_encryption_key.description, /administrator/i);
    for (const t of ["export_credentials", "import_credentials"]) {
      assert.match(byName[t].description, /owner or (?:an )?admin/i);
    }
  });
});

describe("#3435 credential tools: execution", () => {
  it("a user / system / keyless-dev session reaches the backend and gets the raw result back", async () => {
    for (const session of [{ scope: "user" }, { scope: "system" }, undefined]) {
      const { byName, calls } = build(async () => ({ status: "ok", files_imported: [".env"] }));
      for (const t of TOOLS) {
        const out = await byName[t].execute(args(t), { session });
        assert.deepEqual(JSON.parse(out), { status: "ok", files_imported: [".env"] });
      }
      assert.deepEqual(calls, ["export:acme-bot", "import:acme-bot", "key"]);
    }
  });

  it("a human-only 403 becomes a typed envelope, not a thrown string", async () => {
    const { byName } = build(async () => { throw new ApiError(403, JSON.stringify({ detail: HUMAN_ONLY })); });
    for (const t of TOOLS) {
      const out = JSON.parse(await byName[t].execute(args(t), { session: { scope: "user" } }));
      assert.equal(out.success, false);
      assert.equal(out.human_only, true);
      assert.equal(out.error, HUMAN_ONLY);
    }
  });

  it("an admin-gate 403 is admin_only; any other 403 is not_authorized", async () => {
    const admin = build(async () => { throw new ApiError(403, JSON.stringify({ detail: "Admin access required" })); });
    const out = JSON.parse(await admin.byName.get_credential_encryption_key.execute({}, { session: { scope: "user" } }));
    assert.deepEqual(out, { success: false, error: "Admin access required", admin_only: true });

    const other = build(async () => { throw new ApiError(403, JSON.stringify({ detail: "Forbidden" })); });
    const o2 = JSON.parse(await other.byName.import_credentials.execute(args("import_credentials"), {}));
    assert.deepEqual(o2, { success: false, error: "Forbidden", not_authorized: true });
  });

  it("a non-JSON 403 body is never copied into the envelope — a fixed sentence instead", async () => {
    const MARKER = "PROXY-PAGE-MARKER-3435";
    const body = `<html><body><h1>403 Forbidden</h1><p>${MARKER}</p></body></html>`;
    const { byName } = build(async () => { throw new ApiError(403, body); });
    for (const t of TOOLS) {
      const raw = await byName[t].execute(args(t), { session: { scope: "user" } });
      assert.ok(!raw.includes(MARKER), `${t}: the 403 body leaked into the envelope: ${raw}`);
      assert.deepEqual(JSON.parse(raw), { success: false, error: "Access denied (HTTP 403)", not_authorized: true });
    }
  });

  it("400, 404 and 503 stay thrown, and so does a non-API failure (TD-4)", async () => {
    for (const status of [400, 404, 503]) {
      const { byName } = build(async () => { throw new ApiError(status, JSON.stringify({ detail: "x" })); });
      for (const t of TOOLS) {
        await assert.rejects(() => byName[t].execute(args(t), {}), (e: unknown) => e instanceof ApiError && e.status === status);
      }
    }
    const { byName } = build(async () => { throw new Error("socket hang up"); });
    await assert.rejects(() => byName.export_credentials.execute(args("export_credentials"), {}), /socket hang up/);
  });
});

describe("#3435 a 403 refusal is audited as denied (#2807)", () => {
  const realFetch = globalThis.fetch;
  let rows: Array<{ details: Record<string, unknown> }>;

  beforeEach(() => {
    configureAudit({ apiUrl: "http://audit.test", secret: "test-secret" });
    rows = [];
    globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
      if (String(input).endsWith("/api/internal/audit")) {
        rows.push(JSON.parse(String(init?.body)));
        return new Response(JSON.stringify({ status: "logged" }), { status: 200 });
      }
      throw new Error(`unstubbed fetch: ${String(input)}`);
    }) as typeof fetch;
  });
  afterEach(() => {
    globalThis.fetch = realFetch;
  });

  it("the row says refused for each tool", async () => {
    const { byName } = build(async () => { throw new ApiError(403, JSON.stringify({ detail: HUMAN_ONLY })); });
    for (const t of TOOLS) {
      const wrapped = withAudit(t, byName[t].execute as never);
      await wrapped(args(t) as never, { session: { scope: "user", userId: "u1" } } as never);
    }
    assert.equal(rows.length, 3);
    for (const row of rows) {
      assert.equal(row.details.success, false, JSON.stringify(row.details));
      assert.equal(row.details.denied, true);
      assert.equal(row.details.error, HUMAN_ONLY);
    }
  });
});
