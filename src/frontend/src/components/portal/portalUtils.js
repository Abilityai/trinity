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

// ent#364 / #2424: asks per agent — the ask twin of `unreadByAgent`.
//
// Deliberately a SEPARATE map, never summed into the unread count. The two are
// different facts about different obligations: an ask is waiting on you to
// DECIDE, an unread reply on you to READ. PortalSidebar has said so since
// ent#364; what it lacked was this half.
export function asksByAgent(asks) {
  const out = {}
  for (const a of Array.isArray(asks) ? asks : []) {
    const name = a?.agent_name
    if (!name) continue
    out[name] = (out[name] || 0) + 1
  }
  return out
}

// The aggregate badge's accessible name.
//
// #2424: it counted ASKS and said "agents" — two asks raised by one agent read
// as "2 agents are waiting on your answer". The number was right and the noun
// was wrong, and the two only diverge when a single agent raises more than one
// ask, which is why nobody caught it.
//
// Resolved toward asks rather than agents, because the row badges added
// alongside this now answer "which agent" — so the header's job is "how many
// decisions", and that is a count of asks.
export function askBadgeTitle(count) {
  const n = Number(count) || 0
  if (n <= 0) return ''
  return `${n} ${n === 1 ? 'ask is' : 'asks are'} waiting on your answer`
}

// The agent row's accessible name.
//
// #2424: this composed unread replies and the availability chip and never
// mentioned asks, so a blocked agent's title was the bare "Open ws-sage" — the
// pending decision was unreachable for a screen-reader user as well as
// invisible. Asks lead: a decision outranks unread chatter.
export function agentRowTitle({ label, name, unread = 0, askCount = 0, chipTitle = '' } = {}) {
  const who = label && label !== name ? `${label} (${name})` : (label || name || '')
  const asks = Number(askCount) || 0
  const reads = Number(unread) || 0

  const parts = []
  if (asks > 0) parts.push(`${asks} ${asks === 1 ? 'ask' : 'asks'} waiting on you`)
  if (reads > 0) parts.push(`${reads} unread ${reads === 1 ? 'reply' : 'replies'}`)

  const base = parts.length ? `${who} — ${parts.join(', ')}` : `Open ${who}`
  return chipTitle ? `${base} — ${chipTitle}` : base
}

// #2159 capped the roster at five so a long fleet could not push chats below
// the fold. #2424: the cap is a plain roster-order slice, so on any fleet larger
// than five the agent WAITING ON YOU is as likely as not to be behind the
// toggle — observed with an agent 11th of 12 while the header advertised its
// two asks.
//
// Ask-bearing agents are appended, not floated to the top: re-sorting on a
// transient count moves rows under the cursor between refreshes, which is the
// same reason the roster is not re-sorted by availability. So the first N stay
// exactly where they were and the visible list simply grows.
export const AGENT_COLLAPSE_LIMIT = 5

export function visibleAgentRows(roster, { expanded = false, askCounts = {}, limit = AGENT_COLLAPSE_LIMIT } = {}) {
  const list = Array.isArray(roster) ? roster : []
  if (expanded) return list

  const head = list.slice(0, limit)
  const shown = new Set(head.map((a) => a?.name))
  const counts = askCounts || {}
  const waiting = list.filter((a) => a?.name && !shown.has(a.name) && (Number(counts[a.name]) || 0) > 0)
  return waiting.length ? [...head, ...waiting] : head
}

export function totalUnread(threads) {
  return (Array.isArray(threads) ? threads : [])
    .reduce((sum, t) => sum + (Number(t?.unread) || 0), 0)
}

// ent#359: whether a turn that just finished should clear its unread badge.
//
// Only when the user is still looking at that thread. The conversation's send
// is an async closure that outlives its component, so the "turn done" event
// fires even after the user navigated away mid-turn — which is the main way a
// reply legitimately arrives unseen. Marking read unconditionally cleared
// exactly the badge the feature exists to show, making it near-unreachable in
// normal use (open a chat, and it is marked read; stay, and it is marked read;
// leave, and it was marked read anyway).
export function shouldMarkTurnRead(completedSessionId, openSessionId) {
  return !!completedSessionId && completedSessionId === openSessionId
}

// #2133/#2214: how long the client may wait for a reply before concluding it
// has lost track of the turn. The server owns the turn timeout and sends the
// budget with every dispatch (`wait_budget_seconds` on the 202) and every
// reattach (`in_flight_wait_budget_seconds` on the history response), so a
// current backend never forces the client to guess.
//
// This literal is therefore DELIBERATELY FROZEN at the pre-#2214 server bound
// — `(2 × (300 + 10 + 300) + 60)s` — because its only remaining audience is a
// backend that predates the per-agent bound, whose real ceiling WAS exactly
// this number. Do NOT "sync" it with the new server arithmetic: the server
// bound is per-agent now (there is no one number to mirror), and re-sizing this
// to, say, the 3600-default arithmetic would make a client of an OLD backend
// wait ~2h for a turn that server killed at ~21min. When the budget field is
// unreadable on a NEW backend, under-waiting degrades to the honest
// "lost track — check shortly" message with no Retry — never a double-bill.
export const REPLY_MAX_WAIT_MS_FALLBACK = (2 * (300 + 10 + 300) + 60) * 1000

// #2214: the budget pick, extracted from the component so it is testable
// without a mount harness (the ent#392 rule — decidable logic lives here).
// A positive server budget wins; anything else (absent field from an old
// backend, 0, NaN, negative) falls back to the frozen literal above.
export function resolveWaitBudgetMs(budgetSeconds) {
  const s = Number(budgetSeconds)
  return s > 0 ? s * 1000 : REPLY_MAX_WAIT_MS_FALLBACK
}

// ent#361: @mentioning another agent from a 1:1 turns it into a group chat.
//
// The pattern MIRRORS the rooms engine's `_MENTION_RE`
// (`@([A-Za-z0-9][A-Za-z0-9_-]{0,99})`) so that what looks like a mention here
// is what the engine will treat as one there. If the two drift, a user gets a
// room built around a handle the engine then renders as plain text.
//
// Resolution is against the CALLER'S ROSTER, and that is the whole safety
// story: an @name that is not an agent shared with this user stays plain text,
// exactly as it does in a room. It is not an error and must never be reported
// as one — telling someone "no such agent" would answer, for any string they
// care to type, whether an agent by that name exists on the instance.
const MENTION_RE = /@([A-Za-z0-9][A-Za-z0-9_-]{0,99})/g

