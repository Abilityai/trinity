// trinity-enterprise#610 — the Workspace Inbox's decidable rules.
//
// The Inbox is four windows onto data the shell already holds, not a feed of
// its own (plan D6): Action reads the ONE asks list (`clientPortal.openAsks`),
// Unread and All read the shell's `sidebarThreads` snapshot plus the previews
// that ride the same `/chat-state` response (D5). No store, no endpoint. Every
// rule that decides what appears, in what order, and what the counts say lives
// here, pure, so a node test can reach it — a rule composed inside a component
// is a rule no test can pin.
//
// `threads` below is ALWAYS the shell's `sidebarThreads` (D13): the Inbox's
// "came back" count and the sidebar's own unread sum are read from the same
// projection, so they agree by construction rather than by coincidence.

import {
  STAGE_QUERY_KEYS, WORKSPACE_ROOT, WORKSPACE_INBOX, totalUnread, askBadgeTitle, unreadBadgeTitle,
} from './portalUtils'
import { capCount } from '@/utils/tabTitle'

export const INBOX_TABS = ['action', 'unread', 'all']

// All (§3g D-4a): EVERY chat the sidebar lists, of any age, plus the asks list —
// pending asks always, ended ones for the 7 days the server keeps them
// (`asks/service.py`, which also stops the read at 200 rows, pending first).
// Not bounded here: the list pages it (`pageWindow`).
export const ENDED_ASK_WINDOW_DAYS = 7
export const ASKS_READ_CAP = 200

const DAY_MS = 24 * 60 * 60 * 1000

const ts = (v) => {
  if (!v) return 0
  const n = Date.parse(v)
  return Number.isFinite(n) ? n : 0
}

const threadId = (t) => t?.id || t?.session_id || null

// ent#523: what the SIDEBAR lists. An unused Main (no message yet) is not a
// recent chat. This is the same predicate `Portal.vue::sidebarThreads` applies
// inline; it is mirrored here so the parity property ("every thread with an
// arrival survives the sidebar filter") has something pure to run against.
// Keep the two identical.
export function inSidebar(t) {
  return !!t && !(t.is_main && !t.last_message_at)
}

export function sidebarThreadsOf(threads) {
  return (Array.isArray(threads) ? threads : []).filter(inSidebar)
}

// D9: the one-time landing. Bare `/workspace` — no route param, no stage query
// key — lands on the Inbox. Anything that names a stage (`/c/`, `/r/`, `/a/`,
// `?agent=`, `?new=`, the armed-once `?voice=`) is an explicit target and wins.
// Returns the path to replace to, or null for "stay where you are".
export function inboxLandingTarget({ path, params, query } = {}) {
  const p = String(path || '').replace(/\/+$/, '') || '/'
  if (p !== WORKSPACE_ROOT) return null
  const hasParam = Object.values(params || {}).some((v) => v !== undefined && v !== null && v !== '')
  if (hasParam) return null
  const q = query || {}
  if (STAGE_QUERY_KEYS.some((k) => q[k] !== undefined && q[k] !== null)) return null
  return WORKSPACE_INBOX
}

// ?item= values. `ask:<id>` and `thread:<id>`; ids may themselves contain ':'.
export function itemKey(type, id) {
  return `${type}:${id}`
}

export function parseItemKey(key) {
  if (typeof key !== 'string') return null
  const i = key.indexOf(':')
  if (i <= 0 || i === key.length - 1) return null
  const type = key.slice(0, i)
  if (type !== 'ask' && type !== 'thread') return null
  return { type, id: key.slice(i + 1) }
}

export function normalizeInboxTab(v) {
  return INBOX_TABS.includes(v) ? v : null
}

// Which tab to open when the URL names none: the one with something in it,
// decisions first.
export function defaultInboxTab({ needs = 0, came = 0 } = {}) {
  if (needs > 0) return 'action'
  if (came > 0) return 'unread'
  return 'all'
}

function askItem(a) {
  return {
    key: itemKey('ask', a.id),
    type: 'ask',
    id: a.id,
    agent_name: a.agent_name,
    title: a.title || a.question || '',
    status: a.status,
    at: a.status === 'pending' ? a.created_at : (a.ended_at || a.created_at),
    ask: a,
  }
}

