// What the Chat tab does with a session re-read while it is already on screen
// (#4537: a wake's reply written into the thread outside any turn the panel
// waited on).
//
// The screen can hold rows the server does not: a turn's own messages are
// pushed locally the moment they exist and saved a moment later, and a failed
// or cancelled turn leaves a message that is never saved. So a re-read is not
// "replace when it is longer" — after a failed turn the counts are equal and
// the new reply would be dropped. Rows are matched by identity instead.

/** A stored message, in the shape the panel renders. Keeps `id`: it is the identity. */
export function panelMessage(msg) {
  return {
    id: msg.id,
    role: msg.role,
    content: msg.content,
    timestamp: msg.timestamp,
    source: msg.source || 'text',
    held: msg.held,
  }
}

const sameText = (a, b) => String(a ?? '').trim() === String(b ?? '').trim()

/**
 * The list to show after a re-read, or `null` when the server holds nothing
 * the screen does not already show (so nothing a person is reading moves).
 *
 * A server row is on screen when a row carries its id, or when a row the panel
 * pushed itself (no id yet) has the same role and text — each such row matches
 * once. Rows only the screen has are kept where they were: right after the
 * message they followed. Order comes from position, never from timestamps (a
 * local row carries the browser's clock, a stored one the server's).
 *
 * @param {Array} onScreen  the panel's `messages`
 * @param {Array} fresh     the session's messages, already `panelMessage`d
 * @returns {Array|null}
 */
export function mergeSessionMessages(onScreen = [], fresh = []) {
  // on-screen index → index of the server row it is
  const match = new Map()
  const used = new Set()
  fresh.forEach((row, j) => {
    let i = row.id ? onScreen.findIndex((m, k) => !used.has(k) && m.id === row.id) : -1
    if (i < 0) i = onScreen.findIndex((m, k) => !used.has(k) && !m.id && m.role === row.role && sameText(m.content, row.content))
    if (i >= 0) { used.add(i); match.set(i, j) }
  })
  if (used.size === fresh.length) return null

  // Rows only the screen has, grouped under the server row they came after (-1: before all).
  const after = new Map()
  let anchor = -1
  onScreen.forEach((m, i) => {
    if (match.has(i)) { anchor = Math.max(anchor, match.get(i)); return }
    after.set(anchor, [...(after.get(anchor) || []), m])
  })
  const out = [...(after.get(-1) || [])]
  fresh.forEach((row, j) => { out.push(row, ...(after.get(j) || [])) })
  return out
}
