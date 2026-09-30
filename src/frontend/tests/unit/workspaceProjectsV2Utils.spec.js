import { describe, it, expect } from 'vitest'
import {
  TASK_STATUSES, taskStatusLabel, TASK_STATUS_BADGE, taskStatusOptions, groupTasks,
  LOG_KINDS, logKindLabel, authorLabel, wrapUpPrompt, chatProjectActions, isOpenTask,
} from '@/components/portal/projects/projectsUtils'

describe('ent#661 v2 — tasks', () => {
  it('names every status and gives each a badge', () => {
    expect(TASK_STATUSES).toEqual(['active', 'blocked', 'needs-decision', 'paused', 'pending-verification', 'done'])
    for (const s of TASK_STATUSES) {
      expect(taskStatusLabel(s)).not.toBe('Unknown')
      expect(TASK_STATUS_BADGE[s]).toBeTruthy()
    }
    expect(taskStatusLabel('pending-verification')).toBe('Awaiting verification')
  })

  it('open means not awaiting verification and not done', () => {
    expect(isOpenTask({ status: 'blocked' })).toBe(true)
    expect(isOpenTask({ status: 'pending-verification' })).toBe(false)
    expect(isOpenTask({ status: 'done' })).toBe(false)
  })

  it('a done task offers only reopening', () => {
    expect(taskStatusOptions({ status: 'done' })).toEqual(['done', 'active'])
    expect(taskStatusOptions({ status: 'active' })).toEqual(TASK_STATUSES)
  })

  it('groups tasks the way the steward reads them', () => {
    const g = groupTasks([
      { id: 'T-003', status: 'done' }, { id: 'T-001', status: 'active' },
      { id: 'T-002', status: 'pending-verification' }, { id: 'T-004', status: 'blocked' },
    ])
    expect(g.open.map((t) => t.id)).toEqual(['T-001', 'T-004'])
    expect(g.verifying.map((t) => t.id)).toEqual(['T-002'])
    expect(g.done.map((t) => t.id)).toEqual(['T-003'])
  })
})

describe('ent#661 v2 — the log', () => {
  it('names the kinds and the authors', () => {
    expect(LOG_KINDS).toEqual(['decision', 'deliverable', 'task', 'blocker', 'handoff', 'note'])
    expect(logKindLabel('handoff')).toBe('Hand-off')
    expect(authorLabel('agent:scout', 'me@x.com')).toBe('scout (agent)')
    expect(authorLabel('me@x.com', 'me@x.com')).toBe('You')
    expect(authorLabel('import:corbin', 'me@x.com')).toBe('Imported from corbin')
  })

  it('the wrap-up instruction names the project and asks for one entry per outcome', () => {
    const p = wrapUpPrompt('Q4 launch')
    expect(p).toContain('Q4 launch')
    expect(p).toMatch(/one entry per/i)
    expect(p).toMatch(/add_project_log_entry/)
  })
})

describe('ent#661 v2 — chat header for guests', () => {
  const base = { projectsAvailable: true, isMain: false, sessionId: 's1' }
  it('a guest gets the badge and Detach, never the Project button or Wrap up', () => {
    expect(chatProjectActions({ ...base, isPlatform: false, project: null }).show).toBe(false)
    const linked = chatProjectActions({ ...base, isPlatform: false, project: { id: 'p', guest: true } })
    expect(linked).toMatchObject({ show: true, linked: true, canAdd: false, canWrapUp: false })
  })
  it('a platform user gets everything in a linked chat', () => {
    const a = chatProjectActions({ ...base, isPlatform: true, project: { id: 'p' } })
    expect(a).toMatchObject({ show: true, linked: true, canAdd: true, canWrapUp: true })
  })
})
