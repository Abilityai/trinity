/**
 * Tool-access profiles — the client's own decisions (trinity-enterprise#341):
 * the Read-only toggle's state, the Custom list's parsing and validation
 * (mirroring the backend: ≤50 entries, 1–256 chars), and the toasts.
 */
import { describe, expect, it } from 'vitest'
import {
  isStrict, parseToolList, toolListText, toolListError, saveMessage, toggleMessage,
  MAX_TOOLS, MAX_TOOL_CHARS,
} from '../../src/utils/toolAccess'

describe('isStrict', () => {
  it('is ON for Strict read-only only', () => {
    expect(isStrict({ tool_access_profile: 'strict_read_only' })).toBe(true)
    expect(isStrict({ tool_access_profile: 'read_only_report', read_only_enabled: true })).toBe(false)
    expect(isStrict({})).toBe(false)
    expect(isStrict(null)).toBe(false)
  })
})

describe('the Custom list', () => {
  it('parses one tool per line, trims, drops blanks and duplicates', () => {
    expect(parseToolList(' Bash \n\nWebFetch\nBash,Edit ')).toEqual(['Bash', 'WebFetch', 'Edit'])
    expect(toolListText(['Bash', 'Edit'])).toBe('Bash\nEdit')
  })

  it('accepts what the backend accepts', () => {
    expect(toolListError('')).toBe('')
    expect(toolListError(Array.from({ length: MAX_TOOLS }, (_, i) => `T${i}`).join('\n'))).toBe('')
    expect(toolListError('x'.repeat(MAX_TOOL_CHARS))).toBe('')
  })

  it('names the problem, the limit and an example', () => {
    const tooMany = toolListError(Array.from({ length: MAX_TOOLS + 1 }, (_, i) => `T${i}`).join('\n'))
    expect(tooMany).toContain(`at most ${MAX_TOOLS}`)
    expect(tooMany).toContain(`${MAX_TOOLS + 1}`)
    const tooLong = toolListError('x'.repeat(MAX_TOOL_CHARS + 1))
    expect(tooLong).toContain(`${MAX_TOOL_CHARS} characters`)
    expect(tooLong).toContain('for example Bash')
  })
})

describe('toasts', () => {
  const before = { read_only: false }

  it('a named profile that changes both layers', () => {
    const after = { profile_label: 'Strict read-only', read_only: true, read_only_applied: true, restart_required: true }
    expect(saveMessage(before, after)).toBe(
      'Strict read-only saved. File protection is on now. The tool list applies after a restart.')
  })

  it('only the tool list', () => {
    const after = { profile_label: 'Workspace-only', read_only: false, read_only_applied: null, restart_required: true }
    expect(saveMessage(before, after)).toBe('Workspace-only saved. The tool list applies after a restart.')
  })

  it('a live apply that failed is said, never hidden', () => {
    const after = { profile_label: 'Read-only + report', read_only: true, read_only_applied: false, restart_required: true }
    expect(saveMessage(before, after)).toContain('File protection applies when the agent next starts.')
  })

  it('the toggle', () => {
    expect(toggleMessage({ profile: 'strict_read_only', read_only_applied: true, restart_required: true }, 'Scout'))
      .toBe('Strict read-only is on for Scout. File protection and the send block apply now. The tool list updates at the next restart.')
    expect(toggleMessage({ profile: 'workspace_only', profile_label: 'Workspace-only', restart_required: false }, 'Scout'))
      .toBe('Scout is back on Workspace-only.')
  })
})
