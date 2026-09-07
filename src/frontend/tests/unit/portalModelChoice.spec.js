/**
 * ent#403 — the Workspace composer's model choice.
 *
 * Two kinds of test, and the split is the point. `vitest` runs
 * `environment: 'node'` with no mount harness, so:
 *
 *   - every RULE lives in `portalModelChoice.js` and is exercised directly;
 *   - everything about the rendered control is a SOURCE-STRUCTURE assertion,
 *     the same shape `portalVoiceMode.spec.js` and `portalComposerAlignment.spec.js`
 *     use. Nothing here proves a pixel; the visual pass is human (light + dark,
 *     the select against the textarea, a narrow viewport).
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

import {
  INHERIT_VALUE,
  defaultOptionText,
  modelControlState,
  optionText,
  optionTitle,
  reconcileStored,
  shouldClearChoice,
  storedFor,
  withChoice,
} from '../../src/components/portal/portalModelChoice.js'
import { MODEL_CATALOG } from '../../src/constants/modelCatalog.js'

const read = (p) => readFileSync(fileURLToPath(new URL(p, import.meta.url)), 'utf8')
const CODE = read('../../src/components/portal/PortalConversation.vue')
const STORE = read('../../src/stores/clientPortal.js')
const VOICE = read('../../src/components/portal/portalVoiceMode.js')

const OPTIONS = [
  { id: 'claude-opus-5', tier: 'Most capable', label: 'Claude Opus 5' },
  { id: 'claude-sonnet-5', tier: 'Balanced — fast and smart', label: 'Claude Sonnet 5' },
  { id: 'claude-haiku-4-5-20251001', tier: 'Fastest', label: 'Claude Haiku 4.5' },
]
const DEFAULT = { model: 'claude-sonnet-4-6', label: 'Claude Sonnet 4.6', source: 'platform' }
const on = (over = {}) => modelControlState({
  isPlatform: true, modelDefault: DEFAULT, options: OPTIONS, ...over,
})

// ---------------------------------------------------------------------------
// The control renders only when the server says it may
// ---------------------------------------------------------------------------

describe('the control fails closed', () => {
  it('renders for a platform user the server gave a default to', () => {
    expect(on()).toMatchObject({ render: true, enabled: true })
  })

  it('renders NOTHING for a client-portal principal', () => {
    // The roster payload is the only capability channel an external client has
    // (#2128), and a gate written against the platform feature-flag endpoint is
    // JWT-gated — dead for exactly this audience.
    expect(on({ isPlatform: false }).render).toBe(false)
  })

  it.each([
    ['no default at all (older backend, non-Claude runtime, failed read)', { modelDefault: null }],
    ['a default with no model id', { modelDefault: { label: 'x' } }],
    ['an empty option list', { options: [] }],
    ['options that are not a list', { options: null }],
    ['options with no tier to render', { options: [{ id: 'x', label: 'X' }] }],
  ])('renders nothing on %s', (_why, over) => {
    expect(on(over).render).toBe(false)
  })

  it('a partial option list keeps only the usable entries', () => {
    const state = on({ options: [...OPTIONS, { id: 'broken' }, null] })
    expect(state.options).toHaveLength(OPTIONS.length)
  })
})

describe('a room-bound draft disables the control instead of lying about it', () => {
  // ent#361: an @mention diverts the send into a ROOM, whose composer has no
  // model control. A live select there would display a setting it is not
  // honouring — the dead-affordance failure, inverted.
  it('renders, disabled, with a reason a person can act on', () => {
    const state = on({ roomBound: true })
    expect(state.render).toBe(true)
    expect(state.enabled).toBe(false)
    expect(state.reason).toMatch(/group chat/i)
  })

  it('still offers nothing when the control would not render at all', () => {
    expect(on({ roomBound: true, isPlatform: false }).render).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// The stored choice heals itself
// ---------------------------------------------------------------------------

describe('reconcileStored', () => {
  it('keeps a stored id that is still on offer', () => {
    expect(reconcileStored('claude-opus-5', OPTIONS)).toBe('claude-opus-5')
  })

  it('DROPS an id the catalog no longer offers', () => {
    // The catalog churns by design (three entries already carry a "Legacy"
    // relabel). Without this a retired id is sent on every turn, refused every
    // time, forever, across reloads, with nothing naming the cause.
    expect(reconcileStored('claude-opus-4-2-retired', OPTIONS)).toBe(INHERIT_VALUE)
  })

  it.each([[null], [undefined], [42], [{}], ['   '], ['']])('reads %p as inherit', (v) => {
    expect(reconcileStored(v, OPTIONS)).toBe(INHERIT_VALUE)
  })

  it('drops everything when there is nothing on offer', () => {
    expect(reconcileStored('claude-opus-5', [])).toBe(INHERIT_VALUE)
  })
})

describe('the record is per (user, agent)', () => {
  const record = { alpha: 'claude-opus-5', beta: 'claude-haiku-4-5-20251001' }

  it('reads only this agent’s entry', () => {
    expect(storedFor(record, 'alpha', OPTIONS)).toBe('claude-opus-5')
    expect(storedFor(record, 'beta', OPTIONS)).toBe('claude-haiku-4-5-20251001')
    expect(storedFor(record, 'gamma', OPTIONS)).toBe(INHERIT_VALUE)
  })

  it('heals a malformed record rather than throwing', () => {
    // The client owns the schema of its own preference value and heals what it
    // reads (`normalizeLayout`'s rule) — a bad row must not break a composer.
    expect(storedFor(null, 'alpha', OPTIONS)).toBe(INHERIT_VALUE)
    expect(storedFor('not-an-object', 'alpha', OPTIONS)).toBe(INHERIT_VALUE)
    expect(storedFor(record, '', OPTIONS)).toBe(INHERIT_VALUE)
  })

  it('writes without mutating what the store holds', () => {
    const next = withChoice(record, 'alpha', 'claude-haiku-4-5-20251001')
    expect(next.alpha).toBe('claude-haiku-4-5-20251001')
    expect(record.alpha).toBe('claude-opus-5')      // untouched
    expect(next.beta).toBe('claude-haiku-4-5-20251001')  // and nobody else moved
  })

  it('DELETES the entry on inherit rather than storing an empty string', () => {
    // One representation of "no choice", not two — otherwise the reconcile and
    // the read have to agree about a second falsy value forever.
    const next = withChoice(record, 'alpha', INHERIT_VALUE)
    expect('alpha' in next).toBe(false)
    expect(next.beta).toBe('claude-haiku-4-5-20251001')
  })
})

// ---------------------------------------------------------------------------
// What the options say
// ---------------------------------------------------------------------------

describe('option text is plain language, and short', () => {
  it('renders the TIER alone, never `label — tier`', () => {
    // A native select sizes to its widest option and this one sits beside a
    // `flex-1 min-w-0` textarea, so the closed state has to stay short. The
    // model name rides the title, where it costs no width.
    expect(optionText(OPTIONS[0])).toBe('Most capable')
    expect(optionText(OPTIONS[1])).toBe('Balanced — fast and smart')
    expect(optionText(OPTIONS[0])).not.toContain('Claude')
  })

  it('never renders blank, whatever the entry is missing', () => {
    expect(optionText({ id: 'x', label: 'X' })).toBe('X')
    expect(optionText({ id: 'x' })).toBe('x')
    expect(optionText(null)).toBe('')
  })

  it('the title carries the real model name and its id', () => {
    expect(optionTitle(OPTIONS[0])).toBe('Claude Opus 5 (claude-opus-5)')
    expect(optionTitle({ id: 'x' })).toBe('x')
  })
})

describe('the default option reads as the AGENT’s choice', () => {
  it('names the model the agent will actually use', () => {
    // "plainly reads as the agent's own choice" — naming the resolved model is
    // what makes it a fact rather than a shrug.
    expect(defaultOptionText(DEFAULT)).toBe('Agent’s default (Claude Sonnet 4.6)')
  })

  it('degrades to the raw id, never to a blank', () => {
    // `platform_default_model` is written through the generic settings PUT with
    // no catalog check, and ids like `claude-sonnet-4-6[1m]` are in legitimate
    // circulation.
    expect(defaultOptionText({ model: 'claude-sonnet-4-6[1m]' }))
      .toBe('Agent’s default (claude-sonnet-4-6[1m])')
    expect(defaultOptionText(null)).toBe('Agent’s default')
  })
})

// ---------------------------------------------------------------------------
// The self-heal keys on the token, not the prose
// ---------------------------------------------------------------------------

describe('shouldClearChoice', () => {
  it('clears only on the verdict that says the MODEL was the problem', () => {
    expect(shouldClearChoice({ category: 'invalid_model' })).toBe(true)
  })

  it.each(['auth', 'capacity', 'timeout', 'agent_error', 'cancelled', 'busy',
    'agent_unavailable', 'internal', 'lost', 'failed'])(
    'leaves a deliberate choice alone on %s', (category) => {
      // Every one of these has a true and specific cause that has nothing to do
      // with the model; clearing on them would quietly discard a choice.
      expect(shouldClearChoice({ category })).toBe(false)
    })

  it.each([[null], [undefined], [{}]])('is false for %p', (o) => {
    expect(shouldClearChoice(o)).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// The curated set, against the generated catalog
// ---------------------------------------------------------------------------

describe('the curated set comes from the ONE catalog', () => {
  const workspace = MODEL_CATALOG.filter((m) => m.workspace)

  it('is short, and every entry carries a plain-language tier', () => {
    expect(workspace.length).toBeGreaterThan(0)
    expect(workspace.length).toBeLessThanOrEqual(5)   // "a SHORT curated set"
    workspace.forEach((m) => expect(m.workspaceTier.trim()).not.toBe(''))
  })

  it('is a subset of the #894 public-channel allow-list', () => {
    // Asserted on the Python side too (import-time + the parity test). Here as
    // well because THIS is the copy the browser reads, and the failure it would
    // cause — a 422 on send — lands on the client.
    workspace.forEach((m) => expect(m.publicChannel).toBe(true))
  })

  it('offers no "Legacy" tier to a client', () => {
    // A client-facing dropdown offering "Claude Opus 4.6 — Legacy" IS the
    // operator combobox this issue reacts against.
    workspace.forEach((m) => expect(m.note).not.toMatch(/legacy/i))
  })

  it('no two options lead with the same words', () => {
    const tiers = workspace.map((m) => m.workspaceTier)
    expect(new Set(tiers).size).toBe(tiers.length)
  })
})

// ---------------------------------------------------------------------------
// The wiring the pure rules cannot reach
// ---------------------------------------------------------------------------

describe('the composer is wired to the rules', () => {
  it('uses the BaseSelect primitive, never a hand-rolled lookalike', () => {
    expect(CODE).toContain("import BaseSelect from '../base/BaseSelect.vue'")
    expect(CODE).toMatch(/<BaseSelect[\s\S]{0,400}data-testid="portal-model-picker"/)
  })

  it('renders on the SERVER’s answer, never on a feature flag', () => {
    expect(CODE).toContain('modelDefault: props.agent?.model_default || null')
    expect(CODE).toMatch(/v-if="modelControl\.render"/)
    // #2128: the feature-flag endpoint is JWT-gated and empty for a portal client.
    const block = CODE.slice(CODE.indexOf('const modelControl'), CODE.indexOf('const modelDefaultText'))
    expect(block).not.toMatch(/featureFlag/i)
  })

  it('bounds its own width so the textarea keeps its own', () => {
    // `FIELD_CLASS` is `w-full` and a native select otherwise sizes to its
    // widest option, beside a `flex-1 min-w-0` textarea (#2259's geometry).
    expect(CODE).toMatch(/<BaseSelect[\s\S]{0,300}max-w-\[/)
  })

  it('goes inert during a voice call, and is on the locked list', () => {
    expect(CODE).toMatch(/<BaseSelect[\s\S]{0,300}:disabled="voiceCallActive \|\| !modelControl\.enabled"/)
    expect(VOICE).toContain("'model-picker'")
  })

  it('sends the model on BOTH turn paths, read once before dispatch', () => {
    // A field honoured by only one route brings the bug back exactly when
    // streaming fails and the synchronous fallback runs.
    expect(CODE).toContain('const chosenModel = modelControl.value.enabled ? selectedModel.value : \'\'')
    expect(CODE).toMatch(/startPortalChat\([\s\S]{0,220}model: chosenModel/)
    expect(CODE).toMatch(/sendPortalChat\([\s\S]{0,220}model: chosenModel/)
    expect(STORE).toMatch(/sendPortalChat\([\s\S]{0,400}model: model \|\| null/)
    expect(STORE).toMatch(/startPortalChat\([\s\S]{0,400}model: model \|\| null/)
  })

  it('persists to the user’s SERVER record, not the browser', () => {
    expect(CODE).toContain("import { useUserPreferencesStore } from '@/stores/userPreferences'")
    expect(CODE).toContain('PREF_KEYS.workspaceModel')
    // The portal store's `clientEmail` starts null and is filled from a network
    // response, so a browser key built on it reads `anon` on every reload
    // (`useColumnResize.js`, caught live). There is no storage here at all.
    const block = CODE.slice(CODE.indexOf('// ---- The model choice'), CODE.indexOf('const voice = useVoiceSession'))
    expect(block).not.toMatch(/localStorage|sessionStorage/)
  })

  it('self-heals on THIS tab’s settle AND on the reload path', () => {
    // Without the reattach arm the stored model survives a refresh and the next
    // turn re-fails identically, with nothing naming the cause.
    expect(CODE).toMatch(/function rememberVerdict\(outcome\)[\s\S]{0,1200}clearModelChoiceOnFailure\(outcome\)/)
    expect(CODE).toMatch(/markFailed\(index[\s\S]{0,400}clearModelChoiceOnFailure\(res\)/)
    // The verdict TOKEN has to survive `deliver()` for the rule to see it.
    expect(CODE).toContain('category: data.outcome.category')
  })

  it('the roster’s option list fails closed in the store', () => {
    expect(STORE).toMatch(/this\.modelOptions = Array\.isArray\(data\.model_options\)/)
    // Cleared with the session: a different client on the same browser must not
    // inherit the previous one's capability verdict.
    expect(STORE).toMatch(/this\.modelOptions = \[\]/)
  })
})
