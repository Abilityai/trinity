/**
 * ent#155 — Escape / Stop cancels the in-flight turn and gives the words back.
 *
 * The rules a person notices are pure (`utils/turnCancel.js`) because
 * `vitest.config.js` runs `environment: 'node'` with no mount harness: a rule
 * decided inside an SFC is a rule no test can reach. The three surfaces — Agent
 * Detail chat, the public link, the Workspace — are then asserted from source,
 * which is the only thing source can answer: that all three call the same
 * rules rather than each inventing its own.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import {
  shouldCancelOnEscape, restoreDraft, cancelOutcome, isTerminalStatus, TERMINAL_STATUSES,
} from '../../src/utils/turnCancel'

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')
const chatPanel = read('../../src/components/ChatPanel.vue')
const publicChat = read('../../src/views/PublicChat.vue')
const workspace = read('../../src/components/portal/PortalConversation.vue')
const chatInput = read('../../src/components/chat/ChatInput.vue')
const portalStore = read('../../src/stores/clientPortal.js')

const esc = (over = {}) => ({ key: 'Escape', isComposing: false, defaultPrevented: false, ...over })

describe('Escape cancels a turn — and never hijacks anything else', () => {
  it('cancels while a turn is in flight', () => {
    expect(shouldCancelOnEscape(esc(), { inFlight: true, cancelling: false })).toBe(true)
  })

  it('is a no-op with no turn in flight', () => {
    // The AC is explicit: Escape with nothing running must never clear the
    // input or steal the key from whatever else wants it.
    expect(shouldCancelOnEscape(esc(), { inFlight: false, cancelling: false })).toBe(false)
  })

  it('does not fire twice while a cancel is already in flight', () => {
    expect(shouldCancelOnEscape(esc(), { inFlight: true, cancelling: true })).toBe(false)
  })

  it('yields to anything else that owns Escape right now', () => {
    // A menu, a modal, the composer typeahead, the voice loop. One truthy
    // overlay is enough — cancelling is destructive and a missed cancel costs
    // one click, so the rule is deliberately conservative.
    expect(shouldCancelOnEscape(esc(), { inFlight: true, overlays: [false, true] })).toBe(false)
    expect(shouldCancelOnEscape(esc(), { inFlight: true, overlays: [false, false] })).toBe(true)
  })

  it('leaves an IME candidate alone', () => {
    // A composed session uses Escape to abandon a candidate; taking it here
    // would cancel the turn instead of the character.
    expect(shouldCancelOnEscape(esc({ isComposing: true }), { inFlight: true })).toBe(false)
  })

  it('respects a handler that already claimed the key', () => {
    expect(shouldCancelOnEscape(esc({ defaultPrevented: true }), { inFlight: true })).toBe(false)
  })

  it('ignores every other key, and a missing event', () => {
    expect(shouldCancelOnEscape(esc({ key: 'Enter' }), { inFlight: true })).toBe(false)
    expect(shouldCancelOnEscape(null, { inFlight: true })).toBe(false)
  })
})

describe('the words come back without destroying a draft', () => {
  it('restores the cancelled message into an empty composer', () => {
    expect(restoreDraft('summarise the logs', '')).toBe('summarise the logs')
  })

  it('prepends rather than overwriting a draft typed while waiting', () => {
    // "restore prepends or merges sensibly" — the cancelled text is what the
    // user is returning to edit, so it belongs where the caret starts.
    expect(restoreDraft('first', 'second')).toBe('first\n\nsecond')
  })

  it('never drops either half', () => {
    const out = restoreDraft('alpha', 'beta')
    expect(out).toContain('alpha')
    expect(out).toContain('beta')
  })

  it('is idempotent — Escape then Stop cannot stack two copies', () => {
    const once = restoreDraft('alpha', '')
    expect(restoreDraft('alpha', once)).toBe(once)
    const merged = restoreDraft('alpha', 'beta')
    expect(restoreDraft('alpha', merged)).toBe(merged)
  })

  it('leaves the draft alone when there is nothing to restore', () => {
    expect(restoreDraft('', 'typed')).toBe('typed')
    expect(restoreDraft('   ', 'typed')).toBe('typed')
  })

  it('treats a whitespace-only draft as empty rather than merging into it', () => {
    expect(restoreDraft('alpha', '   ')).toBe('alpha')
  })
})

describe('what the user is told', () => {
  it('a successful cancel is not an error', () => {
    // AC: "after a successful cancel the turn renders as cancelled (not an
    // error)".
    expect(cancelOutcome({ ok: true, alreadyTerminal: false })).toEqual({ kind: 'cancelled', message: 'Stopped.' })
  })

  it('a cancel that lost the race says nothing at all', () => {
    // The reply is already on screen; there is no action to offer.
    expect(cancelOutcome({ ok: true, alreadyTerminal: true }).kind).toBe('noop')
    expect(cancelOutcome({ ok: false, alreadyTerminal: true }).kind).toBe('noop')
  })

  it('a refused cancel says the turn is still running', () => {
    // AC: "if the terminate call itself fails, the turn keeps running and an
    // unobtrusive error is shown" — so the message must not imply it stopped.
    const out = cancelOutcome({ ok: false, alreadyTerminal: false })
    expect(out.kind).toBe('failed')
    expect(out.message).toMatch(/still running/i)
  })
})

describe('terminal statuses', () => {
  it('knows the four terminals', () => {
    expect(TERMINAL_STATUSES).toEqual(['success', 'failed', 'cancelled', 'skipped'])
    expect(isTerminalStatus('running')).toBe(false)
    expect(isTerminalStatus('queued')).toBe(false)
    expect(isTerminalStatus('cancelled')).toBe(true)
  })
})

describe('all three surfaces use the shared rules (what only source can answer)', () => {
  const surfaces = [['ChatPanel', chatPanel], ['PublicChat', publicChat], ['Workspace', workspace]]

  it('imports the rules rather than re-deciding them', () => {
    for (const [name, src] of surfaces) {
      expect(src, name).toMatch(/import \{[^}]*shouldCancelOnEscape[^}]*\} from ['"][^'"]*utils\/turnCancel['"]/)
      expect(src, name).toContain('restoreDraft(')
      expect(src, name).toContain('cancelOutcome(')
    }
  })

  it('offers Stop only once there is an execution to stop', () => {
    // A control offered before the id arrives is a lie: there is no turn yet.
    for (const [name, src] of surfaces) {
      expect(src, name).toMatch(/canCancelTurn = computed\(\(\) => \w+\.value && !!activeExecutionId\.value\)/)
    }
  })

  it('captures the id and the words together, and clears them together', () => {
    // Half of this pair is a Stop that cannot restore, or a restore with
    // nothing to stop.
    for (const [name, src] of surfaces) {
      expect(src, name).toMatch(/activeExecutionId\.value = /)
      expect(src, name).toMatch(/activeExecutionId\.value = null/)
      expect(src, name).toMatch(/(pendingUserMessage|pendingUserText)\.value = ''/)
    }
  })

  it('registers and removes the Escape listener', () => {
    for (const [name, src] of surfaces) {
      expect(src, name).toContain("addEventListener('keydown', onEscapeKeydown)")
      expect(src, name).toContain("removeEventListener('keydown', onEscapeKeydown)")
    }
  })

  it('does not restore the words when the cancel was refused', () => {
    // The turn is still running; restoring would imply a stop that did not
    // happen. Asserted by shape: the restore sits inside the success branch.
    for (const [name, src] of surfaces) {
      expect(src, name).toMatch(/outcome\.kind === 'cancelled'[\s\S]{0,400}restoreDraft\(/)
    }
  })

  it('guards against a double cancel', () => {
    for (const [name, src] of surfaces) {
      expect(src, name).toMatch(/if \(!executionId \|\| cancelling\.value\) return/)
    }
  })
})

describe('the Stop control', () => {
  it('is one control with the Send button, not a second competing target', () => {
    // Send is disabled for the whole time a turn runs, so an always-present
    // Stop would be a dead control most of the time.
    expect(chatInput).toContain('v-if="cancellable"')
    expect(chatInput).toContain("$emit('cancel')")
    expect(chatInput).toMatch(/emit = defineEmits\(\[[^\]]*'cancel'/)
    expect(workspace).toContain('v-if="canCancelTurn"')
  })

  it('is shared by the two chat surfaces rather than built twice', () => {
    // ChatPanel and PublicChat mount the SAME ChatInput; the bug this avoids
    // is the second hand-built copy (#2370's lesson).
    for (const [name, src] of [['ChatPanel', chatPanel], ['PublicChat', publicChat]]) {
      expect(src, name).toContain(':cancellable=')
      expect(src, name).toContain('@cancel="cancelTurn"')
    }
  })

  it('names Escape in its own tooltip', () => {
    expect(chatInput).toMatch(/Stop this turn \(Esc\)/)
    expect(workspace).toMatch(/Stop this turn \(Esc\)/)
  })

  it('cannot be pressed twice while it is working', () => {
    expect(chatInput).toContain(':disabled="cancelling"')
    expect(workspace).toContain(':disabled="cancelling"')
  })
})

describe('each surface cancels through its own credential', () => {
  it('the operator chat uses the JWT terminate route', () => {
    expect(chatPanel).toMatch(/\/api\/agents\/\$\{props\.agentName\}\/executions\/\$\{executionId\}\/terminate/)
    expect(chatPanel).toContain('authStore.authHeader')
  })

  it('the public link uses the token route — no JWT exists there', () => {
    expect(publicChat).toMatch(/\/api\/public\/executions\/\$\{token\.value\}\/\$\{executionId\}\/terminate/)
  })

  it('the Workspace goes through the portal store, which carries the portal token', () => {
    expect(workspace).toContain('store.cancelPortalTurn(')
    expect(portalStore).toContain('async cancelPortalTurn(')
    expect(portalStore).toMatch(/client-portal\/agents\/\$\{agentName\}\/executions\/\$\{executionId\}\/terminate/)
    expect(portalStore).toContain('this.authHeader')
  })
})
