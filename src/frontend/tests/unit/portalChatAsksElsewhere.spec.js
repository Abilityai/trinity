/**
 * trinity-enterprise#610 PR A2, §3g L6 B1 residual — the chat's "N more asks"
 * line must lead somewhere EVERY reader can go.
 *
 * Sign-off round 4 pointed it at the rail's Work tab ("Open in Work"). Work is a
 * platform-door tab (`RAIL_DOORS.PLATFORM`): for an external client the rail
 * never renders it, so the link opened nothing. A client now goes to the Inbox's
 * Action tab narrowed to this agent (§3g C2 `?from=`), which lists exactly those
 * asks; a platform reader keeps Work (Andrii's round-5 ruling: Work is the asks'
 * home).
 *
 * @source-text-pin: PortalConversation.vue has no mount harness (it needs the
 * whole shell: store, router, stick-to-bottom, voice, rail slots); the rule is
 * proven on the pure helper, and the read only pins that the template WIRES it
 * (router-link for the client arm, the emit for Work).
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { chatAsksElsewhere, WORKSPACE_INBOX } from '@/components/portal/portalUtils'
import { RAIL_TABS, RAIL_DOORS } from '@/components/portal/portalRail'

describe('chatAsksElsewhere', () => {
  it('Work is a platform-only tab — the premise of the fix', () => {
    expect(RAIL_TABS.find((t) => t.id === 'work').door).toBe(RAIL_DOORS.PLATFORM)
  })

  it('a client is sent to the Inbox, narrowed to this agent', () => {
    expect(chatAsksElsewhere({ n: 3, agentName: 'scout', isPlatform: false })).toEqual({
      text: '3 more asks from this agent',
      action: 'Open in Inbox',
      to: { path: WORKSPACE_INBOX, query: { tab: 'action', from: 'scout' } },
    })
  })

  it('a platform reader keeps Work', () => {
    expect(chatAsksElsewhere({ n: 1, agentName: 'scout', isPlatform: true })).toEqual({
      text: '1 more ask is waiting in other chats',
      action: 'Open in Work',
      to: null,
    })
  })

  it('nothing elsewhere → nothing to say', () => {
    expect(chatAsksElsewhere({ n: 0, agentName: 'scout', isPlatform: false })).toBeNull()
  })
})

describe('PortalConversation renders the rule (source-asserted: no mount harness exists for it)', () => {
  const sfc = readFileSync(
    fileURLToPath(new URL('../../src/components/portal/PortalConversation.vue', import.meta.url)), 'utf8',
  )
  it('the line comes from chatAsksElsewhere; a client link is a router-link, Work stays an emit', () => {
    expect(sfc).toMatch(/chatAsksElsewhere\(/)
    expect(sfc).toMatch(/<router-link[\s\S]{0,200}v-if="asksElsewhere\.to"[\s\S]{0,200}:to="asksElsewhere\.to"/)
    expect(sfc).toMatch(/@click="emit\('open-work'\)"/)
  })
})
