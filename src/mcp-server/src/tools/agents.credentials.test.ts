/**
 * #3435 — the three credential-portability tools say who may call them.
 *
 * `export_credentials` / `import_credentials` sit behind the backend's
 * `get_owned_agent_by_name` + `reject_agent_principal` (owner or admin, human
 * only); `get_credential_encryption_key` behind `require_admin` (admin, human
 * only). An agent-scoped key can never pass either gate, so:
 *
 *   - each tool carries its own `canAccess` ALLOW-list — what is advertised
 *     equals what the backend admits, and an unknown scope fails closed (#848):
 *     `{user, system}` for the key, `{user}` for export and no key for import
 *     since abilityai/trinity-enterprise#719;
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
import { HUMAN_ONLY_WHO_MAY_CALL } from "../access.js";

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

  it("the key is listed for user and system keys — what the backend admits", () => {
    for (const scope of ["user", "system"]) {
      assert.equal(byName.get_credential_encryption_key.canAccess!({ scope }), true, `the key must be listed for ${scope}`);
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
      assert.match(byName[t].description, /agent-scoped (?:and system-scoped )?keys? (?:can never|cannot)|no MCP key/i, `${t}: no caller class`);
      assert.ok(byName[t].description.length <= 2048, `${t}: ${byName[t].description.length} chars`);
    }
    assert.match(byName.get_credential_encryption_key.description, /administrator/i);
    assert.match(byName.get_credential_encryption_key.description, /user-scoped or system-scoped key/i);
    for (const t of ["export_credentials", "import_credentials"]) {
      assert.match(byName[t].description, /owner or (?:an )?admin/i);
    }
  });
});

describe("ent#719 credential tools: who is shown them", () => {
  // Since abilityai/trinity-enterprise#719 the backend's credential routes follow
  // the human-only rules: export is a person's call (a signed-in session or the
  // person's own user-scoped key), inject and import bind credentials (a
  // signed-in session only). Each tool's canAccess is the shared predicate for
  // its rule, so no key is shown a tool its scope can never call.
  const { byName } = build(async () => ({}));
  const SCOPES = ["user", "agent", "system", "connector", "anonymous", "portal_delegate", "ops", "some_future_tier"];
  const LISTED: Record<string, string[]> = {
    export_credentials: ["user"],
    inject_credentials: [],
    import_credentials: [],
    get_credential_encryption_key: ["user", "system"],
  };

  it("each tool is listed to exactly the scopes its route admits, and to nothing without a scope", () => {
    for (const [t, scopes] of Object.entries(LISTED)) {
      const shown = SCOPES.filter((scope) => byName[t].canAccess!({ scope }));
      assert.deepEqual(shown, scopes, `${t} is listed to [${shown}]`);
      for (const auth of [{}, undefined, null, { scope: 7 }]) {
        assert.equal(byName[t].canAccess!(auth), false, `${t} must be hidden for ${JSON.stringify(auth)}`);
      }
    }
  });

  it("each description carries the sentence for its rule", () => {
    assert.ok(byName.export_credentials.description.includes(HUMAN_ONLY_WHO_MAY_CALL.person));
    for (const t of ["inject_credentials", "import_credentials"]) {
      assert.ok(byName[t].description.includes(HUMAN_ONLY_WHO_MAY_CALL.interactive), t);
      assert.ok(byName[t].description.length <= 2048, `${t}: ${byName[t].description.length} chars`);
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

/**
 * trinity-enterprise#719 — the PERSON gate (`require_person`, #2996) refuses a
 * machine principal with an OBJECT detail, `{detail: {code: "person_required",
 * message}}`, where the older guard sent a string. The mapper reads the code,
 * so that refusal is `human_only`, not the generic `not_authorized`. The
 * INTERACTIVE gate (`require_interactive`) refuses every key-authenticated
 * caller with a STRING that has no code (`reject_non_interactive_principal`,
 * src/backend/dependencies.py); the mapper compares that whole sentence, so it
 * is `human_only` too.
 *
 * Driven through the production path: `requireApiKey` on, so the tool builds a
 * real TrinityClient from the session's key, and the ApiError comes from a real
 * 403 Response off a stubbed backend (`fetch`), not from a hand-thrown error.
 * The bodies are the bytes the real gates put on the wire.
 */
