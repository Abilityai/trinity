/**
 * trinity#3274 — reading a gated-skill answer (`utils/skillGate.js`).
 *
 * A run request naming a gated skill answers 202 `pending_approval` (nothing
 * ran; show the server's message, poll nothing) or a named refusal. These two
 * readers are what every run sender branches on; the senders themselves are
 * mounted in `skillGateSenders.spec.js`.
 */
import { describe, it, expect } from 'vitest'
import { isGateRefusal, pendingApprovalMessage } from '../../src/utils/skillGate'

const NOTICE = 'Not run: the skill pay-invoice on fin needs approval before it can run.'

describe('pendingApprovalMessage', () => {
  it('is the server message on a 202 pending_approval', () => {
    expect(pendingApprovalMessage({ status: 202, data: { status: 'pending_approval', message: NOTICE } }))
      .toBe(NOTICE)
  })

  it('falls back to the MCP layer\'s wording when the body carries no message', () => {
    expect(pendingApprovalMessage({ status: 202, data: { status: 'pending_approval' } }))
      .toBe('Not run: this needs approval.')
  })

  it.each([
    [{ status: 202, data: { status: 'accepted', execution_id: 'e1' } }],   // an ordinary async accept
    [{ status: 200, data: { status: 'pending_approval', message: NOTICE } }],
    [undefined],
  ])('is null for anything else (%#)', (response) => {
    expect(pendingApprovalMessage(response)).toBeNull()
  })
})

describe('isGateRefusal', () => {
  const refusal = (code, headerCode = code, headers = 'plain') => ({
    response: {
      status: 409,
      headers: headers === 'plain'
        ? { 'x-trinity-error-code': headerCode }
        : { get: (k) => (k === 'x-trinity-error-code' ? headerCode : null) },
      data: { detail: { status: 'refused', code, message: 'Not installed.' } },
    },
  })

  it('reads the header as a plain object or through get()', () => {
    expect(isGateRefusal(refusal('gated_skill_not_installed'))).toBe(true)
    expect(isGateRefusal(refusal('gated_skill_not_installed', undefined, 'get'))).toBe(true)
  })

  it('is false without the matching header, or for another error', () => {
    expect(isGateRefusal(refusal('gated_skill_not_installed', 'other'))).toBe(false)
    expect(isGateRefusal({ response: { status: 500, headers: {}, data: { detail: 'boom' } } })).toBe(false)
    expect(isGateRefusal(new Error('network'))).toBe(false)
  })
})