function threadItem(t, previews) {
  const id = threadId(t)
  const p = (previews && previews[`thread:${id}`]) || null
  const latest = p?.latest || null
  const lastAt = t.last_message_at || null
  const latestAt = latest?.at || null
  return {
    key: itemKey('thread', id),
    type: 'thread',
    id,
    agent_name: t.agent_name,
    title: t.title || null,
    is_main: !!t.is_main,
    archived: !!t.archived_at,
    n: Number(t.unread) || 0,
    latest,
    first_unread_message_id: p?.first_unread_message_id || null,
    // All's recency key: an arrival (a deliverable) can be newer than the last
    // message, so the later of the two orders the row. A chat with no message
    // yet (an empty non-Main chat — an unused Main is not listed) is ordered by
    // when it was created (§3g D-4a).
    at: (ts(latestAt) >= ts(lastAt) ? (latestAt || lastAt) : lastAt) || t.created_at || null,
    thread: t,
  }
}

const byAtDesc = (a, b) => ts(b.at) - ts(a.at) || String(b.id).localeCompare(String(a.id))

// Action (D8): exactly `openAsks` — pending only — newest first.
export function actionItems(openAsks) {
  return (Array.isArray(openAsks) ? openAsks : [])
    .filter((a) => a && a.id && a.status === 'pending')
    .map(askItem)
    .sort(byAtDesc)
}

// Unread (D1): one row per CHAT with arrivals. Archived chats stay (an archived
// chat is an ordinary tab and `totalUnread` counts it — dropping it would break
// Σ "N new" == came). Rooms are excluded: their unread is 0 until PR C.
// Ordered by the latest arrival, falling back to the last message.
export function unreadItems(threads, previews) {
  return (Array.isArray(threads) ? threads : [])
    .filter((t) => t && !t.is_room && threadId(t) && (Number(t.unread) || 0) > 0)
    .map((t) => {
      const it = threadItem(t, previews)
      return { ...it, at: it.latest?.at || t.last_message_at || null }
    })
    .sort(byAtDesc)
}

// All: every chat (read or not, any age; rooms wait for PR C), merged with the
// asks list — pending asks always, ended ones for 7 days. The server applies
// that window too; the client guard stays for an ask that expired while it was
// still listed as pending.
export function allItems(threads, asks, previews, now = Date.now()) {
  const askCutoff = now - ENDED_ASK_WINDOW_DAYS * DAY_MS
  const chats = (Array.isArray(threads) ? threads : [])
    .filter((t) => t && !t.is_room && threadId(t))
    .map((t) => threadItem(t, previews))
  const askRows = (Array.isArray(asks) ? asks : [])
    .filter((a) => a && a.id)
    .map(askItem)
    .filter((it) => it.status === 'pending' || ts(it.at) >= askCutoff)
  return [...chats, ...askRows].sort(byAtDesc)
}

// §3g SM / C4: every tab renders at most `limit` rows, then "Show more". Not
// virtualisation — rows vary in height, and Tab order and ghosts must hold.
// A selected row past the window widens it to include that row (a deep link to
// row 72 shows 72), so the selection is always on screen.
export const PAGE_SIZE = 50
export function pageWindow(items, limit = PAGE_SIZE, selectedKey = null) {
  const list = Array.isArray(items) ? items : []
  let n = Math.max(0, Number(limit) || 0)
  if (selectedKey) {
    const i = list.findIndex((it) => it && it.key === selectedKey)
    if (i >= n) n = i + 1
  }
  const shown = list.slice(0, n)
  return { shown, total: list.length, hidden: list.length - shown.length }
}

// §3g D-4b / T16: what All's footer says, beyond the paging line. The ended-ask
// window always; rooms only to a viewer who has some; the asks read's cap only
// when it was hit (server pagination + a total is #3059).
export function allFooterNotes({ hasRooms = false, askCount = 0 } = {}) {
  const notes = ['Answered, expired and cancelled asks drop off after 7 days.']
  if (askCount >= ASKS_READ_CAP) notes.push(`Showing your ${ASKS_READ_CAP} most recent asks.`)
  if (hasRooms) notes.push("Rooms aren't in the Inbox yet. Open them from the sidebar.")
  return notes
}

