// trinity-enterprise#1010 — the read write's `through`: the newest instant the
// client actually held. Compared as instants, returned in the spelling it came
// in: the server stores it verbatim and compares stored values as strings, so a
// message's own `created_at` sent back unchanged reads exactly that message.
export function laterInstant(a, b) {
  const ta = a ? Date.parse(a) : NaN
  const tb = b ? Date.parse(b) : NaN
  if (Number.isNaN(ta)) return Number.isNaN(tb) ? null : b
  if (Number.isNaN(tb)) return a
  return tb > ta ? b : a
}

// The newest `field` instant in a list of rendered rows, or null for none.
export function newestAt(rows, field = 'created_at') {
  let out = null
  for (const r of rows || []) out = laterInstant(out, r && r[field])
  return out
}

// "Read up to now": the server's own clock, the meaning before #1010. Used when
// the newest row on screen carries no instant (a live reply the synchronous
// path returned without one), so the person's own just-watched reply is never
// left badged because the client could not name when it landed.
export const READ_UP_TO_NOW = 'now'

// What a view that renders `rows` held: the newest row's instant, or
// READ_UP_TO_NOW when the newest row has none. An empty view says nothing
// (null), and the read falls back to the last chat-state `as_of`.
export function heldThrough(rows, field = 'created_at') {
  const list = rows || []
  if (!list.length) return null
  const last = list[list.length - 1]
  if (!(last && last[field]) || Number.isNaN(Date.parse(last[field]))) return READ_UP_TO_NOW
  return newestAt(list, field)
}
