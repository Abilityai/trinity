/**
 * Tool-access profiles — the client side of trinity-enterprise#341 (GUARD-008).
 *
 * The bundles, the card copy and the per-runtime coverage come from the
 * backend (`GET /api/agents/{name}/tool-access`), so the API and the cards
 * cannot disagree. This module holds only what the browser decides: the
 * Read-only toggle's state, the Custom list's parsing and its validation
 * (mirroring the backend contract: at most 50 entries, each 1–256 characters),
 * and the sentence a save or a toggle reports.
 */

export const STRICT = 'strict_read_only'
export const CUSTOM = 'custom'
export const MAX_TOOLS = 50
export const MAX_TOOL_CHARS = 256

/** The header and list Read-only toggle shows ON for Strict read-only only. */
export function isStrict(agent) {
  return agent?.tool_access_profile === STRICT
}

/** One tool name per line (commas also split), trimmed, blank lines dropped, de-duplicated. */
export function parseToolList(text) {
  const out = []
  for (const raw of String(text || '').split(/[\n,]/)) {
    const name = raw.trim()
    if (name && !out.includes(name)) out.push(name)
  }
  return out
}

export function toolListText(list) {
  return (list || []).join('\n')
}

/**
 * The named error for a Custom list, or '' when the backend will accept it.
 * Counted on the raw lines, as the backend counts the raw list.
 */
export function toolListError(text) {
  const lines = String(text || '').split(/[\n,]/).map((l) => l.trim()).filter(Boolean)
  if (lines.length > MAX_TOOLS) {
    return `Remove some tools: a Custom list holds at most ${MAX_TOOLS} (this one has ${lines.length}).`
  }
  const long = lines.find((l) => l.length > MAX_TOOL_CHARS)
  if (long) {
    return `"${long.slice(0, 24)}…" is too long: a tool name is at most ${MAX_TOOL_CHARS} characters, for example Bash or mcp__trinity__send_message.`
  }
  return ''
}

function readOnlySentence(before, after) {
  if (!before || before.read_only === after.read_only) return ''
  if (after.read_only_applied === false) return 'File protection applies when the agent next starts.'
  return after.read_only ? 'File protection is on now.' : 'File protection is off now.'
}

/** The toast after a Tool access save (the copy approved at the design gate). */
export function saveMessage(before, after) {
  const parts = [`${after.profile_label} saved.`]
  const ro = readOnlySentence(before, after)
  if (ro) parts.push(ro)
  if (after.restart_required) parts.push('The tool list applies after a restart.')
  return parts.join(' ')
}

/** The toast after the header or list Read-only toggle. */
export function toggleMessage(after, agentLabel) {
  const parts = []
  if (after.profile === STRICT) {
    parts.push(`Strict read-only is on for ${agentLabel}.`)
    parts.push(after.read_only_applied === false
      ? 'The send block applies now; file protection applies when the agent next starts.'
      : 'File protection and the send block apply now.')
  } else {
    parts.push(`${agentLabel} is back on ${after.profile_label}.`)
    if (after.read_only_applied === false) parts.push('File protection lifts when the agent next starts.')
  }
  if (after.restart_required) parts.push('The tool list updates at the next restart.')
  return parts.join(' ')
}
