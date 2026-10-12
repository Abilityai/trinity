// @vitest-environment jsdom
/**
 * #3459 — Esc while renaming a Workspace chat abandons the edit.
 *
 * `PortalEditableTitle` is the one editor behind the sidebar row, the 1:1
 * header and the room header, so its Enter / Esc / blur contract is pinned
 * here once, on the mounted component. The defect was an ordering one: Esc
 * left edit mode, the field's removal blurred it, and the blur handler saved
 * any draft that differed from the stored title. Only a mount attached to the
 * document, with real focus, can see that.
 *
 * A browser fires `blur` on a focused field as it is removed; jsdom does not.
 * `teardownBlur` delivers that event to the field the way the browser would
 * (Vue leaves the listener on the detached node), so the Esc tests fail on the
 * save rather than pass on jsdom's silence.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import PortalEditableTitle from '../../src/components/portal/PortalEditableTitle.vue'

let wrapper

function mountTitle(props = {}) {
  const rename = vi.fn().mockResolvedValue(undefined)
  wrapper = mount(PortalEditableTitle, {
    attachTo: document.body,
    props: { value: 'Quarterly plan', rename, ...props },
  })
  return { wrapper, rename }
}

// Open the editor through the pencil and type a different title.
async function editTo(w, text) {
  await w.get('[data-testid="rename-chat"]').trigger('click')
  await nextTick()
  const field = w.get('[data-testid="rename-chat-field"]')
  await field.setValue(text)
  return field
}

// The blur a browser delivers to a focused field when it leaves the DOM.
function teardownBlur(el) {
  el.dispatchEvent(new FocusEvent('blur'))
}

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
})

describe('PortalEditableTitle — Enter / Esc / blur (#3459)', () => {
  it('Esc discards the draft: no save, and the stored title is shown again', async () => {
    const { wrapper: w, rename } = mountTitle()
    const field = await editTo(w, 'Something else entirely')
    expect(document.activeElement).toBe(field.element)

    await field.trigger('keydown', { key: 'Escape' })
    await nextTick()
    teardownBlur(field.element)
    await flushPromises()

    expect(rename).not.toHaveBeenCalled()
    expect(w.find('[data-testid="rename-chat-field"]').exists()).toBe(false)
    expect(w.text()).toContain('Quarterly plan')
    expect(w.text()).not.toContain('Something else entirely')
  })

  it('Esc hands focus back to the pencil, and reopening starts from the stored title', async () => {
    const { wrapper: w, rename } = mountTitle()
    const field = await editTo(w, 'Abandoned draft')

    await field.trigger('keydown', { key: 'Escape' })
    await nextTick()
    teardownBlur(field.element)
    await flushPromises()

    const pencil = w.get('[data-testid="rename-chat"]')
    expect(document.activeElement).toBe(pencil.element)

    await pencil.trigger('click')
    await nextTick()
    expect(w.get('[data-testid="rename-chat-field"]').element.value).toBe('Quarterly plan')
    expect(rename).not.toHaveBeenCalled()
  })

  it('Enter saves the trimmed draft exactly once, even through the teardown blur', async () => {
    const { wrapper: w, rename } = mountTitle()
    const field = await editTo(w, '  Renamed chat  ')

    await field.trigger('keydown', { key: 'Enter' })
    await flushPromises()
    teardownBlur(field.element)
    await flushPromises()

    expect(rename).toHaveBeenCalledTimes(1)
    expect(rename).toHaveBeenCalledWith('Renamed chat')
    expect(w.find('[data-testid="rename-chat-field"]').exists()).toBe(false)
  })

  it('blur with a changed draft saves it', async () => {
    const { wrapper: w, rename } = mountTitle()
    const field = await editTo(w, 'Saved by leaving')

    await field.trigger('blur')
    await flushPromises()

    expect(rename).toHaveBeenCalledTimes(1)
    expect(rename).toHaveBeenCalledWith('Saved by leaving')
  })

  it('blur with an unchanged draft is an abandon, not a save', async () => {
    const { wrapper: w, rename } = mountTitle()
    const field = await editTo(w, 'Quarterly plan')

    await field.trigger('blur')
    await flushPromises()

    expect(rename).not.toHaveBeenCalled()
    expect(w.find('[data-testid="rename-chat-field"]').exists()).toBe(false)
  })
})
