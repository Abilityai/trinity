// @vitest-environment jsdom
/**
 * trinity-enterprise#936 — Settings › Judge, MOUNTED.
 *
 * Loads the platform policy into a pre-filled form, saves the exact document
 * the server validates, and turns a named refusal into a sentence next to the
 * Save button (the policy on screen is kept). A failed first load is LoadFailed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

const http = vi.hoisted(() => ({ get: null, put: null }))
vi.mock('axios', () => {
  const instance = {
    get: vi.fn((...a) => http.get(...a)),
    put: vi.fn((...a) => http.put(...a)),
    post: vi.fn(async () => ({ data: {} })),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})

import EvalJudgePanel from '../../src/components/settings/EvalJudgePanel.vue'
import { SETTINGS_URL, toPolicy, toForm } from '../../src/stores/evalJudge'

const POLICY = {
  enabled: true, agent_default: 'on', judge_models: ['claude-sonnet-5', 'claude-opus-5-5'],
  sampling: { every_tier0_failure: true, passing_per_agent_per_day: 5, min_run_cost_multiple: 10, cost_floor_lanes: ['pass'] },
  daily_cap_usd_per_agent: 0.5, excluded_triggers: ['public', 'telegram'], excluded_archetypes: ['receptionist'],
}
const ALLOW = ['claude-sonnet-5', 'claude-opus-5', 'claude-opus-5-5', 'claude-fable-5-1']

async function flush() { for (let i = 0; i < 4; i++) { await nextTick(); await flushPromises() } }

function mountPanel({ get, put } = {}) {
  http.get = vi.fn(get || (async () => ({ data: { platform: POLICY, judge_model_allowlist: ALLOW } })))
  http.put = vi.fn(put || (async (url, body) => ({ data: { platform: body } })))
  setActivePinia(createPinia())
  return mount(EvalJudgePanel, { global: { stubs: { SkeletonLoader: true } } })
}

describe('evalJudge form mapping', () => {
  it('round-trips the server policy exactly', () => {
    expect(toPolicy(toForm(POLICY))).toEqual(POLICY)
  })
  it('drops an empty fallback and blank list items', () => {
    const f = { ...toForm(POLICY), fallback: '', excludedTriggers: ' public , ,slack ' }
    const p = toPolicy(f)
    expect(p.judge_models).toEqual(['claude-sonnet-5'])
    expect(p.excluded_triggers).toEqual(['public', 'slack'])
  })
})

describe('EvalJudgePanel', () => {
  beforeEach(() => vi.clearAllMocks())

  it('opens pre-filled and saves the edited policy', async () => {
    const w = mountPanel()
    await flush()
    expect(w.find('select[data-testid="eval-judge-primary"]').element.value).toBe('claude-sonnet-5')
    await w.find('input[data-testid="eval-judge-cap"]').setValue('1.25')
    await w.find('form').trigger('submit')
    await flush()
    expect(http.put).toHaveBeenCalledTimes(1)
    const [url, body] = http.put.mock.calls[0]
    expect(url).toBe(SETTINGS_URL)
    expect(body.daily_cap_usd_per_agent).toBe(1.25)
    expect(body.judge_models).toEqual(['claude-sonnet-5', 'claude-opus-5-5'])
    expect(w.find('[data-testid="eval-judge-saved"]').exists()).toBe(true)
  })

  it('a named refusal is a sentence next to Save, and the form is kept', async () => {
    const w = mountPanel({
      put: async () => { throw { response: { status: 422, data: { detail: { code: 'judge_models_same_family', message: 'x' } } } } },
    })
    await flush()
    await w.find('select[data-testid="eval-judge-fallback"]').setValue('claude-sonnet-5')
    await w.find('form').trigger('submit')
    await flush()
    expect(w.text()).toContain('The fallback must be a different model family')
    expect(w.find('select[data-testid="eval-judge-fallback"]').element.value).toBe('claude-sonnet-5')
  })

  it('a failed first load is LoadFailed, not an empty form', async () => {
    const w = mountPanel({ get: async () => { throw new Error('boom') } })
    await flush()
    expect(w.text()).toContain("Couldn't load the judge policy")
    expect(w.find('[data-testid="eval-judge-form"]').exists()).toBe(false)
  })
})
