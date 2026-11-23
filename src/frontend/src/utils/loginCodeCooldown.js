/**
 * #4603 — the login page's own clock for sign-in code requests.
 *
 * Trinity sends at most PER_ADDRESS codes per WINDOW to one address, a budget
 * shared with Telegram, Slack, WhatsApp and MCP sign-in
 * (`services/channel_login_limit.py`). The rule is published and the same for
 * every address, so the page can keep its own count without learning anything
 * from the server. A client clock understates the server's count (another
 * browser or a channel may have spent it), so the copy states the rule, never a
 * guaranteed countdown.
 *
 * Kept per normalised address in sessionStorage, so going back to the email
 * step, or reloading, does not reset it. Storage that throws falls back to
 * memory for this page's life.
 */
export const PER_ADDRESS = 3
export const WINDOW_MS = 10 * 60 * 1000
export const LOCK_MS = 60 * 1000

const KEY = 'trinity.loginCodeRequests'
let memory = {}

const normalise = (email) => (email || '').trim().toLowerCase()

function load() {
  try {
    const raw = window.sessionStorage.getItem(KEY)
    const parsed = raw ? JSON.parse(raw) : {}
    if (parsed && typeof parsed === 'object') return parsed
  } catch {
    // storage blocked or unreadable: fall through to memory
  }
  return memory
}

function save(all) {
  memory = all
  try {
    window.sessionStorage.setItem(KEY, JSON.stringify(all))
  } catch {
    // memory already holds it
  }
}

function recent(all, address, now) {
  const times = Array.isArray(all[address]) ? all[address] : []
  return times.filter((t) => typeof t === 'number' && t > now - WINDOW_MS && t <= now)
}

/** Note one request for `email` at `now` (ms). */
export function recordCodeRequest(email, now = Date.now()) {
  const address = normalise(email)
  if (!address) return
  const all = load()
  all[address] = [...recent(all, address, now), now]
  save(all)
}

/** Note that the server refused `email` as over budget: hold it for the window. */
export function recordOverBudget(email, now = Date.now()) {
  const address = normalise(email)
  if (!address) return
  const all = load()
  const times = recent(all, address, now)
  while (times.length < PER_ADDRESS) times.push(now)
  all[address] = times
  save(all)
}

/** How long (ms) until the page will ask for another code for `email`. */
export function codeRequestWaitMs(email, now = Date.now()) {
  const address = normalise(email)
  if (!address) return 0
  const times = recent(load(), address, now).sort((a, b) => a - b)
  if (!times.length) return 0
  let until = times[times.length - 1] + LOCK_MS
  if (times.length >= PER_ADDRESS) {
    until = Math.max(until, times[times.length - PER_ADDRESS] + WINDOW_MS)
  }
  return Math.max(0, until - now)
}

/** "4:12" */
export function formatWait(ms) {
  const total = Math.ceil(ms / 1000)
  const mins = Math.floor(total / 60)
  const secs = total % 60
  return `${mins}:${String(secs).padStart(2, '0')}`
}
