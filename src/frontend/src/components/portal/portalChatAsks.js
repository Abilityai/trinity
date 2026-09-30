// trinity-enterprise#610 — the 2026-09-30 ruling as amended the same day, on
// ent#734's data (#3137). Two kinds of ask, two homes:
//
//   raised during a chat turn → a tile inside THAT chat's thread, answerable in
//     place; once ended, a muted row in the chat's history.
//   raised by a background process (schedule, loop, gate) → the Inbox only.
//     Its `chat_id` is Main, but only as the reply target: it renders in no chat.
//
// The pinned box above the composer that this replaces grew with the number of
// asks and moved the composer per chat (design principle 30).

import { queueEnding, queueEndingText, queueTypeLabel } from '@/utils/operatorQueue'
import { relativeTime } from './portalUtils'

// The asks drawn in the chat on screen. Only the platform's literal `true`
// counts (ent#734 writes it; the backend projection already coerces, and this
// repeats the rule so a stray truthy value can never draw a background ask).
export function chatTurnAsks(asks, sessionId) {
  if (!sessionId || !Array.isArray(asks)) return []
  return asks.filter((a) => a && a.raised_in_turn === true && a.chat_id === sessionId)
}

const ms = (iso) => {
  const t = Date.parse(iso || '')
  return Number.isFinite(t) ? t : null
}

function rowTime(item) {
  if (item?.kind === 'voice-call') {
    for (const t of item.turns || []) {
      const at = ms(t?.at)
      if (at !== null) return at
    }
    return null
  }
  return ms(item?.message?.at)
}

// The thread's rows with each ask placed by time: before the first row that is
// strictly newer than the ask (a tie goes after the row). A row with no time of
// its own is a reply this client just received, so it is the newest thing in the
// thread: a live turn's ask lands above the reply it came before. (The client
// stamps the person's own message when it sends it, so the ask lands below that.)
export function placeAsksInThread(items, asks) {
  const rows = Array.isArray(items) ? items : []
  if (!Array.isArray(asks) || !asks.length) return rows
  const pending = asks
    .map((a) => ({ a, at: ms(a.created_at) ?? -Infinity }))
    .sort((x, y) => (x.at - y.at) || String(x.a.id).localeCompare(String(y.a.id)))
  const out = []
  let next = 0
  for (const item of rows) {
    const at = rowTime(item) ?? Infinity
    while (next < pending.length && pending[next].at < at) {
      out.push({ kind: 'ask', ask: pending[next].a })
      next += 1
    }
    out.push(item)
  }
  for (; next < pending.length; next += 1) out.push({ kind: 'ask', ask: pending[next].a })
  return out
}

// A waiting ask is the answerable card. So is one that ended while this visit
// watched it wait — the ent#468 confirmation sits on that card. Any other ended
// ask is history: one muted row.
export function askTileMode(ask, seenPending = new Set()) {
  return ask?.status === 'pending' || seenPending.has(ask?.id) ? 'card' : 'row'
}

// The history row: kind · title · ending (with who) · when. The kind is a noun
// here: the card's "Needs approval" is a state, and beside "Answered by you" it
// would say two contradicting things.
const HISTORY_KIND = Object.freeze({ approval: 'Approval', question: 'Question', alert: 'Alert' })
export function askHistoryLine(ask, now = Date.now()) {
  const ending = queueEnding(ask)
  const at = ending?.when || null
  return {
    kind: HISTORY_KIND[ask?.kind] || queueTypeLabel(ask?.kind) || 'Question',
    title: ask?.title || '',
    ending: queueEndingText(ending),
    when: at ? relativeTime(at, now) : '',
    at,
  }
}
