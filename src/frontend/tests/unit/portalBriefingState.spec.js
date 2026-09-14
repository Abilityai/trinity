import { describe, it, expect } from 'vitest'
import {
  BRIEFING_PENDING,
  BRIEFING_READY,
  BRIEFING_UNAVAILABLE,
  applyBriefings,
  briefingHydrationPlan,
  briefingStateOf,
  briefingZone,
  mergeRosterBriefings,
  shouldRequestBriefing,
  stageZone,
} from '@/components/portal/portalBriefingState'

/**
 * #2163 — the rules behind taking the new-chat briefing off the roster's
 * critical path. `vitest.config.js` pins `environment: 'node'` with no
 * component-mount harness, so these pure functions are the only unit-testable
 * home for them: a rule left inside an SFC is a rule no test can reach.
 */

const card = (name, extra = {}) => ({ name, description: null, playbooks: [], ...extra })

describe('briefingStateOf', () => {
  it('reads an absent field as ready, not pending', () => {
    // A backend that predates #2163 resolves the briefing inline and serves no
    // /briefings route. Reading absence as `pending` would leave that client
    // waiting forever on a call it will never successfully make.
    expect(briefingStateOf(card('a'))).toBe(BRIEFING_READY)
    expect(briefingStateOf(null)).toBe(BRIEFING_READY)
    expect(briefingStateOf({ name: 'a', briefing_state: 'nonsense' })).toBe(BRIEFING_READY)
  })

  it('passes the two real states through', () => {
    expect(briefingStateOf(card('a', { briefing_state: BRIEFING_PENDING }))).toBe(BRIEFING_PENDING)
    expect(briefingStateOf(card('a', { briefing_state: BRIEFING_UNAVAILABLE })))
      .toBe(BRIEFING_UNAVAILABLE)
  })
})

describe('mergeRosterBriefings', () => {
  it('carries a hydrated briefing across a refetch', () => {
    // The p13 rule: a background refresh swaps values in place and must never
    // re-enter the loading phase on a zone that already has data.
    const prev = [card('a', { description: 'd', playbooks: [{ title: 'x' }],
                              briefing_state: BRIEFING_READY })]
    const next = [card('a', { briefing_state: BRIEFING_PENDING })]

    const merged = mergeRosterBriefings(prev, next)

    expect(merged[0].briefing_state).toBe(BRIEFING_READY)
    expect(merged[0].description).toBe('d')
    expect(merged[0].playbooks).toHaveLength(1)
  })

  it('re-arms an unavailable card as pending', () => {
    // A refetch is an explicit user act (both "Try again" buttons call
    // fetchRoster). One earlier 429 must not read "couldn't load" until reload.
    const prev = [card('a', { briefing_state: BRIEFING_UNAVAILABLE })]
    const next = [card('a', { briefing_state: BRIEFING_PENDING })]

    expect(mergeRosterBriefings(prev, next)[0].briefing_state).toBe(BRIEFING_PENDING)
  })

  it('leaves a newly-shared agent pending', () => {
    const merged = mergeRosterBriefings(
      [card('a', { briefing_state: BRIEFING_READY, description: 'd' })],
      [card('a', { briefing_state: BRIEFING_PENDING }), card('b', { briefing_state: BRIEFING_PENDING })]
    )
    expect(merged[1].briefing_state).toBe(BRIEFING_PENDING)
  })

  it('never lets a stale local briefing beat a server-sent one', () => {
    const prev = [card('a', { description: 'old', briefing_state: BRIEFING_READY })]
    const next = [card('a', { description: 'new', briefing_state: BRIEFING_READY })]
    expect(mergeRosterBriefings(prev, next)[0].description).toBe('new')
  })

  it('mutates neither argument', () => {
    const prev = [card('a', { description: 'd', briefing_state: BRIEFING_READY })]
    const next = [card('a', { briefing_state: BRIEFING_PENDING })]
    mergeRosterBriefings(prev, next)
    expect(next[0].description).toBeNull()
    expect(next[0].briefing_state).toBe(BRIEFING_PENDING)
  })

  it('is a plain copy on the first load', () => {
    const next = [card('a', { briefing_state: BRIEFING_PENDING })]
    expect(mergeRosterBriefings([], next)).toEqual(next)
    expect(mergeRosterBriefings(undefined, next)).toEqual(next)
  })
})

