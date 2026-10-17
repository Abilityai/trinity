/**
 * Settings → AAuth: the rules the panel runs on (abilityai/trinity-enterprise#862).
 *
 * The save body is the part that bites: a settings form that sends every field
 * freezes untouched values into rows, and for a value sourced from the
 * environment that silently ends the variable's authority (learnings
 * 2026-09-29). So the body carries only what the admin changed.
 */
import { describe, it, expect } from 'vitest'
import { buildAAuthUpdate, issuerProblem, readinessItems } from '@/utils/aauthSettings'

const loaded = {
  enabled: false, enabled_source: 'default',
  issuer: 'https://a.example.com', issuer_source: 'env', issuer_valid: true,
  prototype_enabled: false,
}

describe('buildAAuthUpdate', () => {
  it('sends nothing when nothing changed', () => {
    expect(buildAAuthUpdate(loaded, { enabled: false, issuer: 'https://a.example.com' })).toEqual({})
  })

  it('sends only the toggled switch, never the env-sourced issuer it did not touch', () => {
    expect(buildAAuthUpdate(loaded, { enabled: true, issuer: 'https://a.example.com' })).toEqual({ enabled: true })
  })

  it('sends a changed issuer trimmed', () => {
    expect(buildAAuthUpdate(loaded, { enabled: false, issuer: '  https://b.example.com ' }))
      .toEqual({ issuer: 'https://b.example.com' })
  })

  it('sends an empty issuer to clear a stored one', () => {
    const stored = { ...loaded, issuer_source: 'settings' }
    expect(buildAAuthUpdate(stored, { enabled: false, issuer: '' })).toEqual({ issuer: '' })
  })
})

describe('issuerProblem', () => {
  it.each([
    ['https://trinity.example.com', ''],
    ['', ''],
  ])('accepts %s', (v, want) => expect(issuerProblem(v)).toBe(want))

  it.each([
    ['http://trinity.example.com', /https/],
    ['https://trinity.example.com:8443', /port/],
    ['https://trinity.example.com/a2a', /path/],
    ['https://Trinity.example.com', /lowercase/],
    ['https://10.0.0.5', /host name/],
    ['trinity.example.com', /https/],
  ])('names what is wrong with %s', (v, re) => expect(issuerProblem(v)).toMatch(re))
})

describe('readinessItems', () => {
  const base = {
    live: true,
    readiness: { issuer_valid: true, outbound_enabled: true, endpoints: 2, aauth_endpoints: 1 },
  }

  it('is all done for a ready instance', () => {
    expect(readinessItems(base).every((i) => i.ok)).toBe(true)
  })

  it('names the next step for each missing piece', () => {
    const items = readinessItems({
      live: false,
      readiness: { issuer_valid: false, outbound_enabled: false, endpoints: 0, aauth_endpoints: 0 },
    })
    expect(items.map((i) => i.key)).toEqual(['issuer', 'live', 'outbound', 'endpoint'])
    expect(items.every((i) => !i.ok && i.next.length > 0)).toBe(true)
  })
})
