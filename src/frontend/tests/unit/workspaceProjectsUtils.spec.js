import { describe, it, expect } from 'vitest'
import {
  STATUS_BADGE, statusLabel, visibilityLabel, workOnItOptions, chatProjectActions,
  projectErrorMessage, filterProjects, validateProjectForm, agentStateLabel,
} from '@/components/portal/projects/projectsUtils'

describe('ent#661 projects — display', () => {
  it('maps each status to a badge variant and a label', () => {
    expect(STATUS_BADGE).toEqual({ active: 'success', paused: 'warning', done: 'neutral' })
    expect(statusLabel('paused')).toBe('Paused')
    expect(statusLabel('bogus')).toBe('Unknown')
  })

  it('names visibility in the user\'s words', () => {
    expect(visibilityLabel('members')).toBe('Members only')
    expect(visibilityLabel('company')).toBe('Company')
  })

  it('says what an agent state means', () => {
    expect(agentStateLabel('active')).toBe('Can work on it')
    expect(agentStateLabel('pending')).toBe('Waiting for owner')
    expect(agentStateLabel('declined')).toBe('Owner declined')
  })
})

describe('ent#661 projects — Work on it', () => {
  const project = {
    agents: [
      { agent_name: 'a1', state: 'active' },
      { agent_name: 'a2', state: 'active' },
      { agent_name: 'a3', state: 'pending' },
      { agent_name: 'a4', state: 'active' },
      { agent_name: 'a5', state: 'declined' },
    ],
    my_chats: [
      { id: 'old', agent_name: 'a1', title: 'Old', last_message_at: '2026-09-01T00:00:00Z' },
      { id: 'new', agent_name: 'a1', title: 'Newer', last_message_at: '2026-09-20T00:00:00Z' },
    ],
  }

  it('reopens my newest chat, offers a new one, and never offers what I cannot use', () => {
    const out = workOnItOptions(project, ['a1', 'a2', 'a3'])
    expect(out.map((o) => [o.agent, o.kind])).toEqual([
      ['a1', 'reopen'], ['a2', 'new'], ['a3', 'pending'], ['a4', 'unavailable'],
    ])
    expect(out[0].chat.id).toBe('new')
  })

  it('an archived project offers only reopening', () => {
    const out = workOnItOptions({ ...project, archived_at: '2026-09-25T00:00:00Z' }, ['a1', 'a2'])
    expect(out.map((o) => o.kind)).toEqual(['reopen', 'archived', 'pending', 'unavailable'])
  })
})

describe('ent#661 projects — chat header actions', () => {
  const base = { projectsAvailable: true, isPlatform: true, isMain: false, sessionId: 's1' }

  it('offers make/add in a plain chat, detach in a linked one', () => {
    expect(chatProjectActions({ ...base, project: null })).toMatchObject({ show: true, linked: false })
    expect(chatProjectActions({ ...base, project: { id: 'p' } })).toMatchObject({ show: true, linked: true })
  })

  it('never in Main, never for an outside client, never before a chat exists', () => {
    expect(chatProjectActions({ ...base, isMain: true }).show).toBe(false)
    expect(chatProjectActions({ ...base, isPlatform: false }).show).toBe(false)
    expect(chatProjectActions({ ...base, projectsAvailable: false }).show).toBe(false)
    expect(chatProjectActions({ ...base, sessionId: null }).show).toBe(false)
  })
})

describe('ent#661 projects — errors, filters, validation', () => {
  it('turns a backend code into what to do next', () => {
    const err = { response: { status: 409, data: { detail: { code: 'owner_unreachable', message: 'x' } } } }
    expect(projectErrorMessage(err)).toMatch(/owner/i)
    expect(projectErrorMessage({ response: { status: 500 } })).toMatch(/try again/i)
    expect(projectErrorMessage({})).toMatch(/connection/i)
  })

  it('filters by the chosen tab and a name search', () => {
    const list = [
      { id: '1', name: 'Q4 launch', my_role: 'creator', visibility: 'members' },
      { id: '2', name: 'Hiring', my_role: null, visibility: 'company' },
      { id: '3', name: 'Offsite', my_role: 'member', visibility: 'company' },
    ]
    expect(filterProjects(list, 'member', '').map((p) => p.id)).toEqual(['1', '3'])
    expect(filterProjects(list, 'company', '').map((p) => p.id)).toEqual(['2', '3'])
    expect(filterProjects(list, 'all', 'HIR').map((p) => p.id)).toEqual(['2'])
  })

  it('names what is wrong with a form, with an example', () => {
    expect(validateProjectForm({ name: '', goal: '' })).toMatchObject({ name: expect.any(String), goal: expect.any(String) })
    expect(validateProjectForm({ name: 'Q', goal: 'G', tracker_url: 'ftp://x' }).tracker_url).toMatch(/https:\/\//)
    expect(validateProjectForm({ name: 'Q', goal: 'G', tracker_url: 'https://example.com/i/1' })).toEqual({})
    expect(validateProjectForm({ name: 'x'.repeat(121), goal: 'G' }).name).toMatch(/120/)
  })
})

// trinity-enterprise#1240: a task's GitHub issues and PRs.
import { REF_STATE_BADGE, refBadge, refHref, refLabel } from '@/components/portal/projects/projectsUtils'

describe('ent#1240 projects — GitHub ref badges', () => {
  it('names every state with a semantic badge variant', () => {
    expect(REF_STATE_BADGE.open).toEqual({ label: 'Open', variant: 'success' })
    expect(REF_STATE_BADGE.merged).toEqual({ label: 'Merged', variant: 'purple' })
    expect(REF_STATE_BADGE.closed).toEqual({ label: 'Closed', variant: 'neutral' })
    expect(REF_STATE_BADGE.not_planned).toEqual({ label: 'Not planned', variant: 'neutral' })
    expect(REF_STATE_BADGE.unreadable).toEqual({ label: "Couldn't read", variant: 'warning' })
  })

  it("an unreadable ref is 'couldn't read' in the warning tone, never closed", () => {
    const b = refBadge({ readable: false, state: 'closed' })
    expect(b).toBe(REF_STATE_BADGE.unreadable)
    expect(b.variant).not.toBe(REF_STATE_BADGE.closed.variant)
    expect(refBadge({ readable: true, state: 'merged' })).toBe(REF_STATE_BADGE.merged)
  })

  it('a readable ref with no state yet reads as unreadable, not as a guessed state', () => {
    expect(refBadge({ readable: true, state: null })).toBe(REF_STATE_BADGE.unreadable)
    expect(refBadge(null)).toBe(REF_STATE_BADGE.unreadable)
  })

  it('links only an https:// url', () => {
    expect(refHref({ url: 'https://github.com/o/r/pull/3' })).toBe('https://github.com/o/r/pull/3')
    expect(refHref({ url: 'http://github.com/o/r/pull/3' })).toBeNull()
    expect(refHref({ url: 'javascript:alert(1)' })).toBeNull()
    expect(refHref({ url: ' https://x' })).toBeNull()
    expect(refHref({})).toBeNull()
    expect(refHref(null)).toBeNull()
  })

  it('labels a ref owner/repo#N', () => {
    expect(refLabel({ repo: 'acme/web', number: 42 })).toBe('acme/web#42')
  })
})