describe('applyBriefings', () => {
  const roster = [
    card('a', { briefing_state: BRIEFING_PENDING }),
    card('b', { briefing_state: BRIEFING_PENDING }),
  ]

  it('folds returned entries onto the matching cards only', () => {
    const out = applyBriefings(roster, {
      a: { description: 'd', playbooks: [{ title: 'x' }], searchable_playbooks: [],
           playbooks_total: 3, state: BRIEFING_READY },
    }, ['a'])

    expect(out[0].briefing_state).toBe(BRIEFING_READY)
    expect(out[0].description).toBe('d')
    expect(out[0].playbooks_total).toBe(3)
    expect(out[1]).toBe(roster[1])   // untouched, same object
  })

  it('marks a requested-but-absent name unavailable rather than leaving it pending', () => {
    // Otherwise its zone beams forever and no retry rule ever fires.
    const out = applyBriefings(roster, { b: { state: BRIEFING_READY } }, ['a', 'b'])
    expect(out[0].briefing_state).toBe(BRIEFING_UNAVAILABLE)
  })

  it('honours a server-sent unavailable state', () => {
    const out = applyBriefings(roster, { a: { state: BRIEFING_UNAVAILABLE } }, ['a'])
    expect(out[0].briefing_state).toBe(BRIEFING_UNAVAILABLE)
  })

  it('marks every requested name unavailable when the whole call failed', () => {
    const out = applyBriefings(roster, null, null, { failed: true })
    expect(out.every((c) => c.briefing_state === BRIEFING_UNAVAILABLE)).toBe(true)
  })

  it('never downgrades a card that already has a real briefing', () => {
    const hydrated = [card('a', { description: 'd', briefing_state: BRIEFING_READY })]
    const out = applyBriefings(hydrated, null, null, { failed: true })
    expect(out[0].briefing_state).toBe(BRIEFING_READY)
    expect(out[0].description).toBe('d')
  })
})

describe('briefingHydrationPlan', () => {
  it('batches for a SINGLE pending card', () => {
    // A `>= 2` rule strands the one-agent roster: the active-agent single only
    // fires from the conversation stage, and a deep link into an existing
    // thread never renders it — that agent would stay pending all session.
    expect(briefingHydrationPlan([card('a', { briefing_state: BRIEFING_PENDING })]).batch).toBe(true)
  })

  it('does not batch when nothing is pending', () => {
    const plan = briefingHydrationPlan([
      card('a', { briefing_state: BRIEFING_READY }),
      card('b', { briefing_state: BRIEFING_UNAVAILABLE }),
    ])
    expect(plan).toEqual({ pending: 0, batch: false })
  })

  it('does not batch an empty roster', () => {
    expect(briefingHydrationPlan([]).batch).toBe(false)
    expect(briefingHydrationPlan(undefined).batch).toBe(false)
  })
})

