// @vitest-environment jsdom
/**
 * #3194 — the Trinity Prompt copy on Settings described a delivery path that
 * was retired in #136.
 *
 * It said the prompt is injected into each agent's CLAUDE.md at startup and
 * that agents must be restarted to receive a change. Neither is true:
 * `platform_prompt_service.get_platform_system_prompt()` reads the
 * `trinity_prompt` setting on EVERY chat and task turn and appends it under
 * `## Custom Instructions` after the platform instructions, delivered via
 * `--append-system-prompt`. An admin who believed the old copy restarted
 * agents for nothing, or concluded a saved prompt had not reached them.
 *
 * The page is mounted (child panels stubbed, stores faked) and the assertions
 * are on the rendered General tab, so they hold for whatever the template
 * actually shows, not for strings that merely exist somewhere in the file.
 */
import { describe, it, expect, vi, beforeAll } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { reactive } from 'vue'

// A store stand-in: any state read is undefined-safe, any action resolves.
function fakeStore(state = {}) {
  const s = reactive({ ...state })
  return new Proxy(s, {
    get(target, key) {
      if (key in target) return target[key]
      if (typeof key === 'symbol' || key === 'then' || key === '__v_isRef') return undefined
      return vi.fn(() => Promise.resolve({}))
    },
  })
}

vi.mock('vue-router', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useRoute: () => ({ query: { tab: 'general' }, params: {} }),
}))
vi.mock('axios', () => {
  const ok = () => Promise.resolve({ data: {} })
  const inst = {
    get: vi.fn(ok), post: vi.fn(ok), put: vi.fn(ok), delete: vi.fn(ok), patch: vi.fn(ok),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...inst, create: () => inst } }
})
// The section is admin-only; an admin mount also exercises the admin loads,
// which the faked stores / axios absorb.
vi.mock('../../src/stores/auth', () => ({
  useAuthStore: () => fakeStore({ role: 'admin', user: { username: 'u' }, authHeader: {} }),
}))
vi.mock('../../src/stores/settings', () => ({ useSettingsStore: () => fakeStore() }))
vi.mock('../../src/stores/sessions', () => ({ useSessionsStore: () => fakeStore() }))
vi.mock('../../src/stores/enterprise', () => ({ useEnterpriseStore: () => fakeStore({ features: [] }) }))

let wrapper
let text = ''

beforeAll(async () => {
  const { default: Settings } = await import('../../src/views/Settings.vue')
  wrapper = shallowMount(Settings)
  await flushPromises()
  text = wrapper.text().replace(/\s+/g, ' ')
}, 60_000)  // Settings.vue is ~4k lines; its first transform alone takes seconds

describe('Settings → Trinity Prompt copy (#3194)', () => {
  it('renders the Trinity Prompt section on the General tab', () => {
    // The editor itself, not just the info box below it — otherwise every
    // negative assertion here would pass against a page that never rendered.
    expect(wrapper.find('textarea#trinity-prompt').exists()).toBe(true)
  })

  it('says the prompt applies on every turn with no restart', () => {
    expect(text).toMatch(/added to every agent's instructions on each (chat and task )?turn/i)
    expect(text).toMatch(/from the next turn/i)
    expect(text).toMatch(/no restart/i)
  })

  it('no longer claims CLAUDE.md, startup injection, or a required restart', () => {
    expect(text).not.toMatch(/CLAUDE\.md/)
    expect(text).not.toMatch(/at startup|when the agent starts/i)
    expect(text).not.toMatch(/need(s)? to be restarted|restarted agents|newly started/i)
    expect(text).not.toMatch(/Planning System/)
  })

  it('describes the real order: platform instructions, then ## Custom Instructions', () => {
    expect(text).toMatch(/after Trinity's platform instructions.*## Custom Instructions/i)
  })

  it('warns that a system manifest prompt: replaces the setting', () => {
    expect(text).toMatch(/system manifest.*top-level prompt:.*replaces/i)
  })

  it('carries the fleet-rules guidance from the recommended-prompt page', () => {
    expect(text).toMatch(/fleet rules only/i)
    expect(text).toMatch(/never copy (the )?platform instructions/i)
    expect(text).toMatch(/keep it short/i)
  })

  it('links the "How it works" box to the published recommended-prompt page (#3206)', () => {
    const link = wrapper.find('a[data-testid="trinity-prompt-docs-link"]')
    expect(link.exists()).toBe(true)
    // The published docs site, never a repo blob path: `main` trails `dev`
    // between release cuts, so a blob link 404s (see hardeningGuide.js).
    expect(link.attributes('href')).toBe('https://docs.ability.ai/guides/recommended-fleet-prompt')
    expect(link.attributes('target')).toBe('_blank')
    expect(link.attributes('rel')).toBe('noopener noreferrer')
    expect(link.text()).toMatch(/recommended Trinity prompt/i)
  })
})
