/**
 * #4654 — the key owner's role reaches the auth context.
 *
 * The chat ACL's admin bypass reads `authContext.userRole`. Its only source is
 * `user_role` on the backend's `/api/mcp/validate` response, copied across by
 * the `authenticate` callback in server.ts. `chat.admin-acl.test.ts` hand-builds
 * a session, so it cannot see that copy: without it every admin loses the
 * bypass while that suite and `tsc` stay green.
 *
 * This drives the REAL callback — the one `createServer` hands FastMCP, read
 * back off `server.options.authenticate` — against a stubbed validate response.
 * The backend half (the response carrying the field) is pinned in
 * `tests/unit/test_4654_mcp_validate_user_role.py`.
 */
import { strict as assert } from "node:assert";
import { afterEach, describe, it } from "node:test";

import { createServer } from "./server.js";

const realFetch = globalThis.fetch;
const BACKEND = "http://backend.invalid:8000";

function stubBackend(validateBody: Record<string, unknown>) {
  globalThis.fetch = (async (input: any) => {
    const url = typeof input === "string" ? input : input.url ?? String(input);
    const body = url === `${BACKEND}/api/mcp/validate` ? validateBody : {};
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }) as typeof fetch;
}

async function authenticateWith(validateBody: Record<string, unknown>): Promise<any> {
  stubBackend(validateBody);
  const { server } = await createServer({ trinityApiUrl: BACKEND, requireApiKey: true });
  const authenticate = (server as any).options.authenticate;
  assert.equal(typeof authenticate, "function", "key mode must install an authenticate callback");
  return authenticate({ headers: { authorization: "Bearer trinity_mcp_test" } });
}

const validated = { valid: true, key_id: "k1", key_name: "laptop", user_id: "root-person" };

describe("#4654 authenticate copies the validated owner role into the auth context", () => {
  afterEach(() => { globalThis.fetch = realFetch; });

  it("a user-scoped key owned by an admin carries userRole 'admin'", async () => {
    const auth = await authenticateWith({ ...validated, scope: "user", user_role: "admin" });
    assert.equal(auth.scope, "user");
    assert.equal(auth.userId, "root-person");
    assert.equal(auth.userRole, "admin");
  });

  it("the role is copied as sent, not derived from the username", async () => {
    const auth = await authenticateWith({ ...validated, user_id: "admin", scope: "user", user_role: "user" });
    assert.equal(auth.userId, "admin");
    assert.equal(auth.userRole, "user");
  });

  it("an agent-scoped key carries its OWNER's role (callers must check scope first)", async () => {
    const auth = await authenticateWith({ ...validated, scope: "agent", agent_name: "atlas", user_role: "admin" });
    assert.equal(auth.scope, "agent");
    assert.equal(auth.userRole, "admin");
  });

  it("a response without user_role yields undefined, never a default role", async () => {
    const auth = await authenticateWith({ ...validated, scope: "user" });
    assert.equal(auth.scope, "user");
    assert.equal(auth.userRole, undefined);
  });
});
