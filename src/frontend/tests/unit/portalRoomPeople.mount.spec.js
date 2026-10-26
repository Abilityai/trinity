// @vitest-environment jsdom
/**
 * trinity-enterprise#701 — the people in a room, MOUNTED (design contract
 * #2918: a predicate that gates a store write is proven by a mount).
 *
 *   roster      present people first, the reader marked "(you)", who left kept
 *               with when; roles in the user's words
 *   offer       seating appears only where the room says `can_seat_people`
 *               (the server's verdict); elsewhere a platform person is shown
 *               the next step (@mention), never a control that would refuse
 *   seating     the search asks the store; an unseatable colleague is listed
 *               disabled with the reason; Add seats with the history choice
 *   managing    Remove goes through a confirmation that restates the
 *               consequence (cancel writes nothing); Show earlier messages is
 *               offered only for a collaborator who cannot see them yet
 *   leaving     Leave room calls the store and tells the shell; a refusal
 *               (the last moderator) is shown in place, never swallowed
 *   picker      New room is a room even with one agent, in room words
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn() }),
}))
vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return {
    default: Object.assign(
      { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import { useClientPortalStore } from '@/stores/clientPortal'
import PortalRoomPeople from '@/components/portal/PortalRoomPeople.vue'
import PortalAgentPicker from '@/components/portal/PortalAgentPicker.vue'

const PEOPLE = [
  { kind: 'user', identity: 'alice', role: 'moderator', joined_at: '2026-10-26T10:00:00Z', left_at: null, sees_history: true },
  { kind: 'user', identity: 'bob', role: 'collaborator', joined_at: '2026-10-26T10:05:00Z', left_at: null, sees_history: false },
  { kind: 'user', identity: 'dave', role: 'collaborator', joined_at: '2026-10-26T10:01:00Z', left_at: '2026-10-26T10:30:00Z', sees_history: false },
]
const room = (over = {}) => ({ id: 'room_1', status: 'open', people: PEOPLE,
  you: { kind: 'user', identity: 'alice' }, can_seat_people: true, ...over })

let store, wrapper
beforeEach(() => {
  setActivePinia(createPinia())
  store = useClientPortalStore()
})
afterEach(() => {
  wrapper?.unmount()
  vi.useRealTimers()
  document.body.innerHTML = ''
})

function open(r = room()) {
  wrapper = mount(PortalRoomPeople, { props: { roomId: 'room_1', room: r }, attachTo: document.body })
  return wrapper
}
const person = (name) => wrapper.find(`[data-testid="room-person-${name}"]`)
const body = () => document.body

describe('roster', () => {
  it('lists people with the reader marked, roles in words, and who left', () => {
    open()
    expect(person('alice').text()).toContain('alice (you)')
    expect(person('alice').text()).toContain('Started this room')
    expect(person('bob').text()).toContain('Collaborator')
    expect(person('dave').text()).toContain('Left')
  })
})

describe('offer', () => {
  it('offers seating only where the room says so, and names the next step elsewhere', () => {
    open(room({ can_seat_people: false }))
    expect(wrapper.find('[data-testid="room-seat-search"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="room-people-next-step"]').text())
      .toBe('To let a colleague read along, @mention them in a message.')
    expect(wrapper.find('[data-testid="room-person-remove-bob"]').exists()).toBe(false)
  })

  it('shows an outside client neither the seat control nor the platform next step', () => {
    open(room({ can_seat_people: false, you: { kind: 'workspace_user', identity: 'c@example.com' } }))
    expect(wrapper.find('[data-testid="room-people-next-step"]').exists()).toBe(false)
  })
})

describe('seating', () => {
  it('searches, marks the unseatable with the reason, and seats with the history choice', async () => {
    vi.useFakeTimers()
    const fetch = vi.spyOn(store, 'fetchSeatablePeople').mockResolvedValue([
      { username: 'erin', label: 'Erin', email: 'erin@example.com', seatable: true, reason: null },
      { username: 'carol', label: 'Carol', email: 'carol@example.com', seatable: false, reason: "Can't reach agent-b" },
    ])
    const seat = vi.spyOn(store, 'seatPerson').mockResolvedValue({ username: 'erin' })
    open()
    await wrapper.find('#room-seat-search').setValue('er')
    await vi.advanceTimersByTimeAsync(300)
    await flushPromises()
    expect(fetch).toHaveBeenCalledWith('room_1', 'er')
    expect(wrapper.find('[data-testid="room-seat-carol"]').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain("Can't reach agent-b")

    await wrapper.find('[role="switch"]').trigger('click')
    await wrapper.find('[data-testid="room-seat-erin"]').trigger('click')
    await flushPromises()
    expect(seat).toHaveBeenCalledWith('room_1', 'erin', { showHistory: true })
    expect(wrapper.emitted('changed')).toHaveLength(1)
  })

  it('shows a refused seat in place', async () => {
    vi.useFakeTimers()
    vi.spyOn(store, 'fetchSeatablePeople').mockResolvedValue([
      { username: 'erin', label: 'Erin', email: 'erin@example.com', seatable: true, reason: null },
    ])
    vi.spyOn(store, 'seatPerson').mockRejectedValue({ response: { data: { detail: {
      code: 'cannot_reach_agents', message: "erin can't reach agent-b. Share that agent with them first, then seat them." } } } })
    open()
    await wrapper.find('#room-seat-search').setValue('er')
    await vi.advanceTimersByTimeAsync(300)
    await flushPromises()
    await wrapper.find('[data-testid="room-seat-erin"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Share that agent with them first')
    expect(wrapper.emitted('changed')).toBeUndefined()
  })
})

describe('managing', () => {
  it('removes only after a confirmation that restates the consequence', async () => {
    const remove = vi.spyOn(store, 'removePerson').mockResolvedValue({ removed: true })
    open()
    await wrapper.find('[data-testid="room-person-remove-bob"]').trigger('click')
    await flushPromises()
    expect(body().textContent).toContain('bob will no longer see this room or anything written in it')
    body().querySelector('[data-testid="confirm-dialog-cancel"]').click()
    await flushPromises()
    expect(remove).not.toHaveBeenCalled()

    await wrapper.find('[data-testid="room-person-remove-bob"]').trigger('click')
    await flushPromises()
    expect(body().querySelector('[data-testid="confirm-dialog-confirm"]').textContent.trim()).toBe('Remove')
    body().querySelector('[data-testid="confirm-dialog-confirm"]').click()
    await flushPromises()
    expect(remove).toHaveBeenCalledWith('room_1', 'bob')
    expect(wrapper.emitted('changed')).toHaveLength(1)
  })

  it('offers earlier messages only to a collaborator who cannot see them, and never manages oneself or who left', async () => {
    const grant = vi.spyOn(store, 'showPersonHistory').mockResolvedValue({ sees_history: true })
    open()
    expect(wrapper.find('[data-testid="room-person-history-alice"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="room-person-remove-alice"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="room-person-remove-dave"]').exists()).toBe(false)
    await wrapper.find('[data-testid="room-person-history-bob"]').trigger('click')
    await flushPromises()
    expect(grant).toHaveBeenCalledWith('room_1', 'bob')
  })
})

describe('leaving', () => {
  it('leaves and tells the shell', async () => {
    const leave = vi.spyOn(store, 'leaveRoom').mockResolvedValue({ left: true })
    open(room({ you: { kind: 'user', identity: 'bob' }, can_seat_people: false }))
    await wrapper.find('[data-testid="room-leave"]').trigger('click')
    await flushPromises()
    expect(leave).toHaveBeenCalledWith('room_1')
    expect(wrapper.emitted('left')).toEqual([['room_1']])
  })

  it('shows the last-moderator refusal in place', async () => {
    vi.spyOn(store, 'leaveRoom').mockRejectedValue({ response: { data: { detail: {
      code: 'last_moderator', message: 'You manage this room and others are still in it. Remove them or close the room before you leave.' } } } })
    open()
    await wrapper.find('[data-testid="room-leave"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Remove them or close the room before you leave.')
    expect(wrapper.emitted('left')).toBeUndefined()
  })

  it('offers no Leave to someone no longer in the room', () => {
    open(room({ you: { kind: 'user', identity: 'dave' } }))
    expect(wrapper.find('[data-testid="room-leave"]').exists()).toBe(false)
  })
})

describe('picker', () => {
  const AGENTS = [{ name: 'atlas' }, { name: 'scout' }]
  const mountPicker = (props) => mount(PortalAgentPicker, { props: { agents: AGENTS, multi: true, ...props } })

  it('New room is a room even with one agent, in room words', async () => {
    const w = mountPicker({ mode: 'room' })
    expect(w.text()).toContain('Start a room')
    await w.findAll('[role="checkbox"]')[0].trigger('click')
    expect(w.text()).toContain('1 agent in this room')
    const start = w.findAll('button').find((b) => b.text() === 'Start room')
    await start.trigger('click')
    expect(w.emitted('confirm')).toEqual([[['atlas']]])
    w.unmount()
  })

  it('New chat says chat for one agent and room once several are picked', async () => {
    const w = mountPicker({ mode: 'chat' })
    expect(w.text()).toContain('Start a chat')
    await w.findAll('[role="checkbox"]')[0].trigger('click')
    expect(w.findAll('button').some((b) => b.text() === 'Start chat')).toBe(true)
    await w.findAll('[role="checkbox"]')[1].trigger('click')
    expect(w.text()).toContain('2 agents — this starts a room')
    expect(w.findAll('button').some((b) => b.text() === 'Start room')).toBe(true)
    w.unmount()
  })
})
