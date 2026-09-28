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

import { STAGE_QUERY_KEYS, WORKSPACE_ROOT, WORKSPACE_INBOX, totalUnread } from './portalUtils'

export const INBOX_TABS = ['action', 'unread', 'all']

// All: chats with activity in the last 30 days, ended asks for the 7 days the
// server keeps them (`ENDED_WINDOW_DAYS`), at most 50 rows with the total stated.
export const ALL_WINDOW_DAYS = 30
export const ENDED_ASK_WINDOW_DAYS = 7
export const ALL_LIMIT = 50

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
    // message, so the later of the two orders the row.
    at: ts(latestAt) >= ts(lastAt) ? (latestAt || lastAt) : lastAt,
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

// All: read and unread chats active in the last 30 days, merged with the asks
// list — pending asks always, ended ones for 7 days. Bounded to 50 rows; the
// total is returned so the list can state it ("12 · latest 50 shown").
export function allItems(threads, asks, previews, now = Date.now()) {
  const chatCutoff = now - ALL_WINDOW_DAYS * DAY_MS
  const askCutoff = now - ENDED_ASK_WINDOW_DAYS * DAY_MS
  const chats = (Array.isArray(threads) ? threads : [])
    .filter((t) => t && !t.is_room && threadId(t))
    .map((t) => threadItem(t, previews))
    .filter((it) => ts(it.at) >= chatCutoff)
  const askRows = (Array.isArray(asks) ? asks : [])
    .filter((a) => a && a.id)
    .map(askItem)
    .filter((it) => it.status === 'pending' || ts(it.at) >= askCutoff)
  const merged = [...chats, ...askRows].sort(byAtDesc)
  return { items: merged.slice(0, ALL_LIMIT), total: merged.length }
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

// Bounded lists state their total (contract: "412 · latest 50 shown").
export function totalLabel(total, shown) {
  const t = Number(total) || 0
  const s = Number(shown) || 0
  return t > s ? `${t} · latest ${s} shown` : `${t}`
}
