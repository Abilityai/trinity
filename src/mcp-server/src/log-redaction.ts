/**
 * Transport-session-id redaction for the process log (#2035, condition 1).
 *
 * #2035 memoizes the anonymous auth context by `Mcp-Session-Id`, which makes
 * that header **bearer-equivalent for the keyless (#848) tier**: whoever
 * presents a live one is handed that session's verified identity. `server.ts`
 * stopped logging it and keeps `McpAuthContext.sessionId` as an independent
 * correlation id for exactly that reason — but neither of those reaches the
 * copy `mcp-proxy` writes.
 *
 * `mcp-proxy` (bundled by fastmcp; 6.5.1 in this tree) prints the raw id at
 * INFO on three paths in `src/startHTTPServer.ts`:
 *
 *     [mcp-proxy] establishing new SSE stream for session ID <uuid>
 *     [mcp-proxy] client reconnecting with Last-Event-ID <id> for session ID <uuid>
 *     [mcp-proxy] received delete request for session <uuid>
 *
 * Vector captures container stdout into `/data/logs`, so without this every
 * keyless session id is written to a log file in plaintext — live bearer
 * credentials at rest. The first line fires on every connection, so this is
 * the common path, not an edge case.
 *
 * WHY A CONSOLE WRAPPER AND NOT A LOGGER. FastMCP does accept `logger`
 * (`options.logger || console`) and threads it into the session, so a custom
 * logger looks like the clean seam — but those three call sites are bare
 * `console.log`, and mcp-proxy takes no logger of its own (the `logger` symbols
 * in its bundle are vendored ajv). There is no injection point. Redacting
 * downstream at the Vector layer was rejected for leaving the id in Docker's
 * own json log, which `docker logs` and anything reading the container's log
 * files still serve.
 *
 * WHY TRUNCATE RATHER THAN DROP. Condition 1 asks for "hash or truncate". The
 * kept prefix keeps the SSE line joinable to the delete line for the same
 * connection, which is the whole diagnostic value of logging it; 8 hex chars
 * is 32 bits of a 122-bit v4 id, so the remaining 90 bits still have to be
 * guessed online against a live session inside its TTL.
 *
 * SCOPE — deliberately narrow. Only calls whose first argument is a string
 * beginning with `[mcp-proxy]` are rewritten, so ids Trinity logs on purpose
 * (execution ids, schedule ids, the anonymous correlation id) keep their full
 * value. The prefix is an assumption about a dependency, so it is guarded two
 * ways in `log-redaction.test.ts` rather than trusted: a static scan of the
 * installed mcp-proxy source asserts every session-id-bearing `console.*` call
 * still carries the prefix, and the transport suite asserts behaviourally that
 * a real client's session id never reaches stdout in full. A dependency bump
 * that adds an unprefixed site fails CI instead of leaking quietly.
 */

/**
 * v4-shaped UUID. Both `StreamableHTTPServerTransport` (which mints the
 * session id) and `randomUUID()` produce this shape; matching on shape rather
 * than on the surrounding prose means a reworded log line is still covered.
 */
const UUID_PATTERN =
  /[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/g;

/** Leading characters kept so log lines for one connection still join up. */
export const REDACTION_KEEP_CHARS = 8;

/** Suffix marking a deliberate truncation — greppable, ASCII, unambiguous. */
export const REDACTION_MARKER = "-REDACTED";

/** The log-line prefix that identifies output from the bundled mcp-proxy. */
export const MCP_PROXY_LOG_PREFIX = "[mcp-proxy]";

/** Replace every UUID-shaped token with a truncated, marked form. */
export function redactUuids(text: string): string {
  return text.replace(
    UUID_PATTERN,
    (match) => `${match.slice(0, REDACTION_KEEP_CHARS)}${REDACTION_MARKER}`
  );
}

/**
 * Rewrite a console call's arguments if it came from mcp-proxy.
 *
 * Every string argument is rewritten, not only the first: the delete-request
 * line passes the id as a SEPARATE argument
 * (`console.log("[mcp-proxy] …for session", sessionId)`), so a
 * first-argument-only rewrite would miss it entirely.
 */
export function redactMcpProxyArgs(args: unknown[]): unknown[] {
  const [first] = args;
  if (typeof first !== "string" || !first.startsWith(MCP_PROXY_LOG_PREFIX)) {
    return args;
  }
  return args.map((arg) => (typeof arg === "string" ? redactUuids(arg) : arg));
}

/** The console methods mcp-proxy writes through. */
const CONSOLE_METHODS = ["log", "info", "warn", "error", "debug"] as const;

type RedactableConsole = Pick<Console, (typeof CONSOLE_METHODS)[number]>;

/**
 * Marker for an already-wrapped method. `Symbol.for` rather than a module-level
 * flag because the entry point and the test suite can both reach this module
 * and must agree on whether the global console is already patched — a bare flag
 * would double-wrap under a second module instance.
 */
const WRAPPED = Symbol.for("trinity.mcp.logRedaction.wrapped");

/**
 * Wrap `target`'s log methods so mcp-proxy output is redacted before it is
 * written. Idempotent: a second call on the same object is a no-op, so calling
 * `createServer()` more than once in one process (the test suite does) cannot
 * stack wrappers.
 */
export function installLogRedaction(target: RedactableConsole = console): void {
  for (const method of CONSOLE_METHODS) {
    const original = target[method] as ((...args: unknown[]) => void) & {
      [WRAPPED]?: true;
    };
    if (typeof original !== "function" || original[WRAPPED]) continue;

    const wrapped = ((...args: unknown[]) => {
      original(...redactMcpProxyArgs(args));
    }) as ((...args: unknown[]) => void) & { [WRAPPED]?: true };
    wrapped[WRAPPED] = true;

    target[method] = wrapped as RedactableConsole[typeof method];
  }
}
