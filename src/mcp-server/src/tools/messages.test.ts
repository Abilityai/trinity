/**
 * trinity-enterprise#1221 — send_message tells the agent what happened.
 *
 * Pins the tool-layer contract:
 *   - a structured refusal (`detail: {code, message, channels?}`) reaches the
 *     agent as that code and message, with the per-channel reasons — never the
 *     old hardcoded "They may not have Telegram or Slack configured";
 *   - the structured branch runs BEFORE the substring checks, and the
 *     agent-access denial (a plain-string 404 "Agent not found") is still
 *     reported as not_authorized;
 *   - a Workspace delivery says nothing notifies the person.
 *
 * Drives the real tool execute() with a fake TrinityClient (requireApiKey=false
 * → getClient() returns the fake directly, the same seam as assignments.test.ts).
 *
 * Runner: node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createMessageTools } from "./messages.js";
import { ApiError } from "../client.js";
import type { TrinityClient } from "../client.js";

function apiError(status: number, detail: unknown): ApiError {
  return new ApiError(status, JSON.stringify({ detail }));
}

function makeTool(send: (agent: string, body: unknown) => Promise<unknown>) {
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    sendUserMessage: send as TrinityClient["sendUserMessage"],
  };
  return createMessageTools(fake as TrinityClient, false).sendMessage;
}

async function run(send: (agent: string, body: unknown) => Promise<unknown>,
                   params: Record<string, unknown> = {}) {
  const tool = makeTool(send);
  return JSON.parse(await tool.execute(
    { to: "primary", text: "Your brief is ready.", agent_name: "ops", ...params } as never,
    {},
  ));
}

describe("ent#1221 send_message — refusals name why", () => {
  it("a role refusal carries its code, message and per-channel reasons", async () => {
    const out = await run(async () => {
      throw apiError(404, {
        code: "role_no_channel", role: "primary",
        message: "No delivery channel reaches the person in the primary role.",
        channels: { telegram: "telegram_not_linked", slack: "slack_not_connected",
                    web: "workspace_not_shared" },
      });
    });
    assert.equal(out.success, false);
    assert.equal(out.code, "role_no_channel");
    assert.equal(out.error, "No delivery channel reaches the person in the primary role.");
    assert.deepEqual(out.channels, { telegram: "telegram_not_linked",
                                     slack: "slack_not_connected",
                                     web: "workspace_not_shared" });
    assert.equal(out.recipient_not_found, true);
    assert.doesNotMatch(JSON.stringify(out), /Telegram or Slack configured/);
  });

  it("an email-addressed refusal is structured too", async () => {
    const out = await run(async () => {
      throw apiError(404, { code: "no_channel", message: "No delivery channel available",
                            channels: { web: "web_agent_only" } });
    }, { to: undefined, recipient_email: "user@example.com" });
    assert.equal(out.code, "no_channel");
    assert.deepEqual(out.channels, { web: "web_agent_only" });
  });

  it("a structured message mentioning 'not found' is not reclassified", async () => {
    const out = await run(async () => {
      throw apiError(403, { code: "role_not_opted_in", role: "primary",
                            message: "Agent not found in their opt-ins (403)" });
    });
    assert.equal(out.code, "role_not_opted_in");
    assert.equal(out.not_authorized, true);
    assert.equal(out.recipient_not_found, undefined);
  });

  it("an unfilled seat is named", async () => {
    const out = await run(async () => {
      throw apiError(422, { code: "role_unassigned", role: "approver",
                            message: "Nobody fills the approver role." });
    }, { to: "approver" });
    assert.equal(out.code, "role_unassigned");
    assert.equal(out.seat_unfilled, true);
  });

  it("the plain-string agent-access 404 stays not_authorized", async () => {
    const out = await run(async () => { throw apiError(404, "Agent not found"); });
    assert.equal(out.not_authorized, true);
    assert.equal(out.code, undefined);
  });
});

describe("ent#1221 send_message — a Workspace delivery is honest", () => {
  it("says nothing notifies the person", async () => {
    const out = await run(async () => ({ success: true, channel: "web",
                                         message_id: "proactive-1",
                                         delivery: "workspace_inbox" }));
    assert.equal(out.success, true);
    assert.equal(out.delivery, "workspace_inbox");
    assert.match(out.note, /no notification is sent/);
  });

  it("a Telegram delivery carries no note", async () => {
    const out = await run(async () => ({ success: true, channel: "telegram" }));
    assert.equal(out.delivery, undefined);
    assert.equal(out.note, undefined);
  });

  it("the description no longer says only Telegram and Slack work", () => {
    const tool = makeTool(async () => ({}));
    const channel = (tool.parameters.shape as Record<string, { description?: string }>).channel;
    assert.doesNotMatch(channel.description ?? "", /Currently Telegram and Slack are supported/);
    assert.match(channel.description ?? "", /Workspace/);
  });
});
