/**
 * MCP server definition — two stateless tools over the public docs Q&A
 * endpoint. Separated from index.ts so tests can connect it over an
 * in-memory transport.
 */
import { readFileSync } from "node:fs";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { askTrinity, fetchAgentGuide, MAX_QUESTION_CHARS } from "./client.js";

export const SERVER_NAME = "trinity-docs";
/**
 * The version npm installed, read from this package's own package.json (one
 * level above both src/ and dist/). The publish workflow writes the release
 * version into that field before it builds, so serverInfo and the startup log
 * always name the version that is actually running.
 */
export const SERVER_VERSION: string = readPackageVersion(
  new URL("../package.json", import.meta.url),
);

/** Exported for tests: a manifest without a usable version must stop startup. */
export function readPackageVersion(url: URL): string {
  const { version } = JSON.parse(readFileSync(url, "utf8")) as { version?: unknown };
  if (typeof version !== "string" || version === "") {
    throw new Error(`${SERVER_NAME}: package.json at ${url.pathname} has no version`);
  }
  return version;
}

export function createServer(): McpServer {
  const server = new McpServer({
    name: SERVER_NAME,
    version: SERVER_VERSION,
  });

  server.registerTool(
    "ask_trinity",
    {
      description:
        "Ask a question about the Trinity agent-orchestration platform " +
        "(https://github.com/abilityai/trinity) and get an answer grounded in its " +
        "documentation. Answers are AI-generated from Trinity's public docs and may " +
        "be inaccurate — treat them as guidance, not authority. For follow-up " +
        "questions, pass the session_id returned by the previous call so the " +
        "conversation keeps its context (sessions expire after ~30 minutes; an " +
        "expired session silently starts a new one — the response warns when that " +
        "happens).",
      inputSchema: {
        question: z
          .string()
          .max(MAX_QUESTION_CHARS)
          .describe("The question to ask about Trinity"),
        session_id: z
          .string()
          .optional()
          .describe(
            "Opaque session token from a previous ask_trinity response, for multi-turn follow-ups",
          ),
      },
    },
    async ({ question, session_id }) => {
      const result = await askTrinity(question, session_id);
      return {
        content: [{ type: "text", text: result.text }],
        isError: result.isError,
      };
    },
  );

  server.registerTool(
    "get_agent_requirements",
    {
      description:
        "Get the complete Trinity Compatible Agent Guide — required files, " +
        "directory structure, template.yaml schema, credential management, and " +
        "best practices for building agents that run on Trinity. Fetched live " +
        "from the Trinity repository.",
      inputSchema: {},
    },
    async () => {
      const result = await fetchAgentGuide();
      return {
        content: [{ type: "text", text: result.text }],
        isError: result.isError,
      };
    },
  );

  return server;
}
