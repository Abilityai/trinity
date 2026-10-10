// @vitest-environment jsdom
/**
 * #3463: show the backend-normalized identifier before submitting, and refuse
 * a name that normalizes to empty. These vectors follow sanitize_agent_name
 * in src/backend/utils/helpers.py, including its ASCII alphabet and no cap.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import CreateAgentModal from '../../src/components/CreateAgentModal.vue'

const createAgent = vi.fn()
vi.mock('../../src/stores/agents', () => ({ useAgentsStore: () => ({ createAgent }) }))
vi.mock('../../src/stores/auth', () => ({ useAuthStore: () => ({ fetchGithubPatStatus: async () => false }) }))
vi.mock('../../src/api', () => ({ default: { get: vi.fn(async () => ({ data: [
  { id: 'github:acme/helper', source: 'github', display_name: 'Helper' },
  { id: 'local:scout', source: 'local', display_name: 'Scout' },
] })) } }))

const wrappers = []
async function mountModal(props = {}) {
  const wrapper = mount(CreateAgentModal, {
    props,
    global: { stubs: { ImportValidationStep: true } },
  })
  wrappers.push(wrapper)
  await flushPromises()
  return wrapper
}
const nameInput = (w) => w.get('input[placeholder="my-agent"]')
const preview = (w) => w.find('#agent-name-preview')
const submitButton = (w) => w.get('button[type="submit"]')
async function submit(w) {
  await w.get('form').trigger('submit')
  await flushPromises()
}
async function pickTemplate(w, text) {
  await w.findAll('div.cursor-pointer').find((card) => card.text().includes(text)).trigger('click')
}

beforeEach(() => {
  vi.clearAllMocks()
  createAgent.mockResolvedValue({ name: 'bad-name' })
})
afterEach(() => wrappers.splice(0).forEach((w) => w.unmount()))

describe('#3463 identifier preview', () => {
  it.each([
    ['bad@name!!!', 'bad-name'],
    ['UPPERCASE', 'uppercase'],
    ['._--Name', 'name'],
    ['  My   -- Agent-- ', 'my-agent'],
    ['Agent_Name.v2._', 'agent_name.v2._'],
    ['éAgent你好Name🙂', 'agent-name'],
    ['123', '123'],
    ['a'.repeat(300), 'a'.repeat(300)],
  ])('previews %j as %j before any request without rewriting the payload', async (raw, expected) => {
    const w = await mountModal()
    await nameInput(w).setValue(raw)
    expect(preview(w).exists()).toBe(true)
    expect(preview(w).get('code').text()).toBe(expected)
    expect(nameInput(w).element.value).toBe(raw)
    expect(nameInput(w).attributes('maxlength')).toBeUndefined()
    expect(nameInput(w).attributes('aria-describedby').split(' ')).toContain('agent-name-preview')
    expect(submitButton(w).element.disabled).toBe(false)
    expect(createAgent).not.toHaveBeenCalled()
    await submit(w)
    expect(createAgent).toHaveBeenCalledExactlyOnceWith({ name: raw })
  })

  it('updates the full preview while editing, including after an invalid value', async () => {
    const w = await mountModal()
    await nameInput(w).setValue('First Name')
    expect(preview(w).get('code').text()).toBe('first-name')
    await nameInput(w).setValue('!!!')
    expect(preview(w).exists()).toBe(false)
    await nameInput(w).setValue('Second.Name_')
    expect(preview(w).get('code').text()).toBe('second.name_')
    expect(nameInput(w).attributes('aria-invalid')).toBeUndefined()
    expect(w.find('[role="alert"]').exists()).toBe(false)
    expect(createAgent).not.toHaveBeenCalled()
  })

  it.each(['', '   ', '!!!', '._--', '你好🙂'])('blocks %j even when the form submit bypasses the disabled button', async (raw) => {
    const w = await mountModal()
    await nameInput(w).setValue(raw)
    expect(submitButton(w).element.disabled).toBe(true)
    await submit(w)
    expect(createAgent).not.toHaveBeenCalled()
    expect(w.emitted('created')).toBeUndefined()
    expect(w.emitted('close')).toBeUndefined()
    expect(preview(w).exists()).toBe(false)
    expect(nameInput(w).attributes('aria-invalid')).toBe('true')
    const error = w.get('[role="alert"]')
    expect(error.text()).toContain('Enter a name with at least one ASCII letter or number')
    expect(error.text()).toContain('my-agent')
    expect(nameInput(w).attributes('aria-describedby')).toBe(error.attributes('id'))
    // The same form remains usable after correcting the name.
    await nameInput(w).setValue('Fixed Name')
    expect(submitButton(w).element.disabled).toBe(false)
    expect(w.find('[role="alert"]').exists()).toBe(false)
    await submit(w)
    expect(createAgent).toHaveBeenCalledExactlyOnceWith({ name: 'Fixed Name' })
  })

  it.each([
    ['', undefined],
    ['Scout', 'local:scout'],
    ['Helper', 'github:acme/helper'],
    ['GitHub Repository', 'github:acme/custom'],
  ])('keeps the preview and raw payload on the %j selection path', async (selection, template) => {
    const w = await mountModal()
    if (selection) await pickTemplate(w, selection)
    if (selection === 'GitHub Repository') {
      await w.get('input[placeholder^="owner/repo"]').setValue('acme/custom')
    }
    await nameInput(w).setValue('bad@name!!!')
    expect(preview(w).get('code').text()).toBe('bad-name')
    expect(createAgent).not.toHaveBeenCalled()
    await submit(w)
    expect(createAgent).toHaveBeenCalledTimes(1)
    expect(createAgent.mock.calls[0][0]).toMatchObject({ name: 'bad@name!!!' })
    expect(createAgent.mock.calls[0][0].template).toBe(template)
    if (selection === 'GitHub Repository') {
      expect(createAgent.mock.calls[0][0]).toMatchObject({ import_intent: 'clone', kind: 'agent' })
    }
  })

  it.each(['local:scout', 'github:acme/helper', 'missing:template'])('handles the initialTemplate entry point %j', async (initialTemplate) => {
    const w = await mountModal({ initialTemplate })
    await nameInput(w).setValue('Preset Name')
    expect(preview(w).get('code').text()).toBe('preset-name')
    expect(createAgent).not.toHaveBeenCalled()
    await submit(w)
    expect(createAgent.mock.calls[0][0]).toEqual({
      name: 'Preset Name',
      ...(initialTemplate !== 'missing:template' ? { template: initialTemplate } : {}),
    })
  })
})
