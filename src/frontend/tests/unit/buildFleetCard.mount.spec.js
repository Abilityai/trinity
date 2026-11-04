// @vitest-environment jsdom
/**
 * The card in the chat: the page's words, Confirm and Not now, one change each.
 */
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import PortalBuildFleetCard from '@/components/portal/builder/PortalBuildFleetCard.vue'

const confirm = { kind: 'confirm', id: 'p1', action: 'allow-call', from: 'researcher', to: 'writer', words: 'Let Researcher call Writer', showMe: 'permissions' }

describe('the Confirm card', () => {
  it("shows the page's words with Confirm and Not now, and nothing to confirm in bulk", () => {
    const w = mount(PortalBuildFleetCard, { props: { card: confirm } })
    expect(w.get('[data-testid="build-fleet-card-words"]').text()).toBe('Let Researcher call Writer')
    expect(w.get('[data-testid="build-fleet-confirm"]').text()).toBe('Confirm')
    expect(w.get('[data-testid="build-fleet-decline"]').text()).toBe('Not now')
    expect(w.text()).not.toMatch(/confirm all/i)
    expect(w.findAll('button').filter((b) => /confirm/i.test(b.text()))).toHaveLength(1)
  })

  it('Confirm asks once, however often it is clicked', async () => {
    const w = mount(PortalBuildFleetCard, { props: { card: confirm } })
    await w.get('[data-testid="build-fleet-confirm"]').trigger('click')
    await w.get('[data-testid="build-fleet-confirm"]').trigger('click')
    expect(w.emitted('confirm')).toHaveLength(1)
  })

  it('then says it is done, with where it is set, or why it failed', async () => {
    const done = mount(PortalBuildFleetCard, { props: { card: confirm, status: 'done' } })
    expect(done.text()).toContain('Done')
    expect(done.get('[data-testid="build-fleet-see-where"]').attributes('href')).toBe('/agents/researcher?tab=permissions&showme=permissions')
    // A card that turned something on walks there from the Dashboard instead.
    const walked = mount(PortalBuildFleetCard, { props: { card: { ...confirm, walk: { to: 'writer' } }, status: 'done' } })
    expect(walked.get('[data-testid="build-fleet-see-where"]').attributes('href')).toBe('/?walk=permissions&walk_agent=researcher&walk_to=writer')
    const schedule = { kind: 'confirm', id: 'p4', action: 'schedule-on', agent: 'researcher', schedule_id: 's1', words: 'Turn on Morning scan for Researcher', showMe: 'schedules', walk: { schedule: 's1' } }
    expect(mount(PortalBuildFleetCard, { props: { card: schedule, status: 'done' } }).get('[data-testid="build-fleet-see-where"]').attributes('href'))
      .toBe('/?walk=schedules&walk_agent=researcher&walk_schedule=s1')
    const failed = mount(PortalBuildFleetCard, { props: { card: confirm, status: 'failed', reason: 'Only the owner can modify agent permissions' } })
    expect(failed.text()).toContain('Nothing was changed. Only the owner can modify agent permissions')
  })

  it('a Show me card opens the real page in a new tab', () => {
    const w = mount(PortalBuildFleetCard, { props: { card: { kind: 'show-me', id: 'p2', key: 'skills', agent: 'writer', words: "Show me where Writer's skills are set" } } })
    const link = w.get('[data-testid="build-fleet-show-me"]')
    expect(link.attributes('href')).toBe('/agents/writer?tab=skills&showme=skills')
    expect(link.attributes('target')).toBe('_blank')
    expect(w.find('[data-testid="build-fleet-confirm"]').exists()).toBe(false)
  })

  it("a credentials Show me starts the walk on the Dashboard, with the keys to light up", () => {
    const card = { kind: 'show-me', id: 'p3', key: 'credentials', agent: 'game-promoter', keys: ['SUPABASE_URL', 'SUPABASE_KEY'], words: "Show me how to add Game Promoter's credentials" }
    const w = mount(PortalBuildFleetCard, { props: { card } })
    expect(w.text()).toContain("Show me how to add Game Promoter's credentials")
    expect(w.get('[data-testid="build-fleet-show-me"]').attributes('href'))
      .toBe('/?walk=credentials&walk_agent=game-promoter&walk_keys=SUPABASE_URL%2CSUPABASE_KEY')
  })

  it('a calling or schedules Show me starts its walk with the agent to call or the schedule', () => {
    const call = { kind: 'show-me', id: 'p5', key: 'permissions', agent: 'researcher', to: 'writer', words: 'Show me how to let Researcher call Writer' }
    expect(mount(PortalBuildFleetCard, { props: { card: call } }).get('[data-testid="build-fleet-show-me"]').attributes('href'))
      .toBe('/?walk=permissions&walk_agent=researcher&walk_to=writer')
    const turnOn = { kind: 'show-me', id: 'p6', key: 'schedules', agent: 'researcher', schedule: 's1', words: "Show me how to turn on Researcher's Morning scan" }
    expect(mount(PortalBuildFleetCard, { props: { card: turnOn } }).get('[data-testid="build-fleet-show-me"]').attributes('href'))
      .toBe('/?walk=schedules&walk_agent=researcher&walk_schedule=s1')
  })

  it('says when See where this is set was opened, so the page can refresh what Atlas sees on return', async () => {
    const w = mount(PortalBuildFleetCard, { props: { card: { ...confirm, walk: { to: 'writer' } }, status: 'done' } })
    await w.get('[data-testid="build-fleet-see-where"]').trigger('click')
    expect(w.emitted('opened')).toHaveLength(1)
  })

  it('a refusal says why and can only be dismissed', () => {
    const w = mount(PortalBuildFleetCard, { props: { card: { kind: 'refused', id: 'p3', reason: "Atlas suggested a change this page can't make." } } })
    expect(w.text()).toContain("Atlas suggested a change this page can't make.")
    expect(w.find('[data-testid="build-fleet-confirm"]').exists()).toBe(false)
    expect(w.get('[data-testid="build-fleet-dismiss"]').text()).toBe('OK')
  })
})

describe('final review: a card that is not sure', () => {
  it('never says nothing was changed when it may have been', () => {
    const w = mount(PortalBuildFleetCard, { props: { card: confirm, status: 'unsure', reason: "Couldn't reach Trinity, so it may not have been made." } })
    expect(w.text()).toContain("This may or may not have been made. Couldn't reach Trinity, so it may not have been made.")
    expect(w.text()).not.toContain('Nothing was changed')
    expect(w.get('[data-testid="build-fleet-see-where"]').attributes('href')).toBe('/agents/researcher?tab=permissions&showme=permissions')
  })
})

