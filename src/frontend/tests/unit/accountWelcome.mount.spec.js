// @vitest-environment jsdom
/**
 * A welcome per account type: someone who cannot build is told so at the start, in plain
 * words, and shown only what their account has. A Workspace-only user: their agents and
 * the Inbox. An operator: that, and the Dashboard. People who can build get Get started.
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import PortalAccountWelcome from '@/components/portal/builder/PortalAccountWelcome.vue'
import { CREATOR_NOTE, accountKind, accountWelcome, welcomeDue, welcomeTourSteps } from '@/components/portal/builder/accountWelcome'
import { tasksWalk, walkFromQuery, walkLink, walkSteps, SHOW_ME_WALKS } from '@/components/onboarding/showMeWalk'

describe('which welcome an account gets', () => {
  it('is by the role the server reported, for someone signed in to Trinity', () => {
    expect(accountKind({ isPlatformSession: true, role: 'user' })).toBe('user')
    expect(accountKind({ isPlatformSession: true, role: 'operator' })).toBe('operator')
    // People who can build have Get started, and a role still loading says nothing.
    expect(accountKind({ isPlatformSession: true, role: 'creator' })).toBeNull()
    expect(accountKind({ isPlatformSession: true, role: 'admin' })).toBeNull()
    expect(accountKind({ isPlatformSession: true, role: undefined })).toBeNull()
    // Someone a single agent was shared with by email is a guest, not a member with an admin.
    expect(accountKind({ isPlatformSession: false, role: 'user' })).toBeNull()
    expect(accountKind()).toBeNull()
  })

  it('is due once per kind of account, and only once the role is the server\'s word', () => {
    expect(welcomeDue({ kind: 'user', profileVerified: true, intake: {} })).toBe(true)
    expect(welcomeDue({ kind: 'user', profileVerified: false, intake: {} })).toBe(false)
    expect(welcomeDue({ kind: 'user', profileVerified: true, intake: { accountWelcome: 'user' } })).toBe(false)
    // Raised to operator: there is something new to say.
    expect(welcomeDue({ kind: 'operator', profileVerified: true, intake: { accountWelcome: 'user' } })).toBe(true)
    expect(welcomeDue({ kind: null, profileVerified: true, intake: {} })).toBe(false)
    expect(welcomeDue()).toBe(false)
  })
})

describe('what each welcome says', () => {
  it('names the account first, then what it has, then what is not part of it', () => {
    const user = accountWelcome('user', { agents: [{ name: 'a' }] })
    expect(user.account).toBe('Workspace account')
    expect(user.lead).toMatch(/^You have a Workspace account\./)
    expect(user.cannot.join(' ')).toMatch(/Building agents/)
    expect(user.cannot.join(' ')).toMatch(/Dashboard/)
    expect(user.can.join(' ')).not.toMatch(/Dashboard/)
    expect(user.closing).toBe('Need more? Ask your admin.')
    expect(user.empty).toBe('')

    const operator = accountWelcome('operator', { agents: [{ name: 'a' }] })
    expect(operator.account).toBe('Operator account')
    expect(operator.lead).toMatch(/^You have an operator account\./)
    expect(operator.can.join(' ')).toMatch(/Dashboard/)
    expect(operator.cannot.join(' ')).toMatch(/Building agents/)
    expect(operator.cannot.join(' ')).toMatch(/owner or an admin/)
    for (const w of [user, operator]) expect(JSON.stringify(w)).not.toMatch(/—|Atlas|level/i)
    expect(accountWelcome('creator')).toBeNull()
  })

  it('says so when nothing has been shared with them yet', () => {
    expect(accountWelcome('user', { agents: [] }).empty).toBe('No agents have been shared with you yet. Ask your admin or a colleague to share one.')
  })

  it('walks a user round their agents and Inbox, and an operator on to the Dashboard', () => {
    expect(welcomeTourSteps('user').map((s) => s.target)).toEqual(['agents', 'inbox'])
    expect(welcomeTourSteps('operator').map((s) => s.target)).toEqual(['agents', 'inbox', 'dashboard'])
    expect(welcomeTourSteps('operator').at(-1).title).toBe('This is how you get to the Dashboard')
    expect(CREATOR_NOTE).toMatch(/creator account/)
    expect(CREATOR_NOTE).toMatch(/belongs to your admin/)
  })
})

describe('the welcome window', () => {
  const show = (props) => mount(PortalAccountWelcome, { props: { open: true, ...props }, global: { stubs: { teleport: true } } })

  it('shows a user their account, with no Dashboard and nothing about building to do', () => {
    const w = show({ kind: 'user', agents: [{ name: 'a' }] })
    const box = w.get('[data-testid="account-welcome"]')
    expect(box.attributes('data-kind')).toBe('user')
    expect(box.text()).toContain('You have a Workspace account.')
    expect(box.text()).toContain('Not part of this account')
    expect(box.text()).toContain('Need more? Ask your admin.')
    expect(w.find('[data-testid="account-welcome-empty"]').exists()).toBe(false)
  })

  it('tells someone with no agents yet who to ask, and closes on Skip', async () => {
    const w = show({ kind: 'operator', agents: [] })
    expect(w.get('[data-testid="account-welcome-empty"]').text()).toContain('No agents have been shared with you yet')
    await w.get('[data-testid="account-welcome-skip"]').trigger('click')
    expect(w.emitted('done')).toHaveLength(1)
  })

  it('renders nothing for an account that builds', () => {
    expect(show({ kind: null }).find('[data-testid="account-welcome"]').exists()).toBe(false)
  })
})

describe("the operator's walk on the Dashboard: an agent's Tasks", () => {
  it('opens the agent, then its Tasks, and says what the rows are; it is not one of the guide\'s walks', () => {
    expect(SHOW_ME_WALKS).toEqual(['credentials', 'people', 'permissions', 'schedules'])
    expect(walkLink('tasks', 'game-filler')).toBe('/?walk=tasks&walk_agent=game-filler')
    expect(walkFromQuery({ walk: 'tasks', walk_agent: 'game-filler' })).toMatchObject({ key: 'tasks', agent: 'game-filler' })
    const steps = tasksWalk({ agent: 'game-filler', label: 'Game Filler' })
    expect(steps.map((s) => s.id)).toEqual(['agent', 'tab', 'finished'])
    expect(steps[1].at).toEqual({ tab: 'tasks' })
    expect(steps[2].end).toBe(true)
    expect(steps[2].body).toMatch(/Workspace/)
    expect(JSON.stringify(steps)).not.toMatch(/Atlas|—/)
    expect(walkSteps({ key: 'tasks', agent: 'game-filler' }, { agent: 'game-filler', label: 'Game Filler' }).map((s) => s.id)).toEqual(['agent', 'tab', 'finished'])
  })
})
