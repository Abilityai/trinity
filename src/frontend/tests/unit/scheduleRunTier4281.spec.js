/**
 * #4281 slice F — the decidable rules behind the Schedules tab's run-tier
 * badge, the take-over request and the restricted-run marker.
 *
 * Every value here comes from the server's schedule and execution reads
 * (slices A, C, D). The page decides only how to say it, and one thing the
 * server cannot: what the person reviewed is what the take-over sends back.
 */
import { describe, it, expect } from 'vitest'
import {
  runTierView,
  warnsOnRunNow,
  deliveryDecision,
  takeOverBody,
  takeOverRefusalText,
  restrictedTurnOf,
  restrictedRunSentence,
} from '../../src/utils/scheduleRunTier'

const shared = (over = {}) => ({
  id: 's1',
  name: 'Morning brief',
  runs_as: 'shared_person',
  runs_as_reason: { code: 'creator_not_owner_tier', message: 'Server says: runs restricted (sentinel).' },
  created_by_you: false,
  created_by: 'Priya Q.',
  can_take_over: true,
  updated_at: '2026-11-08T09:15:00.123456Z',
  message: 'Summarise overnight tickets',
  allowed_tools: ['Read', 'Grep'],
  model: 'claude-sentinel-model',
  validation_enabled: true,
  validation_prompt: 'Check the summary names every ticket',
  deliver_to_workspace_email: null,
  ...over,
})

describe('runTierView: the badge follows runs_as only', () => {
  it('a shared_person schedule gets the badge, the server reason and the creator name', () => {
    const v = runTierView(shared())
    expect(v.badge).toBe('Runs without shell')
    expect(v.reason).toBe('Server says: runs restricted (sentinel).')
    expect(v.creatorLine).toBe('Created by Priya Q.')
  })

  it('an owner-tier schedule shows nothing', () => {
    expect(runTierView(shared({ runs_as: 'owner' }))).toBeNull()
  })

  it('null runs_as (not computed) shows nothing, never a guess either way', () => {
    expect(runTierView(shared({ runs_as: null }))).toBeNull()
    expect(runTierView({ ...shared(), runs_as: undefined })).toBeNull()
  })

  it('a share holder gets "you created it" and no name (D5: the read carries none)', () => {
    const v = runTierView(shared({ created_by: null, created_by_you: true }))
    expect(v.creatorLine).toBe('You created this schedule')
  })

  it('a name the read carries wins over created_by_you', () => {
    expect(runTierView(shared({ created_by_you: true })).creatorLine).toBe('Created by Priya Q.')
  })

  it('neither name nor "you" → no creator line, and a missing reason has a generic sentence', () => {
    const v = runTierView(shared({ created_by: null, runs_as_reason: null }))
    expect(v.creatorLine).toBeNull()
    expect(v.reason).toMatch(/no shell commands and no tools the owner added/)
  })
})

describe('warnsOnRunNow (D9): an owner-tier person on a shared_person schedule', () => {
  it('warns the owner tier on a shared_person schedule only', () => {
    expect(warnsOnRunNow(shared(), true)).toBe(true)
    expect(warnsOnRunNow(shared({ runs_as: 'owner' }), true)).toBe(false)
    expect(warnsOnRunNow(shared({ runs_as: null }), true)).toBe(false)
  })

  it('never tells a share holder the run has owner access', () => {
    expect(warnsOnRunNow(shared(), false)).toBe(false)
  })
})

describe('deliveryDecision (D8)', () => {
  it('no delivery → none', () => {
    expect(deliveryDecision(shared(), 'owner@example.com')).toBe('none')
  })
  it("the taker's own Workspace → own, compared trimmed and case-insensitive", () => {
    expect(deliveryDecision(shared({ deliver_to_workspace_email: ' Owner@Example.com ' }), 'owner@example.com')).toBe('own')
  })
  it("another person's Workspace → other", () => {
    expect(deliveryDecision(shared({ deliver_to_workspace_email: 'p@example.com' }), 'owner@example.com')).toBe('other')
  })
  it('an unknown viewer email is never taken as "own"', () => {
    expect(deliveryDecision(shared({ deliver_to_workspace_email: 'p@example.com' }), null)).toBe('other')
  })
})

