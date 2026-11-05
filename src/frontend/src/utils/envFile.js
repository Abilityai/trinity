/**
 * One value of an agent's `.env` file: how the Credentials panel writes it and
 * how it reads it back.
 *
 * The panel rewrites the WHOLE file on every save (read, merge, write), so
 * every credential already in the file passes through `unquoteEnvValue` and
 * then `formatEnvLine` again. Both halves therefore have to agree with the
 * agent's own encoding (`format_env_line` / `unquote_env_value` in
 * `docker/base-image/agent_server/services/execution_env.py`), or a value the
 * operator never touched changes on someone else's save.
 */

/**
 * `KEY="value"`, exactly as the agent's `format_env_line` emits it.
 *
 * Backslash is escaped BEFORE quote, because every reader reverses both.
 * Escaping only the quote made a written `\\` read back as `\`, so a credential
 * containing two backslashes lost one on the next save of any credential in
 * the file. The order matters too: escaping the quote first would then double
 * the backslash just added, so `a"b` would be written `a\\"b` and read back as
 * `a\"b`.
 */
export function formatEnvLine (key, value) {
  const escaped = String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"')
  return `${key}="${escaped}"`
}

/**
 * The value as written, from the text after `=` (already trimmed).
 *
 * A double-quoted value has one matched pair stripped and its escapes reversed
 * in a single scan (`\"` -> `"`, `\\` -> `\`). A single-quoted value is taken
 * literally. Anything else is returned unchanged.
 */
export function unquoteEnvValue (raw) {
  if (raw.length >= 2 && raw.startsWith('"') && raw.endsWith('"')) {
    return raw.slice(1, -1).replace(/\\(["\\])/g, '$1')
  }
  if (raw.startsWith("'") && raw.endsWith("'")) return raw.slice(1, -1)
  return raw
}
