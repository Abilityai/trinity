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

/** `KEY="value"`, with the value's `"` escaped. */
export function formatEnvLine (key, value) {
  const escaped = String(value).replace(/"/g, '\\"')
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
  let value = raw
  const wasDoubleQuoted = value.startsWith('"') && value.endsWith('"') && value.length >= 2
  if (wasDoubleQuoted || (value.startsWith("'") && value.endsWith("'"))) {
    value = value.slice(1, -1)
  }
  if (wasDoubleQuoted) {
    value = value.replace(/\\(["\\])/g, '$1')
  }
  return value
}
