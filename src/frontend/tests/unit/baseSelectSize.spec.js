// @vitest-environment jsdom
/**
 * trinity-enterprise#754 — BaseSelect `size="sm"`, MOUNTED.
 *
 * The Skills card's approver picker sits in a 40px row beside a 20px toggle.
 * The `field` recipe is a form field (38px measured in Chromium, edge to edge
 * of that row) and `ghost` is the composer's 44px box, so neither fits; the
 * approved card design draws a compact bordered select. `size="sm"` gives
 * `field` BaseButton sm's box (12.5 ink, padding 4×10; 28.8px measured). It
 * sizes `field` only, and the default `field` recipe stays byte-identical:
 * BaseInput and BaseTextarea wear it too.
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { h } from 'vue'
import BaseSelect from '../../src/components/base/BaseSelect.vue'
import { FIELD_CLASS, FIELD_SM_CLASS, FIELD_GHOST_CLASS } from '../../src/components/base/fieldClasses.js'

const tokens = (s) => s.split(/\s+/).filter(Boolean)
function render(props) {
  const w = mount(BaseSelect, { props: { modelValue: 'a', ...props }, slots: { default: () => h('option', { value: 'a' }, 'A') } })
  return { select: w.find('select'), chevron: w.find('svg') }
}

describe('BaseSelect size', () => {
  it('sm: the field recipe at BaseButton sm\'s box, with the 12px chevron', () => {
    const { select, chevron } = render({ size: 'sm' })
    for (const t of tokens(FIELD_SM_CLASS)) expect(select.classes()).toContain(t)
    expect(select.classes()).toEqual(expect.arrayContaining(['px-2.5', 'py-1', 'text-[12.5px]', 'pr-7']))
    expect(select.classes()).not.toContain('py-2')
    expect(select.classes()).not.toContain('text-[13.5px]')
    expect(chevron.classes()).toEqual(expect.arrayContaining(['right-2', 'h-3', 'w-3']))
  })

  it('md is the default and the form field it always was', () => {
    const { select, chevron } = render({})
    for (const t of tokens(FIELD_CLASS)) expect(select.classes()).toContain(t)
    expect(select.classes()).toContain('pr-8')
    expect(chevron.classes()).toEqual(expect.arrayContaining(['right-[10px]', 'h-3.5', 'w-3.5']))
  })

  it('ghost takes no size: it stays the 44px composer box', () => {
    const sm = render({ variant: 'ghost', size: 'sm' })
    const md = render({ variant: 'ghost' })
    for (const t of tokens(FIELD_GHOST_CLASS)) expect(sm.select.classes()).toContain(t)
    expect(sm.select.classes()).toContain('h-11')
    expect(sm.select.classes()).toEqual(md.select.classes())
    expect(sm.chevron.classes()).toEqual(md.chevron.classes())
  })

  it('the shared field recipe BaseInput and BaseTextarea wear is unchanged', () => {
    expect(FIELD_CLASS).toBe(
      'w-full rounded-md border bg-white dark:bg-gray-900 px-[11px] py-2 text-[13.5px] text-gray-900 dark:text-gray-100 '
      + 'placeholder:text-gray-500 dark:placeholder:text-gray-500 focus:outline-none focus:ring-[3px] '
      + 'disabled:opacity-45 disabled:cursor-not-allowed')
    // sm differs in the box only.
    expect(FIELD_SM_CLASS).toBe(FIELD_CLASS.replace('px-[11px] py-2 text-[13.5px]', 'px-2.5 py-1 text-[12.5px]'))
  })
})
