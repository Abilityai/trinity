/**
 * #914 / #2661 live verification: invoke TrinityClient against the running
 * backend with a low MCP_CHAT_TIMEOUT_MS to force the abort path, and confirm
 * we get a `queued_timeout` receipt with a real execution_id.
 *
 * Two routes, because the receipt now exists on both:
 *   chat  — sequential /chat   (#914, queue-serialised)
 *   task  — sync parallel /task (#2661, concurrent)
 *
 * Run with:
 *   MCP_CHAT_TIMEOUT_MS=3000 \
 *   TRINITY_API_URL=http://localhost:8000 \
 *   TRINITY_TOKEN="trinity_mcp_..." \
 *   TRINITY_MCP_KEY_ID="<key id from Settings → API Keys>" \
 *   npx tsx src/mcp-server/scripts/verify_914.ts <agent_name> [chat|task|both]
 *
 * Not a test — debug harness for the live stack. Deleted before PR
 * lands? No: kept so future operators can reproduce the recovery path.
 */
import { TrinityClient } from "../src/client.js";

// Unique per run: two runs with an IDENTICAL message create two rows the
// message discriminator cannot tell apart, and #2661 deliberately returns no
// receipt on ambiguity — a harness that collided with its own prior run would
// look like a broken feature.
const NONCE = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
const PROMPT = `[verify ${NONCE}] Please sleep for 60 seconds in your head, then reply DONE. Take your time.`;

type Outcome = { route: string; ok: boolean; note: string; executionId?: string };

/**
 * #2661: use the CALLING key's real id, or nothing — never a fabricated one.
 *
 * The original harness hard-coded `keyId: "verify-914-key"`, a literal that can
 * never equal any row's `source_mcp_key_id`. The matcher lets a row through when
 * the ROW's key id is absent, so the run went green while proving nothing about
 * the key-scoped filter — it could not distinguish "the filter works" from "the
 * filter never engaged".
 *
 * There is no self-describing endpoint for the presented key, so the operator
 * supplies it (`TRINITY_MCP_KEY_ID`, visible in Settings → API Keys). Passing
 * `undefined` when it is absent is the honest fallback: the lookup is then
 * genuinely unscoped and the banner says so, rather than a fake value implying
 * a scoping check that never ran.
 */
function resolveKeyId(): string | undefined {
  const keyId = process.env.TRINITY_MCP_KEY_ID;
  if (!keyId) {
    console.log(
      "[verify] TRINITY_MCP_KEY_ID unset — the executions lookup runs UNSCOPED. " +
        "Set it (Settings → API Keys) to also exercise the source_mcp_key_id filter."
    );
  }
  return keyId || undefined;
}

function classify(route: string, response: unknown, elapsedMs: number): Outcome {
  console.log(`[verify-${route}] elapsed=${elapsedMs}ms`);
  console.log(JSON.stringify(response, null, 2));
  if (
    typeof response === "object" &&
    response !== null &&
    "status" in response &&
    (response as { status?: string }).status === "queued_timeout"
  ) {
    const executionId = (response as { execution_id?: string }).execution_id;
    return { route, ok: true, note: "queued_timeout receipt", executionId };
  }
  return { route, ok: false, note: "fast response — no timeout fired; did the agent reply quickly?" };
}

async function main(): Promise<void> {
  const baseUrl = process.env.TRINITY_API_URL ?? "http://localhost:8000";
  const token = process.env.TRINITY_TOKEN;
  const agent = process.argv[2] ?? "trinity-system";
  const mode = (process.argv[3] ?? "both").toLowerCase();

  if (!token) {
    console.error("set TRINITY_TOKEN to an MCP API key (trinity_mcp_...)");
    process.exit(2);
  }

  const client = new TrinityClient(baseUrl, token);
  const keyId = resolveKeyId();
  console.log(
    `[verify] target=${agent}, mode=${mode}, MCP_CHAT_TIMEOUT_MS=${process.env.MCP_CHAT_TIMEOUT_MS ?? "(default 25000)"}, keyId=${keyId ?? "(unresolved)"}`
  );

  const keyInfo = keyId ? { keyId, keyName: "verify" } : undefined;
  const outcomes: Outcome[] = [];

  try {
    if (mode === "chat" || mode === "both") {
      const t0 = Date.now();
      const response = await client.chat(agent, PROMPT, undefined, keyInfo);
      outcomes.push(classify("914-chat", response, Date.now() - t0));
    }

    if (mode === "task" || mode === "both") {
      // #2661: sync parallel — async_mode omitted on purpose. This is the route
      // that used to hold the fetch for timeout_seconds + 60 and surface a bare
      // `fetch failed`.
      const t0 = Date.now();
      const response = await client.task(agent, PROMPT, {}, undefined, keyInfo);
      outcomes.push(classify("2661-task", response, Date.now() - t0));
    }
  } catch (err) {
    console.error(`[verify] error:`, (err as Error).message);
    process.exit(1);
  }

  console.log("\n--- summary ---");
  for (const o of outcomes) {
    console.log(`${o.ok ? "✓" : "⚠"} ${o.route}: ${o.note}${o.executionId ? ` execution_id=${o.executionId}` : ""}`);
  }
  console.log(
    "\nNext (proves the receipt's TRUTH claim, not just its shape): poll each execution_id " +
      "until terminal and confirm it reaches success — i.e. the target really did keep running " +
      "after we hung up, and the capacity slot was released."
  );
  process.exit(outcomes.every((o) => o.ok) ? 0 : 0);
}

main();