// D13: the two counts on the pinned row. `came` is `totalUnread` over the SAME
// `sidebarThreads` the sidebar sums, and the tab title (#557) shows.
export function inboxCounts(threads, openAsks) {
  return {
    needs: Array.isArray(openAsks) ? openAsks.length : 0,
    came: totalUnread(threads),
  }
}

export function newLabel(n) {
  const k = Number(n) || 0
  return k > 0 ? `${k} new` : ''
}

// §3g A8 / D-1: the line above a tab's rows, in units — never a bare number.
// Counts LIVE rows (a ghost is not a member); the "new" sum is the same message
// count the tab badge and the sidebar show (D13), capped like them.
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`
export function listHeadLabel(tab, liveItems) {
  const items = Array.isArray(liveItems) ? liveItems : []
  if (!items.length) return 'All caught up'
  const chats = items.filter((it) => it && it.type === 'thread')
  const asks = items.filter((it) => it && it.type === 'ask')
  if (tab === 'action') return plural(asks.length, 'ask', 'asks')
  if (tab === 'unread') {
    const fresh = chats.reduce((sum, it) => sum + (Number(it.n) || 0), 0)
    return `${plural(chats.length, 'chat', 'chats')} · ${capCount(fresh) || 0} new`
  }
  const parts = []
  if (chats.length) parts.push(plural(chats.length, 'chat', 'chats'))
  if (asks.length) parts.push(plural(asks.length, 'ask', 'asks'))
  return parts.join(' · ')
}

// §3g A9: Mark all read names what it does — the number of CHATS it reads —
// and asks first when that is more than one.
export function markAllLabel(k) {
  const n = Number(k) || 0
  return n > 0 ? `Mark ${plural(n, 'chat', 'chats')} read` : 'Mark all read'
}
export function markAllConfirm({ chats = 0, messages = 0 } = {}) {
  return {
    title: `${markAllLabel(chats)}?`,
    message: `${plural(Number(messages) || 0, 'new message', 'new messages')} across ${plural(Number(chats) || 0, 'chat', 'chats')} will be marked read. You can't undo this.`,
    confirm: markAllLabel(chats),
    done: `Marked ${plural(Number(chats) || 0, 'chat', 'chats')} read`,
  }
}

// §3g B6b: the pinned sidebar row's accessible name — named once, with both
// counts in words, so a screen reader never hears "Inbox 3 5".
export function inboxRowLabel({ needs = 0, came = 0 } = {}) {
  return ['Inbox', askBadgeTitle(needs), unreadBadgeTitle(came)].filter(Boolean).join(', ')
}


// ---- The shell's seams (Portal.vue) -------------------------------------------
//
// Kept here, pure, so the shell's Inbox decisions are testable without mounting
// the 2,000-line view: the shell wires them and adds no logic of its own.

export function isInboxPath(path) {
  return String(path || '').replace(/\/+$/, '') === WORKSPACE_INBOX
}

// D9 (Stage 2): the Inbox renders only on a READY stage. On `failed` / `empty`
// the route falls through to the bare-stage block (roster error, "No agents
// here yet"), whose guard is also true on `/workspace/inbox` — the ent#253
// lesson that a branch must never render under another's verdict.
export function inboxBranchVisible({ isInboxRoute = false, stageState = 'loading' } = {}) {
  return Boolean(isInboxRoute) && stageState === 'ready'
}

// D10: on the Inbox route the rail follows the SELECTED item's agent — never
// the `agents[0]` fallback, and never by writing `activeAgentName` (that would
// mint a Main and retarget the conversation watchers). No selection, or an
// agent no longer on the roster → null → no rail.
export function inboxSelectedAgent({ item, threads = [], asks = [], agents = [] } = {}) {
  const parsed = parseItemKey(item)
  if (!parsed) return null
  let name = null
  if (parsed.type === 'thread') {
    name = (Array.isArray(threads) ? threads : []).find((t) => threadId(t) === parsed.id)?.agent_name || null
  } else {
    name = (Array.isArray(asks) ? asks : []).find((a) => a && a.id === parsed.id)?.agent_name || null
  }
  if (!name) return null
  return (Array.isArray(agents) ? agents : []).find((a) => a && a.name === name) || null
}

