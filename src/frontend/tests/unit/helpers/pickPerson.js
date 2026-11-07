/**
 * Driving a BasePeopleInput from a mounted spec (trinity-enterprise#1154).
 *
 * `candidateAnswer(users, holders)` stands in for a scoped candidate read: the
 * people whose name, username or email starts with the query, never a
 * suspended account, never someone `holders(kind)` names. `pickPerson` types a
 * prefix, waits out the field's pause on fake timers, and clicks the row.
 */
import { flushPromises } from '@vue/test-utils'
import { vi } from 'vitest'

export function candidateAnswer(users, holders = () => []) {
  return (params = {}) => {
    const q = String(params.q || '').trim().toLowerCase()
    const held = new Set(holders(params.kind))
    return users
      .filter((u) => !u.suspended_at && !held.has(u.id))
      .filter((u) => [u.name, u.username, u.email].some((v) => String(v || '').toLowerCase().startsWith(q)))
      .slice(0, 8)
      .map((u) => ({
        user_id: u.id,
        email: String(u.email || `${u.username}@example.com`).toLowerCase(),
        label: u.name || u.username,
        username: u.username,
        role: u.role || null,
      }))
  }
}

/** Type `text` into the picker `testid`, then pick the row whose text includes `name`. */
export async function pickPerson(w, testid, text, name = text) {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  try {
    await w.find(`[data-testid="${testid}"]`).setValue(text)
    vi.advanceTimersByTime(250)
    await flushPromises()
  } finally {
    vi.useRealTimers()
  }
  const row = w.findAll('[role="option"]').find((o) => o.text().includes(name))
  if (!row) throw new Error(`no row for ${name} in ${testid}: ${w.findAll('[role="option"]').map((o) => o.text())}`)
  await row.trigger('mousedown')
  await flushPromises()
}

/** Type without picking, and let the answer arrive. */
export async function typePerson(w, testid, text) {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  try {
    await w.find(`[data-testid="${testid}"]`).setValue(text)
    vi.advanceTimersByTime(250)
    await flushPromises()
  } finally {
    vi.useRealTimers()
  }
}
