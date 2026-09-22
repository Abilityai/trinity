/**
 * trinity-enterprise#549 — a shared file is for the person the turn was for.
 *
 * The backend decides who a file is for; the `share_file` tool has two jobs at
 * this layer and both are wiring, which is why they are pinned here:
 *
 *  1. `audience_email` — the ONE override an agent has — reaches the request
 *     body. A parameter the schema accepts and the body drops is the ent#2659
 *     class: the backend half works, the client half is inert, and nothing fails.
 *  2. The honest-status fields come BACK to the agent. When the platform could
 *     not tell which conversation a share came from, the file is the owner's
 *     only; an agent that is not told cannot fix it, and will tell its user
 *     "it is in your Files tab" when it is not.
 *
 * Runner: built-in node:test → `node --import tsx --test src/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import { createFileTools } from "./tools/files.js";
import type { TrinityClient } from "./client.js";
import type { McpAuthContext } from "./types.js";

type SentBody = {
  filename: string;
  execution_id?: string;
  dedup_label?: string;
  audience_email?: string;
};

const AGENT_SESSION = { session: { agentName: "atlas", scope: "agent" } as unknown as McpAuthContext };

function makeShareTool(sent: SentBody[], reply: Record<string, unknown> = {}) {
  const fake: Partial<TrinityClient> = {
    getBaseUrl: () => "http://localhost:8000",
    shareAgentFile: async (_agent: string, data: any) => {
      sent.push(data as SentBody);
      return {
        file_id: "f-1",
        url: "https://chat.example.com/api/files/f-1?sig=t",
        expires_at: "2099-01-01T00:00:00+00:00",
        size_bytes: 3,
        mime_type: "text/plain",
        ...reply,
      } as any;
    },
  };
  return createFileTools(fake as unknown as TrinityClient, false).shareFile;
}

describe("ent#549 share_file carries the audience both ways", () => {
  it("forwards audience_email into the backend body", async () => {
    const sent: SentBody[] = [];
    await makeShareTool(sent).execute(
      { filename: "deck.pdf", execution_id: "exec-1", audience_email: "bob@example.com" },
      AGENT_SESSION
    );
    assert.equal(sent.length, 1);
    assert.equal(sent[0].audience_email, "bob@example.com");
    assert.equal(sent[0].execution_id, "exec-1");
  });

  it("forwards an omitted audience_email as undefined, never a default", async () => {
    const sent: SentBody[] = [];
    await makeShareTool(sent).execute({ filename: "deck.pdf" }, AGENT_SESSION);
    assert.equal(sent[0].audience_email, undefined);
  });

  it("tells the agent when the file landed with the owner only, and how to fix it", async () => {
    const out = JSON.parse(
      await makeShareTool([], {
        visible_to_requester: false,
        visibility_note: "listed for the agent's owner only … audience_email",
      }).execute({ filename: "deck.pdf" }, AGENT_SESSION)
    );
    assert.equal(out.success, true);
    assert.equal(out.visible_to_requester, false);
    assert.match(out.visibility_note, /audience_email/);
  });

  it("reports no claim as null rather than inventing one", async () => {
    const out = JSON.parse(await makeShareTool([]).execute({ filename: "deck.pdf" }, AGENT_SESSION));
    assert.equal(out.visible_to_requester, null);
    assert.equal(out.visibility_note, null);
    assert.equal(out.addressed_to, null);
  });

  it("echoes the address the AGENT chose", async () => {
    const out = JSON.parse(
      await makeShareTool([], { addressed_to: "bob@example.com" }).execute(
        { filename: "deck.pdf", audience_email: "bob@example.com" },
        AGENT_SESSION
      )
    );
    assert.equal(out.addressed_to, "bob@example.com");
  });

  it("says in its own description whose Files tab a file lands in", () => {
    // The platform prompt's "Sharing Files with Users" section is dropped at the
    // minimal prompt tier — by design, the tool description is the durable home
    // of tool guidance. An agent that never sees this text has no reason to pass
    // an execution_id at all, and its files then land with the owner only.
    const tool = makeShareTool([]);
    assert.match(tool.description, /Files tab/);
    const shape = (tool.parameters as any).shape;
    assert.match(shape.execution_id.description, /Files tab/);
    assert.match(shape.audience_email.description, /shared with/);
  });
});