describe('briefingZone', () => {
  // #2540: the zone's placeholder is a skeleton keyed on `state === 'pending'`
  // — the scanline (and its `reveal` verdict) is the chart motion and is gone
  // from this zone. `pending` is the ONE state that shows a placeholder.
  it('is the placeholder state while pending', () => {
    const z = briefingZone(card('a', { briefing_state: BRIEFING_PENDING }))
    expect(z.state).toBe(BRIEFING_PENDING)
    expect(z.loading).toBe(true)
  })

  it('is ready with hints, with a description alone, and with nothing at all', () => {
    // A completed briefing renders whatever it holds — including nothing. An
    // empty ready zone is an agent with nothing exposed, not a loading one.
    for (const extra of [{ playbooks: [{ title: 'x' }] }, { description: 'd' }, {}]) {
      const z = briefingZone(card('a', { briefing_state: BRIEFING_READY, ...extra }))
      expect(z.state).toBe(BRIEFING_READY)
      expect(z.loading).toBe(false)
    }
  })

  it('is unavailable — no placeholder, and it says which', () => {
    const z = briefingZone(card('a', { briefing_state: BRIEFING_UNAVAILABLE }))
    expect(z).toMatchObject({ state: BRIEFING_UNAVAILABLE, loading: false, unavailable: true })
  })

  it('carries no reveal verdict any more', () => {
    // The field fed ScanlineReveal's celebratory pass; a skeleton has no
    // arrival animation, so a surviving `reveal` would be a dead output that
    // invites the primitive back.
    expect('reveal' in briefingZone(card('a'))).toBe(false)
  })
})

describe('shouldRequestBriefing', () => {
  it('always requests a pending card', () => {
    expect(shouldRequestBriefing(card('a', { briefing_state: BRIEFING_PENDING }), 0)).toBe(true)
    expect(shouldRequestBriefing(card('a', { briefing_state: BRIEFING_PENDING }), 5)).toBe(true)
  })

  it('retries an unavailable card exactly once per session', () => {
    const c = card('a', { briefing_state: BRIEFING_UNAVAILABLE })
    expect(shouldRequestBriefing(c, 0)).toBe(true)
    expect(shouldRequestBriefing(c, 1)).toBe(false)
  })

  it('never re-requests a card that already has a verdict with data', () => {
    // A background re-fetch would re-enter loading on a zone that has data.
    expect(shouldRequestBriefing(card('a', { briefing_state: BRIEFING_READY }), 0)).toBe(false)
    expect(shouldRequestBriefing(card('a'), 0)).toBe(false)
  })
})

describe('stageZone', () => {
  const agents = [card('a')]

  it('loads until the roster reaches a verdict', () => {
    expect(stageZone({ rosterLoaded: false, resolved: false, agents }).loading).toBe(true)
  })

  it('keeps loading until bootstrap has placed the caller', () => {
    // A deep link assigns activeAgentName/pendingSession only after
    // refreshThreads(); landing on the roster alone flashes agents[0].
    expect(stageZone({ rosterLoaded: true, resolved: false, agents }).loading).toBe(true)
    expect(stageZone({ rosterLoaded: true, resolved: true, agents }).state).toBe('ready')
  })

  it('ignores an in-flight refetch once data is on screen', () => {
    // The whole #1927 rule: `loading` is not a state input. A background
    // refresh must be invisible.
    const z = stageZone({ rosterLoaded: true, resolved: true, agents })
    expect(z.loading).toBe(false)
    expect(z.state).toBe('ready')
  })

  it('is failed on a failed roster, never the placeholder', () => {
    const z = stageZone({ rosterLoaded: true, resolved: true, error: 'boom', agents: [] })
    expect(z).toMatchObject({ state: 'failed', loading: false })
  })

  it('never treats a failure as still-loading', () => {
    // `rosterLoaded` is false on a 401 sign-out, but an error is a verdict.
    expect(stageZone({ rosterLoaded: false, resolved: true, error: 'boom' }).loading).toBe(false)
  })

  it('is empty on an empty roster, never the placeholder', () => {
    const z = stageZone({ rosterLoaded: true, resolved: true, agents: [] })
    expect(z).toMatchObject({ state: 'empty', loading: false })
  })

  it('carries no reveal verdict any more (#2540)', () => {
    // The stage is a skeleton now; the `reveal` bit (and the `unreachable`
    // input that only ever fed it) are gone rather than left as dead output.
    const z = stageZone({ rosterLoaded: true, resolved: true, agents })
    expect('reveal' in z).toBe(false)
  })
})
