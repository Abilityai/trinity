// @vitest-environment jsdom
/**
 * #3884 — the first-run password meter never reads as acceptable while a
 * requirement is unmet. An 8-character password with all four character
 * classes meets 4 of 5 requirements; it used to read "Strong" in green while
 * the submit button stayed disabled.
 */
import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'

vi.mock('axios', () => ({ default: { post: vi.fn(), get: vi.fn() } }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('../../src/router', () => ({ clearSetupCache: vi.fn(), getSetupClaimRequired: () => null }))
vi.mock('../../src/stores/auth', () => ({ useAuthStore: () => ({ loginWithCredentials: vi.fn() }) }))
vi.mock('../../src/components/TrinityMark.vue', () => ({ default: { template: '<span />' } }))

// eslint-disable-next-line import/first
import SetupPassword from '../../src/views/SetupPassword.vue'

const GREEN = 'rgb(74, 222, 128)'

async function meter(pw) {
  const w = mount(SetupPassword)
  await w.find('#password').setValue(pw)
  return {
    label: w.find('.strength').text(),
    labelColor: w.find('.strength').element.style.color,
    barColor: w.find('.meter > i').element.style.background,
  }
}

describe('setup password meter', () => {
  it('an 8-character password with all four classes is not "Strong" and not green, and names what is missing', async () => {
    const m = await meter('Ab1!efgh')
    expect(m.label).not.toMatch(/Strong|Excellent/)
    expect(m.label).toContain('12+ characters')
    expect(m.labelColor).not.toBe(GREEN)
    expect(m.barColor).not.toContain(GREEN)
  })

  it('a 12-character password missing only a symbol names the symbol', async () => {
    const m = await meter('Abcdefgh1234')
    expect(m.label).toContain('Symbol')
    expect(m.barColor).not.toContain(GREEN)
  })

  it('every requirement met reads green', async () => {
    const m = await meter('Sup3rSecret!!')
    expect(m.label).toBe('Excellent')
    expect(m.barColor).toContain(GREEN)
  })
})