export function mentionedAgents(content, roster, { exclude = [] } = {}) {
  const names = new Set(
    (Array.isArray(roster) ? roster : []).map((a) => a && a.name).filter(Boolean),
  )
  const skip = new Set(exclude.filter(Boolean))
  const out = []
  const seen = new Set()
  for (const m of String(content || '').matchAll(MENTION_RE)) {
    const name = m[1]
    if (!names.has(name) || skip.has(name) || seen.has(name)) continue
    seen.add(name)
    out.push(name)
  }
  return out
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

// #2128 — one place decides what a click does to the selection, so single- and
// multi-select cannot drift, and the collapse case is testable with no harness.
//
// `multi: false` is the fail-safe shape: on a build with no rooms substrate a
// chat can only ever hold one agent, so clicking another REPLACES the pick
// rather than adding to it. Clicking the selected one clears it (the picker's
// Start button is disabled on an empty selection, so "none" is a reachable and
// harmless state — and a control you cannot un-press is worse).
//
// Always returns a NEW array; the caller's ref is never mutated in place.
export function applyAgentSelection(selected, name, { multi = false } = {}) {
  const list = Array.isArray(selected) ? selected : []
  const has = list.includes(name)
  if (!multi) return has ? [] : [name]
  return has ? list.filter((n) => n !== name) : list.concat(name)
}

// Collapse an existing selection when the capability turns out to be absent —
// a late roster, or the store's self-heal firing while the dialog is open.
// Keeps the MOST RECENT pick: in single-select, the last thing you clicked is
// what you chose. Identity when multi-select is live, or when there is nothing
// to collapse.
export function collapseSelection(selected, { multi = false } = {}) {
  const list = Array.isArray(selected) ? selected : []
  if (multi || list.length <= 1) return list
  return [list[list.length - 1]]
}

// #2161 — the stage escape, as a shape test rather than a param enumeration.
//
// `Portal.vue` renders ONE thing on its main stage, chosen by which route param
// is present: an agent page (`/workspace/a/:agentName`), a room
// (`/workspace/r/:roomId`), or a conversation (`/workspace/c/:sessionId`). Every
// handler that wants to hand the stage back to a fresh chat has to leave the
// current route first, and each one used to ask that question by listing the
// params it knew about.
//
// That list went stale three times. #2128 found `roomId` missing from guards
// written when `sessionId` was the only stage route; ent#360 then added
// `/workspace/a/:agentName` and did not revisit them, which is why "Start a
// chat" on the agent page did nothing at all — the chat was prepared behind a
// page that never yielded the stage.
//
// So the question is inverted, and it fails CLOSED: anything that is not the
// bare workspace root is a stage that must be left. A fourth stage route needs
// no edit here, and cannot silently re-break the button.
// A stage can also be named by the QUERY, not just the path. `?agent=` is the
// ent#358 landing spot and `?new=` forces a fresh thread; both are read at
// `bootstrap()`, which runs again after a sign-in. So a lingering `?agent=X`
// is not cosmetic: signing out at `/workspace?agent=X` and handing the browser
// to the next person makes THEIR first screen "You don't have access to X" —
// the previous session's agent name, surfaced to someone who never asked for
// it. That is the same class the path guard exists to close, so it belongs in
// the same predicate rather than in a second one somebody has to remember.
export const STAGE_QUERY_KEYS = ['agent', 'new']

export const WORKSPACE_ROOT = '/workspace'

export function shouldEscapeStage(path, query) {
  if (path && path.replace(/\/+$/, '') !== WORKSPACE_ROOT) return true
  if (!query) return false
  return STAGE_QUERY_KEYS.some((k) => query[k] !== undefined && query[k] !== null && query[k] !== '')
}

// #2258 — the accessible name of the sidebar's sign-out button, per principal.
//
// A platform user's workspace session IS their platform session (ent#357), so
// for them the button ends the platform session and must SAY so: "Sign out"
// promised less than it did, and "Leave workspace" (the alternative the issue
// weighed) would promise navigation while leaving a live credential in a
// browser the person just asked to leave — the reported bug with an honest
// sign on it. A client's button ends only their portal session, and says so.
// Pure so the label the view renders is the one the test pins.
export const SIGN_OUT_LABEL_PLATFORM = 'Sign out of Trinity'
export const SIGN_OUT_LABEL_CLIENT = 'Sign out'

export function signOutLabelFor(isPlatformSession) {
  return isPlatformSession ? SIGN_OUT_LABEL_PLATFORM : SIGN_OUT_LABEL_CLIENT
}

// #2161 — client-facing names for the activity chart's trigger buckets.
//
// The bucket names are the backend's internal vocabulary (`_BUCKET_ORDER`), and
// the agent page already translates that vocabulary everywhere else it shows it
// (`triggerLabel`: `mcp` → "Tool call"). A legend reading "MCP" beside a row
// reading "Tool call" is the same fact in two languages on a page an external
// client may be reading.
//
// Presentation ONLY. These are never substituted into the `buckets` array the
// chart stacks by — those entries are the keys it indexes `by_type` with, so a
// translated array would find nothing and draw an empty chart.
export const PORTAL_BUCKET_LABELS = {
  'Chat/Tasks': 'Chat',
  'MCP': 'Tool call',
  'Channels': 'Messaging',
  'Public': 'Public link',
}

// ---------------------------------------------------------------------------
// ent#392 — composer typeahead: `/` for the agent's playbooks, `@` for agents.
//
// Both invocation syntaxes already worked and neither was discoverable: a
// client had to know the exact slug, and a near-miss parses as plain text with
// no feedback. Everything below is PURE and exported, because this project has
// no component-mount harness (`vitest` runs `environment: 'node'`) — so any
// decision left inside the component is a decision no test can reach. The
// components are dispatchers over these functions.
//
// OSS-core by decision (ent#392): deliberately ungated — no
// `requires_entitlement`, logic stays in the OSS tree. Recorded explicitly
// because CLAUDE.md's default for an enterprise-tracker feature is gated unless
// ruled otherwise, so the ruling must never be inferred later from the mere
// fact that it merged (the ent#326 / ent#384 discipline).
// ---------------------------------------------------------------------------

// The longest token the typeahead will look back over. This FLOORS the backward
// scan rather than merely capping the result: an unfloored scan walks the whole
// composer on every keystroke, and a pasted 100 KB blob then costs O(n) per key.
export const MAX_TYPEAHEAD_QUERY = 64

// Rows drawn at once. The `/` source is already belted to 24 server-side
// (`_bound_briefing_hints`, #2101), so this is layout, not safety.
export const TYPEAHEAD_LIMIT = 8

// "Preceded by a non-WORD char" — deliberately NOT "preceded by whitespace".
// A whitespace-only rule cannot fire in CJK (你好@rec), after an emoji, or after
// punctuation ((@bob, "@bob), while the non-word rule still closes every AC#6
// case: `50/50` (prev '0'), `and/or` (prev 'd'), `user@example.com` (prev 'r').
const WORD_CHAR_RE = /[A-Za-z0-9_]/
const WS_RE = /\s/

// Keys that move the caret without producing an input event. While the popup is
// open they close it and pass through: the alternative is accepting against
// bounds computed for a caret that has since moved.
const CARET_KEYS = new Set(['ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown'])

/**
 * Find the trigger token the caret is sitting in.
 *
 * → `null` | `{ kind: '/' | '@', start, end, query }`, where `[start, end)` is
 * the whole token — `start` from a backward scan, `end` from a FORWARD one.
 *
 * The forward scan is load-bearing. Without it the splice ends at the caret, so
 * accepting *alice* with the caret parked at `@bo|b` leaves `@alice b` — a
 * dangling tail that turns a correct mention into two wrong words.
 *
 * The trigger rule is deliberately STRICTER than `MENTION_RE`, which is
 * unanchored and therefore parses `user@example.com` as `@example`. Being
 * stricter is safe in the only direction that matters: the popup can never open
 * on something the parser would not see, so it can never offer a token that
 * silently degrades to plain text.
 *
 * `caretEnd` defaults to `caret`; a non-collapsed selection yields no trigger
 * (the user is selecting, not composing a token).
 */
export function detectTypeaheadTrigger(text, caret, caretEnd) {
  const s = String(text ?? '')
  const clamp = (n, fallback) => {
    const v = Number.isFinite(n) ? Math.trunc(n) : fallback
    return Math.min(Math.max(v, 0), s.length)
  }
  const c = clamp(caret, s.length)
  if (caretEnd !== undefined && caretEnd !== null && clamp(caretEnd, c) !== c) return null

  const floor = Math.max(0, c - MAX_TYPEAHEAD_QUERY - 1)
  let start = -1
  let kind = null
  for (let i = c - 1; i >= floor; i--) {
    const ch = s[i]
    if (WS_RE.test(ch)) return null                       // token boundary crossed
    if (ch !== '/' && ch !== '@') continue
    if (i === 0 || !WORD_CHAR_RE.test(s[i - 1])) { start = i; kind = ch; break }
    // A trigger char mid-word is not a trigger, but the scan CONTINUES past it:
    // that is what turns `see /etc/hosts` into a query of `etc/hosts`, which the
    // next rule then rejects. Returning null here instead would leave the same
    // input matching prose playbook titles through a shorter query.
  }
  if (start < 0) return null

  const query = s.slice(start + 1, c)
  // A query carrying a second trigger char is not a token anyone is naming.
  // (`@@bo` and `/@rec` do NOT hit this: the backward scan finds the INNERMOST
  // trigger, so their query is clean.)
  if (query.includes('/') || query.includes('@')) return null

  let end = c
  while (end < s.length && !WS_RE.test(s[end])) end++
  return { kind, start, end, query }
}

/**
 * Splice a chosen insert over the whole trigger token.
 *
 * Pure, DOM-free, and it takes no live caret — the trigger already carries its
 * own bounds, so this cannot splice against a caret that moved.
 *
 * The separator is decided HERE, never baked into the token. Baking a trailing
 * space into `@name` produced `Hello @alice  there` and turned `@| x` into
 * `@recon  x`; dropping it produced `@reconx`, which resolves to nothing and
 * degrades to plain text — the exact AC#4 failure this feature exists to close.
 */
export function applyTypeaheadInsert(text, trigger, insert, { separator = ' ' } = {}) {
  const s = String(text ?? '')
  if (!trigger || !Number.isInteger(trigger.start) || !Number.isInteger(trigger.end)) {
    return { value: s, caret: s.length }
  }
  const start = Math.min(Math.max(trigger.start, 0), s.length)
  const end = Math.min(Math.max(trigger.end, start), s.length)
  const body = String(insert ?? '')
  const nextChar = s.slice(end, end + 1)
  const spliced = body + (separator && !(nextChar && WS_RE.test(nextChar)) ? separator : '')
  return { value: s.slice(0, start) + spliced + s.slice(end), caret: start + spliced.length }
}

// The mention token, and ONLY the token — see the separator rule above.
export function buildMentionToken(name) { return `@${name}` }

/**
 * Can this agent be reached by an @mention at all?
 *
 * DERIVED from the real parser, never re-typed. Agent slugs are minted by
 * `sanitize_agent_name`, whose alphabet is `[a-zA-Z0-9_.-]` with NO length cap,
 * while the mention grammar allows no dot and stops at 100 characters. So
 * `data.scout` is a perfectly ordinary agent that `MENTION_RE` reads as `@data`
 * — a token that resolves to nothing and stays plain text. Nothing offers those
 * names today, so the failure is invisible; a typeahead that listed them would
 * MANUFACTURE it and make it look like a product bug.
 *
 * Asking `mentionedAgents` itself (rather than writing a second regex twelve
 * lines from the first) is the point: this file's own header says drift in this
 * grammar is its own bug class, and a hand-copied predicate is drift waiting to
 * happen. It also rejects names containing `@` or whitespace, which a
 * hand-written copy would have handled only by luck.
 *
 * (`MENTION_RE` carries /g. `matchAll` clones the regex, so `lastIndex` is never
 * advanced — but never call `.test()`/`.exec()` on it from new code.)
 */
export function isMentionable(name) {
  const n = String(name ?? '')
  if (!n) return false
  return mentionedAgents(buildMentionToken(n), [{ name: n }]).length === 1
}

// The `/` fallback rule, lifted out of PortalBriefing.vue so the card and the
// typeahead cannot diverge on what a hint inserts. Null-safe: the typeahead can
// reach it with a row from a roster that refreshed underneath it.
export function starterFor(p) {
  return String(p?.starter_prompt ?? '').trim() || String(p?.title ?? '')
}

// Empty-state copy. Exported so a test pins WHICH condition selects which line,
// and phrased so it never claims what the client cannot observe: `_agent_briefing`
// returns `[]` for a stopped or slow agent exactly as it does for one with no
// playbooks, and the roster is fetched once at mount. "This agent has no
// playbooks exposed" would therefore be a false claim about operator
// configuration for the ordinary state of an idle fleet.
// ---------------------------------------------------------------------------
// #2196 — the availability chip
// ---------------------------------------------------------------------------
//
// ONE rule, here, consumed by every surface that renders it (sidebar row today;
// picker, @-typeahead and briefing line next). Four inline `v-if`s across four
// components is how four surfaces end up disagreeing about the same agent — and
// there is no component-mount harness in this project (`package.json` carries no
// @vue/test-utils, jsdom or happy-dom), so a rule expressed in a template can
// only be guarded by regexing source, which catches deletion but never a wrong
// rule. A pure function is genuinely unit-tested.

export const AVAILABILITY_READY = 'ready'
export const AVAILABILITY_STOPPED = 'stopped'
export const AVAILABILITY_UNAVAILABLE = 'unavailable'
export const AVAILABILITY_UNKNOWN = 'unknown'

/**
 * The chip for one roster card, or `null` when nothing should be rendered.
 *
 * Null for `ready` AND for `unknown`: `unknown` means Trinity could not read
 * container state at all (an unreadable Docker socket marks every card at once),
 * so labelling it would put a warning on the whole roster over an infrastructure
 * fault the viewer can neither see nor act on. Fail-open — render as before.
 *
 * `detailed` distinguishes STOPPED from NO-CONTAINER, and defaults to false.
 * That is a disclosure decision, not a formatting one: to an external client the
 * two states differ only in whether the operator deleted or lost the agent, and
 * neither is actionable for them (starting an agent is an operator action, and
 * the Workspace deliberately has no start control). A platform session — an
 * operator looking at their own fleet — sees the distinction, because for them
 * it is the difference between "start it" and "find out what happened to it".
 *
 * Nothing here is derived from a Docker string: `availability` is one of four
 * server-chosen constants, and an unrecognised value renders nothing.
 */
export function availabilityChip(agent, { detailed = false } = {}) {
  const state = String(agent?.availability ?? '')
  if (state !== AVAILABILITY_STOPPED && state !== AVAILABILITY_UNAVAILABLE) return null

  const owner = String(agent?.owner ?? '').trim()
  // The card already carries the owner, so naming them costs nothing and is far
  // more actionable than "its owner".
  const who = owner ? owner : 'its owner'

  if (!detailed) {
    return {
      state,
      label: 'Unavailable',
      variant: 'warning',
      title: `This agent can't take a message right now — ask ${who} to start it.`,
    }
  }
  return state === AVAILABILITY_STOPPED
    ? {
      state,
      label: 'Stopped',
      variant: 'warning',
      title: `This agent is stopped — ask ${who} to start it.`,
    }
    : {
      state,
      label: 'Unavailable',
      variant: 'danger',
      title: `This agent has no running container — ask ${who} to start it.`,
    }
}

export const EMPTY_REASON_NO_PLAYBOOKS = 'No playbooks are available for this agent right now.'
export const EMPTY_REASON_NO_PEERS = 'No other agents are shared with you.'
export const EMPTY_REASON_NO_MENTIONABLE_PEERS =
  "The other agents shared with you can't be @mentioned — their names aren't valid mention handles."
export const EMPTY_REASON_NO_ROOM_PEERS = 'No other agents are in this conversation yet.'

function rank(haystack, query) {
  const h = String(haystack ?? '').toLowerCase()
  const q = String(query ?? '').toLowerCase()
  if (!q) return 0
  if (!h) return -1
  if (h.startsWith(q)) return 0
  // Word-start prefix: hint titles are PROSE up to 200 chars, not slugs, so a
  // bare substring rule makes a one-character query match nearly everything.
  for (let i = 1; i < h.length; i++) {
    if (!WORD_CHAR_RE.test(h[i - 1]) && h.startsWith(q, i)) return 1
  }
  return -1
}

// Stable rank-then-original-order. `Array.prototype.sort` is stable in every
// engine we target, but the index tiebreak makes that explicit rather than
// assumed.
function rankedBy(list, score) {
  return list
    .map((item, i) => ({ item, i, r: score(item) }))
    .filter((e) => e.r >= 0)
    .sort((a, b) => (a.r - b.r) || (a.i - b.i))
    .map((e) => e.item)
}

/**
 * `/` candidates.
 *
 * Returns a RECORD, not a bare array, because the popup has two different empty
 * states and the component must not re-derive which one it is: a source with no
 * playbooks shows one honest line, a query that matches nothing CLOSES the popup
 * (AC#6 — the user is writing "50/50", not picking).
 */
export function filterPlaybookCandidates(playbooks, query) {
  const source = (Array.isArray(playbooks) ? playbooks : [])
    .filter((p) => p && String(p.title ?? '').trim())
  const q = String(query ?? '')
  return {
    items: q ? rankedBy(source, (p) => rank(p.title, q)) : source.slice(),
    sourceCount: source.length,
  }
}

/**
 * `@` candidates.
 *
 * `enabled` is the #2128 capability gate, and it lives HERE rather than in the
 * component so it is tested rather than grepped: without a rooms substrate an
 * @mention is deliberately ordinary text, so offering a picker for it is a
 * dead-end affordance.
 *
 * Matching is case-insensitive on the slug AND the display label (AC#3 — the
 * roster shows labels, the parser keys on slugs), and unlike the playbook rule
 * it accepts a plain SUBSTRING. That divergence is deliberate: agent names are
 * short identifiers, frequently sharing a deployment prefix (`acme-scout`,
 * `acme-scribe`), so a strict prefix rule would fail the obvious query.
 */
export function filterAgentCandidates(roster, query, {
  exclude = [],
  requireMentionable = true,
  enabled = true,
} = {}) {
  if (!enabled) return { items: [], enabled: false, peerCount: 0, mentionableCount: 0 }
  const skip = new Set((Array.isArray(exclude) ? exclude : []).filter(Boolean))
  const peers = (Array.isArray(roster) ? roster : [])
    .filter((a) => a && a.name && !skip.has(a.name))
  const reachable = requireMentionable ? peers.filter((a) => isMentionable(a.name)) : peers
  const q = String(query ?? '').toLowerCase()
  const items = q
    ? rankedBy(reachable, (a) => {
      const slug = String(a.name).toLowerCase()
      const label = String(a.display_label || a.name).toLowerCase()
      const r = Math.min(...[rank(slug, q), rank(label, q)].map((n) => (n < 0 ? 99 : n)))
      if (r < 99) return r
      return (slug.includes(q) || label.includes(q)) ? 2 : -1
    })
    : reachable.slice()
  return { items, enabled: true, peerCount: peers.length, mentionableCount: reachable.length }
}

/**
 * Which honest line the popup shows when the source itself is empty.
 *
 * "No peers" and "peers exist but none is mentionable" are DIFFERENT statements
 * and neither may be told in place of the other — the second one is the P-3
 * class made visible instead of silent.
 */
export function typeaheadEmptyMessage(kind, result, { scope = 'roster' } = {}) {
  if (kind === '/') return EMPTY_REASON_NO_PLAYBOOKS
  if (!result || result.enabled === false) return null
  if (result.peerCount > 0 && result.mentionableCount === 0) return EMPTY_REASON_NO_MENTIONABLE_PEERS
  return scope === 'room' ? EMPTY_REASON_NO_ROOM_PEERS : EMPTY_REASON_NO_PEERS
}

// Bounded window + honest overflow count — the `planHintDisplay` house pattern
// (principle 28: unbounded data is contained, and the total is stated).
export function boundCandidates(list, limit = TYPEAHEAD_LIMIT) {
  const all = Array.isArray(list) ? list : []
  const n = Number.isInteger(limit) && limit > 0 ? limit : TYPEAHEAD_LIMIT
  return { visible: all.slice(0, n), overflow: Math.max(0, all.length - n) }
}

// ---------------------------------------------------------------------------
// ent#402 — sidebar search filters the AGENT roster too, not only the chats.
//
// Everything decidable lives here rather than in PortalSidebar.vue, because
// vitest runs `environment: 'node'` with no component-mount harness: a rule
// written inside the SFC is a rule no test can reach (the ent#392 precedent).
// ---------------------------------------------------------------------------

// Its own constant, deliberately not TYPEAHEAD_LIMIT: a sidebar row is taller
// than a typeahead row (avatar + label + slug + chips), so the two windows are
// answering different questions about the same list.
export const SIDEBAR_AGENT_RESULT_LIMIT = 8

export const SEARCH_PLACEHOLDER = 'Search agents and chats…'

/**
 * Agent matches for the sidebar search.
 *
 * Two properties are load-bearing and both live HERE rather than at the call
 * site, so a caller cannot get them wrong:
 *
 *  1. `requireMentionable: false`. `filterAgentCandidates` defaults it TRUE for
 *     the composer, where an un-mentionable slug is a dead-end pick. A sidebar
 *     row is not a mention — a dotted slug like `data.scout` opens perfectly
 *     well — so filtering by mentionability here would hide a real agent from a
 *     search for its own name.
 *  2. The window is `visibleAgentRows`, the #2424 rule the steady state already
 *     uses, NOT a plain `boundCandidates` slice. A slice is rank-ordered, so an
 *     agent with an open ask can fall past the window and be hidden from the
 *     result set — the exact failure #2424 fixed for the collapsed list, which a
 *     second bounding rule would quietly reintroduce for search.
 *
 * Returns a RECORD, because the section header, the toggle and the empty line
 * each need a different fact about the same result and must not re-derive it.
 */
export function searchAgents(roster, query, {
  askCounts = {},
  expanded = false,
  limit = SIDEBAR_AGENT_RESULT_LIMIT,
} = {}) {
  const { items } = filterAgentCandidates(roster, query, { requireMentionable: false })
  const visible = visibleAgentRows(items, { expanded, askCounts, limit })
  return {
    items,
    visible,
    total: items.length,
    hidden: Math.max(0, items.length - visible.length),
  }
}

/**
 * Which shape the search region is in.
 *
 * `roster-loading` wins outright: a two-character query typed while the roster
 * is still in flight must not read "No agents match." over a roster that has
 * not arrived — loading is not empty (design-system p15). `searching` describes
 * the CHAT request only; agents are filtered client-side over a roster already
 * in hand, so they render regardless of what the chat request is doing.
 */
export function sidebarSearchState({
  agentTotal = 0,
  chatCount = 0,
  chatsSearching = false,
  rosterLoading = false,
} = {}) {
  if (rosterLoading) return 'roster-loading'
  if (chatsSearching) return 'searching'
  if (agentTotal > 0 && chatCount > 0) return 'both'
  if (agentTotal > 0) return 'agents-only'
  if (chatCount > 0) return 'chats-only'
  return 'none'
}

/**
 * The honest lines, PER SECTION — never one combined sentence.
 *
 * A combined "Nothing matches" over-claims: the chat half is a server request
 * whose failure the view currently swallows into `[]`, so "nothing matches"
 * would assert something about chats that was never actually answered. Two
 * lines each state only what their own section knows, and neither can stand in
 * for the other: "nothing matched at all" is BOTH lines plus the hint, while
 * "agents matched, no chats" is the chats line alone.
 *
 * `agentsEmpty` exists because the STATE cannot express one real case. The chat
 * request's own flag is set on every keystroke and stays set until the request
 * settles, so `searching` covers the whole time someone is typing — and the
 * agent half is a client-side filter that already knows its answer. Reading the
 * agents line off the state alone left an agents section with a header, no rows
 * and no sentence for the entire typing session, which is the dead state this
 * function exists to prevent. `chats-only`/`none` already MEAN no agent matched,
 * so the flag only adds the arm the state cannot reach; loading still outranks
 * both (loading is not empty).
 */
export function searchEmptyLines(state, query = '', { agentsEmpty = false } = {}) {
  const q = String(query ?? '')
  const noAgents = agentsEmpty || state === 'chats-only' || state === 'none'
  const agents = (state !== 'roster-loading' && noAgents) ? 'No agents match.' : null
  let chats = null
  if (state === 'searching') chats = 'Searching chats…'
  else if (state === 'agents-only' || state === 'none') chats = 'No chats match.'
  const hint = state === 'none'
    ? (q ? 'Try another word, or clear the search.' : null)
    : null
  return { agents, chats, hint }
}

// The count, not the overflow — the toggle beside it already states how many
// are hidden, and a header that repeats it says the same thing twice.
export function agentResultsLabel(total) {
  const n = Number(total) || 0
  return n > 0 ? `Agents · ${n}` : 'Agents'
}

/**
 * The label on the ONE persistent toggle (#2159 — two v-if-alternated buttons
 * drop keyboard focus on collapse). Its count changes with the mode because it
 * expands a different list: the whole roster in the steady state, the match set
 * while searching.
 */
export function agentToggleLabel({
  searching = false,
  expanded = false,
  rosterCount = 0,
  matchCount = 0,
} = {}) {
  if (expanded) return 'Show fewer'
  return searching
    ? `Show all (${Number(matchCount) || 0} matches)`
    : `Show all (${Number(rosterCount) || 0})`
}

// While searching the toggle is only meaningful when it has something to do:
// something is hidden, or the list is expanded and can be collapsed back.
export function showAgentToggle({ searching = false, hidden = 0, expanded = false } = {}) {
  if (!searching) return true
  return expanded || (Number(hidden) || 0) > 0
}

/**
 * The room's `@` wake-set.
 *
 * Established by OBSERVING the running server, not by reading the engine: the
 * rooms module is a private submodule that is not even checked out here, so the
 * only trustworthy evidence is what `POST /api/rooms/{id}/messages` answers.
 * Posting `@<participant>` returned `{"mentions":["acme-scout"],"woke":["acme-scout"]}`;
 * posting `@<non-participant>` returned `{"mentions":[],"woke":[]}`.
 *
 * So a participant mention demonstrably wakes, and a non-participant mention
 * demonstrably wakes nobody on that turn — which is all this list needs, and
 * exactly why it is the participants. Offering the roster would put names in
 * front of the user with no evidence that choosing one does anything, the same
 * class of silent no-op as listing an un-mentionable slug.
 *
 * What is deliberately NOT claimed: that a non-participant mention has no effect
 * at all. requirements §5.12 records an engine-side newcomer-join path from
 * ent#361, and two empty response fields do not disprove it — a join would show
 * up as a participant change, which was not observed either way. If that path is
 * live, this list is narrower than the engine allows; recruiting then stays with
 * the explicit "+ Add agent" control, which is the honest home for an action
 * that spends money on another agent.
 *
 * Participants arrive as bare identities; the roster is joined in only to
 * recover display labels, so a participant the caller cannot see in their
 * roster still appears (it is in the room, so it is wakeable).
 */
export function roomMentionSource(participantNames, roster) {
  const byName = new Map(
    (Array.isArray(roster) ? roster : []).filter((a) => a && a.name).map((a) => [a.name, a]),
  )
  return (Array.isArray(participantNames) ? participantNames : [])
    .filter(Boolean)
    .map((n) => byName.get(n) || { name: n })
}

/**
 * The composer keymap, as data.
 *
 * A truth table rather than a substring assertion, and it fixes one thing the
 * current binding gets wrong: `@keydown.enter.exact` has no IME guard, so an IME
 * user's candidate-commit Enter sends the message mid-word.
 *
 * NO IMPLICIT SELECTION. `activeIndex` starts at -1 and a plain Enter accepts
 * ONLY with an explicit selection; otherwise it sends. The harm is asymmetric:
 * an accidental accept destroys typed work (a popup that merely happens to be
 * open — a paste, or prose like "check /status of the deploy" — would splice up
 * to 500 characters over the message), while an accidental send is the thing the
 * user was reaching for anyway. Tab still accepts the top row, and nobody
 * presses Tab to send.
 */
export function resolveComposerKey({
  key, shiftKey = false, ctrlKey = false, metaKey = false, altKey = false,
  isComposing = false, keyCode = 0,
  open = false, hasActive = false, hasCandidates = false,
} = {}) {
  if (isComposing || keyCode === 229) return 'pass'
  // A faithful reproduction of Vue's `.exact`: any modifier falls through
  // unprevented and inserts a newline, exactly as today.
  const plainEnter = key === 'Enter' && !shiftKey && !ctrlKey && !metaKey && !altKey

  if (open) {
    if (key === 'Escape') return 'dismiss'
    if (key === 'ArrowDown') return hasCandidates ? 'move-down' : 'close'
    if (key === 'ArrowUp') return hasCandidates ? 'move-up' : 'close'
    if (key === 'Tab' && !shiftKey) return hasCandidates ? 'accept' : 'pass'
    if (plainEnter && hasCandidates && hasActive) return 'accept'
    if (CARET_KEYS.has(key)) return 'close'
  }
  if (plainEnter) return 'send'
  return 'pass'
}

/**
 * Esc dismissal, as state rather than a boolean.
 *
 * ONLY Esc arms it. A click-outside, a capability change or an agent switch
 * closes WITHOUT arming — otherwise "type `@`, click away to read a message,
 * click back, type `b`" stays suppressed until the user deletes the `@`.
 *
 * Suppression then holds while the user is still editing the token they
 * dismissed: same kind, same start, and a query that EXTENDS the dismissed one.
 * Strict query equality was rejected because it un-dismisses on the very next
 * keystroke (Esc has to mean "stop offering this"); keying on `start` alone was
 * rejected because it is wrong in both directions — text inserted before the
 * token shifts `start` and re-opens, while a full retype at the same offset
 * stays suppressed forever.
 */
export function nextDismissState(trigger) {
  if (!trigger) return null
  return { kind: trigger.kind, start: trigger.start, query: trigger.query }
}

export function isSuppressed(dismissed, trigger) {
  if (!dismissed || !trigger) return false
  if (dismissed.kind !== trigger.kind || dismissed.start !== trigger.start) return false
  return String(trigger.query ?? '').startsWith(String(dismissed.query ?? ''))
}

/**
 * The sentinel an ACCEPT has to arm — and why accepting needs one at all.
 *
 * The splice only appends its separator when the character after the replaced
 * token is not already whitespace, so accepting mid-sentence leaves the caret
 * INSIDE the token that was just inserted: `Hello @bo| there` becomes
 * `Hello @alice| there`. Any recompute at that caret therefore re-detects the
 * very token the user just finished choosing — and one is guaranteed to happen,
 * because `setSelectionRange()` fires a `select` event whenever it moves the
 * caret, which is exactly what the accept does on `nextTick`. The popup would
 * reopen by itself, listing the agent that was just picked.
 *
 * Nothing is destroyed by that (the roving index is cleared, so Enter still
 * sends) but the panel visibly comes back over the composer having been
 * dismissed by a successful choice, which is the one outcome a pick must not
 * produce. Suppressing the resulting token is the honest statement: it has been
 * chosen, so stop offering it. Editing it back re-arms, exactly as after Esc,
 * because `isSuppressed` only holds while the query still EXTENDS the sentinel.
 *
 * Returns `null` when the caret landed past a separator (the common
 * end-of-message case), where nothing needs suppressing.
 */
export function dismissAfterInsert(value, caret) {
  return nextDismissState(detectTypeaheadTrigger(value, caret))
}

// Roving selection. `-1` means "nothing chosen", which is the state Enter reads
// (see resolveComposerKey), so it must survive an empty list.
export function nextActiveIndex(current, delta, length) {
  const n = Number.isInteger(length) ? length : 0
  if (n <= 0) return -1
  const cur = Number.isInteger(current) ? current : -1
  if (cur < 0) return delta > 0 ? 0 : n - 1
  return ((cur + delta) % n + n) % n
}

// A stale index is dropped, not clamped to a neighbour: after a roster refresh
// under an open popup, "the row that is now at index 5" is not the row the user
// chose, and inserting it is how `@undefined` (or the wrong agent) ships.
export function clampActiveIndex(current, length) {
  const n = Number.isInteger(length) ? length : 0
  if (n <= 0) return -1
  return Number.isInteger(current) && current >= 0 && current < n ? current : -1
}

// ---------------------------------------------------------------------------
// #2212 — dictation: which mic path to take, and what to SAY when it fails.
//
// Measured in Playwright's Chromium (secure context, mic permission granted,
// fake audio device): `webkitSpeechRecognition` exists, `start()` does not
// throw, and then NOTHING happens — no `start`, no `audiostart`, no `result`,
// no `error`, no `end` inside 15s. `continuous` also defaults to `false`, so
// even on a build where the service does answer, recognition ends at the first
// pause. That API is a browser-hosted, Google-backed service: we cannot fix it,
// cannot see into it, and it is the branch the component preferred whenever the
// object merely EXISTED.
//
// So the preference inverts: when the platform can transcribe server-side, we
// record locally and upload, because that path is ours end to end (a real HTTP
// status, a real error string, a real transcript). Web Speech stays as the
// no-key fallback — free and often fine in official Chrome — rather than the
// default.
// ---------------------------------------------------------------------------

// Below this, a "recording" is a click-through, not speech (~0.12s of Opus;
// a 2s clip measures ~24 KB). Kept as a named constant because it is also the
// threshold the "too short" message describes.
export const MIN_RECORDING_BYTES = 1500

// A speech attempt that produced no result and no error still failed, and the
// user needs to hear that rather than watch the mic quietly switch itself off.
export const SPEECH_NO_RESULT_MESSAGE =
  "Didn't catch anything — try again and speak once the mic turns red."

// The measured Chromium case: `start()` returns and the engine never reports
// anything at all. Without a watchdog the button stays lit forever, which is
// the one state that cannot be recovered from by waiting.
export const SPEECH_UNRESPONSIVE_MESSAGE =
  "Your browser's dictation service didn't respond — type your message instead."

export const RECORDING_TOO_SHORT_MESSAGE =
  'That recording was too short — hold the mic on while you speak.'

export const TRANSCRIPT_EMPTY_MESSAGE =
  "Didn't catch that — try again, or type your message instead."

export const TTS_FAILED_MESSAGE =
  "Couldn't play that reply aloud — the text is above."

// How long to wait for the browser's speech engine to say ANYTHING before
// declaring it unresponsive. Long enough not to trip a slow-but-working start,
// short enough that a dead engine does not look like a hung UI.
export const SPEECH_START_TIMEOUT_MS = 4000

// `serverStt` is the platform's honest answer ("an ElevenLabs key resolves"),
// NOT a browser capability — the previous gate mixed the two, so an instance
// with no provider still rendered a mic that could not work.
//
//   record  → MediaRecorder + POST /stt   (ours: real errors, real transcript)
//   speech  → browser Web Speech API      (no key needed; unfixable when broken)
//   null    → no mic control at all       (rather than a dead affordance)
export function resolveMicMode({ speechApi = false, canRecord = false, serverStt = false } = {}) {
  if (canRecord && serverStt) return 'record'
  if (speechApi) return 'speech'
  return null
}

// SpeechRecognitionErrorEvent.error codes. `aborted` returns null: it is what
// the user's own Stop click raises, and an "error" notice for a deliberate stop
// teaches people the feature is broken when it just obeyed them.
const SPEECH_ERROR_MESSAGES = {
  'not-allowed':
    'Microphone access is blocked — allow it for this site in your browser, then try again.',
  'service-not-allowed':
    "Your browser wouldn't let its dictation service run — type your message instead.",
  network:
    "Your browser's dictation service is unreachable — type your message instead.",
  'audio-capture': 'No microphone was found — check your input device.',
  'no-speech': "Didn't hear anything — try again and speak once the mic turns red.",
  'language-not-supported': 'Your browser cannot dictate in this language.',
  'bad-grammar': 'Your browser rejected the dictation request — type your message instead.',
}

export function speechErrorMessage(code) {
  if (code === 'aborted') return null
  if (code && SPEECH_ERROR_MESSAGES[code]) return SPEECH_ERROR_MESSAGES[code]
  // An unknown code is still a code: naming it beats "something went wrong",
  // and it is the string a support conversation actually needs.
  return code
    ? `Dictation stopped (${code}) — type your message instead.`
    : 'Dictation stopped unexpectedly — type your message instead.'
}

// getUserMedia / MediaRecorder rejections are DOMExceptions distinguished only
// by `name`. A denied permission previously returned from `catch {}` with no
// state change at all, which is indistinguishable from "started, then stopped".
export function recorderErrorMessage(err) {
  switch (err?.name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return 'Microphone access is blocked — allow it for this site in your browser, then try again.'
    case 'NotFoundError':
    case 'OverconstrainedError':
      return 'No microphone was found — check your input device.'
    case 'NotReadableError':
      return 'Your microphone is in use by another app — close it and try again.'
    default:
      return "Couldn't start the microphone — type your message instead."
  }
}

// The /stt endpoint already answers with user-facing strings ("Voice input is
// not available", "Recording is too long", "Didn't catch that — please try
// again"); the component was throwing them away in a bare `catch {}`.
export function transcriptionErrorMessage(err) {
  const detail = err?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail.trim()
  if (!err?.response) return "Couldn't reach Trinity to transcribe that — type your message instead."
  if (err.response.status === 429) return 'Too many voice messages just now — wait a moment and try again.'
  if (err.response.status === 413) return 'That recording is too long.'
  if (err.response.status === 404) return 'Voice input is not set up on this workspace — type your message instead.'
  return `Transcription failed (error ${err.response.status}) — type your message instead.`
}

// One verdict for a finished dictation attempt, so "what do we tell the user"
// is decided in exactly one place instead of across three event handlers that
// each fire in a different order per engine.
//
// Precedence is deliberate: an unresponsive engine outranks everything (it is
// the state the user cannot escape by waiting), then an explicit error code,
// then "we got words" (say nothing — the transcript IS the feedback), and the
// remaining case — ended, no words, no reason — is the silent failure this
// issue was filed for.
export function speechAttemptOutcome({ gotText = false, errorCode = null, timedOut = false } = {}) {
  if (timedOut) return SPEECH_UNRESPONSIVE_MESSAGE
  if (errorCode) return speechErrorMessage(errorCode)   // null when the user stopped it
  if (gotText) return null
  return SPEECH_NO_RESULT_MESSAGE
}

// What the recorded clip actually IS. Measured (Playwright, fake mic):
//
//   Chromium  rec.mimeType = "audio/webm;codecs=opus", chunk.type = same
//   Firefox   rec.mimeType = ""                      , chunk.type = "audio/ogg; codecs=opus"
//
// The component read `rec.mimeType || 'audio/webm'`, so every Firefox clip — the
// engine that has no Web Speech API and therefore ALWAYS records — was uploaded
// as `voice.webm` with `content-type: audio/webm` while containing Ogg. The
// chunks carry the truth when the recorder does not, so they are the fallback
// ahead of the constant.
export function resolveRecordingMimeType(recorderMimeType, chunks = []) {
  const fromRecorder = (recorderMimeType || '').trim()
  if (fromRecorder) return fromRecorder
  const fromChunk = (Array.isArray(chunks) ? chunks : []).find((c) => (c?.type || '').trim())
  return (fromChunk?.type || '').trim() || 'audio/webm'
}

// ---------------------------------------------------------------------------
// #2213 — the `/` typeahead must search every skill the agent offers
// ---------------------------------------------------------------------------
//
// `playbooks[]` is bounded for the hint-card grid (24 server-side, #2101), and
// the composer's `/` popup searched that same array — so on an agent with 33
// client-visible skills the tail was unreachable. Measured on a live instance:
// `GET /api/skills` in the container returned 33, the roster payload carried 24,
// and typing "probe 27" matched 0 rows with nothing on screen saying why.
//
// The roster now also ships `searchable_playbooks` (the same client-visible set,
// search-bounded, no descriptions) and `playbooks_total`. Search reads the former;
// `hiddenPlaybookCount` turns the latter into the honest "N not shown".

export function playbookSearchSource(agent) {
  const searchable = agent?.searchable_playbooks
  // Fall back to the card list when the field is absent, so a client talking to
  // an older backend keeps working exactly as before rather than losing `/`.
  if (!Array.isArray(searchable) || !searchable.length) {
    return Array.isArray(agent?.playbooks) ? agent.playbooks : []
  }
  // Review finding: `searchable_playbooks` deliberately ships without descriptions
  // (that is what keeps 200 entries cheap), and the popup renders
  // `secondary: c.description || ''` — so EVERY row lost its subtitle, including on
  // agents with fewer than 24 skills whose descriptions did ship on the card list.
  // Merge them back by title: the cards are a prefix of the same set, so this
  // restores the subtitle wherever one exists and leaves it blank only for the tail
  // that genuinely has none.
  const described = new Map(
    (Array.isArray(agent?.playbooks) ? agent.playbooks : [])
      .filter((p) => p && p.description)
      .map((p) => [p.title, p.description]),
  )
  if (!described.size) return searchable
  return searchable.map((p) => (
    p && !p.description && described.has(p.title)
      ? { ...p, description: described.get(p.title) }
      : p
  ))
}

// How many client-visible skills exist that the popup cannot even search. This is
// the difference between a bounded list and a dishonest one: `boundCandidates`
// already reports the rows it did not RENDER, but a payload-level truncation is
// invisible to it — the rows never arrived.
export function hiddenPlaybookCount(agent, searchableLength) {
  const total = Number(agent?.playbooks_total)
  if (!Number.isFinite(total) || total <= 0) return 0
  const have = Number.isInteger(searchableLength)
    ? searchableLength
    : playbookSearchSource(agent).length
  return Math.max(0, total - have)
}

// #2211 — composer auto-grow, without the scrollbar in an empty field
// ---------------------------------------------------------------------------
//
// `scrollHeight` EXCLUDES the border under `box-sizing: border-box`, and the
// composer has a 1px border per side. Assigning `height = scrollHeight` therefore
// left the field 2px shorter than the single line it must hold, so the browser
// showed a vertical scrollbar at ZERO characters. `overflow-y` was also never
// toggled, so the scrollbar stayed available below the ceiling.
//
// Pure so it can be tested without a DOM: the caller reads the three measurements
// and applies the result. It also lives here because BOTH composers
// (PortalConversation, PortalRoom) need it — two copies of this arithmetic is how
// one of them silently keeps the bug.
export function resolveComposerGrowth(metrics, max) {
  const { scrollHeight = 0, offsetHeight = 0, clientHeight = 0 } = metrics || {}
  const ceiling = Number.isFinite(max) && max > 0 ? max : 160
  // Measured, not hardcoded as 2: a future `border-2` must not reintroduce this.
  const borderY = Math.max(0, offsetHeight - clientHeight)
  const wanted = Math.max(0, scrollHeight) + borderY
  return {
    height: Math.min(wanted, ceiling),
    // Below the ceiling there is nothing to scroll, so the scrollbar must be gone
    // rather than merely unused.
    overflowY: wanted > ceiling ? 'auto' : 'hidden',
  }
}

/**
 * How an expired ask describes itself (ent#429).
 *
 * "Visibly expired" is not enough on its own: the #1142 sweep DELETES terminal
 * rows, so between lapsing and being swept an ask is the only evidence that a
 * question was ever asked — and "this expired" without a WHEN leaves the reader
 * unable to tell a question that lapsed an hour ago from one that lapsed in
 * March. The first is worth chasing; the second is history.
 *
 * Pure, and `now` is injected, because a component that formats its own dates is
 * a component whose wording no test can reach (the ent#392 rule for this file).
 *
 * Degrades to the bare sentence on anything it cannot read — a missing, garbled
 * or future `expires_at`. A wrong time is worse than no time here: the reader
 * would act on it.
 */
export function expiredLabel(expiresAt, now = Date.now()) {
  const bare = 'This expired before it was answered.'
  if (!expiresAt) return bare

  const at = Date.parse(expiresAt)
  if (Number.isNaN(at)) return bare

  const ms = now - at
  // A future timestamp means the row is not actually expired and something
  // upstream disagrees with us; say less rather than something false.
  if (ms < 0) return bare

  const mins = Math.floor(ms / 60000)
  if (mins < 1) return 'This expired moments ago, before it was answered.'
  if (mins < 60) return `This expired ${mins}m ago, before it was answered.`

  const hours = Math.floor(mins / 60)
  if (hours < 24) return `This expired ${hours}h ago, before it was answered.`

  const days = Math.floor(hours / 24)
  if (days < 30) return `This expired ${days}d ago, before it was answered.`

  // Past a month, "43d ago" stops meaning anything — a date does.
  return `This expired on ${new Date(at).toLocaleDateString()}, before it was answered.`
}

/**
 * Should this ask offer "open the conversation it belongs to"? (ent#429)
 *
 * `chat_id` is written at RAISE time so the ask is never homeless — but a link
 * is only useful when it goes somewhere the reader is not. Offered when the ask
 * names a thread AND that is not the thread already on screen; the agent page
 * and the sidebar pass no current thread, so there it is always offered.
 *
 * Additive by design: it never HIDES an ask from a thread it was not raised
 * against. The attachment exists so a scheduled run's question is durable and
 * findable, not to restrict where it may be answered — and an ask the reader
 * can see but not reach is the failure this closes, not one it should create.
 */
export function askThreadLink(ask, currentSessionId = null) {
  const target = ask?.chat_id
  if (!target) return null
  if (currentSessionId && target === currentSessionId) return null
  return target
}


// ---- ent#365: deliverables ------------------------------------------------

// The badge on a deliverable card. Keyed off the report's `display_hint`, which
// is the same enum the renderer dispatches on — so a hint the renderer knows
// always has a label, and one it does not degrades to the same honest word the
// fallback renderer is showing.
export const DELIVERABLE_KIND_LABELS = {
  table: 'Table',
  kpi: 'Metrics',
  markdown: 'Document',
  timeline: 'Timeline',
  json: 'Data',
}

export function deliverableKindLabel(displayHint) {
  return DELIVERABLE_KIND_LABELS[displayHint] || 'Report'
}

// Relative for recency, absolute for anything older than a week (principle 22).
// Locale-free by construction so it is testable: the absolute form is the ISO
// date, not a formatted one.
export function relativeTime(iso, now = Date.now()) {
  if (!iso) return ''
  const then = new Date(iso).getTime()
  if (!Number.isFinite(then)) return ''
  const diff = Math.max(0, now - then)
  if (diff < 60_000) return 'just now'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)}d ago`
  return new Date(then).toISOString().slice(0, 10)
}


// ---- ent#366: ratings ------------------------------------------------------

// The words differ by what is being rated, and the difference is deliberate: a
// message is judged as an answer ("did this help?"), a deliverable as a piece of
// work ("was this what you needed?"). Same underlying up/down.
export const RATING_LABELS = {
  message: { up: 'Helpful', down: 'Not helpful' },
  deliverable: { up: 'Useful', down: 'Not what I needed' },
}

export function ratingLabels(targetKind) {
  return RATING_LABELS[targetKind] || RATING_LABELS.message
}

// Clicking the rating you already gave is a no-op, not an un-rate: there is no
// retract endpoint, and silently clearing a score locally would show the person
// a state the server does not have.
export function nextRating(current, clicked) {
  return current === clicked ? null : clicked
}

// A negative rating is the one that opens the comment box (ent#366): the free
// text was always the valuable part, and asking a happy person to explain
// themselves is how you stop getting either.
export function shouldPromptForComment(rating) {
  return rating === 'down'
}

// The tally, as words. RAW COUNTS, never a percentage — one thumbs-down out of
// one rating is "100% negative", a number that looks like evidence and is not.
export const RATINGS_UNAVAILABLE_TEXT = 'Ratings unavailable right now.'
export const RATINGS_EMPTY_TEXT = 'No ratings yet.'

export function ratingTallyText(tally, targetKind = 'message') {
  if (!tally || tally.unavailable) return RATINGS_UNAVAILABLE_TEXT
  const up = Number(tally.up) || 0
  const down = Number(tally.down) || 0
  if (up + down === 0) return RATINGS_EMPTY_TEXT
  const labels = ratingLabels(targetKind)
  return `${up} ${labels.up.toLowerCase()} · ${down} ${labels.down.toLowerCase()}`
}

// What to tell someone after their words are saved. Both branches are honest:
// "passed to the agent" only when it was, and otherwise the comment is still
// recorded — never a promise of a follow-up that will not happen (AC #6).
export const FEEDBACK_SENT_TEXT = 'Thanks — passed on to the agent.'
export const FEEDBACK_RECORDED_TEXT = 'Thanks — recorded for the team.'

// `already_dispatched` (ent#366 review) reads as SENT, not merely recorded: the
// agent was handed this target's feedback on the first down-rating and one turn
// per person per target is the rule, so "passed on to the agent" is the true
// sentence. Only the no-skill and unknown cases fall back to "recorded".
const FEEDBACK_REACHED_AGENT = new Set(['dispatched', 'already_dispatched'])

export function feedbackAcknowledgement(captureFeedback) {
  return FEEDBACK_REACHED_AGENT.has(captureFeedback) ? FEEDBACK_SENT_TEXT : FEEDBACK_RECORDED_TEXT
}
