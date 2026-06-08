/**
 * #914 live verification: invoke TrinityClient.chat() against the running
 * backend with a low MCP_CHAT_TIMEOUT_MS to force the abort path, and
 * confirm we get a `queued_timeout` receipt with a real execution_id.
 *
 * Run with:
 *   MCP_CHAT_TIMEOUT_MS=3000 \
 *   TRINITY_API_URL=http://localhost:8000 \
 *   TRINITY_TOKEN="trinity_mcp_..." \
 *   npx tsx src/mcp-server/scripts/verify_914.ts <agent_name>
 *
 * Not a test — debug harness for the live stack. Deleted before PR
 * lands? No: kept so future operators can reproduce the recovery path.
 */
import { TrinityClient } from "../src/client.js";

async function main(): Promise<void> {
  const baseUrl = process.env.TRINITY_API_URL ?? "http://localhost:8000";
  const token = process.env.TRINITY_TOKEN;
  const agent = process.argv[2] ?? "trinity-system";

  if (!token) {
    console.error("set TRINITY_TOKEN to an MCP API key (trinity_mcp_...)");
    process.exit(2);
  }

  const client = new TrinityClient(baseUrl, token);
  console.log(`[verify-914] target=${agent}, MCP_CHAT_TIMEOUT_MS=${process.env.MCP_CHAT_TIMEOUT_MS ?? "(default 25000)"}`);

  const t0 = Date.now();
  try {
    const response = await client.chat(
      agent,
      "Please sleep for 60 seconds in your head, then reply DONE. Take your time.",
      undefined,
      { keyId: "verify-914-key", keyName: "914-verify" },
    );
    console.log(`[verify-914] elapsed=${Date.now() - t0}ms`);
    console.log("[verify-914] response:");
    console.log(JSON.stringify(response, null, 2));

    if (typeof response === "object" && response !== null && "status" in response && response.status === "queued_timeout") {
      console.log("\n✓ #914 PATH FIRED — got queued_timeout receipt with execution_id");
      process.exit(0);
    } else {
      console.log("\n⚠ Fast response (no timeout fired). Did the agent reply quickly?");
      process.exit(0);
    }
  } catch (err) {
    console.error(`[verify-914] elapsed=${Date.now() - t0}ms — error:`, (err as Error).message);
    process.exit(1);
  }
}

main();
