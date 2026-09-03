/**
 * Workspace sidebar search filters AGENTS, not only chats (trinity-enterprise#402).
 *
 * The sidebar's search box searched the chat history only, so a client with a
 * fleet larger than the collapsed agents block had no way to reach an agent by
 * name — the search box was in front of the roster and did nothing to it.
 *
 * vitest runs `environment: 'node'` with no mount harness, so the decidable
 * rules are pure exports from `portalUtils.js` and the wiring is guarded by
 * source-structure assertions (the repo's established pattern). What is worth
 * pinning here is where the obvious implementation is subtly wrong:
 *
 *   * the composer's matching rule defaults to MENTIONABLE agents only, which
 *     would hide `data.scout` from a search for "scout";
 *   * a plain rank-ordered slice can drop an agent that is waiting on you —
 *     the #2424 failure, reintroduced for search by a second bounding rule;
 *   * "no agents match" told while the roster is still loading is a lie, and
 *     one empty line must never stand in for the other's fact.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import {
  searchAgents, sidebarSearchState, searchEmptyLines,
  agentResultsLabel, agentToggleLabel, showAgentToggle,
  SEARCH_PLACEHOLDER, SIDEBAR_AGENT_RESULT_LIMIT, AGENT_COLLAPSE_LIMIT,
} from '../../src/components/portal/portalUtils'

const here = dirname(fileURLToPath(import.meta.url))
const SIDEBAR = readFileSync(
  resolve(here, '../../src/components/portal/PortalSidebar.vue'), 'utf8',
)

const roster = (n, prefix = 'acme-scout-') =>
  Array.from({ length: n }, (_, i) => ({ name: `${prefix}${String(i).padStart(2, '0')}` }))

describe('searchAgents — matching', () => {
  it('matches the slug and the display label, case-insensitively', () => {
    const list = [
      { name: 'ws-sage', display_label: 'Research Sage' },
      { name: 'acme-scribe' },
    ]
    expect(searchAgents(list, 'SAGE').items.map((a) => a.name)).toEqual(['ws-sage'])
    expect(searchAgents(list, 'research').items.map((a) => a.name)).toEqual(['ws-sage'])
    expect(searchAgents(list, 'SCRIBE').items.map((a) => a.name)).toEqual(['acme-scribe'])
  })

  it('ranks prefix above word-start above substring', () => {
    // `-` is a word boundary, so `zz-scout-tail` would rank as a WORD-START,
    // not a substring — a real substring case needs the query buried inside a
    // token. Agent slugs share deployment prefixes, so the substring tier is
    // what makes the obvious query work at all; it just ranks last.
    const list = [
      { name: 'megascout' },       // substring
      { name: 'ops-scout' },       // word-start
      { name: 'scout-prime' },     // prefix
    ]
    expect(searchAgents(list, 'scout').items.map((a) => a.name))
      .toEqual(['scout-prime', 'ops-scout', 'megascout'])
  })

  it('includes an un-mentionable slug — a sidebar row is not a mention', () => {
    // filterAgentCandidates defaults requireMentionable TRUE for the composer,
    // where an un-mentionable pick is a dead end. `data.scout` opens fine, so
    // inheriting that default would hide a real agent from a search for its
    // own name. The flag lives inside searchAgents so a caller cannot forget it.
    const list = [{ name: 'data.scout' }, { name: 'plain-scout' }]
    expect(searchAgents(list, 'scout').items.map((a) => a.name))
      .toEqual(['data.scout', 'plain-scout'])
  })

  it('reaches an agent far past the collapsed steady-state window', () => {
    const list = [...roster(29), { name: 'needle-agent' }]
    expect(list.length).toBeGreaterThan(AGENT_COLLAPSE_LIMIT)
    const r = searchAgents(list, 'needle')
    expect(r.visible.map((a) => a.name)).toEqual(['needle-agent'])
  })

  it('returns an empty record for a roster that is not an array', () => {
    for (const bad of [null, undefined, 'nope', 42]) {
      const r = searchAgents(bad, 'x')
      expect(r.items).toEqual([])
      expect(r.visible).toEqual([])
      expect(r.total).toBe(0)
      expect(r.hidden).toBe(0)
    }
  })
})

describe('searchAgents — the results window', () => {
  it('bounds the visible set and states how many are hidden', () => {
    const list = roster(20)
    const r = searchAgents(list, 'scout')
    expect(r.total).toBe(20)
    expect(r.visible.length).toBe(SIDEBAR_AGENT_RESULT_LIMIT)
    expect(r.hidden).toBe(20 - SIDEBAR_AGENT_RESULT_LIMIT)
  })

  it('shows everything when expanded, and hides nothing', () => {
    const r = searchAgents(roster(20), 'scout', { expanded: true })
    expect(r.visible.length).toBe(20)
    expect(r.hidden).toBe(0)
  })

  it('never collapses an ask-bearing match out of its own result', () => {
    // The #2424 rule, reused. A plain rank-ordered slice would drop the one
    // row the person is actually looking for — the header advertises its asks
    // while the search result hides it.
    const list = roster(20)
    const blocked = list[17].name
    const r = searchAgents(list, 'scout', { askCounts: { [blocked]: 2 } })
    expect(r.visible.map((a) => a.name)).toContain(blocked)
    expect(r.visible.length).toBe(SIDEBAR_AGENT_RESULT_LIMIT + 1)
    // ...and `hidden` stays honest about the rest.
    expect(r.hidden).toBe(20 - r.visible.length)
  })
})

describe('sidebarSearchState', () => {
  it('reports loading over empty while the roster is in flight', () => {
    // A two-character query typed during the roster load must not read
    // "No agents match." over a roster that has not arrived.
    expect(sidebarSearchState({ rosterLoading: true, agentTotal: 0, chatCount: 0 }))
      .toBe('roster-loading')
    expect(sidebarSearchState({ rosterLoading: true, chatsSearching: true }))
      .toBe('roster-loading')
  })

  it('covers the rest of the matrix', () => {
    expect(sidebarSearchState({ chatsSearching: true })).toBe('searching')
    expect(sidebarSearchState({ agentTotal: 2, chatCount: 3 })).toBe('both')
    expect(sidebarSearchState({ agentTotal: 2, chatCount: 0 })).toBe('agents-only')
    expect(sidebarSearchState({ agentTotal: 0, chatCount: 3 })).toBe('chats-only')
    expect(sidebarSearchState({ agentTotal: 0, chatCount: 0 })).toBe('none')
    expect(sidebarSearchState()).toBe('none')
  })

  it('renders agents regardless of what the chat request is doing', () => {
    // Agents are filtered client-side over a roster already in hand, so an
    // in-flight chat search says nothing about them.
    expect(sidebarSearchState({ agentTotal: 4, chatsSearching: true })).toBe('searching')
    expect(searchEmptyLines('searching').agents).toBeNull()
  })
})

describe('searchEmptyLines — per section, never one line for the other fact', () => {
  it('nothing matched at all: both lines and a next action', () => {
    const l = searchEmptyLines('none', 'zzz')
    expect(l.agents).toBe('No agents match.')
    expect(l.chats).toBe('No chats match.')
    expect(l.hint).toMatch(/another word|clear/i)
  })

  it('agents matched, no chats: the chats line ALONE', () => {
    const l = searchEmptyLines('agents-only', 'scout')
    expect(l.agents).toBeNull()
    expect(l.chats).toBe('No chats match.')
    expect(l.hint).toBeNull()
  })

  it('chats matched, no agents: the agents line ALONE', () => {
    const l = searchEmptyLines('chats-only', 'invoice')
    expect(l.agents).toBe('No agents match.')
    expect(l.chats).toBeNull()
    expect(l.hint).toBeNull()
  })

  it('says the chats are still loading rather than that there are none', () => {
    const l = searchEmptyLines('searching', 'sc')
    expect(l.chats).toBe('Searching chats…')
    expect(l.agents).toBeNull()
  })

  it('says nothing at all while the roster loads, or when both matched', () => {
    for (const s of ['roster-loading', 'both']) {
      const l = searchEmptyLines(s, 'sc')
      expect(l.agents).toBeNull()
      expect(l.chats).toBeNull()
      expect(l.hint).toBeNull()
    }
  })
})

describe('labels and the one persistent toggle', () => {
  it('states the match count, and does not repeat what the toggle says', () => {
    expect(agentResultsLabel(3)).toBe('Agents · 3')
    expect(agentResultsLabel(0)).toBe('Agents')
    expect(agentResultsLabel()).toBe('Agents')
    expect(agentResultsLabel(3)).not.toMatch(/more|hidden|of/i)
  })

  it('the toggle expands the list it is actually bounding', () => {
    expect(agentToggleLabel({ searching: false, rosterCount: 12 })).toBe('Show all (12)')
    expect(agentToggleLabel({ searching: true, matchCount: 12 })).toBe('Show all (12 matches)')
    expect(agentToggleLabel({ searching: true, expanded: true })).toBe('Show fewer')
    expect(agentToggleLabel({ searching: false, expanded: true })).toBe('Show fewer')
  })

  it('is offered while searching only when it has something to do', () => {
    expect(showAgentToggle({ searching: true, hidden: 4 })).toBe(true)
    expect(showAgentToggle({ searching: true, hidden: 0, expanded: true })).toBe(true)
    expect(showAgentToggle({ searching: true, hidden: 0 })).toBe(false)
    // Outside search the existing `v-if="roster.length > AGENT_COLLAPSE_LIMIT"`
    // is the only condition, so this must not add a second one.
    expect(showAgentToggle({ searching: false, hidden: 0 })).toBe(true)
  })

  it('the placeholder names both things it now searches', () => {
    expect(SEARCH_PLACEHOLDER).toMatch(/agent/i)
    expect(SEARCH_PLACEHOLDER).toMatch(/chat/i)
  })
})

describe('what only source can answer — PortalSidebar.vue', () => {
  it('binds the placeholder to the constant rather than restating it', () => {
    expect(SIDEBAR).toMatch(/:placeholder="SEARCH_PLACEHOLDER"/)
    expect(SIDEBAR).not.toContain('placeholder="Search your chats')
  })

  it('routes BOTH modes through the one shownAgents computed', () => {
    const shown = SIDEBAR.match(/const shownAgents = computed\([\s\S]*?\)\)/)
    expect(shown).toBeTruthy()
    expect(shown[0]).toMatch(/isSearching/)
    // The steady state still goes through the #2424 rule directly.
    expect(SIDEBAR).toMatch(/visibleAgentRows\(props\.roster/)
  })

  it('writes the agent row exactly ONCE, so badges cannot drift by mode', () => {
    const loops = SIDEBAR.match(/v-for="a in shownAgents"/g) || []
    expect(loops.length).toBe(1)
  })

  it('keeps ONE toggle element, shown or hidden rather than swapped', () => {
    // #2159: alternating two v-if buttons drops keyboard focus. A v-show keeps
    // the element (and its focus) while search bounds a different list.
    expect(SIDEBAR).toMatch(/v-if="roster\.length > AGENT_COLLAPSE_LIMIT"/)
    expect(SIDEBAR).toMatch(/v-show="showAgentToggle\(/)
    const toggles = SIDEBAR.match(/@click="agentsExpanded = !agentsExpanded"/g) || []
    expect(toggles.length).toBe(1)
  })

  it('matches agents through searchAgents and nothing else', () => {
    expect(SIDEBAR).toMatch(/searchAgents\(/)
    // No second predicate: not the composer helper raw (it would default to
    // mentionable-only), and not a hand-rolled includes() over roster names.
    expect(SIDEBAR).not.toMatch(/filterAgentCandidates\(/)
    expect(SIDEBAR).not.toMatch(/roster[\s\S]{0,40}\.filter\([\s\S]{0,80}\.includes\(/)
  })

  it('does not print "No agents match." over a roster that is simply empty', () => {
    // With nothing shared at all, the next-action block below is the truer
    // sentence; printing both states the same absence twice in two wordings.
    expect(SIDEBAR).toMatch(/v-if="isSearching && emptyLines\.agents && roster\.length"/)
  })

  it('keeps the skeleton and the empty-roster next action on their exact gates', () => {
    expect(SIDEBAR).toMatch(/v-if="loadingRoster && !roster\.length"/)
    expect(SIDEBAR).toMatch(/v-if="!roster\.length && !loadingRoster"/)
  })

  it('opens an agent through the one emit, in both modes', () => {
    const clicks = SIDEBAR.match(/@click="onAgentClick\(a\.name\)"/g) || []
    expect(clicks.length).toBe(1)
    expect(SIDEBAR).toMatch(/emit\('open-agent', name\)/)
  })
})
