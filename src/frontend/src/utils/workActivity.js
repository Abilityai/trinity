/**
 * trinity-enterprise#620 — ONE vocabulary for "what the agent is doing".
 *
 * Three surfaces used to carry their own: the operator Chat tab
 * (`execution-status.js`, trinity#41), Agent Detail (`useSessionActivity`)
 * and the Workspace card (three generic labels). They now all compose the
 * line here, from two facts:
 *
 *   tool     — the agent's display name for the tool (`Read`, `Bash`,
 *              `mcp:trinity`, `Task:explore`), or null between tools
 *   summary  — the agent's bounded human summary of the input (`.../a/b.py`,
 *              `"pattern"`, `cmd[:50]…`, `agent_name: scout`), never raw input
 *
 * Two feeds produce those facts: the agent's heartbeat (every running
 * execution — delegated, scheduled, room) and, for the chat's own turn, the
 * raw stream frames the SSE carries. The stream path summarises client-side
 * with `summariseToolInput`, a port of the agent server's `get_input_summary`;
 * `tests/fixtures/tool_input_summary.json` holds the parity cases both must
 * pass, because the card renders either feed and they must read the same.
 *
 * Pure module: `vitest` runs it in `environment: 'node'`.
 */

/** The verbs, keyed by the agent's display tool name. Object comes from `summary`. */
const VERBS = Object.freeze({
  Read: 'Reading',
  Edit: 'Editing',
  Write: 'Writing',
  MultiEdit: 'Editing',
  NotebookEdit: 'Editing',
  Glob: 'Finding',
  Grep: 'Searching for',
  Bash: 'Running',
  WebSearch: 'Searching for',
  WebFetch: 'Fetching',
})

