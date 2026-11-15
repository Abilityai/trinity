// @vitest-environment jsdom
/**
 * #3584 — the public-link Edit dialog keeps the expiry it was given.
 *
 * The `datetime-local` input has no time zone: it shows and returns local
 * wall-clock time. The dialog used to load the stored UTC string's own
 * characters into it and read the value back as local, so every untouched Save
 * moved the expiry by the browser's UTC offset, and a cleared field sent
 * nothing, so an expiry could not be removed.
 *
 * The helper is exercised under several zones (Node honours a runtime `TZ`
 * change); the dialog is MOUNTED and the PUT payload it sends is asserted.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import axios from 'axios'
import PublicLinksPanel from '../../src/components/PublicLinksPanel.vue'
import { toDatetimeLocalInput } from '../../src/utils/timestamps'

const ZONES = ['UTC', 'Europe/Paris', 'America/Los_Angeles', 'Asia/Kolkata', 'Pacific/Auckland']
const STORED = '2026-10-22T11:00:00.000Z'
const ORIGINAL_TZ = process.env.TZ

afterEach(() => {
  process.env.TZ = ORIGINAL_TZ
  vi.restoreAllMocks()
})

describe('toDatetimeLocalInput', () => {
  it.each(ZONES)('round-trips the stored instant in %s', (zone) => {
    process.env.TZ = zone
    const value = toDatetimeLocalInput(STORED)
    expect(value).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/)
    // What `saveLink` does with the field: read it back as local time.
    expect(new Date(value).toISOString()).toBe(STORED)
  })

  it('shows local wall-clock time, not the UTC characters', () => {
    process.env.TZ = 'Europe/Paris'          // UTC+2 on 2026-10-22
    expect(toDatetimeLocalInput(STORED)).toBe('2026-10-22T13:00')
    process.env.TZ = 'America/Los_Angeles'   // UTC-7
    expect(toDatetimeLocalInput(STORED)).toBe('2026-10-22T04:00')
  })

  it('treats a zoneless backend string as UTC', () => {
    process.env.TZ = 'Europe/Paris'
    expect(toDatetimeLocalInput('2026-10-22T11:00:00')).toBe('2026-10-22T13:00')
  })

  it.each([null, undefined, '', 'not a date'])('is empty for %s', (v) => {
    expect(toDatetimeLocalInput(v)).toBe('')
  })
})

describe('the Edit dialog', () => {
  let put

  beforeEach(() => {
    setActivePinia(createPinia())
    vi.spyOn(axios, 'get').mockImplementation((url) =>
      Promise.resolve({
        data: url.endsWith('/public-links')
          ? [{ id: 'l1', name: 'Demo', token: 't', enabled: true, link_type: 'chat', expires_at: STORED }]
          : {},
      }))
    put = vi.spyOn(axios, 'put').mockResolvedValue({ data: {} })
  })

  async function openEdit() {
    const wrapper = mount(PublicLinksPanel, { props: { agentName: 'bot' } })
    await flushPromises()
    await wrapper.get('button[title="Edit link"]').trigger('click')
    return wrapper
  }

  async function save(wrapper) {
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(put).toHaveBeenCalledTimes(1)
    return put.mock.calls[0][1]
  }

  it.each(ZONES)('an untouched Save leaves the expiry alone in %s', async (zone) => {
    process.env.TZ = zone
    const wrapper = await openEdit()
    const payload = await save(wrapper)
    expect(payload).not.toHaveProperty('expires_at')
  })

  it('shows the expiry in local time', async () => {
    process.env.TZ = 'Europe/Paris'
    const wrapper = await openEdit()
    expect(wrapper.get('input[type="datetime-local"]').element.value).toBe('2026-10-22T13:00')
  })

  it('clearing the field removes the expiry', async () => {
    const wrapper = await openEdit()
    await wrapper.get('input[type="datetime-local"]').setValue('')
    const payload = await save(wrapper)
    expect(payload).toHaveProperty('expires_at', null)
  })

  it('a changed expiry is sent as the UTC instant of what was typed', async () => {
    process.env.TZ = 'Europe/Paris'
    const wrapper = await openEdit()
    await wrapper.get('input[type="datetime-local"]').setValue('2026-10-23T09:30')
    const payload = await save(wrapper)
    expect(payload.expires_at).toBe('2026-10-23T07:30:00.000Z')
  })
})
