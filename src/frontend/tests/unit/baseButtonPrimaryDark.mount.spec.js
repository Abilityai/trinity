// @vitest-environment jsdom
/**
 * The primary button's DARK fill (ent#610 round-3 design review F5, pre-existing
 * on dev). White on action-primary-500 is 4.47:1 and the 400 hover ~2.9:1 —
 * both under the contract's AA floor, on every primary action in dark mode
 * (the Inbox's "Open in chat" and Mark-all confirm among them). Dark now uses
 * the light pair: 600 (6.29:1), 700 on hover (7.90:1) — `contrast.spec.js`.
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import BaseButton from '@/components/base/BaseButton.vue'

const classesOf = (variant) => mount(BaseButton, { props: { variant }, slots: { default: 'Go' } })
  .find('button').classes()

describe('BaseButton primary, dark', () => {
  it('fills with 600 and hovers to 700 — never 500 / 400 behind white text', () => {
    const c = classesOf('primary')
    expect(c).toContain('dark:bg-action-primary-600')
    expect(c).toContain('dark:hover:bg-action-primary-700')
    expect(c).not.toContain('dark:bg-action-primary-500')
    expect(c).not.toContain('dark:hover:bg-action-primary-400')
    expect(c.filter((k) => /^dark:bg-/.test(k))).toEqual(['dark:bg-action-primary-600'])
  })
})
