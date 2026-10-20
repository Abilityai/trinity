/**
 * trinity-enterprise#549 — the owner's Sharing panel says who each file is for.
 *
 * A shared file used to have no audience at all, so this column did not exist
 * and every rostered client saw every share. Now a row carries three facts —
 * `addressed_to` (an email), `addressed_to_channel` (`whatsapp:+…`) and
 * `audience_source` (how the platform decided) — and the panel has to turn them
 * into a sentence without lying in either direction:
 *
 *  - "Owner only" when the turn had no person (a schedule, an operator chat) is
 *    a different claim from "Owner only" because the platform COULD NOT TELL
 *    which conversation the share came from. The second is recoverable and the
 *    owner should know it happened; rendering both the same hides it.
 *  - A channel share with no verified email is somebody's, not the owner's.
 *
 * The rule lives in a pure module because that is the only place a rule is
 * testable here (`environment: 'node'`, no mount harness) — a `.vue` binding is
 * eyeballed, this is proven.
 */
import { describe, it, expect } from 'vitest'
import { describeSharedFileAudience } from '../../src/utils/sharedFileAudience'

describe('describeSharedFileAudience', () => {
  it('names the person a file is addressed to', () => {
    expect(describeSharedFileAudience({ addressed_to: 'ada@example.com', audience_source: 'turn' }))
      .toEqual({ label: 'ada@example.com', detail: 'The person in the conversation', ownerOnly: false })
  })

  it('says when the AGENT chose the person rather than the platform', () => {
    expect(describeSharedFileAudience({ addressed_to: 'bob@example.com', audience_source: 'override' }).detail)
      .toBe('Addressed by the agent')
  })

  it('shows the channel beside a verified email', () => {
    const got = describeSharedFileAudience({
      addressed_to: 'ada@example.com', addressed_to_channel: 'whatsapp:+15555550142', audience_source: 'channel',
    })
    expect(got.label).toBe('ada@example.com')
    expect(got.detail).toBe('WhatsApp +15555550142')
  })

  it('shows the channel address when nobody verified an email for it', () => {
    expect(describeSharedFileAudience({ addressed_to_channel: 'telegram:424242', audience_source: 'turn' }))
      .toEqual({ label: 'Telegram 424242', detail: 'No verified email — not in any Files tab', ownerOnly: false })
  })

  it('keeps an unknown channel readable instead of dropping it', () => {
    expect(describeSharedFileAudience({ addressed_to_channel: 'signal:abc' }).label).toBe('signal abc')
  })

  it('tells "no person in this turn" apart from "could not tell"', () => {
    const nobody = describeSharedFileAudience({ audience_source: 'none' })
    const unsure = describeSharedFileAudience({ audience_source: 'ambiguous' })
    expect(nobody).toEqual({ label: 'Owner only', detail: 'No person in this turn', ownerOnly: true })
    expect(unsure.label).toBe('Owner only')
    expect(unsure.ownerOnly).toBe(true)
    expect(unsure.detail).toBe("Couldn't tell which conversation it came from")
    expect(unsure.detail).not.toBe(nobody.detail)
  })

  it('reads a row from before the column as the owner’s, and says why', () => {
    expect(describeSharedFileAudience({ filename: 'old.pdf' }))
      .toEqual({ label: 'Owner only', detail: 'Shared before files had an addressee', ownerOnly: true })
  })

  it('never throws on a missing row', () => {
    expect(describeSharedFileAudience(undefined).ownerOnly).toBe(true)
    expect(describeSharedFileAudience(null).label).toBe('Owner only')
  })
})
