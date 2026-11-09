// @vitest-environment jsdom
/**
 * ent#1175 — failed step with no note shows the platform default line.
 *
 * Product ruling: when a step's state is "failed" and no note was provided,
 * PortalProgressCard renders:
 *   "Something went wrong here. Your assistant is looking into it and will tell
 *    you in the chat."
 * When a note IS provided, only the note is shown (never both).
 * Other states with no note show no note line at all.
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import PortalProgressCard from '../../src/components/portal/PortalProgressCard.vue'

const DEFAULT_FAILED_TEXT =
  'Something went wrong here. Your assistant is looking into it and will tell you in the chat.'

function makeStep(overrides = {}) {
  return {
    key: 'assistant_ready',
    label: 'Your assistant is ready',
    state: 'pending',
    note: undefined,
    ...overrides,
  }
}

function mountCard(steps, extra = {}) {
  return mount(PortalProgressCard, {
    props: { steps, active: true, allDone: false, ...extra },
  })
}

describe('PortalProgressCard — failed step default note', () => {
  it('shows the default fallback when state is failed and no note given', () => {
    const wrapper = mountCard([makeStep({ state: 'failed' })])
    const noteEl = wrapper.find('[data-testid="progress-step-assistant_ready"] p:nth-child(2)')
    expect(noteEl.exists()).toBe(true)
    expect(noteEl.text()).toBe(DEFAULT_FAILED_TEXT)
  })

  it('shows the custom note instead of the default when note is provided', () => {
    const wrapper = mountCard([makeStep({ state: 'failed', note: 'Custom message here.' })])
    const noteEl = wrapper.find('[data-testid="progress-step-assistant_ready"] p:nth-child(2)')
    expect(noteEl.exists()).toBe(true)
    expect(noteEl.text()).toBe('Custom message here.')
    expect(noteEl.text()).not.toContain('Something went wrong')
  })

  it('does not show any note for a done step with no note', () => {
    const wrapper = mountCard([makeStep({ state: 'done' })])
    const li = wrapper.find('[data-testid="progress-step-assistant_ready"]')
    // Only one <p> (the label), no second paragraph
    const paragraphs = li.findAll('p')
    expect(paragraphs).toHaveLength(1)
  })

  it('does not show any note for a pending step with no note', () => {
    const wrapper = mountCard([makeStep({ state: 'pending' })])
    const li = wrapper.find('[data-testid="progress-step-assistant_ready"]')
    const paragraphs = li.findAll('p')
    expect(paragraphs).toHaveLength(1)
  })

  it('does not show any note for an in_progress step with no note', () => {
    const wrapper = mountCard([makeStep({ state: 'in_progress' })])
    const li = wrapper.find('[data-testid="progress-step-assistant_ready"]')
    const paragraphs = li.findAll('p')
    expect(paragraphs).toHaveLength(1)
  })
})
