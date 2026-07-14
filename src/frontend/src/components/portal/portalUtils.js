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
