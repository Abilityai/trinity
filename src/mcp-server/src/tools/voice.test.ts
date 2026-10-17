/**
 * abilityai/trinity-enterprise#700 — send_voice_reply gains an addressing
 * surface (recipient_email + channel) so a scheduled run can speak. The tool
 * forwards the address to the backend, which owns resolution + the consent /
 * rate-limit gates; the tool only refuses a half address locally.
 *
 * Runner: built-in node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createVoiceReplyTools } from "./voice.js";
import type { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

const session = (executionId?: string) =>
  ({ session: { agentName: "atlas", scope: "agent", executionId } as unknown as McpAuthContext });

function fakeClient(reply: any = { delivered: true, channel: "telegram" }) {
  const sent: any[] = [];
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    sendVoiceReply: async (_a: string, data: any) => {
      sent.push(data);
      return reply;
    },
  };
  return { sent, tool: createVoiceReplyTools(fake as unknown as TrinityClient, false).sendVoiceReply };
}

describe("ent#700 send_voice_reply addressing", () => {
  it("forwards recipient_email + channel with the execution id", async () => {
    const { sent, tool } = fakeClient();
    const out = JSON.parse(
      await tool.execute(
        { text: "Morning brief.", recipient_email: "person@example.com", channel: "telegram" },
        session("exec-sched")
      )
    );
    assert.equal(out.delivered, true);
    assert.deepEqual(sent[0], {
      text: "Morning brief.",
      execution_id: "exec-sched",
      dedup_label: undefined,
      recipient_email: "person@example.com",
      channel: "telegram",
    });
  });

  it("an unaddressed call sends no address fields (in-session path unchanged)", async () => {
    const { sent, tool } = fakeClient();
    await tool.execute({ text: "hi" }, session("exec-1"));
    assert.equal("recipient_email" in sent[0], false);
    assert.equal("channel" in sent[0], false);
  });

  it("refuses a half address without calling the backend", async () => {
    const { sent, tool } = fakeClient();
    const a = JSON.parse(await tool.execute({ text: "hi", recipient_email: "p@example.com" }, session("e")));
    const b = JSON.parse(await tool.execute({ text: "hi", channel: "telegram" }, session("e")));
    assert.equal(a.delivered, false);
    assert.equal(b.delivered, false);
    assert.equal(sent.length, 0);
  });

  it("forwards a role address (`to`) instead of an email", async () => {
    const { sent, tool } = fakeClient();
    await tool.execute({ text: "Brief.", to: "approver", channel: "telegram" }, session("e"));
    assert.equal(sent[0].to, "approver");
    assert.equal(sent[0].channel, "telegram");
    assert.equal("recipient_email" in sent[0], false);
  });

  it("refuses two addresses without calling the backend", async () => {
    const { sent, tool } = fakeClient();
    const out = JSON.parse(await tool.execute(
      { text: "hi", to: "primary", recipient_email: "p@example.com", channel: "telegram" }, session("e")));
    assert.equal(out.delivered, false);
    assert.equal(sent.length, 0);
  });

  it("passes the backend's guidance and max_chars through", async () => {
    const { tool } = fakeClient({
      delivered: false, channel: "telegram", reason: "text_too_long", guidance: "shorten", max_chars: 1500,
    });
    const out = JSON.parse(
      await tool.execute({ text: "x", recipient_email: "p@example.com", channel: "telegram" }, session("e"))
    );
    assert.equal(out.guidance, "shorten");
    assert.equal(out.max_chars, 1500);
    assert.equal(out.fallback_to_text, true);
  });
});
