// @vitest-environment jsdom
/**
 * trinity-enterprise#1153 — BasePeopleInput, the people-picker form field.
 *
 * Mounted inside a real v-model host, because the keyboard contract, the two
 * modes and what reaches the parent are the feature. Pinned:
 *   - asks only from two characters, after a pause, newest answer wins;
 *   - rows read name first, email second, role only when the row carries it;
 *   - ↑↓ move, Enter accepts ONLY a highlighted row, Esc and blur dismiss;
 *   - suggest-and-allow: plain Enter is left to the form (any address submits);
 *   - must-pick: typed text that is nobody is a named, native-invalid field;
 *     a pick clears it, editing after a pick drops the pick;
 *   - a 403 or a network failure leaves a plain input and stops asking for good;
 *     a 429 is transient;
 *   - a pre-filled pick is shown and asks nothing.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { defineComponent, h, ref } from 'vue'

import BasePeopleInput from '@/components/base/BasePeopleInput.vue'
import { keyAction, mustPickError, personRows } from '@/components/base/peopleInput'

const PAT = { email: 'pat@example.com', label: 'Pat Doe' }
const PAM = { email: 'pam@example.com', label: 'Pam Roe', role: 'admin' }

let fetchPeople

function render(props = {}, { text = '', picked = null } = {}) {
  const state = { text: ref(text), picked: ref(picked), unavailable: ref(0), submits: ref(0) }
  const Host = defineComponent({
    setup() {
      return () => h('form', { onSubmit: (e) => { e.preventDefault(); state.submits.value += 1 } }, [
        h(BasePeopleInput, {
          fetch: fetchPeople,
          'data-testid': 'people',
          ...props,
          modelValue: state.text.value,
          'onUpdate:modelValue': (v) => { state.text.value = v },
          picked: state.picked.value,
          'onUpdate:picked': (p) => { state.picked.value = p },
          onUnavailable: () => { state.unavailable.value += 1 },
        }),
      ])
    },
  })
  const w = mount(Host, { attachTo: document.body })
  return { w, state }
}

const input = (w) => w.find('[data-testid="people"]')
const options = (w) => w.findAll('[role="option"]')

async function type(w, text) {
  await input(w).setValue(text)
  vi.advanceTimersByTime(200)
  await flushPromises()
}

function keydown(w, key) {
  const event = new KeyboardEvent('keydown', { key, cancelable: true, bubbles: true })
  input(w).element.dispatchEvent(event)
  return event
}

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn()
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
  document.body.innerHTML = ''
  fetchPeople = vi.fn(async () => [PAT, PAM])
})

afterEach(() => vi.useRealTimers())

describe('asking', () => {
  it('never on one character, once after a pause, with what was typed', async () => {
    const { w } = render()
    await type(w, 'p')
    expect(fetchPeople).not.toHaveBeenCalled()
    await input(w).setValue('pa')
    vi.advanceTimersByTime(100)
    await input(w).setValue('pat')
    expect(fetchPeople).not.toHaveBeenCalled()
    vi.advanceTimersByTime(200)
    await flushPromises()
    expect(fetchPeople).toHaveBeenCalledTimes(1)
    expect(fetchPeople).toHaveBeenCalledWith('pat')
  })

  it('a row is the name first, then the email, and the role only when sent', async () => {
    const { w } = render()
    await type(w, 'pa')
    const [pat, pam] = options(w)
    expect(pat.text().indexOf('Pat Doe')).toBeLessThan(pat.text().indexOf('pat@example.com'))
    expect(pat.text()).not.toContain('·')
    expect(pam.text()).toMatch(/pam@example\.com · ./)
  })

  it('needs no composer kind: it is a plain form field', async () => {
    const { w } = render()
    await type(w, 'pa')
    expect(w.text()).not.toContain('@Pat')
    expect(w.find('kbd').exists()).toBe(false)
  })
})

describe('the keyboard', () => {
  it('arrows move, wrapping, and the input points at the row', async () => {
    const { w } = render()
    await type(w, 'pa')
    keydown(w, 'ArrowDown')
    await flushPromises()
    expect(options(w)[0].attributes('aria-selected')).toBe('true')
    expect(input(w).attributes('aria-activedescendant')).toBe(options(w)[0].attributes('id'))
    keydown(w, 'ArrowUp')          // from the first row, up wraps to the last
    await flushPromises()
    expect(options(w)[1].attributes('aria-selected')).toBe('true')
  })

  it('Enter on a highlighted row picks it and submits nothing', async () => {
    const { w, state } = render()
    await type(w, 'pa')
    keydown(w, 'ArrowDown')
    const enter = keydown(w, 'Enter')
    await flushPromises()
    vi.advanceTimersByTime(400)
    await flushPromises()
    expect(enter.defaultPrevented).toBe(true)
    expect(state.text.value).toBe('pat@example.com')
    expect(state.picked.value).toEqual(PAT)
    expect(fetchPeople).toHaveBeenCalledTimes(1)
    expect(options(w)).toHaveLength(0)
  })

  it('plain Enter is left to the form in suggest-and-allow mode', async () => {
    const { w, state } = render()
    await type(w, 'new@outside.example')
    expect(keydown(w, 'Enter').defaultPrevented).toBe(false)
    expect(state.picked.value).toBe(null)
  })

  it('Esc and blur dismiss and keep the text', async () => {
    const { w, state } = render()
    await type(w, 'pa')
    keydown(w, 'Escape')
    await flushPromises()
    expect(options(w)).toHaveLength(0)
    expect(state.text.value).toBe('pa')
    await type(w, 'pam')
    expect(options(w)).toHaveLength(2)
    await input(w).trigger('blur')
    expect(options(w)).toHaveLength(0)
  })

  it('a mouse pick fills the field', async () => {
    const { w, state } = render()
    await type(w, 'pa')
    await options(w)[1].trigger('mousedown')
    expect(state.picked.value).toEqual(PAM)
    expect(state.text.value).toBe('pam@example.com')
  })
})

describe('must-pick', () => {
  it('typed text that is nobody yet is a named, invalid field once left', async () => {
    const { w } = render({ mode: 'pick' })
    await type(w, 'pa')
    expect(input(w).element.validity.valid).toBe(false)
    expect(w.find('[role="alert"]').exists()).toBe(false)     // not while still typing
    await input(w).trigger('blur')
    expect(w.find('[role="alert"]').text()).toContain('Choose a person from the list')
    expect(input(w).attributes('aria-invalid')).toBe('true')
  })

  it('a query that matches no one says so, by name', async () => {
    fetchPeople = vi.fn(async () => [])
    const { w } = render({ mode: 'pick', noMatch: (t) => `No one matches “${t}”. Invite them first.` })
    await type(w, 'zed')
    expect(w.text()).toContain('No one matches “zed”. Invite them first.')
    await input(w).trigger('blur')
    expect(w.find('[role="alert"]').text()).toContain('No one matches “zed”')
  })

  it('a pick makes the field valid and says who was chosen', async () => {
    const { w } = render({ mode: 'pick' })
    await type(w, 'pa')
    await input(w).trigger('blur')
    await type(w, 'pat')
    await options(w)[0].trigger('mousedown')
    await flushPromises()
    expect(input(w).element.validity.valid).toBe(true)
    expect(w.find('[role="alert"]').exists()).toBe(false)
    expect(w.text()).toContain('Pat Doe')
  })

  it('editing the text after a pick drops the pick', async () => {
    const { w, state } = render({ mode: 'pick' })
    await type(w, 'pa')
    await options(w)[0].trigger('mousedown')
    await flushPromises()
    await type(w, 'pat@example.co')
    expect(state.picked.value).toBe(null)
    expect(input(w).element.validity.valid).toBe(false)
  })

  it('an empty must-pick field is left to `required`', async () => {
    const { w } = render({ mode: 'pick' })
    await input(w).trigger('blur')
    expect(input(w).element.validity.valid).toBe(true)
  })

  it('a pre-filled pick shows the person and asks nothing', async () => {
    const { w } = render({ mode: 'pick' }, { text: PAT.email, picked: PAT })
    await flushPromises()
    vi.advanceTimersByTime(400)
    await flushPromises()
    expect(fetchPeople).not.toHaveBeenCalled()
    expect(input(w).element.value).toBe(PAT.email)
    expect(input(w).element.validity.valid).toBe(true)
    expect(w.text()).toContain('Pat Doe')
  })

  it('clearing a pre-filled pick clears the person', async () => {
    const { w, state } = render({ mode: 'pick' }, { text: PAT.email, picked: PAT })
    await input(w).setValue('')
    await flushPromises()
    expect(state.picked.value).toBe(null)
  })
})

describe('must-pick with accepted text (trinity-enterprise#1155)', () => {
  it('text the caller accepts is valid without a pick, and nothing else is', async () => {
    const { w, state } = render({ mode: 'pick', acceptText: (t) => t.startsWith('role:') })
    await type(w, 'role:ops')
    await input(w).trigger('blur')
    expect(input(w).element.validity.valid).toBe(true)
    expect(w.find('[role="alert"]').exists()).toBe(false)
    expect(state.picked.value).toBe(null)
    await type(w, 'pat')
    await input(w).trigger('blur')
    expect(input(w).element.validity.valid).toBe(false)
  })
})

describe('when the search is unavailable', () => {
  it.each([
    ['a 403', Object.assign(new Error('403'), { response: { status: 403 } })],
    ['a network failure', new Error('Network Error')],
  ])('%s leaves a plain input and stops asking for the session', async (_name, err) => {
    fetchPeople = vi.fn(async () => { throw err })
    const { w, state } = render()
    await type(w, 'pat')
    expect(w.find('[role="listbox"]').exists()).toBe(false)
    expect(input(w).attributes('aria-expanded')).toBe('false')
    expect(input(w).element.disabled).toBe(false)
    expect(state.unavailable.value).toBe(1)
    await type(w, 'pam')
    expect(fetchPeople).toHaveBeenCalledTimes(1)
  })

  it('a 429 is a pause, not a verdict', async () => {
    let calls = 0
    fetchPeople = vi.fn(async () => {
      calls += 1
      if (calls === 1) throw Object.assign(new Error('429'), { response: { status: 429 } })
      return [PAT]
    })
    const { w, state } = render()
    await type(w, 'pat')
    expect(options(w)).toHaveLength(0)
    await type(w, 'pat ')
    await type(w, 'pa')
    expect(options(w)).toHaveLength(1)
    expect(state.unavailable.value).toBe(0)
  })
})

describe('the pure rules', () => {
  it('keys: Enter picks only a highlighted row; arrows and Esc only while open', () => {
    expect(keyAction('Enter', { open: true, active: -1, count: 2 })).toEqual({ action: 'none' })
    expect(keyAction('Enter', { open: true, active: 1, count: 2 })).toEqual({ action: 'pick', active: 1 })
    expect(keyAction('ArrowDown', { open: true, active: 1, count: 2 })).toEqual({ action: 'move', active: 0 })
    expect(keyAction('ArrowDown', { open: false, active: -1, count: 2 })).toEqual({ action: 'none' })
    expect(keyAction('Escape', { open: true, active: 0, count: 2 })).toEqual({ action: 'close' })
  })

  it('must-pick errors only in pick mode, only for unchosen text', () => {
    const noMatch = () => 'nobody'
    expect(mustPickError({ mode: 'suggest', text: 'x', picked: null, matchedNone: true, noMatch })).toBe('')
    expect(mustPickError({ mode: 'pick', text: '', picked: null, matchedNone: false, noMatch })).toBe('')
    expect(mustPickError({ mode: 'pick', text: 'pat@example.com', picked: PAT, matchedNone: false, noMatch })).toBe('')
    expect(mustPickError({ mode: 'pick', text: 'pa', picked: null, matchedNone: true, noMatch })).toBe('nobody')
    expect(mustPickError({ mode: 'pick', text: 'pa', picked: null, matchedNone: false, noMatch }))
      .toContain('Choose a person from the list')
  })

  it('accepted text needs no pick', () => {
    expect(mustPickError({ mode: 'pick', text: 'role:ops', picked: null, matchedNone: true, accepted: true })).toBe('')
  })

  it('rows key by email', () => {
    expect(personRows([PAT])).toEqual([{ key: PAT.email, primary: 'Pat Doe', secondary: PAT.email }])
  })
})
