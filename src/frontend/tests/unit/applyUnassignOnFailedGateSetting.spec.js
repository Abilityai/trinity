// @vitest-environment jsdom
/**
 * trinity-enterprise#1068 — `apply_unassign_on_failed_gate` is Settings-surfaced
 * beside the SSH toggle: read from and written to the admin-gated ops-config
 * endpoint through the shared api client. Unlike every other bool in that
 * group it defaults ON, so an absent key must read as on (the server default),
 * never as the report-only mode the server is not in. Mounted (#2918): the
 * toggle's reach into the client is what this proves.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { readOpsBool } from '../../src/utils/opsSettings'

vi.mock('../../src/api', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }
  return { default: inst }
})
import api from '../../src/api'
import ApplyUnassignOnFailedGateSetting from '../../src/components/settings/ApplyUnassignOnFailedGateSetting.vue'

const KEY = 'apply_unassign_on_failed_gate'
const payload = (value) => ({ settings: { [KEY]: { value, default: 'true', description: '', is_default: value === 'true' } } })

describe('readOpsBool with a default-on fallback', () => {
  it('reads a stored string as written, whatever the fallback', () => {
    expect(readOpsBool(payload('true'), KEY, true)).toBe(true)
    expect(readOpsBool(payload('false'), KEY, true)).toBe(false)
    expect(readOpsBool(payload(''), KEY, true)).toBe(false)
  })
  it('falls back only when the key holds no string', () => {
    expect(readOpsBool({ settings: {} }, KEY, true)).toBe(true)
    expect(readOpsBool(null, KEY, true)).toBe(true)
    expect(readOpsBool({ settings: { [KEY]: {} } }, KEY, true)).toBe(true)
    // The default-off flags are unchanged: no fallback reads absent as false.
    expect(readOpsBool({ settings: {} }, 'ssh_access_enabled')).toBe(false)
  })
})

describe('ApplyUnassignOnFailedGateSetting (mounted)', () => {
  beforeEach(() => { api.get.mockReset(); api.put.mockReset() })

  const toggle = (w) => w.find('[data-testid="apply-unassign-toggle"]')

  it('renders the stored value', async () => {
    api.get.mockResolvedValueOnce({ data: payload('false') })
    const w = mount(ApplyUnassignOnFailedGateSetting)
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/settings/ops/config')
    expect(toggle(w).attributes('aria-checked')).toBe('false')
    expect(toggle(w).element.disabled).toBe(false)
  })

  it('an absent key renders ON, the server default', async () => {
    api.get.mockResolvedValueOnce({ data: { settings: {} } })
    const w = mount(ApplyUnassignOnFailedGateSetting)
    await flushPromises()
    expect(toggle(w).attributes('aria-checked')).toBe('true')
  })

  it('a click writes the inverted value through the shared client', async () => {
    api.get.mockResolvedValueOnce({ data: payload('true') })
    api.put.mockResolvedValueOnce({ data: {} })
    const w = mount(ApplyUnassignOnFailedGateSetting)
    await flushPromises()
    await toggle(w).trigger('click')
    await flushPromises()
    expect(api.put).toHaveBeenCalledTimes(1)
    expect(api.put).toHaveBeenCalledWith('/api/settings/ops/config', { settings: { [KEY]: 'false' } })
    expect(toggle(w).attributes('aria-checked')).toBe('false')
  })

  it('a refused write keeps the stored state and names the problem', async () => {
    api.get.mockResolvedValueOnce({ data: payload('true') })
    api.put.mockRejectedValueOnce({ response: { status: 422, data: { detail: 'apply_unassign_on_failed_gate must be true or false' } } })
    const w = mount(ApplyUnassignOnFailedGateSetting)
    await flushPromises()
    await toggle(w).trigger('click')
    await flushPromises()
    expect(toggle(w).attributes('aria-checked')).toBe('true')
    expect(w.text()).toContain('must be true or false')
  })

  it('a failed read leaves the switch disabled rather than claiming a state', async () => {
    api.get.mockRejectedValueOnce(new Error('network'))
    const w = mount(ApplyUnassignOnFailedGateSetting)
    await flushPromises()
    expect(toggle(w).element.disabled).toBe(true)
    expect(w.find('[data-testid="apply-unassign-error"]').exists()).toBe(true)
  })
})
