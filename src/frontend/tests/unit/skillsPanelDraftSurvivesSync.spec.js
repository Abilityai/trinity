// @vitest-environment jsdom
/**
 * #2914 — an unsaved tick survives a Sync, MOUNTED.
 *
 * `inject()` re-reads the assignment rows (the conflict verdict rides them),
 * so the panel sees a brand-new `assigned` array after every Sync even when
 * the assignment SET is unchanged. The shipped defect was a deep watch on
 * that array: it called `resetDraft()` on each refetch and wiped whatever the
 * operator had ticked but not yet saved. The fix watches the set IDENTITY
 * (`SkillsPanel.vue`), and until now nothing executed it — the panel had no
 * spec at all, so reverting the fix left CI byte-identically green.
 *
 * Both directions are pinned, because "never reset" would also pass the first
 * test on its own: the draft must survive a same-set refetch AND still follow
 * a real set change.
 *
 * Mount harness per the #2918 / ent#625 precedent (portalComposerDraft.spec.js).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn() },
}))
vi.mock('@/api', () => ({ default: api }))
vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ role: 'admin', isAuthenticated: true }),
}))

import SkillsPanel from '../../src/components/SkillsPanel.vue'
import { useSkillsStore } from '../../src/stores/skills'

const AGENT = 'agent-x'
const LIBRARY = [
  { name: 'alpha', description: 'first' },
  { name: 'beta', description: 'second' },
  { name: 'gamma', description: 'third' },
]
// The assignment SET is {alpha} throughout the first test — only the row
// object changes, exactly as a Sync rewrites the durable verdict.
const rows = (status) => [{ skill_name: 'alpha', delivery_status: status }]

function respond({ assigned = rows(null) } = {}) {
  api.get.mockImplementation((url) => {
    if (url === '/api/skills/library/status') return Promise.resolve({ data: { configured: true, skill_count: 3 } })
    if (url === '/api/skills/library') return Promise.resolve({ data: LIBRARY })
    if (url === `/api/agents/${AGENT}/skills`) return Promise.resolve({ data: assigned })
    return Promise.resolve({ data: [] })
  })
}

async function mountPanel() {
  const wrapper = mount(SkillsPanel, {
    props: { agentName: AGENT, canManage: true, agentRunning: true },
    attachTo: document.body,
    global: { stubs: { SkillContractChips: true } },
  })
  await flushPromises()
  return wrapper
}

const box = (wrapper, name) => wrapper.find(`input[type="checkbox"][value="${name}"]`)

beforeEach(() => {
  setActivePinia(createPinia())
  api.get.mockReset(); api.post.mockReset(); api.put.mockReset()
  respond()
})

describe('#2914 — the draft and a Sync', () => {
  it('keeps an unsaved tick when Sync re-reads the SAME assignment set', async () => {
    const wrapper = await mountPanel()
    const store = useSkillsStore()

    expect(box(wrapper, 'alpha').element.checked).toBe(true)   // from the server
    expect(box(wrapper, 'beta').element.checked).toBe(false)

    await box(wrapper, 'beta').setValue(true)                  // the operator ticks it
    expect(wrapper.text()).toContain('Reset')                  // `dirty` — nothing saved yet

    // Sync: the rows come back as a NEW array carrying a new verdict, same set.
    api.post.mockResolvedValue({ data: { results: {} } })
    respond({ assigned: rows('conflict') })
    await store.inject()
    await flushPromises(); await nextTick()

    expect(store.assigned[0].delivery_status).toBe('conflict')  // the refetch really happened
    expect(box(wrapper, 'beta').element.checked).toBe(true)     // and the tick survived it
    expect(box(wrapper, 'alpha').element.checked).toBe(true)
  })

  it('still follows a real change to the assignment set', async () => {
    const wrapper = await mountPanel()
    const store = useSkillsStore()

    await box(wrapper, 'beta').setValue(true)
    expect(box(wrapper, 'beta').element.checked).toBe(true)

    // The set genuinely changed underneath (another tab saved {alpha, gamma}).
    store.assigned = [
      { skill_name: 'alpha', delivery_status: 'injected' },
      { skill_name: 'gamma', delivery_status: 'injected' },
    ]
    await nextTick(); await nextTick()

    expect(box(wrapper, 'gamma').element.checked).toBe(true)   // reset to the server's set
    expect(box(wrapper, 'beta').element.checked).toBe(false)   // the stale local tick is gone
  })
})
