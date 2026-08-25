/**
 * ent#365 — deliverables in the Workspace.
 *
 * The decidable parts live in `portalUtils.js` for this project's usual reason:
 * `vitest.config.js` is `environment: 'node'` with no mount harness, so a rule
 * inside a `.vue` file is a rule no test can execute. The source assertions at
 * the bottom are scoped to what only source can answer — that the card list is
 * chat-scoped, that it re-reads after a turn rather than on a timer, and that
 * the client fallback renderer is the stricter one (#2162).
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

import { deliverableKindLabel, relativeTime, DELIVERABLE_KIND_LABELS } from '../../src/components/portal/portalUtils'

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')
const card = read('../../src/components/portal/PortalDeliverables.vue')
const conversation = read('../../src/components/portal/PortalConversation.vue')
const store = read('../../src/stores/clientPortal.js')

describe('the card badge', () => {
  it('labels every hint the renderer dispatches on', () => {
    // Keyed off `display_hint`, the same enum ReportRenderer switches on — so a
    // hint the renderer knows always has a word for it.
    for (const hint of ['table', 'kpi', 'markdown', 'timeline', 'json']) {
      expect(DELIVERABLE_KIND_LABELS[hint], hint).toBeTruthy()
      expect(deliverableKindLabel(hint)).toBe(DELIVERABLE_KIND_LABELS[hint])
    }
  })

  it('degrades to an honest word rather than showing nothing', () => {
    expect(deliverableKindLabel(undefined)).toBe('Report')
    expect(deliverableKindLabel(null)).toBe('Report')
    expect(deliverableKindLabel('something-new')).toBe('Report')
  })
})

describe('when it was delivered', () => {
  const NOW = Date.parse('2026-08-24T12:00:00Z')
  const ago = (ms) => new Date(NOW - ms).toISOString()

  it('is relative for recency', () => {
    expect(relativeTime(ago(10_000), NOW)).toBe('just now')
    expect(relativeTime(ago(5 * 60_000), NOW)).toBe('5m ago')
    expect(relativeTime(ago(3 * 3_600_000), NOW)).toBe('3h ago')
    expect(relativeTime(ago(2 * 86_400_000), NOW)).toBe('2d ago')
  })

  it('becomes an absolute date past a week (principle 22)', () => {
    expect(relativeTime(ago(30 * 86_400_000), NOW)).toBe('2026-07-25')
  })

  it('never renders a lie for missing or unparseable input', () => {
    expect(relativeTime(null)).toBe('')
    expect(relativeTime(undefined)).toBe('')
    expect(relativeTime('not a date')).toBe('')
  })

  it('never reports the future as elapsed time', () => {
    // Clock skew between an agent's container and the browser is routine.
    expect(relativeTime(new Date(NOW + 60_000).toISOString(), NOW)).toBe('just now')
  })
})

describe('what only source can answer', () => {
  it('lists deliverables for THIS chat, not for the agent', () => {
    expect(card).toContain('store.fetchSessionDeliverables(props.agentName, props.sessionId)')
    expect(store).toContain('params: { session_id: sessionId }')
  })

  it('re-reads after a turn rather than on a timer', () => {
    // A poll would spend requests on a conversation nobody is talking in; a
    // turn is the only thing that can produce a deliverable here.
    expect(conversation).toContain('deliverableTick.value += 1')
    expect(card).toContain('watch(() => props.refreshKey, load)')
    expect(card).not.toContain('setInterval')
  })

  it('clears expanded payloads when the thread changes', () => {
    // Carrying one open payload across a switch renders one conversation's
    // deliverable inside another.
    expect(card).toMatch(/watch\(\(\) => \[props\.agentName, props\.sessionId\][\s\S]{0,320}delete payloads\[k\]/)
  })

  it('gives a client the stricter fallback renderer, never the raw payload', () => {
    // The #2162 rule: an operator surface may dump JSON, a client surface may
    // not — the payload is free-form agent JSON.
    expect(card).toContain(':fallback-component="ReportSummary"')
    expect(card).not.toContain('JSON.stringify')
  })

  it('surfaces a failed payload read next to its own control', () => {
    expect(card).toContain('<InlineError')
    expect(card).not.toContain('console.error')
  })
})
