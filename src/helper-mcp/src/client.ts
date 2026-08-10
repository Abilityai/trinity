/**
 * Endpoint client for the Trinity docs Q&A service (DOCS-QA-001).
 *
 * Pure adapter over the public ask-trinity Cloud Function — no auth, no
 * credentials, no retries. All failure modes map to structured text so a
 * tool call never crashes the server.
 *
 * Contract note (verified live, 2026-07-12): the endpoint returns
 * {answer, state, session_id} — session expiry is SILENT (an expired or
 * invalid session_id yields HTTP 200, state SUCCEEDED, and a NEW session_id),
 * and session_id values exceed Number.MAX_SAFE_INTEGER, so they are handled
 * as opaque strings end-to-end. No citations field exists today; if the
 * service ever adds one, it is passed through (see formatAnswer).
 *
 * Kept contract-identical with the main Trinity MCP server's ask_trinity
 * (issue #1460) — same tool name, same {question, session_id} schema.
 */

export const DEFAULT_ENDPOINT =
  "https://us-central1-mcp-server-project-455215.cloudfunctions.net/ask-trinity";

export const AGENT_GUIDE_URL =
  "https://raw.githubusercontent.com/abilityai/trinity/main/docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md";

export const MAX_QUESTION_CHARS = 4_000;
export const MAX_SESSION_ID_CHARS = 128;
export const MAX_OUTPUT_CHARS = 65_536;
export const REQUEST_TIMEOUT_MS = 50_000;

export interface ToolText {
  text: string;
  isError: boolean;
}

let endpointOverrideLogged = false;

export function resolveEndpoint(): string {
  const override = process.env.ASK_TRINITY_ENDPOINT?.trim();
  if (override) {
    if (!endpointOverrideLogged) {
      // stderr only — stdout is the JSON-RPC channel
      console.error(`[trinity-docs-mcp] endpoint override: ${override}`);
      endpointOverrideLogged = true;
    }
    return override;
  }
  return DEFAULT_ENDPOINT;
}

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  // Step back off a high surrogate (#2027). `length`/`slice` count UTF-16 code
  // units, so a cut landing inside a surrogate pair leaves half of one — an
  // unpaired surrogate is ill-formed Unicode, and a strict UTF-8 encoder or
  // JSON-RPC client replaces it with U+FFFD or rejects the message outright.
  // Cheaper and clearer than re-slicing by code point: only the final unit can
  // be orphaned, so one check covers it.
  let end = max;
  const last = text.charCodeAt(end - 1);
  if (last >= 0xd800 && last <= 0xdbff) end -= 1;
  return `${text.slice(0, end)}\n\n[output truncated at ${max} characters]`;
}

function formatAnswer(
  answer: string,
  state: string | undefined,
  responseSessionId: string | undefined,
  requestSessionId: string | undefined,
  citations: unknown,
): string {
  const parts: string[] = [truncate(answer, MAX_OUTPUT_CHARS)];

  if (state !== undefined && state !== "SUCCEEDED") {
    parts.push(`⚠️ The docs service reported state "${state}" for this answer.`);
  }

  // Pass-through for a future citations field — the endpoint returns none
  // today (verified live); this lights up automatically if it ever does.
  if (Array.isArray(citations) && citations.length > 0) {
    const rendered = citations
      .map((c) => (typeof c === "string" ? `- ${c}` : `- ${JSON.stringify(c)}`))
      .join("\n");
    parts.push(`Sources:\n${rendered}`);
  }

  // #2027: the context-loss warning is decided by the REQUEST's session, not by
  // whether a new one came back. It used to live inside `if (responseSessionId)`,
  // so a 200 that answered but omitted `session_id` produced no warning at all —
  // the caller silently lost its conversation context, which is exactly what
  // this warning exists to prevent. That branch was covered; the neighbouring
  // one was not.
  if (requestSessionId && requestSessionId !== responseSessionId) {
    parts.push(
      responseSessionId
        ? "⚠️ The previous session expired — a new session was started and prior " +
            "conversation context was lost. Re-state any needed context."
        : "⚠️ The docs service returned no session_id, so your previous session " +
            "was not continued and prior conversation context was lost. " +
            "Re-state any needed context.",
    );
  }

  if (responseSessionId) {
    parts.push(
      `session_id: ${responseSessionId} (pass this to ask_trinity to continue ` +
        "the conversation; sessions expire after ~30 minutes of inactivity)",
    );
  }

  return parts.join("\n\n");
}

