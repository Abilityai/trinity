// @vitest-environment jsdom
/**
 * #4344 — a client never reads the execution's stored error summary on a Work
 * card. The summary is masked for credentials and truncated, but it is still a
 * stack-trace-shaped line ("TimeoutError: agent did not respond in 900s"). A
 * platform session keeps it; a client reads "This run failed". The prop fails
 * closed: a host that forgets to pass it shows the client line.
 */
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import PortalWorkCard from '../../src/components/portal/PortalWorkCard.vue'

const SUMMARY = 'TimeoutError: agent did not respond within 900s (exec VAHvU8sG)'
const failed = { id: 'exec-1', agent_name: 'proj-alpha', status: 'failed', outcome: 'failed', kind: 'background', title: 'Weekly digest', steps: null, error: SUMMARY }
const errorLine = (w) => w.get('[data-testid="portal-work-error"]').text()

describe('PortalWorkCard — the failure line by audience', () => {
  it('a platform session sees the stored error summary', () => {
    const w = mount(PortalWorkCard, { props: { item: failed, compact: true, technical: true } })
    expect(errorLine(w)).toBe(SUMMARY)
  })

  it('a client session sees a plain line, never the summary', () => {
    const w = mount(PortalWorkCard, { props: { item: failed, compact: true, technical: false } })
    expect(errorLine(w)).toBe('This run failed')
    expect(w.text()).not.toContain('TimeoutError')
  })

  it('fails closed when the host passes no audience', () => {
    const w = mount(PortalWorkCard, { props: { item: failed, compact: true } })
    expect(errorLine(w)).toBe('This run failed')
  })

  it('a run with no error shows no failure line in either audience', () => {
    for (const technical of [true, false]) {
      const w = mount(PortalWorkCard, { props: { item: { ...failed, error: null, status: 'success', outcome: 'success' }, compact: true, technical } })
      expect(w.find('[data-testid="portal-work-error"]').exists()).toBe(false)
    }
  })
})
