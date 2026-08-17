/**
 * Mutating a pushed message must go THROUGH the reactive array (ent#358).
 *
 * A failed send in the Workspace showed no error — not because the reason was
 * missing, but because nothing re-rendered. `send()` did:
 *
 *     const msg = { role: 'user', content, failed: false }
 *     messages.value.push(msg)
 *     ...
 *     msg.failed = true          // ← writes past the proxy
 *
 * `ref([])` stores what you push as the raw target and only proxies it when you
 * read `messages.value[i]`. Mutating the local variable changes the value and
 * notifies nobody, so the view keeps rendering the old state. `retry()` never
 * had the bug because it starts from `messages.value[i]`.
 *
 * This pins the mechanism rather than the component (no DOM needed): a watcher
 * stands in for the render, and the raw-mutation case must NOT wake it.
 */
import { describe, it, expect } from 'vitest'
import { ref, watch, nextTick } from 'vue'

async function renders(mutate) {
  const messages = ref([])
  let renders = 0
  watch(messages, () => { renders += 1 }, { deep: true })

  const raw = { role: 'user', content: 'hi', failed: false, error: null }
  const index = messages.value.push(raw) - 1
  await nextTick()
  const before = renders

  mutate({ messages, raw, index })
  await nextTick()
  return { woke: renders > before, state: messages.value[index] }
}

describe('failed-message mutation', () => {
  it('mutating the pushed object directly does NOT re-render — the original bug', async () => {
    const { woke } = await renders(({ raw }) => { raw.failed = true })
    expect(woke).toBe(false)
  })

  it('mutating through the array DOES re-render — the fix', async () => {
    const { woke, state } = await renders(({ messages, index }) => {
      messages.value[index].failed = true
      messages.value[index].error = 'The agent could not respond.'
    })
    expect(woke).toBe(true)
    expect(state.failed).toBe(true)
    expect(state.error).toMatch(/could not respond/)
  })
})