/** Tools whose object is machine text (a path, a command, a pattern): rendered in mono. */
const MONO_TOOLS = new Set(['Read', 'Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'Glob', 'Grep', 'Bash'])

const NO_OBJECT = new Set(['...', '', null, undefined])

/**
 * @typedef {{ text: string, object: string|null, mono: boolean }} ActivityLine
 */

/**
 * Compose the line. Returns null when there is nothing honest to say
 * (no facts at all) — the card then shows nothing rather than a guess.
 * @param {{tool?: string|null, summary?: string|null}|null|undefined} activity
 * @returns {ActivityLine|null}
 */
export function activityLine(activity) {
  if (!activity || typeof activity !== 'object') return null
  const tool = typeof activity.tool === 'string' ? activity.tool.trim() : ''
  const summary = typeof activity.summary === 'string' ? activity.summary.trim() : ''
  const object = NO_OBJECT.has(summary) ? null : summary

  if (!tool) return line('Thinking', null, false)
  if (tool === 'Reply') return line('Writing a reply', null, false)

  if (tool.startsWith('mcp:')) {
    const server = tool.slice(4) || 'a tool'
    // A delegation through the platform names its target when the summary is
    // the roster-masked `agent_name: x` the backend produced.
    const m = object && /^agent_name:\s*(.+)$/.exec(object)
    if (server === 'trinity' && m) return line(`Delegating to ${m[1]}`, null, false)
    return line(`Using ${server}`, null, false)
  }
  if (tool === 'Task' || tool.startsWith('Task:')) {
    const who = tool.includes(':') ? tool.slice(5) : 'an agent'
    return line(object ? `Delegating to ${who}: ${object}` : `Delegating to ${who}`, null, false)
  }
  if (tool === 'Sync' || tool.toLowerCase() === 'git sync') {
    return line(object ? `Syncing ${object}` : 'Syncing', object, false)
  }

  // Two tools whose summary is a fixed phrase, not an object.
  if (tool === 'AskUserQuestion') return line('Asking a question', null, false)
  if (tool === 'TodoWrite') return line('Planning the next steps', null, false)

  const verb = VERBS[tool]
  if (verb) {
    if (!object) return line(`${verb}…`, null, false)
    return line(`${verb} ${object}`, object, MONO_TOOLS.has(tool))
  }
  // An unknown tool still says its name — never "Using a tool…".
  return line(object ? `Using ${tool}: ${object}` : `Using ${tool}`, object, false)
}

function line(text, object, mono) {
  return { text, object, mono }
}

// ---------------------------------------------------------------------------
// The stream feed (own turn): raw stream-json → the same two facts
// ---------------------------------------------------------------------------

function shortenPath(path) {
  if (!path) return '...'
  const parts = path.split('/')
  if (parts.length <= 2) return path
  return `.../${parts.slice(-2).join('/')}`
}

function shortenUrl(url) {
  if (!url) return '...'
  try {
    return new URL(url).hostname || url.slice(0, 30)
  } catch {
    return url.slice(0, 30)
  }
}

/**
 * Port of the agent server's `get_input_summary` (utils/helpers.py) — held to
 * parity by `tests/fixtures/tool_input_summary.json`.
 * @param {string} tool
 * @param {Record<string, unknown>|null|undefined} input
 */
export function summariseToolInput(tool, input) {
  if (!input || typeof input !== 'object' || Object.keys(input).length === 0) return '...'
  const str = (k) => (typeof input[k] === 'string' ? input[k] : '')
  switch (tool) {
    case 'Read':
    case 'Edit':
    case 'Write':
      return shortenPath(str('file_path'))
    case 'Glob':
      return str('pattern') || '...'
    case 'Grep': {
      const p = str('pattern')
      return p ? `"${p.slice(0, 30)}"` : '...'
    }
    case 'Bash': {
      const cmd = str('command')
      return cmd.slice(0, 50) + (cmd.length > 50 ? '...' : '')
    }
    case 'Task':
      return str('description') || (str('prompt') || '...').slice(0, 50)
    case 'WebFetch':
      return shortenUrl(str('url'))
    case 'WebSearch':
      return (str('query') || '...').slice(0, 40)
    case 'TodoWrite':
      return 'Updating todos'
    case 'AskUserQuestion':
      return 'Asking question'
    default: {
      for (const [key, value] of Object.entries(input)) {
        if (typeof value === 'string' && value.length < 50) return `${key}: ${value.slice(0, 30)}`
        if (typeof value === 'string') return `${key}: ${value.slice(0, 30)}...`
      }
      return '...'
    }
  }
}

/** Port of the agent server's `get_tool_name`: `mcp__server__tool` → `mcp:server`, `Task` + subagent_type → `Task:<type>`. */
export function displayToolName(tool, input) {
  if (typeof tool !== 'string' || !tool) return ''
  if (tool.startsWith('mcp__')) {
    const parts = tool.split('__')
    return parts.length >= 2 ? `mcp:${parts[1]}` : `mcp:${tool}`
  }
  if (tool === 'Task') {
    const sub = input && typeof input.subagent_type === 'string' ? input.subagent_type : ''
    return sub ? `Task:${sub}` : 'Task'
  }
  return tool
}

/**
 * One raw stream-json frame (what `GET /api/executions/{id}/stream` carries:
 * `{type:'assistant', message:{content:[{type:'tool_use', name, input}]}}`,
 * `{type:'user', message:{content:[{type:'tool_result'}]}}`, …) → the two
 * facts, or null when the frame changes nothing the line should say.
 *
 * A `tool_result` frame means the run is between tools → `{tool:null}`
 * ("Thinking"); a `text` block means the reply is being written; the
 * backend-injected `error`/`stream_end` frames say nothing here (the card's
 * terminal rendering owns those).
 */
export function activityFromStreamEvent(event) {
  if (!event || typeof event !== 'object') return null
  if (event.type === 'stream_end' || event.type === 'error' || event.type === 'result') return null
  const content = event.message?.content ?? event.content
  if (!Array.isArray(content)) return null
  for (const block of content) {
    if (!block || typeof block !== 'object') continue
    if (block.type === 'tool_use') {
      const tool = displayToolName(block.name, block.input)
      if (!tool) continue
      return { tool, summary: summariseToolInput(block.name, block.input) }
    }
    if (block.type === 'tool_result') return { tool: null, summary: null }
    if (block.type === 'thinking') return { tool: null, summary: null }
    if (block.type === 'text' && typeof block.text === 'string' && block.text.trim()) {
      return { tool: 'Reply', summary: null }
    }
  }
  return null
}

// ---------------------------------------------------------------------------
// The motion rules: one row, minimum display, identical lines do not re-key
// ---------------------------------------------------------------------------

/** How often the Work tab asks for fresh lines while a card is live (the heartbeat is ~5 s). */
export const ACTIVITY_POLL_MS = 2500

/** Minimum time a line stays before the next may replace it (AC #4). */
export const ACTIVITY_MIN_DISPLAY_MS = 700

/** A heartbeat-fed line older than this is dropped (matches the backend ceiling). */
export const ACTIVITY_MAX_AGE_S = 30

/**
 * The queue that turns a burst of facts into a calm sequence of lines:
 * a new line waits out the current line's minimum display, only the LATEST
 * pending line is kept (a burst collapses to its last member — the person
 * wants to know what it is doing NOW, not replay the burst), and an identical
 * line is a no-op so the row never re-animates for nothing.
 *
 * `now()` is injected so the rules are testable without a clock.
 */
export function createActivityLineQueue({ minDisplayMs = ACTIVITY_MIN_DISPLAY_MS, now = () => Date.now() } = {}) {
  let current = null          // { text, key }
  let shownAt = -Infinity
  let pending = null
  let seq = 0

  function offer(lineText) {
    const text = typeof lineText === 'string' ? lineText : null
    if (text === (current && current.text)) {
      pending = null
      return current
    }
    const wait = minDisplayMs - (now() - shownAt)
    if (current && wait > 0) {
      pending = text
      return current
    }
    return show(text)
  }

  function show(text) {
    current = text === null ? null : { text, key: ++seq }
    shownAt = now()
    pending = null
    return current
  }

  /** Called by the owner on its tick: promotes a pending line once the minimum has elapsed. */
  function tick() {
    if (pending === null) return current
    if (now() - shownAt >= minDisplayMs) return show(pending)
    return current
  }

  function clear() {
    current = null
    pending = null
    shownAt = -Infinity
  }

  return { offer, tick, clear, get current() { return current }, get pending() { return pending } }
}

/**
 * The line an item should show, given both feeds (AC #3): the stream's line
 * for the chat's own turn while it streams, else the Work read's activity —
 * both through `activityLine`. `null` = say nothing (no live signal).
 * @param {{ live: boolean, streamActivity?: object|null, activity?: object|null, ageSeconds?: number|null }} args
 */
export function resolveActivityText({ live, streamActivity = null, activity = null, nowMs = Date.now() }) {
  if (!live) return null
  if (streamActivity) return activityLine(streamActivity)?.text ?? null
  if (activity) {
    if (activityAgeSeconds(activity, nowMs) > ACTIVITY_MAX_AGE_S) return null
    return activityLine(activity)?.text ?? null
  }
  return null
}

/**
 * How old a heartbeat-fed line is now: the server's `age_seconds` at read
 * time plus the time since the read (`fetchedAtMs`). Unknown = 0, so a line
 * without provenance is shown rather than dropped on a missing field.
 */
export function activityAgeSeconds(activity, nowMs = Date.now()) {
  if (!activity) return 0
  const base = Number.isFinite(activity.age_seconds) ? activity.age_seconds : 0
  const since = Number.isFinite(activity.fetchedAtMs) ? Math.max(0, (nowMs - activity.fetchedAtMs) / 1000) : 0
  return base + since
}
