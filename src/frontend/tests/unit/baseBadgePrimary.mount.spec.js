// @vitest-environment jsdom
/**
 * trinity-enterprise#610 §3g L1 — BaseBadge's additive `primary` variant: the
 * Inbox list's "N new" is the action-primary family (the same hue as the
 * sidebar's solid "came back" counter), tinted because it is a per-row fact.
 * Light 700 on 100 (6.41:1), dark 300 on 500/16 (7.53:1 over gray-900) —
 * measured in contrast.spec.js. Additive: every existing variant renders as it
 * did, and an unknown one is still refused by the validator.
 */
import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import BaseBadge from '@/components/base/BaseBadge.vue'

const cls = (variant) => mount(BaseBadge, { props: { variant }, slots: { default: '3 new' } }).find('span').classes()

describe('BaseBadge primary', () => {
  it('is the action-primary tint in both themes', () => {
    expect(cls('primary')).toEqual(expect.arrayContaining([
      'bg-action-primary-100', 'text-action-primary-700',
      'dark:bg-action-primary-500/16', 'dark:text-action-primary-300',
    ]))
  })

  it('is accepted by the validator', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      cls('primary')
      expect(warn.mock.calls.some((c) => String(c[0]).includes('Invalid prop'))).toBe(false)
    } finally { warn.mockRestore() }
  })

  it('leaves the existing variants alone', () => {
    expect(cls('info')).toContain('bg-status-info-100')
    expect(cls('neutral')).toContain('bg-gray-100')
  })
})
