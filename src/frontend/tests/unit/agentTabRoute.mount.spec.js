// @vitest-environment jsdom
/**
 * #2900 — the agent detail tab lives in the URL.
 *
 * `?tab=` was read on arrival and never written, so the address bar stayed at
 * `/agents/<name>` whichever tab was open: Back left the page, a reload fell
 * back to Overview, and a tab could not be linked.
 *
 * `useTabRoute` is the binding AgentDetail consumes. It is driven here through
 * a mounted component and a REAL vue-router on memory history, so a click is a
 * click, a push is a history entry, and Back is `router.back()`.
 *
 * The last block is the #2130 half of the acceptance criteria: a navigation
 * that lands late must never move the user off a tab they clicked.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter, useRoute, useRouter } from 'vue-router'
import { defineComponent, h, ref } from 'vue'
import { useTabRoute } from '../../src/composables/useTabRoute'

const TABS = ['overview', 'tasks', 'chat', 'git', 'sharing']
const resolveTab = (id) => (TABS.includes(id) ? id : null)

// What AgentDetail does with it: a ref the tab strip v-models, plus the two
// imperative entry points for programmatic selection.
const Harness = defineComponent({
  setup(_, { expose }) {
    const route = useRoute()
    const router = useRouter()
    const activeTab = ref(resolveTab(route.query.tab) || 'overview')
    const api = useTabRoute({
      activeTab, route, router, routeName: 'AgentDetail', defaultTab: 'overview', resolveTab,
    })
    expose({ activeTab, ...api })
    return () => h('div', [
      h('p', { 'data-testid': 'active' }, activeTab.value),
      ...TABS.map((id) => h('button', { 'data-tab': id, onClick: () => { activeTab.value = id } }, id)),
    ])
  },
})

const Blank = { render: () => h('div') }

let wrapper
afterEach(() => wrapper?.unmount())

async function setup(start = '/agents/alpha', { guard } = {}) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'Dashboard', component: Blank },
      { path: '/agents/:name', name: 'AgentDetail', component: Blank },
    ],
  })
  await router.push('/')
  await router.push(start)
  if (guard) router.beforeEach(guard)
  wrapper = mount(Harness, { global: { plugins: [router] } })
  const click = async (id) => {
    await wrapper.get(`[data-tab="${id}"]`).trigger('click')
    await flushPromises()
  }
  const back = async () => { router.back(); await flushPromises() }
  const active = () => wrapper.get('[data-testid="active"]').text()
  const url = () => router.currentRoute.value.fullPath
  return { router, click, back, active, url, vm: wrapper.vm }
}

describe('#2900 clicking a tab writes it to the URL', () => {
  it('a click puts the tab in the query', async () => {
    const { click, url, active } = await setup()
    await click('chat')
    expect(active()).toBe('chat')
    expect(url()).toBe('/agents/alpha?tab=chat')
  })

  it('keeps the query keys that were already there', async () => {
    const { click, router } = await setup('/agents/alpha?execution=e1')
    await click('tasks')
    expect(router.currentRoute.value.query).toEqual({ execution: 'e1', tab: 'tasks' })
  })

  it('Back steps through the tabs, then off the page — not off it at once', async () => {
    const { click, back, active, url } = await setup()
    await click('tasks')
    await click('chat')
    await click('git')

    await back()
    expect(url()).toBe('/agents/alpha?tab=chat')
    expect(active()).toBe('chat')

    await back()
    expect(active()).toBe('tasks')

    // The entry the user arrived on carried no ?tab= — that is Overview.
    await back()
    expect(url()).toBe('/agents/alpha')
    expect(active()).toBe('overview')

    await back()
    expect(url()).toBe('/')
  })

  it('Forward returns to the tab Back left', async () => {
    const { click, back, router, active } = await setup()
    await click('chat')
    await back()
    router.forward()
    await flushPromises()
    expect(active()).toBe('chat')
  })

  it('re-selecting the tab already in the URL adds no history entry', async () => {
    const { click, back, url } = await setup('/agents/alpha?tab=chat')
    await click('chat')
    await back()
    expect(url()).toBe('/')
  })

  it('an unknown ?tab= reached through history reads as the default tab', async () => {
    const { click, back, active } = await setup('/agents/alpha?tab=nope')
    await click('git')
    await back()
    expect(active()).toBe('overview')
  })
})

describe('#2900 programmatic selection', () => {
  it('selectTab carries extra query keys in the SAME navigation', async () => {
    const { vm, router, back, url } = await setup()
    vm.selectTab('tasks', { query: { execution: 'e9' } })
    await flushPromises()
    expect(router.currentRoute.value.query).toEqual({ tab: 'tasks', execution: 'e9' })
    // One entry, so one Back undoes both halves.
    await back()
    expect(url()).toBe('/agents/alpha')
  })

  it('a replacing selectTab moves the tab without a history entry', async () => {
    const { vm, back, url, active } = await setup('/agents/alpha?tab=sharing')
    vm.selectTab('overview', { replace: true })
    await flushPromises()
    expect(active()).toBe('overview')
    expect(url()).toBe('/agents/alpha?tab=overview')
    await back()
    expect(url()).toBe('/')
  })

  it('syncUrl makes a bare URL name the tab that is showing, by replace', async () => {
    const { vm, router, back, url, active } = await setup('/agents/alpha?tab=chat')
    // A KeepAlive revisit: the view kept `chat`, the link back in was bare.
    await router.push('/')
    await router.push('/agents/alpha')
    expect(active()).toBe('chat')
    vm.syncUrl()
    await flushPromises()
    expect(url()).toBe('/agents/alpha?tab=chat')
    await back()
    expect(url()).toBe('/')
  })

  it('syncUrl leaves a bare URL alone on the default tab', async () => {
    const { vm, url } = await setup()
    vm.syncUrl()
    await flushPromises()
    expect(url()).toBe('/agents/alpha')
  })

  it('switching agent in place keeps the remembered tab and names it in the URL', async () => {
    const { router, click, active, url } = await setup()
    await click('git')
    await router.push('/agents/beta')
    await flushPromises()
    expect(active()).toBe('git')
    expect(url()).toBe('/agents/beta?tab=git')
  })
})

describe('#2900 stays out of the way off its own route', () => {
  it('writes nothing while another page is showing', async () => {
    const { router, vm, url } = await setup()
    await router.push('/?tab=chat')
    vm.activeTab = 'git'
    vm.selectTab('tasks')
    vm.syncUrl()
    await flushPromises()
    expect(url()).toBe('/?tab=chat')
  })

  it('another page\'s ?tab= does not move this view\'s tab', async () => {
    const { router, click, active } = await setup()
    await click('git')
    await router.push('/?tab=chat')
    expect(active()).toBe('git')
  })

  it('arriving from another page is left to the caller\'s lifecycle hooks', async () => {
    const { router, click, active } = await setup()
    await click('git')
    await router.push('/')
    await router.push('/agents/alpha?tab=chat')
    expect(active()).toBe('git')
  })
})

describe('#2130 a late navigation never moves the user off a tab they clicked', () => {
  // Hold every navigation at a guard until released, the way a slow
  // `beforeEach` (auth init, setup status) holds the real one.
  function gate() {
    const waiting = []
    return {
      guard: () => new Promise((resolve) => waiting.push(resolve)),
      // Let the navigation reach the guard, then open it.
      release: async () => {
        await flushPromises()
        while (waiting.length) waiting.shift()()
        await flushPromises()
      },
    }
  }

  it('click A, click B while A is still navigating → B stays', async () => {
    const g = gate()
    const { active, url } = await setup('/agents/alpha', { guard: g.guard })
    await wrapper.get('[data-tab="git"]').trigger('click')
    await wrapper.get('[data-tab="chat"]').trigger('click')
    expect(active()).toBe('chat')
    await g.release()
    await g.release()
    expect(active()).toBe('chat')
    expect(url()).toBe('/agents/alpha?tab=chat')
  })

  it('click A then straight back to the original tab → the URL follows, the tab does not bounce', async () => {
    const g = gate()
    const { active, url } = await setup('/agents/alpha', { guard: g.guard })
    await wrapper.get('[data-tab="git"]').trigger('click')
    await wrapper.get('[data-tab="overview"]').trigger('click')
    await g.release()
    await g.release()
    expect(active()).toBe('overview')
    expect(url()).toBe('/agents/alpha?tab=overview')
  })

  it('Back pressed while a tab write is still navigating wins, and adds no entry of its own', async () => {
    const g = gate()
    const { router, active, url } = await setup('/agents/alpha', { guard: g.guard })
    await wrapper.get('[data-tab="tasks"]').trigger('click')
    await g.release()
    expect(url()).toBe('/agents/alpha?tab=tasks')

    await wrapper.get('[data-tab="git"]').trigger('click')   // held at the guard
    router.back()
    await flushPromises()
    await g.release()
    await g.release()

    expect(url()).toBe('/agents/alpha')
    expect(active()).toBe('overview')
    // The tab following the URL must not have been written back as a push,
    // which would have cut the forward history off.
    router.forward()
    await flushPromises()
    await g.release()
    expect(url()).toBe('/agents/alpha?tab=tasks')
    expect(active()).toBe('tasks')
  })
})