// ---- The list's in-place rule (principle 5; §3g S1) ------------------------------
//
// Reading a chat zeroes its unread and answering an ask ends it — either drops
// the row out of Unread / Action, and a poll can reorder or thin any tab under
// the reader (A1, A7, A12). So the rows of one TAB VISIT keep their place:
//   - a row that leaves the fresh list stays where it was as a GHOST — a chat
//     drawn read (`readInPlace`), an ask drawn as it ended (`endedInPlace`);
//   - a row still listed keeps its position however the fresh order moved;
//   - a new key goes in before its nearest following neighbour in fresh order
//     (after the last row when nothing follows it);
//   - a ghost that comes back (a new arrival) lights up in place (T3);
//   - a deleted thread is dropped, never ghosted.
// The container starts a new visit when the tab changes, when the active tab is
// clicked again, and after a completed bulk mark-read — the only moments the
// list may re-sort under the reader.
//
// Pure and O(n): `visit` is `{ order: [key], snaps: {key: item} }` and is
// returned updated rather than mutated, so re-applying the same fresh list is a
// no-op (a lazily re-evaluated computed can call this more than once).

export function emptyVisit() {
  return { order: [], snaps: {} }
}

// A ghost is not a member of the tab any more; it is counted by nobody (the
// list head counts live rows only).
export function isGhost(it) {
  return !!(it && it.ghost)
}

function ghostOf(snap, live) {
  if (snap.type === 'thread') {
    const t = live.thread ? live.thread(snap.id) : null
    if (!t) return null // deleted
    return { ...snap, n: Number(t.unread) || 0, readInPlace: true, ghost: true }
  }
  if (snap.type === 'ask') {
    const a = live.ask ? live.ask(snap.id) : null
    return a
      ? { ...snap, status: a.status, ask: a, endedInPlace: a.status !== 'pending', ghost: true }
      : { ...snap, endedInPlace: true, ghost: true }
  }
  return null
}

// A kept chat whose refresh carries no preview (the read cleared it) keeps what
// was new, so the pane still shows it under the reader.
function withKeptPreview(it, snap) {
  if (it.type !== 'thread' || !snap || it.first_unread_message_id || it.latest) return it
  if (!snap.first_unread_message_id && !snap.latest) return it
  return {
    ...it,
    ...(snap.first_unread_message_id ? { first_unread_message_id: snap.first_unread_message_id } : {}),
    ...(snap.latest ? { latest: snap.latest } : {}),
  }
}

export function stableRows(fresh, visit, live = {}) {
  const list = (Array.isArray(fresh) ? fresh : []).filter((it) => it && it.key)
  const prevOrder = Array.isArray(visit?.order) ? visit.order : []
  const snaps = (visit && visit.snaps) || {}
  const freshBy = new Map(list.map((it) => [it.key, it]))
  const prevSet = new Set(prevOrder)

  // Each NEW key is anchored to the nearest following fresh key that was
  // already placed; `null` = after everything.
  const anchored = new Map() // anchor key | null → [new keys, fresh order]
  let next = null
  for (let i = list.length - 1; i >= 0; i--) {
    const k = list[i].key
    if (prevSet.has(k)) { next = k; continue }
    if (!anchored.has(next)) anchored.set(next, [])
    anchored.get(next).unshift(k)
  }

  const rows = []
  const order = []
  const nextSnaps = {}
  const place = (k) => {
    const it = freshBy.get(k)
    if (it) {
      const drawn = withKeptPreview(it, snaps[k])
      rows.push(drawn)
      order.push(k)
      nextSnaps[k] = drawn
      return
    }
    const snap = snaps[k]
    if (!snap) return
    const ghost = ghostOf(snap, live)
    if (!ghost) return
    rows.push(ghost)
    order.push(k)
    nextSnaps[k] = snap
  }
  for (const k of prevOrder) {
    for (const nk of anchored.get(k) || []) place(nk)
    place(k)
  }
  for (const nk of anchored.get(null) || []) place(nk)
  return { rows, visit: { order, snaps: nextSnaps } }
}

// The one fallback for a selection the rendered rows do not hold — an old chat
// (All stops at no age, but Unread and Action do not list it), a deep link, a
// row past the window. Built from the shell's data by key, whatever the tab.
export function resolveItem(key, { threads = [], asks = [], previews = {} } = {}) {
  const parsed = parseItemKey(key)
  if (!parsed) return null
  if (parsed.type === 'thread') {
    const t = (Array.isArray(threads) ? threads : []).find((x) => threadId(x) === parsed.id)
    return t ? threadItem(t, previews) : null
  }
  const a = (Array.isArray(asks) ? asks : []).find((x) => x && x.id === parsed.id)
  return a ? askItem(a) : null
}

