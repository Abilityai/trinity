// @vitest-environment jsdom
/**
 * trinity-enterprise#610 §3g B7b — the ghost button's DARK ink.
 *
 * `dark:text-action-primary-500` measured 3.97:1 on gray-900 and 3.29:1 on
 * gray-800: every ghost verb in dark mode ("Mark all read" on the Inbox among
 * them) failed the contract's AA floor. The 400 tier clears it on both
 * grounds (5.95 / 4.92, `contrast.spec.js`). Mounted, because the class string
 * the button renders is the fact — and the other three variants are pinned
 * unchanged, so the fix cannot drift into them.
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import BaseButton from '@/components/base/BaseButton.vue'

const classesOf = (variant) => mount(BaseButton, { props: { variant }, slots: { default: 'Go' } })
  .find('button').classes()

describe('BaseButton ghost ink (B7b)', () => {
  it('reads action-primary-400 in dark, never the 500 tier', () => {
    const c = classesOf('ghost')
    expect(c).toContain('dark:text-action-primary-400')
    expect(c).not.toContain('dark:text-action-primary-500')
    // Light ink is untouched: 600 on white is 6.29:1.
    expect(c).toContain('text-action-primary-600')
  })

  it('carries exactly one dark text colour (mutually exclusive, #2662)', () => {
    const darkText = classesOf('ghost').filter((k) => /^dark:text-/.test(k))
    expect(darkText).toEqual(['dark:text-action-primary-400'])
  })

  it('leaves the other three variants as they were', () => {
    // primary's dark fill moved 500 -> 600 on purpose (round 3):
    // baseButtonPrimaryDark.mount.spec.js.
    expect(classesOf('primary')).toContain('dark:bg-action-primary-600')
    expect(classesOf('secondary')).toContain('dark:text-gray-100')
    expect(classesOf('danger')).toContain('dark:bg-status-danger-500')
  })
})
