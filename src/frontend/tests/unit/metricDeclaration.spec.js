// @vitest-environment jsdom
/**
 * Declaring metrics from the UI (trinity-enterprise#1006) — MOUNTED.
 *
 * The predicates here gate store writes to an agent's template.yaml and a
 * destructive retire, so they are executed, not matched in source (#2918):
 *
 * - owner controls render only for `canManage`, and the empty state names the
 *   action (or who to ask);
 * - the form sends the hash it read, maps the backend's per-field errors onto
 *   fields, and offers a reload that keeps entries on `template_changed`;
 * - a refusal that makes the form pointless (agent stopped) replaces it;
 * - retire with a reference stays disabled until acknowledged, and an
 *   unchecked source counts as a reference.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import DeclaredMetricsTiles from '../../src/components/DeclaredMetricsTiles.vue'
import MetricDeclarationModal from '../../src/components/MetricDeclarationModal.vue'
import RetireMetricDialog from '../../src/components/RetireMetricDialog.vue'
import { useAgentsStore } from '../../src/stores/agents'
import {
  diffPreview,
  formFromDeclaration,
  nameFromLabel,
  payloadFromForm,
  previewLines,
  readRefusal,
  staleHelp,
} from '../../src/utils/metricDeclaration'

vi.mock('../../src/components/SparklineChart.vue', () => ({
  default: { name: 'SparklineChart', template: '<div class="sparkline-stub" />' },
}))

const AGENT = 'decl-agent'
const HASH = 'a'.repeat(64)

async function flush() {
  for (let i = 0; i < 4; i += 1) {
    await nextTick()
    await Promise.resolve()
  }
}

function tile(overrides = {}) {
  return {
    name: 'close_rate', type: 'percentage', label: 'Close rate', description: null, unit: '%',
    direction: 'up_good', aggregation: 'last', cadence: '1w', cadence_seconds: 604800,
    warning_threshold: null, critical_threshold: null, values: null, dimensions: [],
    status: 'active', retired_at: null, type_conflict: null, recorded_by: null,
    latest: null, latest_by_series: [], last_point_at: null, stale: false, freshness: 'no_points',
    stale_after: null, series_count: 0, series: [], chart: null, stats: null, message: null,
    ...overrides,
  }
}

function metricsPayload(metrics) {
  return { agent_name: AGENT, declared: metrics.length > 0, metrics, findings: [], message: null }
}

function editorPayload(declarations = [], extra = {}) {
  return {
    agent_name: AGENT, template_hash: HASH, editable: true, reason: null,
    declared_count: declarations.length, cap: 50, declarations, ...extra,
  }
}

function refusal(status, detail) {
  const e = new Error(`HTTP ${status}`)
  e.response = { status, data: { detail } }
  return e
}

beforeEach(() => {
  setActivePinia(createPinia())
  document.body.innerHTML = ''
})

// ---------------------------------------------------------------------------
// The tiles: who sees the controls
// ---------------------------------------------------------------------------

async function mountTiles({ canManage, metrics }) {
  const store = useAgentsStore()
  store.getAgentMetrics = vi.fn(async () => metricsPayload(metrics))
  const wrapper = mount(DeclaredMetricsTiles, {
    props: { agentName: AGENT, canManage },
    attachTo: document.body,
  })
  await flush()
  return { wrapper, store }
}

describe('owner controls on the tiles', () => {
  it('an owner gets Declare, and Edit/Retire on each tile', async () => {
    const { wrapper } = await mountTiles({ canManage: true, metrics: [tile()] })
    expect(wrapper.find('[data-testid="declare-metric-button"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="metric-edit"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="metric-retire"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="declared-metrics-count"]').text()).toBe('1 of 50 declared')
    wrapper.unmount()
  })

  it('a viewer without owner rights gets none of them', async () => {
    const { wrapper } = await mountTiles({ canManage: false, metrics: [tile()] })
    expect(wrapper.find('[data-testid="declare-metric-button"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="metric-tile-actions"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('the empty state names the action for an owner and who to ask otherwise', async () => {
    const owner = await mountTiles({ canManage: true, metrics: [] })
    const empty = owner.wrapper.find('[data-testid="declared-metrics-empty"]')
    expect(empty.text()).toContain('No metrics declared yet')
    expect(owner.wrapper.find('[data-testid="declared-metrics-empty-declare"]').exists()).toBe(true)
    owner.wrapper.unmount()

    const viewer = await mountTiles({ canManage: false, metrics: [] })
    expect(viewer.wrapper.find('[data-testid="declared-metrics-empty-declare"]').exists()).toBe(false)
    expect(viewer.wrapper.find('[data-testid="declared-metrics-empty"]').text()).toContain("Ask the agent's owner")
    viewer.wrapper.unmount()
  })

  it('shows who records a metric when the note is set', async () => {
    const { wrapper } = await mountTiles({ canManage: false, metrics: [tile({ recorded_by: 'the weekly skill' })] })
    expect(wrapper.find('[data-testid="metric-recorded-by"]').text()).toBe('Recorded by the weekly skill')
    wrapper.unmount()
  })

  it('Declare is disabled at the 50-metric cap', async () => {
    const many = Array.from({ length: 50 }, (_, i) => tile({ name: `m${i}`, label: `M${i}` }))
    const { wrapper } = await mountTiles({ canManage: true, metrics: many })
    const button = wrapper.find('[data-testid="declare-metric-button"]')
    expect(button.attributes('disabled')).toBeDefined()
    expect(button.attributes('title')).toContain('most allowed')
    wrapper.unmount()
  })
})

// ---------------------------------------------------------------------------
// The form
// ---------------------------------------------------------------------------

async function mountForm({ metric = null, editor = editorPayload(), getError = null } = {}) {
  const store = useAgentsStore()
  store.getMetricDeclarations = vi.fn(async () => {
    if (getError) throw getError
    return editor
  })
  store.declareMetric = vi.fn(async () => ({ success: true, op: 'add', registry_refreshed: true }))
  store.updateMetricDeclaration = vi.fn(async () => ({ success: true, op: 'replace', registry_refreshed: true }))
  const wrapper = mount(MetricDeclarationModal, {
    props: { modelValue: true, agentName: AGENT, metric },
    attachTo: document.body,
  })
  await flush()
  return { wrapper, store }
}

const body = () => document.body

async function type(testid, value) {
  const el = body().querySelector(`[data-testid="${testid}"]`)
  el.value = value
  el.dispatchEvent(new Event('input'))
  await flush()
}

async function click(testid) {
  body().querySelector(`[data-testid="${testid}"]`).click()
  await flush()
}

describe('the declaration form', () => {
  it('suggests the name from the label and sends the hash it read', async () => {
    const { wrapper, store } = await mountForm()
    await type('metric-label', 'Proposals sent')
    expect(body().querySelector('[data-testid="metric-name"]').value).toBe('proposals_sent')
    await click('metric-type-counter')
    await click('metric-declaration-submit')
    expect(store.declareMetric).toHaveBeenCalledTimes(1)
    const [agent, fields, hash] = store.declareMetric.mock.calls[0]
    expect(agent).toBe(AGENT)
    expect(hash).toBe(HASH)
    expect(fields).toMatchObject({ name: 'proposals_sent', type: 'counter', label: 'Proposals sent', cadence: '1d' })
    expect(wrapper.emitted('saved')).toHaveLength(1)
    wrapper.unmount()
  })

  it("puts the backend's field errors on the fields and writes nothing else", async () => {
    const { wrapper, store } = await mountForm()
    store.declareMetric.mockRejectedValueOnce(refusal(422, {
      code: 'metric_invalid',
      message: 'Some fields need a fix.',
      errors: [{ field: 'name', code: 'metric_duplicate', message: 'This agent already declares close_rate. Choose another name.' }],
    }))
    await type('metric-label', 'Close rate')
    await click('metric-declaration-submit')
    expect(body().textContent).toContain('This agent already declares close_rate')
    expect(body().querySelector('[data-testid="metric-declaration-error"]').textContent)
      .toContain('One field needs a fix')
    expect(wrapper.emitted('saved')).toBeUndefined()
    wrapper.unmount()
  })

  it('on template_changed, reloads the hash and keeps what was typed', async () => {
    const { wrapper, store } = await mountForm()
    store.declareMetric.mockRejectedValueOnce(refusal(409, {
      code: 'template_changed', message: 'template.yaml changed after you opened this form.',
    }))
    await type('metric-label', 'Proposals sent')
    await click('metric-declaration-submit')
    const retry = body().querySelector('[data-testid="metric-declaration-error"] button')
    expect(body().querySelector('[data-testid="metric-declaration-error"]').textContent).toContain('Reload and keep my entries')
    retry.click()
    await flush()
    expect(store.getMetricDeclarations).toHaveBeenCalledTimes(2)
    expect(body().querySelector('[data-testid="metric-label"]').value).toBe('Proposals sent')
    wrapper.unmount()
  })

  it('a stopped agent replaces the form with the named refusal', async () => {
    const { wrapper } = await mountForm({
      getError: refusal(409, { code: 'agent_not_running', message: 'stopped' }),
    })
    const blocked = body().querySelector('[data-testid="metric-declaration-blocked"]')
    expect(blocked.getAttribute('data-code')).toBe('agent_not_running')
    expect(blocked.textContent).toContain('Start the agent')
    expect(body().querySelector('[data-testid="metric-declaration-submit"]')).toBeNull()
    wrapper.unmount()
  })

  it('a layout the form cannot edit says so before anything is typed', async () => {
    const { wrapper } = await mountForm({ editor: editorPayload([], { editable: false, reason: 'flow list' }) })
    expect(body().querySelector('[data-testid="metric-declaration-blocked"]').getAttribute('data-code'))
      .toBe('template_not_editable')
    wrapper.unmount()
  })

  it('edit locks name and type, and Save waits for a change', async () => {
    const decl = { name: 'close_rate', type: 'percentage', label: 'Close rate', cadence: '1w', direction: 'up_good',
      unit: '%', recorded_by: null, dimensions: [], values: null, warning_threshold: null, critical_threshold: null,
      aggregation: null, description: null }
    const { wrapper, store } = await mountForm({ metric: tile(), editor: editorPayload([decl]) })
    expect(body().querySelector('[data-testid="metric-name-locked"]').textContent).toContain('close_rate')
    expect(body().querySelector('[data-testid="metric-type-locked"]').textContent).toContain('Percentage')
    const save = body().querySelector('[data-testid="metric-declaration-submit"]')
    expect(save.disabled).toBe(true)
    await type('metric-label', 'Win rate')
    expect(save.disabled).toBe(false)
    await click('metric-declaration-submit')
    expect(store.updateMetricDeclaration).toHaveBeenCalledWith(AGENT, 'close_rate',
      expect.objectContaining({ name: 'close_rate', type: 'percentage', label: 'Win rate' }), HASH)
    wrapper.unmount()
  })
})

// ---------------------------------------------------------------------------
// Retire
// ---------------------------------------------------------------------------

async function mountRetire(references, unchecked = []) {
  const store = useAgentsStore()
  store.getMetricReferences = vi.fn(async () => ({ references, unchecked }))
  store.getMetricDeclarations = vi.fn(async () => editorPayload())
  store.retireMetricDeclaration = vi.fn(async () => ({ success: true, op: 'remove' }))
  const wrapper = mount(RetireMetricDialog, {
    props: { modelValue: true, agentName: AGENT, metric: tile() },
    attachTo: document.body,
  })
  await flush()
  return { wrapper, store }
}

describe('retiring a metric', () => {
  it('a metric in use cannot be retired until the person acknowledges it', async () => {
    const { wrapper, store } = await mountRetire([
      { kind: 'points', label: '412 recorded points', detail: null, last_point_at: '2026-10-28T08:00:00Z' },
      { kind: 'project', label: 'Q4 pipeline', detail: 'linked metric' },
    ])
    const confirm = body().querySelector('[data-testid="retire-metric-confirm"]')
    expect(body().querySelectorAll('[data-testid="retire-metric-reference"]')).toHaveLength(2)
    expect(confirm.disabled).toBe(true)
    confirm.click()
    await flush()
    expect(store.retireMetricDeclaration).not.toHaveBeenCalled()

    const ack = body().querySelector('[data-testid="retire-metric-acknowledge"]')
    ack.checked = true
    ack.dispatchEvent(new Event('change'))
    await flush()
    expect(confirm.disabled).toBe(false)
    confirm.click()
    await flush()
    expect(store.retireMetricDeclaration).toHaveBeenCalledWith(AGENT, 'close_rate', HASH, { acknowledgeReferences: true })
    expect(wrapper.emitted('retired')).toHaveLength(1)
    wrapper.unmount()
  })

  it('a source that could not be checked counts as a use', async () => {
    const { wrapper } = await mountRetire([], ['objectives'])
    expect(body().querySelector('[data-testid="retire-metric-unchecked"]').textContent).toContain("couldn't be checked")
    expect(body().querySelector('[data-testid="retire-metric-confirm"]').disabled).toBe(true)
    wrapper.unmount()
  })

  it('an unused metric retires without acknowledgement, and the safe action is focused first', async () => {
    const { wrapper, store } = await mountRetire([])
    expect(body().querySelector('[data-testid="retire-metric-unused"]')).not.toBeNull()
    expect(document.activeElement?.getAttribute('data-testid')).not.toBe('retire-metric-confirm')
    await click('retire-metric-confirm')
    expect(store.retireMetricDeclaration).toHaveBeenCalledWith(AGENT, 'close_rate', HASH, { acknowledgeReferences: false })
    wrapper.unmount()
  })
})

// ---------------------------------------------------------------------------
// The pure helpers
// ---------------------------------------------------------------------------

describe('form helpers', () => {
  it('derives a valid name from a label', () => {
    expect(nameFromLabel('Close rate (%)')).toBe('close_rate')
    expect(nameFromLabel('2024 revenue')).toBe('revenue')
  })

  it('states the stale window in plain words', () => {
    expect(staleHelp('1w')).toBe('Shown as stale if no value arrives within 2 weeks.')
    expect(staleHelp('custom')).toContain('use 4w for monthly')
  })

  it('round-trips a declaration with a custom cadence through the form', () => {
    const form = formFromDeclaration({ name: 'x', type: 'gauge', cadence: '12h', recorded_by: 'a skill', dimensions: ['region'] })
    expect(form.cadenceChoice).toBe('custom')
    const { payload } = payloadFromForm(form)
    expect(payload).toMatchObject({ name: 'x', type: 'gauge', cadence: '12h', recorded_by: 'a skill', dimensions: ['region'] })
  })

  it('flags a non-numeric threshold before any request', () => {
    const form = formFromDeclaration({ name: 'x', type: 'gauge', cadence: '1d' })
    form.warning = 'about 25'
    expect(payloadFromForm(form).errors.warning_threshold).toContain('plain number')
  })

  it('drops direction, unit and thresholds for a status metric', () => {
    const form = formFromDeclaration({ name: 's', type: 'status', cadence: '1h', values: [{ value: 'ok', color: 'green' }] })
    form.unit = 'x'
    const { payload } = payloadFromForm(form)
    expect(payload).toMatchObject({ direction: null, unit: null, warning_threshold: null })
    expect(payload.values).toEqual([{ value: 'ok', color: 'green', label: null }])
  })

  it('diffs two previews line by line', () => {
    const before = previewLines({ name: 'x', type: 'gauge', cadence: '1d' })
    const after = previewLines({ name: 'x', type: 'gauge', cadence: '1w' })
    const changed = diffPreview(before, after).filter((l) => l.kind !== ' ')
    expect(changed).toEqual([{ kind: '-', text: '  cadence: 1d' }, { kind: '+', text: '  cadence: 1w' }])
  })

  it('reads a named refusal, and a plain one', () => {
    expect(readRefusal(refusal(409, { code: 'template_changed', message: 'm' })).code).toBe('template_changed')
    expect(readRefusal(refusal(500, 'boom')).message).toBe('boom')
  })
})
