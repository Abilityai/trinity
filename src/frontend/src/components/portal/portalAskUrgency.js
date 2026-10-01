// trinity-enterprise#610 §3g A10 — how urgent an ask is, in one module.
//
// The row anatomy (A10), the Action sort (C1) and the ask context line (E1) all
// read the same two facts — the priority and the expiry — so the words and the
// thresholds live here once. Pure, with `now` injected: a component that
// formats its own times is one whose wording no test can reach.

const HOUR_MS = 60 * 60 * 1000
const DAY_MS = 24 * HOUR_MS

// T9: only the priorities that change what the reader does are said. Medium is
// the default and low is a reason to wait — badging them is noise on every row.
export function priorityBadge(priority) {
  if (priority === 'critical') return { variant: 'danger', label: 'Critical' }
  if (priority === 'high') return { variant: 'urgent', label: 'High' }
  return null
}

// Within the hour it is a warning; within the day a neutral fact — the reason an
// ask sits where it does in the Action order (C1, the 24h bucket). Past a day,
// in the past, or unreadable: nothing (a wrong time is worse than none).
export function expiresSoonLabel(expiresAt, now = Date.now()) {
  if (!expiresAt) return null
  const at = Date.parse(expiresAt)
  if (!Number.isFinite(at)) return null
  const ms = at - now
  if (ms <= 0 || ms > DAY_MS) return null
  // Floored like the hours (round 3): rounded, 59m30s+ read "60m".
  if (ms < HOUR_MS) return { label: `Expires in ${Math.max(1, Math.floor(ms / 60000))}m`, variant: 'warning' }
  return { label: `Expires in ${Math.floor(ms / HOUR_MS)}h`, variant: 'neutral' }
}

// The row's 30 s clock runs only while a pending ask has an expiry to count
// down within the day — never an idle timer on a list that has nothing to move.
export function needsExpiryTick(asks, now = Date.now()) {
  return (Array.isArray(asks) ? asks : []).some((a) => a && a.status === 'pending' && !!expiresSoonLabel(a.expires_at, now))
}

// When the next pending ask ENTERS the day (ms from `now`), or null. With no
// row inside the window the 30 s clock is off and nothing moves `now`, so an
// ask 25h out never got its badge as it crossed in (round 3): the list sets one
// timer for this instead of ticking for days.
export function nextExpiryEntry(asks, now = Date.now()) {
  let soonest = null
  for (const a of Array.isArray(asks) ? asks : []) {
    if (!a || a.status !== 'pending' || !a.expires_at) continue
    const at = Date.parse(a.expires_at)
    if (!Number.isFinite(at)) continue
    const wait = at - DAY_MS - now
    if (wait > 0 && (soonest === null || wait < soonest)) soonest = wait
  }
  return soonest
}

// Identity by SHAPE (principle 24): an approval, a question and anything else
// (an alert the agent raised) are three outlines, never three hues.
const ICONS = {
  approval: { name: 'shield-check', path: 'M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z' },
  question: { name: 'question-mark-circle', path: 'M8.228 9c.549-1.165 2.03-2 3.772-2 2.21 0 4 1.343 4 3 0 1.4-1.278 2.575-3.006 2.907-.542.104-.994.54-.994 1.093m0 3h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z' },
  alert: { name: 'bell', path: 'M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9' },
}
export function askKindIcon(kind) {
  if (kind === 'approval') return ICONS.approval
  if (kind === 'question') return ICONS.question
  return ICONS.alert
}
