/**
 * #4346 — the agent's pinned primary chat is named for a person, not "Main",
 * and every place that prints the name reads the ONE constant. The tab, the
 * sidebar row, the Inbox pane heading and the project refusal agree, so a
 * future rename is a one-line change.
 */
import { describe, it, expect } from 'vitest'
import { MAIN_TAB_LABEL, chatRowTitle } from '../../src/components/portal/portalUtils'
import { paneHeading } from '../../src/components/portal/portalInbox'
import { projectErrorMessage } from '../../src/components/portal/projects/projectsUtils'

describe('the primary chat name', () => {
  it('is "Ongoing", not "Main"', () => {
    expect(MAIN_TAB_LABEL).toBe('Ongoing')
  })

  it('the sidebar row and the Inbox heading print the constant', () => {
    expect(chatRowTitle({ is_main: true, title: 'Generated opener' })).toBe(MAIN_TAB_LABEL)
    expect(paneHeading({ type: 'thread', is_main: true, title: 'Generated opener' }, 'scout')).toBe(`scout · ${MAIN_TAB_LABEL}`)
  })

  it('the project refusal names it the same way', () => {
    const msg = projectErrorMessage({ response: { status: 409, data: { detail: { code: 'main_chat' } } } })
    expect(msg).toContain(MAIN_TAB_LABEL.toLowerCase())
    expect(msg).not.toMatch(/\bMain\b/)
  })
})