describe("ent#719 credential tools: both human-only gates' refusals map to human_only", () => {
  const BACKEND = "http://backend.test";
  const PERSON_REQUIRED = {
    code: "person_required",
    message: "This operation is human-only; agent- and system-scoped keys cannot perform it",
  };
  const INTERACTIVE = "This operation requires an interactive session; MCP API keys cannot perform it";
  const FIXED = "Access denied (HTTP 403)";
  const SESSION = { scope: "user", userId: "u1", keyName: "k", mcpApiKey: "trinity_mcp_test" };
  const PATHS: Record<string, string> = {
    export_credentials: "POST /api/agents/acme-bot/credentials/export",
    import_credentials: "POST /api/agents/acme-bot/credentials/import",
    get_credential_encryption_key: "GET /api/credentials/encryption-key",
  };

  type Seen = { line: string; auth: string | null };
  const AUDIT = "http://audit.test";
  const auditRows: Array<{ details: Record<string, unknown> }> = [];

  /** Run `fn` with `fetch` answering every backend call `status` + `body`. */
  async function withBackend<T>(status: number, body: string, fn: (seen: Seen[]) => Promise<T>): Promise<T> {
    const realFetch = globalThis.fetch;
    const seen: Seen[] = [];
    globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
      const url = String(input);
      if (url === `${AUDIT}/api/internal/audit`) {
        auditRows.push(JSON.parse(String(init?.body)));
        return new Response(JSON.stringify({ status: "logged" }), { status: 200 });
      }
      if (!url.startsWith(BACKEND)) throw new Error(`unstubbed fetch: ${url}`);
      const headers = (init?.headers ?? {}) as Record<string, string>;
      seen.push({ line: `${init?.method ?? "GET"} ${url.slice(BACKEND.length)}`, auth: headers.Authorization ?? null });
      return new Response(body, { status, headers: { "content-type": "application/json" } });
    }) as typeof fetch;
    try {
      return await fn(seen);
    } finally {
      globalThis.fetch = realFetch;
    }
  }

  function tools(): Record<string, Tool> {
    const base = { getBaseUrl: () => BACKEND } as Partial<TrinityClient>;
    const all = createAgentTools(base as TrinityClient, true) as unknown as Record<string, Tool>;
    return Object.fromEntries(Object.values(all).map((t) => [t.name, t])) as Record<string, Tool>;
  }

  /** Each tool's parsed envelope and raw text for one backend answer. */
  async function refuse(status: number, body: string) {
    return withBackend(status, body, async (seen) => {
      const byName = tools();
      const out: Record<string, { raw: string; env: Record<string, unknown> }> = {};
      for (const t of TOOLS) {
        const raw = await byName[t].execute(args(t), { session: SESSION });
        out[t] = { raw, env: JSON.parse(raw) };
      }
      return { out, seen };
    });
  }

  it("a 403 with detail.code person_required maps to human_only, with the backend's message", async () => {
    const { out, seen } = await refuse(403, JSON.stringify({ detail: PERSON_REQUIRED }));
    for (const t of TOOLS) {
      assert.deepEqual(out[t].env, { success: false, error: PERSON_REQUIRED.message, human_only: true }, t);
    }
    // The real client reached the real routes, authenticated with the session's key.
    assert.deepEqual(seen.map((s) => s.line), TOOLS.map((t) => PATHS[t]));
    for (const s of seen) assert.equal(s.auth, "Bearer trinity_mcp_test");
  });

  it("the code decides, not the wording", async () => {
    // ent#611's refusal carries the same code with a message that never says
    // "human-only"; it is the same rule, so it maps the same way.
    const message = "Only a person can answer or cancel an ask; agent- and system-scoped keys cannot end one";
    const { out } = await refuse(403, JSON.stringify({ detail: { code: "person_required", message } }));
    for (const t of TOOLS) assert.deepEqual(out[t].env, { success: false, error: message, human_only: true }, t);
  });

  it("a person_required detail without a string message keeps the fixed sentence, never the body", async () => {
    const MARKER = "OBJECT-MESSAGE-MARKER-719";
    for (const detail of [{ code: "person_required" }, { code: "person_required", message: { text: MARKER } }]) {
      const { out } = await refuse(403, JSON.stringify({ detail }));
      for (const t of TOOLS) {
        assert.ok(!out[t].raw.includes(MARKER), `${t}: the body leaked: ${out[t].raw}`);
        assert.deepEqual(out[t].env, { success: false, error: FIXED, human_only: true }, t);
      }
    }
  });

  it("a string detail maps by its wording", async () => {
    const cases: Array<[string, string]> = [
      ["This operation is human-only; agent-scoped keys cannot perform it", "human_only"],
      ["Admin access required", "admin_only"],
      // The INTERACTIVE gate's refusal to every key is a string with no code.
      // It is a human-only rule, so it is human_only, not the generic flag.
      [INTERACTIVE, "human_only"],
      // Only that whole sentence: a detail that shares its words keeps the
      // flag its own wording gives it.
      ["Admin access required; maintenance requires an interactive session", "admin_only"],
      ["Interactive sign-in is disabled on this instance", "not_authorized"],
      ["Forbidden", "not_authorized"],
    ];
    for (const [detail, flag] of cases) {
      const { out } = await refuse(403, JSON.stringify({ detail }));
      for (const t of TOOLS) assert.deepEqual(out[t].env, { success: false, error: detail, [flag]: true }, `${t}: ${detail}`);
    }
  });

  it("import_credentials: the INTERACTIVE gate's refusal is human_only, the owner's own user key included", async () => {
    // The import route takes `require_interactive`, which refuses every MCP key
    // with this string, the owner's `user` key too: the right person holding
    // the wrong credential class. The session's scope plays no part in the map.
    const env = await withBackend(403, JSON.stringify({ detail: INTERACTIVE }), async (seen) => {
      const raw = await tools().import_credentials.execute(args("import_credentials"), { session: SESSION });
      assert.deepEqual(seen, [{ line: PATHS.import_credentials, auth: "Bearer trinity_mcp_test" }]);
      return JSON.parse(raw);
    });
    assert.deepEqual(env, { success: false, error: INTERACTIVE, human_only: true });
  });

  it("an object detail with another code is not read as human-only", async () => {
    const detail = { code: "workspace_only", message: "This operation is human-only in wording only" };
    const { out } = await refuse(403, JSON.stringify({ detail }));
    for (const t of TOOLS) assert.deepEqual(out[t].env, { success: false, error: FIXED, not_authorized: true }, t);
  });

  it("a 403 without a JSON detail keeps the fixed sentence", async () => {
    const MARKER = "PROXY-PAGE-MARKER-719";
    for (const body of [
      `<html><body><h1>403 Forbidden</h1><p>${MARKER} person_required</p></body></html>`,
      JSON.stringify({ error: `${MARKER} person_required` }),
      JSON.stringify({ code: "person_required", message: MARKER }),
    ]) {
      const { out } = await refuse(403, body);
      for (const t of TOOLS) {
        assert.ok(!out[t].raw.includes(MARKER), `${t}: the body leaked: ${out[t].raw}`);
        assert.deepEqual(out[t].env, { success: false, error: FIXED, not_authorized: true }, `${t}: ${body}`);
      }
    }
  });

  it("other shape — a 403 with an empty body keeps the fixed sentence", async () => {
    const { out, seen } = await refuse(403, "");
    assert.equal(seen.length, TOOLS.length);
    for (const t of TOOLS) assert.deepEqual(out[t].env, { success: false, error: FIXED, not_authorized: true }, t);
  });

  it("person_required on a status other than 403 is still thrown (TD-4)", async () => {
    for (const status of [400, 404]) {
      await withBackend(status, JSON.stringify({ detail: PERSON_REQUIRED }), async () => {
        const byName = tools();
        for (const t of TOOLS) {
          await assert.rejects(
            () => byName[t].execute(args(t), { session: SESSION }),
            (e: unknown) => e instanceof ApiError && e.status === status,
            `${t}: ${status}`,
          );
        }
      });
    }
  });

  it("the refusal is audited as denied, with the backend's message (#2807)", async () => {
    configureAudit({ apiUrl: AUDIT, secret: "test-secret" });
    auditRows.length = 0;
    await withBackend(403, JSON.stringify({ detail: PERSON_REQUIRED }), async () => {
      const byName = tools();
      for (const t of TOOLS) {
        await withAudit(t, byName[t].execute as never)(args(t) as never, { session: SESSION } as never);
      }
      await new Promise((resolve) => setImmediate(resolve)); // the audit post is fire-and-forget
    });
    assert.equal(auditRows.length, TOOLS.length);
    for (const row of auditRows) {
      assert.equal(row.details.success, false, JSON.stringify(row.details));
      assert.equal(row.details.denied, true);
      assert.equal(row.details.error, PERSON_REQUIRED.message);
    }
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
