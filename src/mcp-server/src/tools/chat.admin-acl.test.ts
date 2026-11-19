/**
 * #4654 — MCP chat tools ACL must gate on the admin ROLE, not the username "admin".
 *
 * Pins:
 *   - A user with username "admin" but role "user" is denied access to a
 *     foreign, non-shared agent (no username-collision bypass).
 *   - A user with a renamed account (e.g. "superadmin") and role "admin" is
 *     allowed access to a foreign, non-shared agent (renamed admin retains access).
 */
import { describe, it, beforeEach, afterEach } from "node:test";
import { strict as assert } from "node:assert";

import { TrinityClient } from "../client.js";
import { createChatTools } from "./chat.js";

const realFetch = globalThis.fetch;

function stubDispatchOk() {
  globalThis.fetch = (async () =>
    new Response(JSON.stringify({ status: "accepted", execution_id: "ex-1" }), {
      status: 200,
      headers: { "content-type": "application/json" },
    })
  ) as typeof fetch;
}

function makeClient(agentOwner = "other-owner"): TrinityClient {
  const client = new TrinityClient("http://backend:8000", "tok");
  (client as any).getAgentAccessInfo = async () => ({
    name: "target",
    owner: agentOwner,
    is_shared: false,
  });
  return client;
}

function tools(client: TrinityClient) {
  return createChatTools(client, false, false);
}

describe("#4654 chat ACL: admin role check, not username check", () => {
  beforeEach(() => { globalThis.fetch = realFetch; });
  afterEach(() => { globalThis.fetch = realFetch; });

  it("user with username 'admin' but role 'user' is denied access to a foreign agent", async () => {
    const session = {
      session: {
        scope: "user",
        userId: "admin",
        userRole: "user",
        keyId: "k1",
        keyName: "kn",
      },
    };
    const result = JSON.parse(
      await tools(makeClient()).chatWithAgent.execute(
        { agent_name: "target", message: "hi" },
        session as any,
      ),
    );
    assert.ok(result.error || result.reason, "expected an access denial");
    assert.match(
      JSON.stringify(result),
      /not found or not accessible|Access denied|access/i,
    );
  });

  it("user with role 'admin' and a non-'admin' username is allowed access to a foreign agent", async () => {
    stubDispatchOk();
    const session = {
      session: {
        scope: "user",
        userId: "superadmin",
        userRole: "admin",
        keyId: "k1",
        keyName: "kn",
      },
    };
    const result = JSON.parse(
      await tools(makeClient()).chatWithAgent.execute(
        { agent_name: "target", message: "hi" },
        session as any,
      ),
    );
    // Should reach dispatch, not hit the access denial
    assert.ok(!result.error, `expected dispatch, got: ${JSON.stringify(result)}`);
  });
});
