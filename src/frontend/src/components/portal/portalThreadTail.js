// #3439 — the open thread's catch-up read, the pure half.
//
// A message an agent writes into the thread a person has OPEN after their own
// turn ended — a completion report, an approval outcome (#3709), a recovered
// lost turn (#2319), a background task's later reply — used to reach the
// sidebar's unread badge on the shell's 20 s tick and never the transcript:
// `PortalConversation.loadThread` ran on mount and on a thread switch only.
//
// The conversation already receives the shell's thread list as a prop and the
// tick re-stamps every row of it, so the row for the open thread IS the signal.
// These helpers decide what in that row counts as a change, how far back to
// read, and what to append — kept out of the component so the traps are pinned
// without a mount (`portalThreadTail.spec.js`).

// The server bounds `GET …/history?limit=` at 50 (`router.py`, `le=50`); a
// larger value is a 422, not a longer read.
export const TAIL_LIMIT_CAP = 50

/**
 * The value the conversation's watcher compares. A string, not the row: the
 * shell's `decorate()` returns NEW objects on every tick (stars, unread, drafts
 * re-stamped), and a watcher over the object — or over a tuple, which Vue
 * compares by identity — would re-read the tail every 20 s on every open chat.
 *
 * `message_count` and `last_message_at` only. Not `unread`: that counts
 * assistant rows past the read cursor, so it never moves for a system row (an
 * ask's discussion notice, a Main-reset notice) and is already reset by the
 * turn-end mark-read for a report that landed mid-turn. Both of those DO move
 * `last_message_at` (`touch_portal_session`). A deliverable touch moves the time
 * with `added=0` — that is a change too, and the caller treats "moved, nothing
 * fresh" as the card list's cue.
 */
export function tailSignature(row) {
  if (!row) return null
  return `${Number(row.message_count ?? 0)}|${row.last_message_at || ''}`
}

/** How many newest rows to read: the arrivals the badge counts, plus the reply poll's depth, capped. */
export function tailLimit(unread, base, cap = TAIL_LIMIT_CAP) {
  const n = Math.max(0, Number(unread) || 0)
  return Math.min(n + base, cap)
}

/**
 * The person's own question comes back from the server with an id; the row
 * `send()` pushed has none (`local: true`). Without this the first read after
 * their own turn — `deliver()` emits `sessions-changed`, the shell refreshes
 * the list, `last_message_at` has moved — would append the server's copy and
 * show the question twice. Adopt the id (and the server time) onto the local
 * row instead, newest local row first, one server row per local row. In place:
 * the bubble keeps its identity.
 */
export function adoptLocalIds(existing, fetched) {
  const known = new Set((existing || []).map((m) => m?.id).filter(Boolean))
  const locals = (existing || []).filter((m) => m && m.role === 'user' && !m.id).reverse()
  for (const row of fetched || []) {
    if (!row?.id || row.role !== 'user' || known.has(row.id)) continue
    const i = locals.findIndex((m) => m.content === row.content)
    if (i < 0) continue
    const [local] = locals.splice(i, 1)
    local.id = row.id
    if (row.created_at) local.at = row.created_at
    known.add(row.id)
  }
}

/** The fetched rows not already on screen, by id, in server (oldest-first) order. Rows without an id are skipped. */
export function freshRows(existing, fetched) {
  const have = new Set((existing || []).map((m) => m?.id).filter(Boolean))
  return (fetched || []).filter((m) => m?.id && !have.has(m.id))
}

/**
 * A bounded read that is ENTIRELY unseen may have missed rows behind it. Only
 * when it is as long as the limit (shorter means the whole thread came back)
 * and the transcript had something to miss.
 */
export function tailOverflowed(existing, fetched, limit) {
  const rows = fetched || []
  if (!rows.length || rows.length < limit || !(existing || []).length) return false
  return freshRows(existing, rows).length === rows.length
}
