/**
 * Endpoint client for the Trinity docs Q&A service (DOCS-QA-001) — ent#328.
 *
 * Pure adapter over the public ask-trinity Cloud Function: no auth, no
 * credentials, no retries. Every failure mode maps to structured text so a
 * tool call never crashes the server.
 *
 * VENDORED, NOT REINVENTED
 * ------------------------
 * This is the `askTrinity` half of `src/helper-mcp/src/client.ts`
 * (`@abilityai/trinity-docs-mcp`, shipped by trinity#1579). That module's own
 * header states it was written "contract-identical with the main Trinity MCP
 * server's ask_trinity" — this is the other side of that contract finally
 * existing.
 *
 * The two are separate npm packages with no workspace between them, so an
 * import is not available; Trinity's answer to that is a vendored copy plus a
 * parity test (the `credential_paths.py` / `model_context.py` / `safe_yaml.py`
 * shape, Invariant #5). Parity here is **behavioural, not byte-for-byte**: the
 * helper's file also carries `fetchAgentGuide`/`AGENT_GUIDE_FALLBACK`, and
 * copying those in would give this package a second, network-based agent-guide
 * fetcher next to the disk-based `readAgentGuide()` it already has. Copying
 * dead code to satisfy a `cmp` is the wrong trade; `ask_trinity.parity.test.ts`
 * asserts agreement on OUTPUT instead, exactly as
 * `test_1713_scheduler_utils_parity.py` does for the scheduler mirror.
 *
 * Contract note (verified live 2026-07-12, carried over verbatim): the endpoint
 * returns {answer, state, session_id}; session expiry is SILENT (an expired or
 * invalid session_id yields HTTP 200, state SUCCEEDED, and a NEW session_id),
 * and session_id values exceed Number.MAX_SAFE_INTEGER — so they are opaque
 * STRINGS end-to-end. Never parse one as a number.
 */

export const DEFAULT_ENDPOINT =
  "https://us-central1-mcp-server-project-455215.cloudfunctions.net/ask-trinity";

export const MAX_QUESTION_CHARS = 4_000;
export const MAX_SESSION_ID_CHARS = 128;
export const MAX_OUTPUT_CHARS = 65_536;
export const REQUEST_TIMEOUT_MS = 50_000;

export interface ToolText {
  text: string;
  isError: boolean;
}

let endpointOverrideLogged = false;

/** Reset the once-only override log. Test seam; not used in production. */
export function __resetEndpointLog(): void {
  endpointOverrideLogged = false;
}

export function resolveEndpoint(): string {
  const override = process.env.ASK_TRINITY_ENDPOINT?.trim();
  if (override) {
    if (!endpointOverrideLogged) {
      // stderr only — stdout is the JSON-RPC channel and a stray line there
      // corrupts the protocol.
      console.error(`[trinity-mcp] ask_trinity endpoint override: ${override}`);
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
