/**
 * MCP server definition — two stateless tools over the public docs Q&A
 * endpoint. Separated from index.ts so tests can connect it over an
 * in-memory transport.
 */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { askTrinity, fetchAgentGuide, MAX_QUESTION_CHARS } from "./client.js";

export const SERVER_NAME = "trinity-docs";
export const SERVER_VERSION = "0.1.0";

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
