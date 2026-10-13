import { describe, it, expect } from 'vitest'
import { buildNavLinks, moreLabel } from '../../src/utils/navLinks.js'

/**
 * #1925 — NavBar's link set became data so the priority+ strip can render the
 * same link inline, in the overflow menu and in the hidden mirror row from one
 * source. These rules decide what is lit and what is counted, so they are
 * driven here rather than asserted against the SFC's text.
 */
const ids = (links) => links.map((l) => l.id)

describe('buildNavLinks', () => {
  it('omits the Enterprise entry on an OSS build and includes it when entitled', () => {
    expect(ids(buildNavLinks({ path: '/' }))).not.toContain('enterprise')
    expect(ids(buildNavLinks({ path: '/', hasAnyEnterprise: true }))).toContain('enterprise')
  })

  it('keeps Enterprise last so an OSS build and an entitled one share a prefix', () => {
    const oss = ids(buildNavLinks({ path: '/' }))
    const ent = ids(buildNavLinks({ path: '/', hasAnyEnterprise: true }))
    expect(ent.slice(0, oss.length)).toEqual(oss)
  })

  const activeId = (path, opts = {}) =>
    buildNavLinks({ path, ...opts }).filter((l) => l.active).map((l) => l.id)

  it('lights exactly one entry per route', () => {
    expect(activeId('/')).toEqual(['dashboard'])
    expect(activeId('/library')).toEqual(['library'])
    expect(activeId('/operations')).toEqual(['operations'])
    expect(activeId('/settings')).toEqual(['settings'])
    expect(activeId('/workspace')).toEqual(['workspace'])
    expect(activeId('/enterprise/audit', { hasAnyEnterprise: true })).toEqual(['enterprise'])
  })

  it('lights Dashboard on an agent-detail route (ent#260 — Agents is the List mode)', () => {
    expect(activeId('/agents/scout')).toEqual(['dashboard'])
    expect(activeId('/agents')).toEqual(['dashboard'])
  })

  it('lights nothing on an unrelated route rather than falling back to Dashboard', () => {
    // `/login` and `/setup` render the bar on some paths; a false highlight is
    // worse than none.
    expect(activeId('/canvas/abc')).toEqual([])
  })

  it('lights a section entry on its sub-routes, not on a lookalike path', () => {
    expect(activeId('/library?tab=skills')).toEqual(['library'])
    expect(activeId('/workspace/r/room-1')).toEqual(['workspace'])
    // Prefix matching is on the whole segment name, so a different word that
    // shares a few letters does not light the entry.
    expect(activeId('/librarian')).toEqual([])
    expect(activeId('/lib')).toEqual([])
  })

  const ops = (opts) => buildNavLinks({ path: '/', ...opts }).find((l) => l.id === 'operations')

  it('carries no Operations badge at zero pending', () => {
    expect(ops({ opsCount: 0 }).badge).toBeNull()
    // Critical with nothing pending is not a badge — it is a contradiction.
    expect(ops({ opsCount: 0, opsCritical: true }).badgeCritical).toBe(false)
  })

  it('clamps the Operations badge at 99+', () => {
    expect(ops({ opsCount: 7 }).badge).toBe('7')
    expect(ops({ opsCount: 99 }).badge).toBe('99')
    expect(ops({ opsCount: 100 }).badge).toBe('99+')
  })

  it('marks the badge critical only when something critical is pending', () => {
    expect(ops({ opsCount: 3 }).badgeCritical).toBe(false)
    expect(ops({ opsCount: 3, opsCritical: true }).badgeCritical).toBe(true)
  })

  it('opens Workspace in a new tab with noopener (ent#456)', () => {
    const ws = buildNavLinks({ path: '/' }).find((l) => l.id === 'workspace')
    expect(ws.target).toBe('_blank')
    expect(ws.rel).toBe('noopener')
  })

  it('gives every other entry a same-tab link', () => {
    for (const l of buildNavLinks({ path: '/', hasAnyEnterprise: true })) {
      if (l.id === 'workspace') continue
      expect(l.target).toBeUndefined()
    }
  })

  it('gives every entry a route and a label', () => {
    for (const l of buildNavLinks({ path: '/', hasAnyEnterprise: true })) {
      expect(l.to.startsWith('/')).toBe(true)
      expect(l.label.length).toBeGreaterThan(0)
    }
  })

  it('survives being called with no arguments', () => {
    expect(buildNavLinks().length).toBeGreaterThan(0)
  })
})

describe('moreLabel', () => {
  it('counts the hidden entries — the contract asks for "N more", not "More"', () => {
    expect(moreLabel(1)).toBe('1 more')
    expect(moreLabel(4)).toBe('4 more')
  })

  it('falls back to a bare label at zero, which is the widest-case measurement', () => {
    expect(moreLabel(0)).toBe('More')
  })
})