describe('takeOverBody: echoes exactly what was reviewed', () => {
  it('carries updated_at verbatim and every reviewed field', () => {
    const body = takeOverBody(shared(), { viewerEmail: 'owner@example.com' })
    expect(body).toEqual({
      expected_updated_at: '2026-11-08T09:15:00.123456Z',
      reviewed: {
        message: 'Summarise overnight tickets',
        allowed_tools: ['Read', 'Grep'],
        model: 'claude-sentinel-model',
        validation_enabled: true,
        validation_prompt: 'Check the summary names every ticket',
      },
    })
  })

  it('null fields stay null (the server requires each one, null included)', () => {
    const body = takeOverBody(shared({ allowed_tools: null, model: null, validation_prompt: null, validation_enabled: false }), {})
    expect(body.reviewed).toEqual({
      message: 'Summarise overnight tickets', allowed_tools: null, model: null, validation_enabled: false, validation_prompt: null,
    })
  })

  it("another person's delivery: clear sends clear, keep repeats the address", () => {
    const s = shared({ deliver_to_workspace_email: 'p@example.com' })
    expect(takeOverBody(s, { viewerEmail: 'o@example.com', delivery: 'clear' })).toMatchObject({ delivery: 'clear' })
    expect(takeOverBody(s, { viewerEmail: 'o@example.com', delivery: 'clear' })).not.toHaveProperty('keep_delivery_to')
    expect(takeOverBody(s, { viewerEmail: 'o@example.com', delivery: 'keep' }))
      .toMatchObject({ delivery: 'keep', keep_delivery_to: 'p@example.com' })
  })

  it("another person's delivery with no decision fails loud rather than send an undecided request", () => {
    const s = shared({ deliver_to_workspace_email: 'p@example.com' })
    expect(() => takeOverBody(s, { viewerEmail: 'o@example.com' })).toThrow(/delivery/)
  })

  it("the taker's own delivery, or none, sends no delivery field", () => {
    const own = takeOverBody(shared({ deliver_to_workspace_email: 'o@example.com' }), { viewerEmail: 'o@example.com', delivery: 'clear' })
    expect(own).not.toHaveProperty('delivery')
    expect(takeOverBody(shared(), { delivery: 'keep' })).not.toHaveProperty('delivery')
  })
})

describe('takeOverRefusalText: each 409 code in user words', () => {
  it.each([
    ['schedule_changed_since_review', /changed after you opened this review/],
    ['schedule_run_in_progress', /in progress or waiting to retry/],
    ['schedule_not_restricted', /nothing to take over/],
    ['schedule_delivers_to_another_person', /another person's Workspace/],
  ])('%s', (code, re) => {
    expect(takeOverRefusalText(code, 'fallback')).toMatch(re)
  })

  it('an unknown code reads the server message', () => {
    expect(takeOverRefusalText('something_new', 'The server sentence.')).toBe('The server sentence.')
  })
})

describe('restrictedTurnOf: the slice C record on turn_integrity', () => {
  const record = { tier: 'share', kind: 'person', cause: 'creator_not_owner_tier', schedule_id: 's1', creator_id: 7, shell: false }

  it('reads the record from the JSON string the API sends', () => {
    expect(restrictedTurnOf(JSON.stringify({ restricted_turn: record }))).toEqual(record)
  })

  it('reads it beside the #2467 keys', () => {
    expect(restrictedTurnOf(JSON.stringify({ killed_notice: 'x', restricted_turn: record }))).toEqual(record)
  })

  it('an object (already parsed) works too', () => {
    expect(restrictedTurnOf({ restricted_turn: record })).toEqual(record)
  })

  it('no column, no key, or an unparseable column → null', () => {
    expect(restrictedTurnOf(null)).toBeNull()
    expect(restrictedTurnOf(undefined)).toBeNull()
    expect(restrictedTurnOf('')).toBeNull()
    expect(restrictedTurnOf(JSON.stringify({ killed_notice: 'x' }))).toBeNull()
    expect(restrictedTurnOf('{not json')).toBeNull()
    expect(restrictedTurnOf(JSON.stringify({ restricted_turn: 'nope' }))).toBeNull()
    expect(restrictedTurnOf(JSON.stringify({ restricted_turn: [record] }))).toBeNull()
    expect(restrictedTurnOf(JSON.stringify([record]))).toBeNull()
  })
})

describe('restrictedRunSentence: worded by cause', () => {
  it.each([
    ['creator_not_owner_tier', /person who created the schedule/],
    ['creator_missing', /no longer exists/],
    ['agent_has_no_owner', /no owner on record/],
    ['schedule_unreadable', /could not be read/],
    ['unknown', /shared person's access/],
  ])('%s', (cause, re) => {
    const s = restrictedRunSentence({ cause })
    expect(s).toMatch(re)
    expect(s).toMatch(/no shell commands/)
  })

  it('a cause that is not a known string reads the generic sentence and never throws', () => {
    const generic = restrictedRunSentence({ cause: 'unknown' })
    for (const cause of [{ toString: null }, 'constructor', 'toString', ['creator_missing'], 7, null]) {
      expect(restrictedRunSentence({ cause })).toBe(generic)
    }
    expect(restrictedRunSentence(restrictedTurnOf('{"restricted_turn":{"cause":{"toString":null}}}'))).toBe(generic)
  })
})

describe('takeOverRefusalText: inherited keys are not codes', () => {
  it('a code like "constructor" reads the fallback', () => {
    expect(takeOverRefusalText('constructor', 'fallback')).toBe('fallback')
  })
})
