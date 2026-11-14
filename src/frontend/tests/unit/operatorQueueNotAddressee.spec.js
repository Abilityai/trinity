/**
 * abilityai/trinity-enterprise#1096 — every refusal names who the card waits
 * for. The server's 403 `not_addressee` now carries the person and the seat in
 * `detail.message`; the card shows that message, and keeps the old generic line
 * only when the server sends none (an older backend).
 */
import { describe, it, expect } from 'vitest'
import { notAddresseeMessage, QUEUE_RESPONSE_NOT_ADDRESSEE } from '../../src/utils/operatorQueue'

const refused = detail => ({ response: { status: 403, data: { detail } } })
const NAMED = "This approval waits for the agent's primary: alice@example.com. Only they can decide it."

describe('notAddresseeMessage', () => {
  it("prefers the server's named refusal", () => {
    expect(notAddresseeMessage(refused({ code: 'not_addressee', message: NAMED }))).toBe(NAMED)
  })

  it.each([
    ['no message', { code: 'not_addressee' }],
    ['a blank message', { code: 'not_addressee', message: '   ' }],
    ['a non-string message', { code: 'not_addressee', message: { x: 1 } }],
    ['a string detail', 'Forbidden'],
  ])('falls back to the generic line on %s', (_label, detail) => {
    expect(notAddresseeMessage(refused(detail))).toBe(QUEUE_RESPONSE_NOT_ADDRESSEE)
  })

  it('falls back when there is no response at all', () => {
    expect(notAddresseeMessage(undefined)).toBe(QUEUE_RESPONSE_NOT_ADDRESSEE)
  })
})
