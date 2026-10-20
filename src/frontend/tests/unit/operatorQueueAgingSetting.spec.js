// @vitest-environment jsdom
/**
 * #2915 — the aging bound is Settings-surfaced (Product Quality Bar p2): read
 * from and written to the admin-gated ops-config endpoint through the shared
 * api client, with the reader/writer rules from utils/opsSettings.js. Mounted
 * (#2918): the change handler's reach into the client is what this proves.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { readOpsInt, opsIntValue } from '../../src/utils/opsSettings'

vi.mock('../../src/api', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }
  return { default: inst }
})
import api from '../../src/api'
import OperatorQueueAgingSetting from '../../src/components/settings/OperatorQueueAgingSetting.vue'

const payload = (value) => ({ settings: { operator_queue_aging_hours: { value, default: '24', description: '', is_default: value === '24' } } })

describe('opsSettings integer reader/writer', () => {
  it('reads the descriptor value; unreadable falls back, never to 0 by accident', () => {
    expect(readOpsInt(payload('48'), 'operator_queue_aging_hours', 24)).toBe(48)
    expect(readOpsInt(payload('0'), 'operator_queue_aging_hours', 24)).toBe(0)     // an explicit 0 is a choice
    expect(readOpsInt(payload(''), 'operator_queue_aging_hours', 24)).toBe(24)
    expect(readOpsInt(payload('abc'), 'operator_queue_aging_hours', 24)).toBe(24)
    expect(readOpsInt({}, 'operator_queue_aging_hours', 24)).toBe(24)
    expect(readOpsInt(payload('-5'), 'operator_queue_aging_hours', 24)).toBe(24)
  })
  it('writes a whole non-negative number as the string the endpoint validates', () => {
    expect(opsIntValue(36)).toBe('36')
    expect(opsIntValue('12.7')).toBe('12')
    expect(opsIntValue(-3)).toBe('0')
    expect(opsIntValue('nope')).toBe('0')
  })
})

describe('OperatorQueueAgingSetting (mounted)', () => {
  beforeEach(() => { api.get.mockReset(); api.put.mockReset() })

  it('loads the stored bound into the field', async () => {
    api.get.mockResolvedValueOnce({ data: payload('48') })
    const w = mount(OperatorQueueAgingSetting)
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/settings/ops/config')
    expect(w.find('input').element.value).toBe('48')
  })

  it('a change writes the validated string through the shared client', async () => {
    api.get.mockResolvedValueOnce({ data: payload('24') })
    api.put.mockResolvedValueOnce({ data: {} })
    const w = mount(OperatorQueueAgingSetting)
    await flushPromises()
    const input = w.find('input')
    await input.setValue('72')   // setValue dispatches input AND change — one save
    await flushPromises()
    expect(api.put).toHaveBeenCalledTimes(1)
    expect(api.put).toHaveBeenCalledWith('/api/settings/ops/config', { settings: { operator_queue_aging_hours: '72' } })
  })

  it('a refused write names the problem beside the field and keeps the control usable', async () => {
    api.get.mockResolvedValueOnce({ data: payload('24') })
    api.put.mockRejectedValueOnce({ response: { status: 422, data: { detail: 'operator_queue_aging_hours must be <= 8760 (got 99999)' } } })
    const w = mount(OperatorQueueAgingSetting)
    await flushPromises()
    const input = w.find('input')
    await input.setValue('99999')
    await flushPromises()
    expect(w.text()).toContain('must be <= 8760')
    expect(input.element.disabled).toBe(false)
  })
})
