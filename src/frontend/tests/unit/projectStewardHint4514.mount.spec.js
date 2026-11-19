// @vitest-environment jsdom
/**
 * #4514 — switching the steward must not resize the new-project dialog.
 *
 * jsdom lays nothing out (the same limit agentHeader3872.mount.spec.js records),
 * so the reserved height is pinned on the class contract. `text-xs` is a 1rem
 * line. The longest hint — the server's `set.message` — measures three lines
 * at the dialog's desktop width (48px) and four at 390px (64px). `min-h-[2.5rem]`
 * is 40px, which is why the dialog grew by 8px at 1440 and 16px at 390. The
 * panel is already at `max-w-lg` once the viewport reaches `sm`, so 4rem below
 * `sm` and 3rem from `sm` up covers both widths, including while the options
 * read has not arrived yet.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() }))
vi.mock('@/api', () => ({ default: api }))

import ProjectCreateModal from '@/components/portal/projects/ProjectCreateModal.vue'
import { useProjectsStore } from '@/stores/projects'

const ME = 'alice@example.com'
const AGENTS = [
  { agent_name: 'pm-steward', holds_set: false, can_assign: true },
  { agent_name: 'bobs-agent', holds_set: false, can_assign: false },
]
const AVAILABLE = {
  set: { name: 'project-management', available: true, message: null },
  agents: AGENTS,
}
// The release-test string: three lines at desktop, four at 390px (#4514).
const SET_MESSAGE = "The project-management skills aren't available on this Trinity yet, so a steward agent can be named but won't get them. An admin can add them once the skills library includes them."
const UNAVAILABLE = {
  set: { name: 'project-management', available: false, message: SET_MESSAGE },
  agents: AGENTS.map((a) => ({ ...a, holds_set: false, can_assign: false })),
}

// text-xs line is 1rem. Four of those below sm, three once the dialog is at max-w-lg.
const FLOOR = ['min-h-[4rem]', 'sm:min-h-[3rem]']

let projects
const hint = () => document.querySelector('[data-testid="project-steward-skills-hint"]')

beforeEach(() => {
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  projects = useProjectsStore()
})

function expectFloor(el) {
  expect(el).not.toBe(null)
  for (const name of FLOOR) expect(el.classList.contains(name), name).toBe(true)
  expect(el.classList.contains('min-h-[2.5rem]')).toBe(false)
}

async function openDialog(options, agents = ['pm-steward', 'bobs-agent']) {
  projects.fetchStewardOptions = typeof options === 'function' ? options : vi.fn().mockResolvedValue(options)
  mount(ProjectCreateModal, { props: { modelValue: true, myEmail: ME, agents }, attachTo: document.body })
  await flushPromises()
}

async function pickSteward(value) {
  const el = document.getElementById('project-steward')
  el.value = value
  el.dispatchEvent(new Event('change'))
  await flushPromises()
}

describe('#4514 — the steward skills hint keeps its reserved height', () => {
  it('reserves four lines below sm and three from sm up, before the read arrives', async () => {
    await openDialog(() => new Promise(() => {}))
    expectFloor(hint())
    expect(hint().textContent).toContain('A person steward')
    await pickSteward('agent:pm-steward')
    expectFloor(hint())
    expect(hint().textContent).toBe('Checking the project-management skills…')
  })

  it('keeps that floor while switching You, someone else and an agent, skills unavailable', async () => {
    await openDialog(UNAVAILABLE)
    const el = hint()
    expectFloor(el)
    expect(el.textContent).toContain('A person steward')

    await pickSteward('agent:pm-steward')
    expect(hint()).toBe(el)
    expectFloor(el)
    expect(el.textContent).toBe(SET_MESSAGE)

    await pickSteward('person')
    expect(hint()).toBe(el)
    expectFloor(el)
    expect(el.textContent).toContain('A person steward')

    await pickSteward('me')
    expect(hint()).toBe(el)
    expectFloor(el)
    expect(el.textContent).toContain('A person steward')
  })

  it('keeps that floor when the skills set is available', async () => {
    await openDialog(AVAILABLE)
    const el = hint()
    expectFloor(el)

    await pickSteward('agent:pm-steward')
    expect(hint()).toBe(el)
    expectFloor(el)
    expect(el.textContent).toContain('pm-steward gets the project-management skills')

    await pickSteward('agent:bobs-agent')
    expectFloor(el)
    expect(el.textContent).toContain("Only bobs-agent's owner or an admin")

    await pickSteward('me')
    expectFloor(el)
    expect(el.textContent).toContain('A person steward')
  })
})
