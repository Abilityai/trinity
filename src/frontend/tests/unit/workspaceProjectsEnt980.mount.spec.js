// @vitest-environment jsdom
/**
 * trinity-enterprise#980 — a steward's proposal waiting on the creator, said on the page.
 *
 * The steward agent proposes members and task owners; the project's creator
 * approves once, in the Inbox. Until they answer, the Steward card says the
 * proposal is waiting on them — "You" when the reader is the creator — so the
 * steward's sweep (and every member reading the page) knows nothing is lost.
 * No pending proposal, no line.
 */
import { describe, it, expect } from 'vitest'
import { mount, RouterLinkStub } from '@vue/test-utils'
import ProjectStewardCard from '@/components/portal/projects/ProjectStewardCard.vue'
import { askInboxRoute } from '@/components/portal/portalRailAsks'
import { INBOX_LINK_CLASS } from '@/components/portal/portalUtils'

const ME = 'eva@example.com'

function project(over = {}) {
  return {
    id: 'prj_1', created_by: ME,
    steward: { kind: 'agent', ref: 'scout', last_sweep_at: null, schedule: { state: 'none' } },
    rollup: { by_status: {}, health: null, health_due: false },
    proposal: null,
    ...over,
  }
}

const card = (p, myEmail = ME) => mount(ProjectStewardCard, {
  props: { project: p, myEmail },
  global: { stubs: { RouterLink: RouterLinkStub } },
})
const line = (w) => w.find('[data-testid="project-steward-proposal"]')

describe('ent#980 — a proposal waiting on the creator', () => {
  const PROPOSAL = { id: 'ppr_1', proposed_by: 'scout', proposed_at: '2026-10-27T09:00:00Z', waiting_on: ME, ask_id: 'oq-7' }

  it('the creator reads that it waits on them, and goes straight to the decision', () => {
    const w = card(project({ proposal: PROPOSAL }))
    expect(line(w).exists()).toBe(true)
    expect(w.find('[data-testid="project-steward-proposal-heading"]').text()).toBe('Waiting on you')
    // True of every proposal — a visibility- or steward-only one included.
    expect(line(w).text()).toContain('scout proposed changes to this project')
    expect(line(w).text()).not.toContain('task owners')
    expect(line(w).text()).toContain('Nothing changes until you approve it.')
    // The time reads as its own words ("project 2 hours ago"), never glued on.
    expect(line(w).text()).toMatch(/this project \S/)
    // An in-app route to the decision (no full Workspace reload), built like
    // the project's other Inbox link, with its 44px touch target.
    const review = w.findComponent(RouterLinkStub)
    expect(review.attributes('data-testid')).toBe('project-steward-proposal-review')
    expect(review.text()).toBe('Review in Inbox')
    expect(review.props('to')).toEqual(askInboxRoute({ id: 'oq-7', agent_name: 'scout' }))
    for (const cls of INBOX_LINK_CLASS.split(' ')) expect(review.classes()).toContain(cls)
  })

  it('another member reads who it waits on, with nothing to act on', () => {
    const w = card(project({ proposal: PROPOSAL }), 'bob@example.com')
    expect(w.find('[data-testid="project-steward-proposal-heading"]').text()).toBe(`Waiting on ${ME}`)
    expect(line(w).text()).toContain('Nothing changes until they approve it.')
    expect(w.find('[data-testid="project-steward-proposal-review"]').exists()).toBe(false)
  })

  it('no pending proposal, no line — and an older backend without the field says nothing', () => {
    expect(line(card(project())).exists()).toBe(false)
    const { proposal, ...older } = project()
    expect(line(card(older)).exists()).toBe(false)
  })
})
