#!/usr/bin/env node
// Thin launcher: guard the runtime BEFORE importing dist/ (which uses modern
// syntax and global fetch), so old Node fails with a clear message instead of
// a SyntaxError or "fetch is not defined" mid-tool-call.
// stdout is the MCP JSON-RPC channel — diagnostics go to stderr only.
const major = parseInt(process.versions.node.split(".")[0], 10);
if (major < 18 || typeof fetch !== "function") {
  console.error(
    `trinity-docs-mcp requires Node.js >= 18 (found ${process.version}). ` +
      "Upgrade Node and try again.",
  );
  process.exit(1);
}
import("../dist/index.js").catch((err) => {
  console.error("trinity-docs-mcp failed to start:", err);
  process.exit(1);
});
