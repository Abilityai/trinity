/**
 * #4344 — one helper turns an axios error into the sentence a person reads.
 * A `detail.message` is kept, a known `detail.code` maps to copy, a plain
 * string `detail` is kept only when it is a sentence, and everything else (a
 * validation list, an object, a 5xx, a network failure) is the caller's
 * fallback. `[object Object]` cannot render.
 */
import { describe, it, expect } from 'vitest'
import { personFacingError, isSentence } from '../../src/utils/personFacingError'

const FALLBACK = 'Could not do that. Try again.'
const res = (status, detail) => ({ response: { status, data: { detail } } })

describe('personFacingError', () => {
  it('keeps a string detail that is a sentence', () => {
    expect(personFacingError(res(400, 'This ask expired before it was answered.'), FALLBACK)).toBe('This ask expired before it was answered.')
    expect(personFacingError(res(409, '  The agent is busy  '), FALLBACK)).toBe('The agent is busy')
  })

  it('drops a string detail that is machine text', () => {
    for (const d of [
      'agents: at most 8 names per request',
      'This endpoint requires a portal-delegate API key',
      'Not found at /api/enterprise/client-portal/x',
      'invalid_title',
      'lowercase sentence here',
      'A'.repeat(150) + ' ' + 'b'.repeat(60),
    ]) {
      expect(personFacingError(res(400, d), FALLBACK)).toBe(FALLBACK)
    }
  })

  it('keeps an object detail\'s message, even on a 5xx', () => {
    expect(personFacingError(res(409, { code: 'x', message: 'This chat is already busy.' }), FALLBACK)).toBe('This chat is already busy.')
    expect(personFacingError(res(503, { message: 'The workspace is restarting.' }), FALLBACK)).toBe('The workspace is restarting.')
  })

  it('maps a known code when there is no message', () => {
    const codes = { agent_not_running: 'This agent is offline right now.' }
    expect(personFacingError(res(409, { code: 'agent_not_running' }), FALLBACK, { codes })).toBe('This agent is offline right now.')
    expect(personFacingError(res(409, { code: 'unknown_code' }), FALLBACK, { codes })).toBe(FALLBACK)
  })

  it('a validation list or a bare object becomes the fallback, never [object Object]', () => {
    const list = [{ loc: ['body', 'email'], msg: 'field required', type: 'value_error.missing' }]
    expect(personFacingError(res(422, list), FALLBACK)).toBe(FALLBACK)
    expect(personFacingError(res(400, { reason: 'x' }), FALLBACK)).toBe(FALLBACK)
    expect(personFacingError(res(400, { message: { nested: true } }), FALLBACK)).toBe(FALLBACK)
    expect(personFacingError(res(400, 42), FALLBACK)).toBe(FALLBACK)
  })

  it('a 5xx string detail becomes the fallback', () => {
    expect(personFacingError(res(500, 'Internal Server Error'), FALLBACK)).toBe(FALLBACK)
    expect(personFacingError(res(502, 'The agent could not be reached'), FALLBACK)).toBe(FALLBACK)
  })

  it('a network error (no response) and a non-error become the fallback', () => {
    expect(personFacingError(new Error('Network Error'), FALLBACK)).toBe(FALLBACK)
    expect(personFacingError(null, FALLBACK)).toBe(FALLBACK)
    expect(personFacingError({ response: { status: 400 } }, FALLBACK)).toBe(FALLBACK)
  })
})

describe('isSentence', () => {
  it('needs a capital start, a space, and no machine markers', () => {
    expect(isSentence('Agent is not running')).toBe(true)
    expect(isSentence('Agent')).toBe(false)
    expect(isSentence('')).toBe(false)
    expect(isSentence(null)).toBe(false)
    expect(isSentence('Detail: something broke')).toBe(false)
    expect(isSentence('Set the API key first')).toBe(false)
  })
})