// ---- Layout (§3g A4) ----------------------------------------------------------------
//
// Split (list beside pane) or stacked (list, then pane, with Back) is decided by
// the Inbox's CONTAINER width, never the viewport: the sidebar and the rail take
// width a viewport query cannot see (1280 with the rail open leaves ~608px).
// Split needs 720 (a 320 list + a 400 pane); from 1100 the list is 384 wide.
// 16px of hysteresis stops a resize — or the rail arriving — from flapping it.
// `allowance` is the rail's width while it is NOT yet a column (0 once it is):
// counting it before it arrives is what keeps a preview that brings the rail in
// from flipping the layout it was chosen in. An unmeasured container (width
// ≤ 0) falls back on the viewport; a phone viewport is always stacked.
export const SPLIT_MIN = 720
export const SPLIT_HYSTERESIS = 16
export const WIDE_MIN = 1100
export function inboxLayout({ width = 0, allowance = 0, phoneViewport = false, prev = null } = {}) {
  if (phoneViewport) return { mode: 'stacked', wide: false }
  const w = Number(width) || 0
  if (w <= 0) return { mode: 'split', wide: false }
  const eff = w - (Number(allowance) || 0)
  const splitAt = prev === 'split' ? SPLIT_MIN - SPLIT_HYSTERESIS : SPLIT_MIN
  const mode = eff >= splitAt ? 'split' : 'stacked'
  return { mode, wide: mode === 'split' && eff >= WIDE_MIN }
}

// ---- The pane (D11) -------------------------------------------------------------

export const PANE_HISTORY_LIMIT = 50
// A read chat (All) has no first unread message; the pane shows the tail.
export const PANE_TAIL = 5

// Which messages the pane renders. History `?limit=N` is the newest N rows of
// ANY role, so the unread arrivals are found by id, not assumed: from
// `first_unread_message_id` (inclusive) to the end. An id that fell out of the
// window is said, never silently under-claimed.
export function paneWindow(messages, firstUnreadId, n = 0) {
  const list = Array.isArray(messages) ? messages : []
  if (!firstUnreadId) return { shown: list.slice(-PANE_TAIL), earlier: 0 }
  const i = list.findIndex((m) => m && m.id === firstUnreadId)
  if (i >= 0) {
    // §3g S2 (A2): a long run of arrivals is capped at the tail too — 41 new
    // rendered 40 bubbles. `earlier` counts the hidden ARRIVALS (assistant
    // rows), the unit the pane's "N earlier arrivals" line names.
    const tail = list.slice(i)
    const shown = tail.slice(-PANE_TAIL)
    const hidden = tail.slice(0, tail.length - shown.length)
    return { shown, earlier: hidden.filter((m) => m && m.role === 'assistant').length }
  }
  const arrivals = list.filter((m) => m && m.role === 'assistant')
  const shown = arrivals.slice(-Math.max(1, Math.min(Number(n) || 1, PANE_TAIL)))
  return { shown, earlier: Math.max(0, (Number(n) || 0) - shown.length) }
}

// "Open in chat": the chat, anchored at what the reader was looking at — the
// first unread message, else the latest deliverable, else the bottom.
export function openInChatTarget(item) {
  if (!item || item.type !== 'thread' || !item.id) return null
  const base = `/workspace/c/${encodeURIComponent(item.id)}`
  if (item.first_unread_message_id) return `${base}?anchor=${encodeURIComponent(`m:${item.first_unread_message_id}`)}`
  if (item.latest?.kind === 'deliverable' && item.latest.id) {
    return `${base}?anchor=${encodeURIComponent(`d:${item.latest.id}`)}`
  }
  return base
}

// agent name → the human-facing label, the sidebar's rule (`display_label`
// trimmed, else the slug).
export function agentLabels(agents) {
  const out = {}
  for (const a of Array.isArray(agents) ? agents : []) {
    if (a && a.name) out[a.name] = String(a.display_label || '').trim() || a.name
  }
  return out
}
