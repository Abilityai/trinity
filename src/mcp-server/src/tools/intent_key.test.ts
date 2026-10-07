/**
 * abilityai/trinity-enterprise#665 — the human-facing send tools forward the
 * caller's idempotency key and report a suppressed send as sent: false.
 *
 * Runner: built-in node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createMessageTools } from "./messages.js";
import { createVoipTools } from "./voip.js";
import { createChannelTools } from "./channels.js";
import type { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

const session = { session: { agentName: "corbin", scope: "agent", executionId: "exec-b" } as unknown as McpAuthContext };
const SUPPRESSED = { sent: false, suppressed_by: "idempotency_key", first_sent_at: "t", first_execution_id: "exec-a" };

describe("ent#665 send_message", () => {
  it("forwards the key and reports suppression", async () => {
    const sent: any[] = [];
    const fake: Partial<TrinityClient> = {
      getBaseUrl: () => "http://localhost:8000",
      sendUserMessage: async (_a: string, data: any) => {
        sent.push(data);
        return { success: true, channel: "telegram", ...SUPPRESSED } as any;
      },
    };
    const tool = createMessageTools(fake as unknown as TrinityClient, false).sendMessage;
    const out = JSON.parse(await tool.execute(
      { to: "primary", text: "hi", idempotency_key: "ceiling", idempotency_ttl: 3600 }, session));
    assert.equal(sent[0].idempotency_key, "ceiling");
    assert.equal(sent[0].idempotency_ttl, 3600);
    assert.equal(out.success, true);
    assert.equal(out.sent, false);
    assert.equal(out.suppressed_by, "idempotency_key");
    assert.equal(out.first_execution_id, "exec-a");
  });

  it("a keyless result carries no intent fields", async () => {
    const fake: Partial<TrinityClient> = {
      getBaseUrl: () => "http://localhost:8000",
      sendUserMessage: async () => ({ success: true, channel: "telegram", message_id: "m1" }) as any,
    };
    const tool = createMessageTools(fake as unknown as TrinityClient, false).sendMessage;
    const out = JSON.parse(await tool.execute({ to: "primary", text: "hi" }, session));
    assert.equal("sent" in out, false);
  });

  it("the description teaches the cross-run case", () => {
    const tool = createMessageTools({ getBaseUrl: () => "" } as unknown as TrinityClient, false).sendMessage;
    assert.match(tool.description, /idempotency_key/);
    assert.ok(tool.parameters.safeParse({ text: "x", idempotency_ttl: 30 }).success === false);
  });
});

describe("ent#665 call_user", () => {
  it("forwards the key", async () => {
    const sent: any[] = [];
    const fake: Partial<TrinityClient> = {
      getBaseUrl: () => "http://localhost:8000",
      placeVoipCall: async (_a: string, data: any) => {
        sent.push(data);
        return { call_id: null, status: "suppressed", to_number: "+1", ...SUPPRESSED } as any;
      },
    };
    const tool = createVoipTools(fake as unknown as TrinityClient, false).callUser;
    const out = JSON.parse(await tool.execute({ to_number: "+14155550100", idempotency_key: "k" }, session));
    assert.equal(sent[0].idempotency_key, "k");
    assert.equal(out.sent, false);
  });
});

describe("ent#665 send_group_message", () => {
  it("telegram: forwards key + execution id; keyless body unchanged", async () => {
    const bodies: any[] = [];
    const fake: Partial<TrinityClient> = {
      getBaseUrl: () => "http://localhost:8000",
      sendTelegramGroupMessage: async (_a: string, _c: string, _m: string, intent?: any) => {
        bodies.push(intent);
        return { ok: true, chat_id: "-1", ...SUPPRESSED } as any;
      },
    };
    const tool = createChannelTools(fake as unknown as TrinityClient, false).sendGroupMessage;
    const out = JSON.parse(await tool.execute(
      { channel_type: "telegram", chat_id: "-1", message: "hi", idempotency_key: "k" }, session));
    assert.equal(bodies[0].idempotency_key, "k");
    assert.equal(bodies[0].execution_id, "exec-b");
    assert.equal(out.sent, false);

    await tool.execute({ channel_type: "telegram", chat_id: "-1", message: "hi" }, session);
    assert.equal(bodies[1], undefined);
  });

  it("slack: a suppressed post is success with sent false", async () => {
    const fake: Partial<TrinityClient> = {
      getBaseUrl: () => "http://localhost:8000",
      sendSlackChannelMessage: async () => ({ channel_type: "slack", channel_id: "C1", ...SUPPRESSED }) as any,
    };
    const tool = createChannelTools(fake as unknown as TrinityClient, false).sendGroupMessage;
    const out = JSON.parse(await tool.execute(
      { channel_type: "slack", chat_id: "C1", message: "hi", idempotency_key: "k" }, session));
    assert.equal(out.success, true);
    assert.equal(out.sent, false);
  });
});
