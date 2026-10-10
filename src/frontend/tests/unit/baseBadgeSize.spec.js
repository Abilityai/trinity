// @vitest-environment jsdom
/**
 * trinity-enterprise#754 — BaseBadge `size="sm"`, MOUNTED.
 *
 * The Skills card's author-mode chip ("runs unattended", "asks mid-run",
 * "start by hand") is the 11px step of the type scale: `sm`. `md` stays the
 * default and renders exactly as before, so every other badge is unchanged.
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import BaseBadge from '@/components/base/BaseBadge.vue'

const cls = (props) => mount(BaseBadge, { props: { variant: 'info', ...props }, slots: { default: 'x' } }).find('span').classes()

describe('BaseBadge size', () => {
  it('md is the default and the recipe it always was: 11.5/550, padding 2.5×9', () => {
    expect(cls({})).toEqual(expect.arrayContaining(['text-[11.5px]', 'font-[550]', 'px-[9px]', 'py-[2.5px]', 'gap-1.5', 'rounded-full']))
  })

  it('sm is 11/550 at padding 1.5×7, the same pill', () => {
    const sm = cls({ size: 'sm' })
    expect(sm).toEqual(expect.arrayContaining(['text-[11px]', 'font-[550]', 'px-[7px]', 'py-[1.5px]', 'gap-1', 'rounded-full']))
    expect(sm).not.toContain('text-[11.5px]')
  })
})
