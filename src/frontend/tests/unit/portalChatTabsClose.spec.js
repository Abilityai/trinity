/**
 * ent#841 + the unread tab count — the chat strip's model (`agentChatTabs`).
 *
 * Every chat but Main is closable; an archived chat leaves the strip; a chat
 * with unread replies that is NOT on screen carries the sidebar row's count, in
 * the same solid blue (`primary`), so the strip says where the message is.
 */
import { describe, it, expect } from 'vitest'
import { agentChatTabs, MAIN_TAB_LABEL } from '@/components/portal/portalUtils'

const main = (over = {}) => ({ id: 'main', agent_name: 'a', is_main: true, ...over })
const chat = (id, over = {}) => ({ id, agent_name: 'a', title: id, ...over })

describe('ent#841 closable', () => {
  it('Main is never closable; every other chat is', () => {
    const tabs = agentChatTabs([main(), chat('c1'), chat('c2')], 'a')
    expect(tabs.map((t) => [t.id, t.closable])).toEqual([['main', false], ['c1', true], ['c2', true]])
  })

  it('the provisional New chat tab has no ×', () => {
    const tabs = agentChatTabs([main()], 'a', { activeId: null, draft: true })
    expect(tabs.find((t) => t.provisional).closable).toBeFalsy()
  })
})

describe('unread count on the tab', () => {
  it('a background chat with unread carries the count in primary blue', () => {
    const tabs = agentChatTabs([main(), chat('c1', { unread: 3 })], 'a', { activeId: 'main' })
    const t = tabs.find((x) => x.id === 'c1')
    expect(t).toMatchObject({ badge: '3', badgeVariant: 'primary' })
    expect(t.badgeLabel).toBe('c1, 3 unread')
  })

  it('the chat on screen never carries one — it is read by definition', () => {
    const tabs = agentChatTabs([main({ unread: 2 }), chat('c1')], 'a', { activeId: 'main' })
    expect(tabs[0].badge).toBeNull()
  })

  it('Main is named by its role in the label, and big counts are capped', () => {
    const tabs = agentChatTabs([main({ unread: 250 }), chat('c1')], 'a', { activeId: 'c1' })
    expect(tabs[0].badge).toBe('99+')
    expect(tabs[0].badgeLabel).toBe(`${MAIN_TAB_LABEL}, 250 unread`)
  })

  it('no unread → no badge, no label override', () => {
    const tabs = agentChatTabs([main(), chat('c1')], 'a', { activeId: 'main' })
    expect(tabs[1]).toMatchObject({ badge: null, badgeLabel: '' })
  })
})
