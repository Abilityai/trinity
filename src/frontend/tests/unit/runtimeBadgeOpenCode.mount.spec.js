// @vitest-environment jsdom
/**
 * #2981 checkpoint H (H1) — the OpenCode runtime badge.
 *
 * Mounted, because the label, tooltip and class string the badge renders are
 * the fact. OpenCode wears its own `brand-opencode` token family and nothing
 * else: a raw palette class here would grow RuntimeBadge.vue's raw-color
 * ratchet entry. The three existing runtimes are pinned unchanged.
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import RuntimeBadge from '@/components/RuntimeBadge.vue'

const badge = (props) => mount(RuntimeBadge, { props })
const RAW = /\b(bg|text|border)-(emerald|teal|green|orange|blue)-/

describe('RuntimeBadge — opencode', () => {
  it('renders the OpenCode label and tooltip', () => {
    const w = badge({ runtime: 'opencode' })
    expect(w.text()).toBe('OpenCode')
    expect(w.attributes('title')).toBe('OpenCode Runtime')
  })

  it('wears the brand-opencode family in both themes and no raw palette class', () => {
    const c = badge({ runtime: 'opencode' }).classes()
    expect(c).toContain('bg-brand-opencode-50')
    expect(c).toContain('dark:text-brand-opencode-300')
    expect(c).toContain('border-brand-opencode-200')
    expect(c.filter((k) => RAW.test(k))).toEqual([])
  })

  it('draws its own glyph with currentColor, never a hex fill', () => {
    const svg = badge({ runtime: 'opencode' }).find('svg')
    expect(svg.exists()).toBe(true)
    expect(svg.attributes('stroke')).toBe('currentColor')
    expect(svg.html()).not.toMatch(/#[0-9a-f]{3,6}/i)
  })

  it('hides the label when showLabel is false', () => {
    const w = badge({ runtime: 'opencode', showLabel: false })
    expect(w.text()).toBe('')
    expect(w.attributes('title')).toBe('OpenCode Runtime')
  })

  it('leaves the other runtimes as they were', () => {
    expect(badge({ runtime: 'claude-code' }).text()).toBe('Claude')
    expect(badge({ runtime: 'gemini-cli' }).text()).toBe('Gemini')
    expect(badge({ runtime: 'codex' }).text()).toBe('Codex')
    expect(badge({ runtime: 'something-else' }).text()).toBe('something-else')
  })
})
