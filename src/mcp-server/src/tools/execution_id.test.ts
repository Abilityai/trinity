/**
 * #2392 — the platform-supplied execution id (X-Trinity-Execution-Id) wins over
 * the agent-supplied `execution_id` param on every effect tool.
 *
 * Runner: built-in node:test → `node --import tsx --test src/tools/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { parseExecutionIdHeader, resolveExecutionId } from "./execution_id.js";
import { createMessageTools } from "./messages.js";
import { createFileTools } from "./files.js";
import { createVoiceReplyTools } from "./voice.js";
import { readHeader } from "../server.js";
import type { TrinityClient } from "../client.js";
import type { McpAuthContext } from "../types.js";

const session = (executionId?: string) =>
  ({ session: { agentName: "atlas", scope: "agent", executionId } as unknown as McpAuthContext });

describe("#2392 resolveExecutionId", () => {
  it("header wins over a conflicting param", () => {
    assert.equal(resolveExecutionId(session("exec-hdr").session, "exec-param"), "exec-hdr");
  });
  it("falls back to the param when no header", () => {
    assert.equal(resolveExecutionId(session().session, "exec-param"), "exec-param");
    assert.equal(resolveExecutionId(undefined, "exec-param"), "exec-param");
  });
  it("is undefined when neither is present", () => {
    assert.equal(resolveExecutionId(session().session, undefined), undefined);
  });
  it("forwards `manual` as-is", () => {
    assert.equal(resolveExecutionId(session("manual").session, "exec-param"), "manual");
  });
});

describe("#2392 parseExecutionIdHeader", () => {
  it("accepts and trims a well-formed id", () => {
    assert.equal(parseExecutionIdHeader("  exec_1.a:b-2 "), "exec_1.a:b-2");
    assert.equal(parseExecutionIdHeader("manual"), "manual");
  });
  it("ignores absent, empty, over-long or malformed values", () => {
    for (const bad of [undefined, "", "   ", "a".repeat(129), "has space", "x;y", "ünï", "a/b"]) {
      assert.equal(parseExecutionIdHeader(bad), undefined, String(bad));
    }
    assert.equal(parseExecutionIdHeader("a".repeat(128)), "a".repeat(128));
  });
  it("takes the first of a repeated header (via readHeader)", () => {
    assert.equal(parseExecutionIdHeader(readHeader(["exec-a", "exec-b"])), "exec-a");
  });
  it("two sequential requests with different headers yield different ids", () => {
    const ctx = (h: string) => ({ executionId: parseExecutionIdHeader(readHeader(h)) }) as McpAuthContext;
    const first = ctx("exec-1");
    const second = ctx("exec-2");
    assert.equal(resolveExecutionId(first, undefined), "exec-1");
    assert.equal(resolveExecutionId(second, undefined), "exec-2");
  });
});

describe("#2392 effect tools forward the header value over a conflicting param", () => {
  it("send_message", async () => {
    const sent: any[] = [];
    const fake: Partial<TrinityClient> = {
      getBaseUrl: () => "http://localhost:8000",
      sendUserMessage: async (_a: string, data: any) => {
        sent.push(data);
        return { success: true, channel: "auto", message_id: "m1" };
      },
    };
    await createMessageTools(fake as unknown as TrinityClient, false).sendMessage.execute(
      { recipient_email: "user@example.com", text: "hi", execution_id: "exec-param" },
      session("exec-hdr")
    );
    assert.equal(sent[0].execution_id, "exec-hdr");
  });

  it("share_file", async () => {
    const sent: any[] = [];
    const fake: Partial<TrinityClient> = {
      getBaseUrl: () => "http://localhost:8000",
      shareAgentFile: async (_a: string, data: any) => {
        sent.push(data);
        return { file_id: "f", url: "u", expires_at: "2099-01-01T00:00:00Z", size_bytes: 1, mime_type: "text/plain" } as any;
      },
    };
    await createFileTools(fake as unknown as TrinityClient, false).shareFile.execute(
      { filename: "deck.pdf", execution_id: "exec-param" },
      session("manual")
    );
    assert.equal(sent[0].execution_id, "manual");
  });

  it("send_voice_reply works from the header alone and refuses with neither", async () => {
    const sent: any[] = [];
    const fake: Partial<TrinityClient> = {
      getBaseUrl: () => "http://localhost:8000",
      sendVoiceReply: async (_a: string, data: any) => {
        sent.push(data);
        return { delivered: true, channel: "telegram" } as any;
      },
    };
    const tool = createVoiceReplyTools(fake as unknown as TrinityClient, false).sendVoiceReply;
    await tool.execute({ text: "hi" }, session("exec-hdr"));
    assert.equal(sent[0].execution_id, "exec-hdr");

    const out = JSON.parse(await tool.execute({ text: "hi" }, session()));
    assert.equal(out.delivered, false);
    assert.equal(sent.length, 1);
  });
});
