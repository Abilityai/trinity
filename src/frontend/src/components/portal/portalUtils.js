// Shared helpers for the client-portal chat shell (#138).

// Deterministic per-agent color — used for the avatar tint and the small thread
// color dots in the sidebar so a thread visually ties to its agent.
export function agentColor(name) {
  let h = 0
  for (let i = 0; i < (name || '').length; i++) h = (h * 31 + name.charCodeAt(i)) % 360
  return `hsl(${h}, 45%, 45%)`
}

export function initials(name) {
  const p = (name || '?').replace(/[^A-Za-z0-9]+/g, ' ').trim().split(' ')
  return ((p[0]?.[0] || '') + (p[1]?.[0] || p[0]?.[1] || '')).toUpperCase() || '?'
}

// Group threads into Today / Yesterday / Previous 7 days / Older buckets, in a
// fixed display order, from an ISO last_message_at/created_at.
export function groupThreadsByDate(threads) {
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const dayMs = 86400000
  const buckets = { Today: [], Yesterday: [], 'Previous 7 days': [], Older: [] }
  for (const t of threads) {
    const iso = t.last_message_at || t.created_at
    const ts = iso ? new Date(iso).getTime() : 0
    if (ts >= startOfToday) buckets.Today.push(t)
    else if (ts >= startOfToday - dayMs) buckets.Yesterday.push(t)
    else if (ts >= startOfToday - 7 * dayMs) buckets['Previous 7 days'].push(t)
    else buckets.Older.push(t)
  }
  return ['Today', 'Yesterday', 'Previous 7 days', 'Older']
    .map((label) => ({ label, threads: buckets[label] }))
    .filter((g) => g.threads.length)
}

export function shortDate(iso) {
  try { return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) }
  catch { return '' }
}

export function threadTitle(t) {
  return (t.title || '').trim() || 'New chat'
}

// #2101: bounded briefing hint grid. Order deterministically — a card with a
// real frontmatter description is a useful hint, a bare humanized slug is
// noise, so described cards come first (stable within each group; the backend
// list order is a directory walk, i.e. arbitrary). Collapsed, only the first
// HINT_COLLAPSE_LIMIT render; a counted toggle expands the rest in place.
export const HINT_COLLAPSE_LIMIT = 6

export function planHintDisplay(hints, expanded, limit = HINT_COLLAPSE_LIMIT) {
  const list = Array.isArray(hints) ? hints : []
  // String(): the backend types description as str|null, but a render error is
  // the wrong failure mode for a briefing card, so coerce defensively.
  const hasDesc = (h) => String((h && h.description) || '').trim()
  const described = list.filter((h) => hasDesc(h))
  const bare = list.filter((h) => !hasDesc(h))
  const ordered = described.concat(bare)
  const collapsible = ordered.length > limit
  return {
    visible: expanded || !collapsible ? ordered : ordered.slice(0, limit),
    total: ordered.length,
    collapsible,
  }
}

// ent#358: resolve a `/workspace?agent=<name>` landing.
//
// This is where anything that used to point at the Agent Detail Session surface
// arrives after the redirect. Such a link names an AGENT, never a thread, so
// "which conversation" has to be decided here: the caller's most recent thread
// with that agent, or a fresh one when there is none. `?new=1` forces fresh.
//
// Returns null when the query names no agent, or one the caller cannot reach.
// Not an error: the roster is the authority, and a stale or hand-edited link
// should land in the Workspace rather than in a dead end.
//
// `threads` is expected most-recent-first (as `fetchAllSessions` returns it),
// so the first match is the latest.
// An axios failure (`isAxiosError`, or a request that never got an answer) is
// the transport's story to tell. Anything else carrying a message is an Error
// we threw ourselves, and its text is more useful than a guess about the
// network.
function isTransportError(err) {
  const code = err?.code || ''
  return err?.isAxiosError === true
    || err?.request !== undefined
    || code === 'ECONNABORTED' || code === 'ERR_NETWORK' || code === 'ETIMEDOUT'
}

export function resolveAgentLanding({ agent, forceNew = false, agents = [], threads = [] } = {}) {
  if (!agent || typeof agent !== 'string') return null
  if (!Array.isArray(agents) || !agents.some((a) => a && a.name === agent)) return null
  if (forceNew) return { agentName: agent, sessionId: null }

  const latest = (Array.isArray(threads) ? threads : []).find((t) => t && t.agent_name === agent)
  return {
    agentName: agent,
    sessionId: latest ? (latest.id || latest.session_id || null) : null,
  }
}

// ent#358: the Workspace is now the ONLY continuous-conversation surface, so a
// send failure it cannot explain is a dead end — there is nowhere else for the
// user to go and find out why. The backend already answers with a specific,
// user-facing reason ("The agent is busy", "The request timed out", "This
// conversation is already handling a message", "The agent couldn't respond (it
// may be offline)"); the UI was discarding it and rendering a bare
// "Not delivered · Retry", which reads the same whether the agent is stopped,
// busy, or the turn simply timed out.
export function deliveryFailureReason(err) {
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail.trim()
  // An Error we threw ourselves carries its own explanation and no `.response`.
  // Falling through to the network branch told the user their CONNECTION had
  // failed when in fact the request succeeded and the turn was still running —
  // which pushed them toward a Retry that re-ran and re-billed it.
  if (!err?.response && err?.message && !isTransportError(err)) return err.message
  // A ClientPortalError always sends a string. Anything else is a framework
  // shape (a 422 validation list, {msg: ...}) — say something true rather than
  // rendering "[object Object]" at the user.
  if (!err?.response) return "Couldn't reach Trinity — check your connection and try again."
  if (err.response.status === 413) return 'That message or attachment is too large.'
  if (err.response.status === 429) return 'Too many messages just now — wait a moment and retry.'
  return `The message wasn't delivered (error ${err.response.status}).`
}
