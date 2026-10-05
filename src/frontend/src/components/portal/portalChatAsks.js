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
// strictly newer than the ask (a tie goes after the row). Only SERVER times are
// compared — an ask's `created_at` is the server's, so the browser's clock never
// takes part (review round 2: a clock 30 s ahead lifted the ask above the
// message that raised it):
//   - the person's own message, sent from here, carries no time but `local`: it
//     sits at the newest server time before it, so an ask this turn raises lands
//     below it and an ask of an earlier turn stays above it;
//   - any other row with no time is a reply this client just received (one
//     whose stored time did not come with it): the newest thing in the thread,
//     so a live turn's ask lands above the reply it came before.
// `truncated` (earlier messages are not shown): an ask older than the first row
// shown belongs to the part that is not, so it is left out rather than stacked
// on top with none of its conversation around it.
export function placeAsksInThread(items, asks, { truncated = false } = {}) {
  const rows = Array.isArray(items) ? items : []
  if (!Array.isArray(asks) || !asks.length) return rows
  let pending = asks
    .map((a) => ({ a, at: ms(a.created_at) ?? -Infinity }))
    .sort((x, y) => (x.at - y.at) || String(x.a.id).localeCompare(String(y.a.id)))
  if (truncated) {
    const first = rows.map(rowTime).find((t) => t !== null)
    if (first !== undefined) pending = pending.filter((p) => p.at >= first)
  }
  const out = []
  let next = 0
  let lastAt = -Infinity
  for (const item of rows) {
    let at = rowTime(item)
    if (at !== null) lastAt = Math.max(lastAt, at)
    else at = item?.message?.local === true ? lastAt : Infinity
    while (next < pending.length && pending[next].at < at) {
      out.push({ kind: 'ask', ask: pending[next].a })
      next += 1
    }
    out.push(item)
  }
  for (; next < pending.length; next += 1) out.push({ kind: 'ask', ask: pending[next].a })
  return out
}

// A waiting ask is the answerable card; an ended one is history, one muted
// row — the moment its answer is recorded, even on the visit that answered it
// (the team's ruling on #3101, 2026-10-01: the confirmation lives in the row,
// announced, and focus moves to it). A failed answer leaves the ask waiting, so
// its card stays open with the error.
export function askTileMode(ask) {
  return ask?.status === 'pending' ? 'card' : 'row'
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
    // The row clips the title on a phone; this is its hover/long-press text.
    plain: (ask?.title || '').replace(/[*_`~]/g, ''),
    ending: queueEndingText(ending),
    when: at ? relativeTime(at, now) : '',
    at,
  }
}
