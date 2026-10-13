// @vitest-environment jsdom
/**
 * ConfirmDialog's icon per variant (ent#610 round 3). `info` is additive: the
 * default (danger) and `warning` render exactly as before.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import ConfirmDialog from '@/components/ConfirmDialog.vue'

let w
afterEach(() => { w?.unmount(); w = null; document.body.innerHTML = '' })
const icon = async (props) => {
  w = mount(ConfirmDialog, { props: { visible: true, title: 'T', message: 'M', ...props }, attachTo: document.body })
  await w.vm.$nextTick()
  return document.querySelector('[data-testid="confirm-dialog-icon"]')
}

describe('ConfirmDialog icon', () => {
  it('defaults to danger (unchanged)', async () => {
    const el = await icon({})
    expect(el.getAttribute('data-variant')).toBe('danger')
    expect(el.getAttribute('class')).toContain('text-status-danger-600')
  })
  it('info is an i in a circle in primary ink, not a warning triangle', async () => {
    const tri = (await icon({ variant: 'warning' })).querySelector('path').getAttribute('d')
    w.unmount(); document.body.innerHTML = ''
    const el = await icon({ variant: 'info' })
    expect(el.getAttribute('class')).toContain('text-action-primary-700')
    expect(el.querySelector('path').getAttribute('d')).not.toBe(tri)
  })
})
