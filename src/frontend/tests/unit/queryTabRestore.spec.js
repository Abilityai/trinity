/**
 * #3453 — a tab named in `?tab=` survives a cold load when its visibility is
 * decided by data that arrives after setup.
 *
 * Settings resolved `?tab=` exactly once, against a tab list that filters on an
 * entitlement check before the entitlement list has loaded, and afterwards
 * followed only the URL. A deep link or refresh on an entitlement-gated tab
 * therefore landed on the default tab and stayed there, with the URL still
 * naming the gated one.
 *
 * `useQueryTab` is the tab-resolution rule Settings.vue consumes; these drive
 * the real composable with real refs. The tab ids below are made up.
 */
import { describe, expect, it, vi } from 'vitest'
import { computed, nextTick, ref } from 'vue'
import { useQueryTab } from '../../src/composables/useQueryTab'

// A tab strip with one always-visible pair and one tab gated on a late list.
function harness({ query = undefined } = {}) {
  const queryTab = ref(query)
  const entitlements = ref([]) // unknown yet === empty, as in the store
  const validTabIds = computed(() => [
    'general',
    'other',
    ...(entitlements.value.includes('gated-feature') ? ['gated-tab'] : []),
  ])
  const pushTab = vi.fn((id) => { queryTab.value = id })
  const tab = useQueryTab({
    queryTab: () => queryTab.value,
    validTabIds,
    defaultTab: computed(() => 'general'),
    pushTab,
  })
  return { ...tab, queryTab, entitlements, pushTab }
}

describe('useQueryTab — a gated tab named in the URL (#3453)', () => {
  it('becomes active once the entitlement that reveals it arrives', async () => {
    const h = harness({ query: 'gated-tab' })
    // Cold load: the gate is unknown, so the default is shown for now…
    expect(h.activeTab.value).toBe('general')
    // …and the URL is NOT rewritten to it — the place is still remembered.
    expect(h.pushTab).not.toHaveBeenCalled()
    expect(h.queryTab.value).toBe('gated-tab')

    h.entitlements.value = ['gated-feature']
    await nextTick()

    expect(h.activeTab.value).toBe('gated-tab')
    expect(h.pushTab).not.toHaveBeenCalled()
  })

  it('does not yank a user off a tab they clicked before the entitlement arrived', async () => {
    const h = harness({ query: 'gated-tab' })
    h.selectTab('other')
    expect(h.activeTab.value).toBe('other')

    h.entitlements.value = ['gated-feature']
    await nextTick()

    expect(h.activeTab.value).toBe('other')
  })

  it('a click on the tab already shown counts as a choice too', async () => {
    // The default is highlighted while the URL still names the gated tab, so
    // clicking the default changes neither the tab nor the URL — the late
    // write would otherwise move the user off the tab they just confirmed.
    const h = harness({ query: 'gated-tab' })
    h.selectTab('general')
    expect(h.pushTab).not.toHaveBeenCalled()
    expect(h.queryTab.value).toBe('gated-tab')

    h.entitlements.value = ['gated-feature']
    await nextTick()

    expect(h.activeTab.value).toBe('general')
  })

  it('stays on the default when the entitlements load without it', async () => {
    const h = harness({ query: 'gated-tab' })
    h.entitlements.value = ['something-else']
    await nextTick()

    expect(h.activeTab.value).toBe('general')
  })
})

describe('useQueryTab — the behaviour Settings already had', () => {
  it('resolves a visible tab from the URL at setup', () => {
    expect(harness({ query: 'other' }).activeTab.value).toBe('other')
  })

  it('falls back to the default for a missing or unknown tab', () => {
    expect(harness().activeTab.value).toBe('general')
    expect(harness({ query: 'nope' }).activeTab.value).toBe('general')
  })

  it('a click pushes the tab once, and ignores an unknown id', () => {
    const h = harness()
    h.selectTab('nope')
    expect(h.pushTab).not.toHaveBeenCalled()
    h.selectTab('other')
    h.selectTab('other')
    expect(h.pushTab).toHaveBeenCalledTimes(1)
    expect(h.pushTab).toHaveBeenCalledWith('other')
  })

  it('follows an external URL change (back/forward)', async () => {
    const h = harness({ query: 'other' })
    h.queryTab.value = 'general'
    await nextTick()
    expect(h.activeTab.value).toBe('general')
    h.queryTab.value = 'nope'
    await nextTick()
    expect(h.activeTab.value).toBe('general')
  })

  it('still follows the URL after the user has clicked', async () => {
    const h = harness({ query: 'gated-tab' })
    h.selectTab('other')
    h.entitlements.value = ['gated-feature']
    await nextTick()
    // Back button to the entry that named the gated tab.
    h.queryTab.value = 'gated-tab'
    await nextTick()
    expect(h.activeTab.value).toBe('gated-tab')
  })
})
