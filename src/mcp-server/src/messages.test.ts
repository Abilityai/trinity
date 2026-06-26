/**
 * #1084 — effect-scoped idempotency params reach the backend body.
 *
 * Pins that the send_message MCP tool forwards `execution_id` + `dedup_label`
 * into the request body (the agent-supplied keys that let the backend dedupe a
 * re-delivered turn to one send), and that an omitted execution_id is forwarded
 * as undefined (fail-open) rather than dropped or defaulted.
 *
 * Also a regression assertion that chat_with_agent STILL derives a non-empty
 * Idempotency-Key (the #525 template #1084 builds on — must not regress).
 *
 * Runner: built-in node:test → `node --import tsx --test src/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createMessageTools } from "./tools/messages.js";
import { createChatTools } from "./tools/chat.js";
import type { TrinityClient } from "./client.js";

type SentBody = {
  recipient_email: string;
  text: string;
  channel?: string;
  reply_to_thread?: boolean;
  execution_id?: string;
  dedup_label?: string;
};

function makeSendTool(sent: SentBody[]) {
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    sendUserMessage: async (_agent: string, data: any) => {
      sent.push(data as SentBody);
      return { success: true, channel: data.channel || "auto", message_id: "m1" };
    },
  };
  const tools = createMessageTools(fake as unknown as TrinityClient, false);
  return tools.sendMessage;
}

describe("#1084 send_message forwards effect-idempotency params", () => {
  it("forwards execution_id + dedup_label into the backend body", async () => {
    const sent: SentBody[] = [];
    const tool = makeSendTool(sent);

    await tool.execute(
      {
        recipient_email: "user@example.com",
        text: "hi",
        channel: "telegram",
        agent_name: "agentA",
        execution_id: "exec-123",
        dedup_label: "reminder",
      },
      undefined
    );

    assert.equal(sent.length, 1);
    assert.equal(sent[0].execution_id, "exec-123");
    assert.equal(sent[0].dedup_label, "reminder");
    assert.equal(sent[0].recipient_email, "user@example.com");
  });

  it("forwards undefined execution_id when the agent omits it (fail-open)", async () => {
    const sent: SentBody[] = [];
    const tool = makeSendTool(sent);

    await tool.execute(
      { recipient_email: "user@example.com", text: "hi", agent_name: "agentA" },
      undefined
    );

    assert.equal(sent.length, 1);
    assert.equal(sent[0].execution_id, undefined);
    assert.equal(sent[0].dedup_label, undefined);
  });
});

describe("#1084 chat_with_agent still sets an Idempotency-Key (no regression)", () => {
  it("passes a non-empty mcp: key to apiClient.chat", async () => {
    let seenKey: string | undefined;
    const fake: Partial<TrinityClient> = {
      getBaseUrl: () => "http://localhost:8000",
      getAgentAccessInfo: async () => ({ owner: "u1", is_shared: true }) as any,
      chat: async (
        _name: string,
        _message: string,
        _sourceAgent?: string,
        _mcpKeyInfo?: any,
        idempotencyKey?: string
      ) => {
        seenKey = idempotencyKey;
        return { response: "ok" } as any;
      },
    };
    const tools = createChatTools(fake as unknown as TrinityClient, false);
    await tools.chatWithAgent.execute(
      { agent_name: "agentA", message: "do a thing" },
      undefined
    );

    assert.ok(seenKey, "Idempotency-Key must be set");
    assert.ok(seenKey!.startsWith("mcp:"), `expected mcp: key, got ${seenKey}`);
  });
});
