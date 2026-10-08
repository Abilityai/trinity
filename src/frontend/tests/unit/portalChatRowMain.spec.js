// @vitest-environment jsdom
/**
 * The Main chat's sidebar row reads "Main", like its tab, the header, the Inbox
 * and the ask cards (ent#523: Main is named by its role and is not
 * renameable). The row used to print the chat's generated title, so Main had
 * two names and two chats with similar openers read as duplicates.
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { chatRowTitle, MAIN_TAB_LABEL } from '../../src/components/portal/portalUtils.js'
import PortalChatRow from '../../src/components/portal/PortalChatRow.vue'

const row = (thread, rename = async () => {}) =>
  mount(PortalChatRow, { props: { thread: { agent_name: 'cornelius', ...thread }, rename } })

describe('chatRowTitle', () => {
  it('names Main by its role, whatever title was generated for it', () => {
    expect(chatRowTitle({ is_main: true, title: 'Client requests number sequence' })).toBe(MAIN_TAB_LABEL)
  })
  it("keeps every other chat's title, and the empty fallback", () => {
    expect(chatRowTitle({ title: 'Client requests number conversion task' })).toBe('Client requests number conversion task')
    expect(chatRowTitle({ title: '' })).toBe('New chat')
  })
})

describe('PortalChatRow', () => {
  it('shows "Main" for the Main chat and offers no rename', () => {
    const w = row({ id: 'm', is_main: true, title: 'Client requests number sequence' })
    expect(w.text()).toContain(MAIN_TAB_LABEL)
    expect(w.text()).not.toContain('Client requests number sequence')
    expect(w.find('[aria-label="Rename this chat"]').exists()).toBe(false)
  })
  it('keeps the title and the rename control on any other chat', () => {
    const w = row({ id: 'c', title: 'Client requests number conversion task' })
    expect(w.html()).toContain('Client requests number conversion task')
  })
})