export async function askTrinity(
  rawQuestion: string,
  rawSessionId?: string,
): Promise<ToolText> {
  const question = rawQuestion?.trim() ?? "";
  if (!question) {
    return {
      text: "The question is empty. Provide a question about Trinity, e.g. \"How do I create an agent?\"",
      isError: true,
    };
  }
  if (question.length > MAX_QUESTION_CHARS) {
    return {
      text: `The question is ${question.length} characters — the limit is ${MAX_QUESTION_CHARS}. Shorten it and try again.`,
      isError: true,
    };
  }

  const sessionId = rawSessionId?.trim() || undefined;
  if (sessionId && sessionId.length > MAX_SESSION_ID_CHARS) {
    return {
      text: `session_id is longer than ${MAX_SESSION_ID_CHARS} characters — pass the session_id exactly as returned by a previous ask_trinity call, or omit it to start a new session.`,
      isError: true,
    };
  }

  const body: Record<string, string> = { question };
  if (sessionId) body.session_id = sessionId;

  let res: Response;
  try {
    res = await fetch(resolveEndpoint(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      redirect: "error",
    });
  } catch (err: unknown) {
    const name = err instanceof Error ? err.name : "";
    if (name === "TimeoutError" || name === "AbortError") {
      return {
        text: `The Trinity docs service took longer than ${REQUEST_TIMEOUT_MS / 1000}s to answer. Retry, or simplify the question.`,
        isError: true,
      };
    }
    return {
      text: "Could not reach the Trinity docs service (network error or unexpected redirect). Check connectivity and retry; if the endpoint moved, set ASK_TRINITY_ENDPOINT to the new URL.",
      isError: true,
    };
  }

  const rawText = await res.text();
  let data: Record<string, unknown> | undefined;
  try {
    const parsed: unknown = JSON.parse(rawText);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      data = parsed as Record<string, unknown>;
    }
  } catch {
    // Google frontends return HTML on cold-start failures / 5xx — fall through.
  }

  if (!res.ok) {
    const detail =
      data && typeof data.error === "string"
        ? data.error
        : truncate(rawText, 200);
    return {
      text: `The Trinity docs service returned HTTP ${res.status}: ${detail}`,
      isError: true,
    };
  }

  if (!data) {
    return {
      text: `The Trinity docs service returned an unexpected non-JSON response: ${truncate(rawText, 200)}`,
      isError: true,
    };
  }

  const answer = typeof data.answer === "string" ? data.answer.trim() : "";
  const state = typeof data.state === "string" ? data.state : undefined;
  const responseSessionId =
    data.session_id !== undefined && data.session_id !== null
      ? String(data.session_id)
      : undefined;

  if (!answer) {
    if (state !== undefined && state !== "SUCCEEDED") {
      return {
        text: `The docs service could not answer (state "${state}"). Try rephrasing the question.`,
        isError: true,
      };
    }
    return {
      text:
        "The docs assistant returned no answer. Try rephrasing the question." +
        (responseSessionId ? `\n\nsession_id: ${responseSessionId}` : ""),
      isError: false,
    };
  }

  return {
    text: formatAnswer(answer, state, responseSessionId, sessionId, data.citations),
    isError: false,
  };
}

// Mirrors the fallback shape of src/mcp-server/src/tools/docs.ts — a short
// quick-reference plus the canonical URL, never an error-only response.
export const AGENT_GUIDE_FALLBACK = `# Trinity Compatible Agent Guide

Unable to fetch the full guide right now. The canonical document lives at:
https://github.com/abilityai/trinity/blob/main/docs/TRINITY_COMPATIBLE_AGENT_GUIDE.md

## Quick Reference

### Required Files
- \`template.yaml\` - Agent metadata (name, display_name, description, resources)
- \`CLAUDE.md\` - Agent instructions (domain-specific only, planning is injected)
- \`.mcp.json.template\` - MCP server config with \${VAR} placeholders
- \`.env.example\` - Documents required credentials
- \`.gitignore\` - Must exclude secrets and platform directories

### template.yaml Minimum
\`\`\`yaml
name: my-agent
display_name: "My Agent"
description: "What this agent does"
resources:
  cpu: "2"
  memory: "4g"
\`\`\`
`;

export async function fetchAgentGuide(): Promise<ToolText> {
  let res: Response;
  try {
    res = await fetch(AGENT_GUIDE_URL, {
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch {
    return { text: AGENT_GUIDE_FALLBACK, isError: false };
  }
  if (!res.ok) {
    return { text: AGENT_GUIDE_FALLBACK, isError: false };
  }
  const text = await res.text();
  if (!text.trim()) {
    return { text: AGENT_GUIDE_FALLBACK, isError: false };
  }
  return { text: truncate(text, MAX_OUTPUT_CHARS), isError: false };
}
