/**
 * trinity-docs-mcp — standalone MCP server for the Trinity docs Q&A assistant.
 *
 * stdio transport: stdout carries JSON-RPC framing; ALL diagnostics go to
 * stderr (console.error). A single stray console.log corrupts the channel.
 */
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { createServer, SERVER_NAME, SERVER_VERSION } from "./server.js";
import { resolveEndpoint } from "./client.js";

async function main(): Promise<void> {
  const endpoint = resolveEndpoint(); // logs the override to stderr, if set
  const server = createServer();
  await server.connect(new StdioServerTransport());
  console.error(
    `[${SERVER_NAME}] v${SERVER_VERSION} ready (stdio) — docs endpoint: ${endpoint}`,
  );
}

main().catch((err) => {
  console.error(`[${SERVER_NAME}] fatal:`, err);
  process.exit(1);
});
