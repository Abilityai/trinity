// @vitest-environment jsdom
/**
 * trinity-enterprise#657 — the composer binding and the three marks, executed.
 *
 * `useComposerDraft` is driven on live refs (real Vue watchers, the real
 * store, a fake Storage); `PortalChatRow`, `PortalChatTabs` and `OverflowTabs`
 * are MOUNTED (`@vue/test-utils`, per-file jsdom — the ent#625 precedent) so
 * the mark is proven to render from data and the provisional tab's click is
 * proven to reach `new-chat`. What jsdom cannot see — widths, the More menu's
 * packing — is left to the hermetic e2e and said so.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { ref, nextTick } from 'vue'
import { draftStorageKey, loadDrafts } from '../../src/components/portal/portalDrafts'
import PortalChatRow from '../../src/components/portal/PortalChatRow.vue'
import PortalChatTabs from '../../src/components/portal/PortalChatTabs.vue'
import OverflowTabs from '../../src/components/OverflowTabs.vue'
import { usePortalDraftsStore } from '../../src/stores/portalDrafts'
import { useComposerDraft } from '../../src/composables/useComposerDraft'

const { __portal } = vi.hoisted(() => ({ __portal: { stub: null } }))
vi.mock('@/stores/clientPortal', async () => {
  const { reactive } = await import('vue')
  __portal.stub = reactive({ clientEmail: 'ada@example.com' })
  return { useClientPortalStore: () => __portal.stub }
})

// jsdom has no ResizeObserver; OverflowTabs installs one at mount.
class RO { observe() {} unobserve() {} disconnect() {} }

const MARK = '[data-testid="draft-mark"]'

async function stores() {
  return { drafts: usePortalDraftsStore(), useComposerDraft }
}

beforeEach(() => {
  window.localStorage.clear()
  globalThis.ResizeObserver = RO
  __portal.stub.clientEmail = 'ada@example.com'
  setActivePinia(createPinia())
})

describe('useComposerDraft — the composer is the draft', () => {
  it('restores a stored draft into an empty composer at setup and reports it', async () => {
    const { drafts, useComposerDraft } = await stores()
    drafts.set('thread:1', 'left here')
    const input = ref('')
    const { restored } = useComposerDraft({ key: ref('thread:1'), input })
    expect(input.value).toBe('left here')
    expect(restored).toBe(true)
  })

  it('never overwrites text already in the composer (a prefill applied first wins)', async () => {
    const { drafts, useComposerDraft } = await stores()
    drafts.set('thread:1', 'stored')
    const input = ref('explicit prefill')
    const { restored } = useComposerDraft({ key: ref('thread:1'), input })
    expect(input.value).toBe('explicit prefill')
    expect(restored).toBe(false)
  })

  it('writes through on every change and clears when the field empties (the send path)', async () => {
    const { drafts, useComposerDraft } = await stores()
    const input = ref('')
    useComposerDraft({ key: ref('thread:1'), input })
    input.value = 'hel'
    await nextTick()
    expect(drafts.get('thread:1')).toBe('hel')
    expect(loadDrafts(window.localStorage, 'ada@example.com')['thread:1'].text).toBe('hel')
    input.value = 'hello'
    await nextTick()
    expect(drafts.get('thread:1')).toBe('hello')
    input.value = ''            // exactly what send() does before dispatch
    await nextTick()
    expect(drafts.has('thread:1')).toBe(false)
    expect(loadDrafts(window.localStorage, 'ada@example.com')).toEqual({})
    input.value = '   '
    await nextTick()
    expect(drafts.has('thread:1')).toBe(false)
  })

  it('with no key (an unresolved thread) nothing is written', async () => {
    const { drafts, useComposerDraft } = await stores()
    const input = ref('')
    useComposerDraft({ key: ref(null), input })
    input.value = 'typed while resolving'
    await nextTick()
    expect([...drafts.keys]).toEqual([])
  })

  it('session adoption (new: → thread:) moves what was typed during the turn', async () => {
    const { drafts, useComposerDraft } = await stores()
    const key = ref('new:scribe')
    const input = ref('')
    useComposerDraft({ key, input })
    input.value = 'typed mid-turn'
    await nextTick()
    expect(drafts.get('new:scribe')).toBe('typed mid-turn')
    key.value = 'thread:x'
    await nextTick()
    expect(input.value).toBe('typed mid-turn')
    expect(drafts.has('new:scribe')).toBe(false)
    expect(drafts.get('thread:x')).toBe('typed mid-turn')
  })

  it('resolution (null → thread:) fills an empty composer and never clobbers typed text', async () => {
    const { drafts, useComposerDraft } = await stores()
    drafts.set('thread:x', 'stored for x')
    const key = ref(null)
    const input = ref('')
    useComposerDraft({ key, input })
    key.value = 'thread:x'
    await nextTick()
    expect(input.value).toBe('stored for x')

    setActivePinia(createPinia())
    const again = await stores()
    again.drafts.set('thread:y', 'stored for y')
    const key2 = ref(null)
    const input2 = ref('')
    again.useComposerDraft({ key: key2, input: input2 })
    input2.value = 'typed first'
    key2.value = 'thread:y'
    await nextTick()
    expect(input2.value).toBe('typed first')
    expect(again.drafts.get('thread:y')).toBe('typed first')
  })

  it('→ null (openAgentPage on the current agent) leaves the composer alone', async () => {
    const { drafts, useComposerDraft } = await stores()
    const key = ref('thread:x')
    const input = ref('')
    useComposerDraft({ key, input })
    input.value = 'keep me'
    await nextTick()
    key.value = null
    await nextTick()
    expect(input.value).toBe('keep me')
    expect(drafts.get('thread:x')).toBe('keep me')
  })
})

describe('the store hears another tab', () => {
  it('a `storage` event for the current bucket re-reads it', async () => {
    const { drafts } = await stores()
    const bucket = draftStorageKey('ada@example.com')
    // "Another tab" writes the bucket directly, then the browser fires the event.
    window.localStorage.setItem(bucket, JSON.stringify({ v: 1, drafts: { 'thread:other': { text: 'from tab A', updatedAt: 1 } } }))
    window.dispatchEvent(new StorageEvent('storage', { key: bucket, newValue: window.localStorage.getItem(bucket) }))
    expect(drafts.get('thread:other')).toBe('from tab A')
    // An event for some other key is ignored.
    window.localStorage.setItem(bucket, JSON.stringify({ v: 1, drafts: {} }))
    window.dispatchEvent(new StorageEvent('storage', { key: 'trinity-theme', newValue: 'dark' }))
    expect(drafts.get('thread:other')).toBe('from tab A')
  })
})

describe('PortalChatRow — the mark rides the thread object', () => {
  const base = { id: 't1', agent_name: 'scribe', title: 'Q3 invoices', unread: 0 }
  it('renders the mark for a thread with a draft and not otherwise', () => {
    const w = mount(PortalChatRow, { props: { thread: { ...base, hasDraft: true } } })
    expect(w.find(MARK).exists()).toBe(true)
    expect(w.text()).toContain('Draft')
    const none = mount(PortalChatRow, { props: { thread: { ...base } } })
    expect(none.find(MARK).exists()).toBe(false)
  })
  it('a room row carries it too', () => {
    const w = mount(PortalChatRow, { props: { thread: { id: 'r1', is_room: true, agent_names: ['a', 'b'], title: 'Room', hasDraft: true } } })
    expect(w.find(MARK).exists()).toBe(true)
  })
})

describe('PortalChatTabs — the mark on a tab, and the provisional tab a draft makes reachable', () => {
  const threads = [
    { id: 'main', agent_name: 'scribe', is_main: true, title: '', last_message_at: '2026-09-01T00:00:00Z' },
    { id: 't2', agent_name: 'scribe', title: 'Second', last_message_at: '2026-09-02T00:00:00Z' },
  ]
  // The valueless `data-*` attributes read as '' through `attributes()`, so
  // ask the element: the hidden mirror row renders every tab a second time.
  const tabButtons = (w) => w.findAll('nav button').filter((b) => !b.element.hasAttribute('data-overflow-trigger') && !b.element.hasAttribute('data-measure-tab'))

  it('marks exactly the tab whose thread has a draft', () => {
    const w = mount(PortalChatTabs, { props: { threads, agentName: 'scribe', activeId: 'main', draftKeys: new Set(['thread:t2']) } })
    const buttons = tabButtons(w)
    const second = buttons.find((b) => b.text().includes('Second'))
    const main = buttons.find((b) => b.text().includes('Main'))
    expect(second.find(MARK).exists()).toBe(true)
    expect(main.find(MARK).exists()).toBe(false)
    // …and the mirror row (what the fit decision measures) carries it too.
    const mirror = w.findAll('[data-measure-tab]')
    expect(mirror.some((b) => b.text().includes('Second') && b.find(MARK).exists())).toBe(true)
  })

  it('lists the provisional New chat tab for an unsaved-chat draft while another chat is open, and its click emits new-chat', async () => {
    const w = mount(PortalChatTabs, { props: { threads, agentName: 'scribe', activeId: 'main', draft: false, draftKeys: new Set(['new:scribe']) } })
    const provisional = tabButtons(w).find((b) => b.text().includes('New chat'))
    expect(provisional).toBeTruthy()
    expect(provisional.find(MARK).exists()).toBe(true)
    await provisional.trigger('click')
    expect(w.emitted('new-chat')).toHaveLength(1)
    expect(w.emitted('select')).toBeUndefined()
  })

  it('clicking the ACTIVE provisional tab emits nothing (no pointless remount)', async () => {
    const w = mount(PortalChatTabs, { props: { threads, agentName: 'scribe', activeId: null, draft: true, draftKeys: new Set(['new:scribe']) } })
    const provisional = tabButtons(w).filter((b) => b.text().includes('New chat'))
    expect(provisional).toHaveLength(1)   // never two
    await provisional[0].trigger('click')
    expect(w.emitted('new-chat')).toBeUndefined()
    expect(w.emitted('select')).toBeUndefined()
  })

  it('without a draft the provisional tab is not listed for a non-active chat (the #2579 rule is unchanged)', () => {
    const w = mount(PortalChatTabs, { props: { threads, agentName: 'scribe', activeId: 'main', draft: false, draftKeys: new Set() } })
    expect(tabButtons(w).some((b) => b.text().includes('New chat'))).toBe(false)
  })
})

describe('OverflowTabs — the optional hasDraft field', () => {
  it('a mark appearing RE-MEASURES the strip — the glyph changes a tab\'s width', async () => {
    // The re-measure key is the only reason `hasDraft` is in `tabsSignature`,
    // and a key that merely exists is the #2829 class: without a consumer
    // executing it, dropping the term stays green while a tab silently
    // overflows one place too late. `measure()` reads every mirror tab's box,
    // so counting those reads IS the watch firing.
    const tabs = [{ id: 'a', label: 'A' }, { id: 'b', label: 'B' }]
    const w = mount(OverflowTabs, { props: { tabs, modelValue: 'a' } })
    await nextTick()
    const rect = vi.spyOn(Element.prototype, 'getBoundingClientRect')
    try {
      await w.setProps({ tabs: [{ id: 'a', label: 'A', hasDraft: true }, { id: 'b', label: 'B' }] })
      await nextTick(); await nextTick()
      expect(rect, 'a mark toggling did not re-measure the strip').toHaveBeenCalled()
      // …and a change that alters nothing measurable does not.
      rect.mockClear()
      await w.setProps({ modelValue: 'b' })
      await nextTick(); await nextTick()
      expect(rect).not.toHaveBeenCalled()
    } finally {
      rect.mockRestore()
    }
  })

  it('draws the mark after the label, inline and in the mirror row, only for tabs that carry it', () => {
    const w = mount(OverflowTabs, { props: {
      tabs: [{ id: 'a', label: 'A', hasDraft: true }, { id: 'b', label: 'B' }],
      modelValue: 'a',
    } })
    const inline = w.findAll('nav button').filter((b) => !b.element.hasAttribute('data-measure-tab') && !b.element.hasAttribute('data-overflow-trigger'))
    expect(inline[0].find(MARK).exists()).toBe(true)
    expect(inline[1].find(MARK).exists()).toBe(false)
    const mirror = w.findAll('[data-measure-tab]')
    expect(mirror[0].find(MARK).exists()).toBe(true)
    expect(mirror[1].find(MARK).exists()).toBe(false)
  })
})
