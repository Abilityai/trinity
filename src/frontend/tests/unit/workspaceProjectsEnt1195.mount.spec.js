// @vitest-environment jsdom
/**
 * trinity-enterprise#1195 — start a task's run from the project (ent#1136 step B).
 *
 * The task detail's "Run this task": a member picks an agent from those active
 * on the project and on their list, previews exactly the first message, and
 * Start opens the task's chat — sending the first message once, with the
 * server's Idempotency-Key, only when the chat has none yet.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn() }),
}))

import ProjectTaskStart from '@/components/portal/projects/ProjectTaskStart.vue'
import { useProjectsStore } from '@/stores/projects'
import { useClientPortalStore } from '@/stores/clientPortal'

const PREVIEW = (agent, over = {}) => ({
  task_id: 'T-001', agent, agents: { available: ['scout', 'writer'], not_on_your_list: [] },
  message: `Let's work on T-001 with ${agent}\n`, message_sha: `sha-${agent}`, context: [{ id: 'c1', filename: 'brief.md', size_bytes: 10 }],
  read_budget_bytes: 204800, log_excerpt: [], chats: [], cost_note: "Runs on the agent's own Claude subscription or API key.",
  archived: false, ...over,
})

const by = (w, id) => w.find(`[data-testid="${id}"]`)
let projects, portal

beforeEach(() => {
  setActivePinia(createPinia())
  projects = useProjectsStore()
  portal = useClientPortalStore()
})

const mountIt = (props = {}) => mount(ProjectTaskStart, {
  props: { projectId: 'prj_1', taskId: 'T-001', canContribute: true, ...props },
})

describe('ent#1195 — run this task', () => {
  it('offers only the agents the member may pick, and previews the exact first message', async () => {
    projects.startPreview = vi.fn().mockResolvedValue(PREVIEW('scout'))
    const w = mountIt()
    await flushPromises()
    const opts = by(w, 'project-task-start-agent').findAll('option').map((o) => o.text())
    expect(opts).toEqual(['scout', 'writer'])
    await by(w, 'project-task-start-preview').trigger('click')
    expect(by(w, 'project-task-start-message').text()).toContain("Let's work on T-001 with scout")
    expect(by(w, 'project-task-start-message').text()).toContain('200 KB per read')
    expect(w.text()).toContain("agent's own Claude subscription")
  })

  it('Start opens the chat and sends the first message once, with the Idempotency-Key', async () => {
    projects.startPreview = vi.fn().mockResolvedValue(PREVIEW('scout'))
    let resolveStart
    projects.startTask = vi.fn(() => new Promise((r) => { resolveStart = r }))   // still in flight…
    portal.startPortalChat = vi.fn().mockResolvedValue({ execution_id: 'e1', session_id: 'th1' })
    const w = mountIt()
    await flushPromises()
    await by(w, 'project-task-start-go').trigger('click')
    await by(w, 'project-task-start-go').trigger('click')          // …when the second click lands
    resolveStart({
      thread_id: 'th1', agent_name: 'scout', created: true, needs_first_message: true,
      message: 'FIRST', idempotency_key: 'project-task-start-abc',
    })
    await flushPromises()
    expect(projects.startTask).toHaveBeenCalledTimes(1)
    expect(projects.startTask).toHaveBeenCalledWith('prj_1', 'T-001', 'scout', 'sha-scout')
    expect(portal.startPortalChat).toHaveBeenCalledTimes(1)
    expect(portal.startPortalChat).toHaveBeenCalledWith('scout', 'FIRST', 'th1', { idempotencyKey: 'project-task-start-abc' })
    expect(w.emitted('open-thread')[0][0]).toEqual({ id: 'th1', agent_name: 'scout' })
  })

  it('a chat already in use is only reopened — no second first message', async () => {
    projects.startPreview = vi.fn().mockResolvedValue(PREVIEW('scout'))
    projects.startTask = vi.fn().mockResolvedValue({ thread_id: 'th1', needs_first_message: false, message: 'FIRST', idempotency_key: 'k' })
    portal.startPortalChat = vi.fn()
    const w = mountIt()
    await flushPromises()
    await by(w, 'project-task-start-go').trigger('click')
    await flushPromises()
    expect(portal.startPortalChat).not.toHaveBeenCalled()
    expect(w.emitted('open-thread')[0][0]).toEqual({ id: 'th1', agent_name: 'scout' })
  })

  it("an existing task chat is offered again", async () => {
    projects.startPreview = vi.fn().mockResolvedValue(PREVIEW('scout', { chats: [{ id: 'th9', agent_name: 'scout' }] }))
    const w = mountIt()
    await flushPromises()
    await by(w, 'project-task-chat-th9').trigger('click')
    expect(w.emitted('open-thread')[0][0]).toEqual({ id: 'th9', agent_name: 'scout' })
  })

  it('with no agent the member can use, it says who to ask', async () => {
    projects.startPreview = vi.fn().mockResolvedValue(PREVIEW(null, {
      agents: { available: [], not_on_your_list: ['scout'] }, message: null,
    }))
    const w = mountIt()
    await flushPromises()
    expect(by(w, 'project-task-start-none').text()).toContain("You can't use scout yet. Ask its owner for access")
    expect(by(w, 'project-task-start-go').exists()).toBe(false)
  })

  it('a slower preview for an agent the member switched away from never lands', async () => {
    let resolveOld
    projects.startPreview = vi.fn()
      .mockResolvedValueOnce(PREVIEW('scout'))                                  // first load
      .mockImplementationOnce(() => new Promise((r) => { resolveOld = r }))     // pick writer… slow
      .mockResolvedValueOnce(PREVIEW('scout', { message: 'FOR SCOUT' }))        // pick scout again, fast
    const w = mountIt()
    await flushPromises()
    const select = by(w, 'project-task-start-agent')
    await select.setValue('writer')
    await select.setValue('scout')
    await flushPromises()
    resolveOld(PREVIEW('writer', { message: 'FOR WRITER' }))                   // the older read resolves last
    await flushPromises()
    await by(w, 'project-task-start-preview').trigger('click')
    expect(by(w, 'project-task-start-message').text()).toContain('FOR SCOUT')
    expect(by(w, 'project-task-start-message').text()).not.toContain('FOR WRITER')
  })

  it("the server's refusal is said next to Start", async () => {
    projects.startPreview = vi.fn().mockResolvedValue(PREVIEW('scout'))
    projects.startTask = vi.fn().mockRejectedValue({
      response: { status: 403, data: { detail: { code: 'agent_not_on_your_list', message: "You can't use scout yet." } } },
    })
    const w = mountIt()
    await flushPromises()
    await by(w, 'project-task-start-go').trigger('click')
    await flushPromises()
    expect(w.find('[role="alert"]').text()).toBe("You can't use scout yet.")
  })

  it('an archived project offers no Start', async () => {
    projects.startPreview = vi.fn().mockResolvedValue(PREVIEW('scout'))
    const w = mountIt({ archived: true })
    await flushPromises()
    expect(by(w, 'project-task-start-go').exists()).toBe(false)
    expect(w.text()).toContain('archived')
  })
})

describe('ent#1195 — review fixes', () => {
  it('a failed reload keeps the agents and chats on screen and says why', async () => {
    projects.startPreview = vi.fn()
      .mockResolvedValueOnce(PREVIEW('scout', { chats: [{ id: 'th9', agent_name: 'scout' }] }))
      .mockRejectedValueOnce({ response: { status: 403, data: { detail: { code: 'agent_not_on_your_list', message: "You can't use writer yet." } } } })
    const w = mountIt()
    await flushPromises()
    await by(w, 'project-task-start-agent').setValue('writer')
    await flushPromises()
    expect(by(w, 'project-task-start-agent').exists()).toBe(true)
    expect(by(w, 'project-task-chat-th9').exists()).toBe(true)
    expect(w.find('[role="alert"]').text()).toBe("You can't use writer yet.")
  })

  it('a first message already on its way (409) still opens the chat', async () => {
    projects.startPreview = vi.fn().mockResolvedValue(PREVIEW('scout'))
    projects.startTask = vi.fn().mockResolvedValue({ thread_id: 'th1', needs_first_message: true, message: 'M', idempotency_key: 'k' })
    portal.startPortalChat = vi.fn().mockRejectedValue({ response: { status: 409, data: { detail: { error: 'request_in_progress' } } } })
    const w = mountIt()
    await flushPromises()
    await by(w, 'project-task-start-go').trigger('click')
    await flushPromises()
    expect(w.emitted('open-thread')[0][0]).toEqual({ id: 'th1', agent_name: 'scout' })
  })

  it('a stale preview reloads with the new text and says so', async () => {
    projects.startPreview = vi.fn()
      .mockResolvedValueOnce(PREVIEW('scout'))
      .mockResolvedValueOnce(PREVIEW('scout', { message: 'NEW TEXT', message_sha: 'sha-new' }))
    projects.startTask = vi.fn().mockRejectedValue({ response: { status: 409, data: { detail: { code: 'preview_changed', message: 'The task changed since you previewed it.' } } } })
    const w = mountIt()
    await flushPromises()
    await by(w, 'project-task-start-go').trigger('click')
    await flushPromises()
    await by(w, 'project-task-start-preview').trigger('click')
    expect(by(w, 'project-task-start-message').text()).toContain('NEW TEXT')
    expect(w.text()).toContain('The task changed since you previewed it.')
  })

  it('a done task offers no Start', async () => {
    projects.startPreview = vi.fn().mockResolvedValue(PREVIEW('scout', { done: true }))
    const w = mountIt()
    await flushPromises()
    expect(by(w, 'project-task-start-done').exists()).toBe(true)
    expect(by(w, 'project-task-start-go').exists()).toBe(false)
  })
})

describe('ent#1195 — the first message carries the Idempotency-Key', () => {
  it('startPortalChat sends the header only when given a key', async () => {
    const { portalHttp } = await import('@/stores/clientPortal')
    const post = vi.spyOn(portalHttp, 'post').mockResolvedValue({ data: { execution_id: 'e1' } })
    await useClientPortalStore().startPortalChat('scout', 'FIRST', 'th1', { idempotencyKey: 'project-task-start-abc' })
    expect(post.mock.calls[0][2].headers['Idempotency-Key']).toBe('project-task-start-abc')
    await useClientPortalStore().startPortalChat('scout', 'again', 'th1')
    expect(post.mock.calls[1][2].headers['Idempotency-Key']).toBeUndefined()
    post.mockRestore()
  })
})
