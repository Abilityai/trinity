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

// ent#359: starred chats are LIFTED OUT of the date groups, not merely copied
// above them. Showing a chat twice — once pinned, once in "Today" — makes the
// list lie about how many conversations there are, and clicking either row goes
// to the same place, so the duplicate carries no information.
export function partitionStarred(threads) {
  const list = Array.isArray(threads) ? threads : []
  return {
    starred: list.filter((t) => t && t.starred),
    rest: list.filter((t) => !(t && t.starred)),
  }
}

// ent#359: per-agent "waiting on you" counts for the agents block, summed from
// the unread counts already attached to each chat.
//
// A room contributes to every agent in it, because there is no single agent the
// conversation is "with" — if three agents are in a room you are behind on, all
// three rows should say so. Rooms report `unread: 0` today (the backend counts
// threads only), so this is the shape being right ahead of the data.
export function unreadByAgent(threads) {
  const out = {}
  for (const t of Array.isArray(threads) ? threads : []) {
    const n = Number(t?.unread) || 0
    if (n <= 0) continue
    const names = Array.isArray(t.agent_names) && t.agent_names.length
      ? t.agent_names
      : (t.agent_name ? [t.agent_name] : [])
    for (const name of names) out[name] = (out[name] || 0) + n
  }
  return out
}

export function totalUnread(threads) {
  return (Array.isArray(threads) ? threads : [])
    .reduce((sum, t) => sum + (Number(t?.unread) || 0), 0)
}

// Normalise a room onto the thread shape the sidebar renders, so one list
// component handles both kinds.
//
// The `agents` / `participants` split is the interesting part, and it shipped
// wrong once: the rooms LIST returns `agents` (identities of the agent
// participants still in the room), while `participants` — objects with
// `kind`/`identity` — is the DETAIL shape. Reading only the detail shape here
// produced an empty list for EVERY room, so a room row drew no avatars at all,
// and the unit test did not catch it because it mocked the response with the
// field production does not send. Both shapes are accepted, and both are
// pinned in portalSidebarIA.spec.js.
export function normalizeRoomRow(r) {
  const fromList = Array.isArray(r?.agents) ? r.agents : null
  const fromDetail = (r?.participants || [])
    .filter((p) => p && p.kind === 'agent')
    .map((p) => p.identity)
  return {
    ...r,
    title: r?.name,
    last_message_at: r?.last_message_at || r?.created_at,
    agent_names: fromList && fromList.length ? fromList : fromDetail,
  }
}

// The avatars to show on one chat row: every participant for a room, the single
// agent for a thread. Capped — a room with eight agents must not push the title
// out of the row.
export const ROW_AVATAR_LIMIT = 3

export function rowAgents(t, limit = ROW_AVATAR_LIMIT) {
  const names = Array.isArray(t?.agent_names) && t.agent_names.length
    ? t.agent_names
    : (t?.agent_name ? [t.agent_name] : [])
  return { shown: names.slice(0, limit), overflow: Math.max(0, names.length - limit) }
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
