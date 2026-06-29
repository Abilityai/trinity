/**
 * Trinity MCP Server - Entry Point
 *
 * Starts the MCP server with Streamable HTTP transport for remote access.
 */

import { createServer } from "./server.js";
import { startExposedToolsReconciler } from "./tools/dynamic-agents.js";

async function main() {
  console.log("Starting Trinity MCP Server...\n");

  try {
    const {
      server,
      port,
      requireApiKey,
      client,
      agentChatPullEnabled,
      trinityApiUrl,
      connectorDenied,
      builtinToolNames,
      registerDynamicTool,
      unregisterDynamicTool,
    } = await createServer();

    // Determine transport type from environment
    const transportType = process.env.MCP_TRANSPORT || "httpStream";

    if (transportType === "stdio") {
      // STDIO transport for direct integration (e.g., local Claude Code)
      console.log("Starting in STDIO mode...");
      server.start({
        transportType: "stdio",
      });
    } else {
      // HTTP Streaming transport for remote access
      // Bind to 0.0.0.0 to allow connections from outside the container
      const host = process.env.MCP_HOST || "0.0.0.0";
      console.log(`Starting HTTP stream server on ${host}:${port}...`);
      server.start({
        transportType: "httpStream",
        httpStream: {
          port,
          host,
        },
      });
      console.log(`\nTrinity MCP Server running at http://localhost:${port}/mcp`);
      console.log(`Authentication: ${requireApiKey ? "API KEY REQUIRED" : "DISABLED (dev mode)"}`);
      console.log(`\nTo test with MCP Inspector:`);
      console.log(`  npx @modelcontextprotocol/inspector http://localhost:${port}/mcp`);
      console.log(`\nTo connect from Claude Code, add to .mcp.json:`);

      if (requireApiKey) {
        console.log(
          JSON.stringify(
            {
              mcpServers: {
                trinity: {
                  url: `http://localhost:${port}/mcp`,
                  headers: {
                    Authorization: "Bearer YOUR_API_KEY",
                  },
                },
              },
            },
            null,
            2
          )
        );
        console.log(`\nGet your API key from your Trinity web UI → Settings → MCP Keys`);
      } else {
        console.log(
          JSON.stringify(
            {
              mcpServers: {
                trinity: {
                  url: `http://localhost:${port}/mcp`,
                },
              },
            },
            null,
            2
          )
        );
        console.log(`\nTo enable API key authentication, set MCP_REQUIRE_API_KEY=true`);
      }
    }

    // #846: start the exposed-agents reconciler AFTER server.start() so that
    // post-start addTool/removeTool fan `notifications/tools/list_changed` to
    // live sessions. Polls the backend internal endpoint (~20s), fail-open.
    const internalSecret = process.env.INTERNAL_API_SECRET || "";
    if (!internalSecret) {
      console.warn(
        "[#846] INTERNAL_API_SECRET unset — dedicated chat_with_<slug> tools disabled " +
        "(the exposed-agents poll would 403). Set it to enable MCP exposure."
      );
    } else {
      startExposedToolsReconciler({
        trinityApiUrl,
        internalSecret,
        client,
        requireApiKey,
        agentChatPullEnabled,
        registerDynamicTool,
        unregisterDynamicTool,
        connectorDenied,
        builtinToolNames,
      });
      console.log("[#846] Exposed-agents reconciler started (poll interval ~20s)");
    }
  } catch (error) {
    console.error("Failed to start server:", error);
    process.exit(1);
  }
}

main();
