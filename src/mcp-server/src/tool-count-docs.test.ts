/**
 * trinity-enterprise#991 (ruling T5) — the documented tool count is the
 * registered one.
 *
 * Before this, the sites quoting the count disagreed four ways (169, 168, 121,
 * ~80), because each was bumped by hand. This file reads the count from where
 * the tools actually come from — `createServer()`'s registration, with inline
 * auth on so the two sign-in tools are counted, which is how the docs count —
 * and holds `docs/user-docs/integrations/mcp-server.md` to it:
 *   - line 3's total, module count and operator count;
 *   - the Tool Categories table, whose rows must sum to the total and number
 *     the modules, so the headline cannot be fixed without the table.
 *
 * Skips when the docs tree is absent (the MCP image build copies only this
 * package).
 *
 * Runner: node:test → `node --import tsx --test src/*.test.ts`.
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { createServer } from "./server.js";
import { TrinityClient } from "./client.js";
import { createConnectorTools } from "./tools/connector.js";
import { createAuthTools } from "./tools/auth.js";

const DOC = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../../../docs/user-docs/integrations/mcp-server.md",
);

describe("ent#991 T5 — the MCP server doc quotes the registered tool count", { skip: !existsSync(DOC) && "docs tree absent" }, () => {
  it("line 3 and the Tool Categories table match createServer()", async () => {
    const { builtinToolNames } = await createServer({
      trinityApiUrl: "http://127.0.0.1:1",
      requireApiKey: true,
      port: 0,
      inlineAuthEnabled: true,
    });
    const client = new TrinityClient("http://127.0.0.1:1");
    const total = builtinToolNames.size;
    const connector = Object.keys(createConnectorTools(client, true)).length;
    const auth = Object.keys(createAuthTools(client, true)).length;

    const text = readFileSync(DOC, "utf8");
    const line3 = text.split("\n")[2];
    const head = line3.match(/exposes (\d+) tools across (\d+) modules/);
    assert.ok(head, `line 3 no longer states "exposes N tools across M modules": ${line3}`);
    const ops = line3.match(/(\d+) of them are the operator tool set/);
    assert.ok(ops, `line 3 no longer states the operator count: ${line3}`);

    const rows = [...text.matchAll(/^\| `([a-z0-9_]+)\.ts` \| (\d+) \|/gm)];
    const tableSum = rows.reduce((n, r) => n + Number(r[2]), 0);

    assert.equal(Number(head[1]), total, "line 3 total ≠ registered tools");
    assert.equal(Number(ops[1]), total - connector - auth, "line 3 operator count ≠ registered operator tools");
    assert.equal(tableSum, total, "Tool Categories rows do not sum to the registered total");
    assert.equal(rows.length, Number(head[2]), "Tool Categories row count ≠ line 3 module count");
  });
});
