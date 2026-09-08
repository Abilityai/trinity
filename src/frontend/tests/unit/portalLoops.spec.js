import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { resolve } from 'path'
import {
  ACTIVE_STATUSES, FORM_INITIAL, GUARDRAIL_DEFAULTS,
  activeLoops, byAgent, isActive, loopHeadroom, loopStatusLabel, loopStatusTone,
  startErrorMessage, startPayload, stripSummary, validateStartForm,
} from '@/components/portal/portalLoopUtils'

const SRC = (p) => readFileSync(resolve(__dirname, '../../src', p), 'utf-8')
const BACKEND = (p) => readFileSync(resolve(__dirname, '../../../backend', p), 'utf-8')

const running = (over = {}) => ({
  loop_id: 'loop_1', agent_name: 'scout', status: 'running',
  max_runs: 10, runs_completed: 3, failed_runs: 0, total_cost: 0, ...over,
})

describe('ent#458 — active detection', () => {
  it('treats only queued and running as active', () => {
    expect(ACTIVE_STATUSES).toEqual(['queued', 'running'])
    expect(isActive(running())).toBe(true)
    expect(isActive(running({ status: 'queued' }))).toBe(true)
    expect(isActive(running({ status: 'completed' }))).toBe(false)
  })

  it('treats an UNKNOWN status as not active, never as still running', () => {
    // A status this bundle has never heard of must not leave the panel
    // claiming work is in flight forever — the stuck-"running" AC #4 forbids.
    expect(isActive(running({ status: 'quantum_superposition' }))).toBe(false)
    expect(isActive({})).toBe(false)
    expect(isActive(null)).toBe(false)
  })

  it('activeLoops tolerates a null list', () => {
    expect(activeLoops(null)).toEqual([])
  })
})

describe('ent#458 — honest terminal words (AC #3)', () => {
  it('names WHY a loop stopped rather than flattening to "Stopped"', () => {
    const cases = {
      budget_exhausted: 'Stopped — cost budget reached',
      deadline_exceeded: 'Stopped — time limit reached',
      no_progress: 'Stopped — it stopped making progress',
      stop_signal_matched: 'Stopped — it reported it was done',
      user_stopped: 'Stopped by you',
    }
    for (const [reason, label] of Object.entries(cases)) {
      expect(loopStatusLabel(running({ status: 'stopped', stop_reason: reason }))).toBe(label)
    }
  })

  it('reports a loop that simply finished its runs as Done, not Stopped', () => {
    // `max_runs_reached` arrives on BOTH a completed row and a stopped one;
    // calling the second "Stopped" would read as a fault to the user.
    expect(loopStatusLabel(running({ status: 'stopped', stop_reason: 'max_runs_reached' }))).toBe('Done')
    expect(loopStatusLabel(running({ status: 'completed' }))).toBe('Done')
    expect(loopStatusTone(running({ status: 'stopped', stop_reason: 'max_runs_reached' }))).toBe('ok')
  })

  it('separates a partial success from a clean one', () => {
    expect(loopStatusLabel(running({ status: 'completed_with_errors' }))).toBe('Done, with errors')
    expect(loopStatusTone(running({ status: 'completed_with_errors' }))).toBe('warn')
  })

  it('says a failure was a failure', () => {
    expect(loopStatusTone(running({ status: 'failed' }))).toBe('danger')
    expect(loopStatusLabel(running({ status: 'failed', stop_reason: 'max_consecutive_failures' })))
      .toBe('Failed — too many errors in a row')
  })

  it('never invents a word for a status it does not know', () => {
    expect(loopStatusLabel(running({ status: 'weird' }))).toBe('weird')
    expect(loopStatusTone(running({ status: 'weird' }))).toBe('neutral')
  })
})

describe('ent#458 — guardrail headroom (AC #1)', () => {
  it('reports null for a guardrail that was never set', () => {
    // "no budget" and "budget untouched" are different facts; a bar rendered at
    // 0% or 100% asserts the wrong one.
    const h = loopHeadroom(running({ max_cost_usd: null, max_duration_seconds: null }))
    expect(h.cost).toBeNull()
    expect(h.time).toBeNull()
    expect(h.runs).not.toBeNull()          // max_runs is always required
  })

  it('computes fractions for the guardrails that are set', () => {
    const h = loopHeadroom(running({ runs_completed: 5, max_runs: 10, total_cost: 0.5, max_cost_usd: 2 }))
    expect(h.runs.fraction).toBe(0.5)
    expect(h.cost.fraction).toBe(0.25)
  })

  it('clamps an overshoot to 1 rather than rendering past the end of the bar', () => {
    // The runtime lets the CURRENT run finish, so cost can legitimately exceed
    // its budget (#1155) — the bar must not overflow its track.
    const h = loopHeadroom(running({ total_cost: 3, max_cost_usd: 2 }))
    expect(h.cost.fraction).toBe(1)
  })

  it('ignores a nonsense limit instead of dividing by it', () => {
    expect(loopHeadroom(running({ total_cost: 1, max_cost_usd: 0 })).cost).toBeNull()
    expect(loopHeadroom(running({ runs_completed: 1, max_runs: null })).runs).toBeNull()
  })
})

describe('ent#458 — the collapsed strip', () => {
  it('says nothing at all when nothing is running', () => {
    // A permanent "0 loops" chip is noise that trains people to stop reading.
    expect(stripSummary([])).toBeNull()
    expect(stripSummary([running({ status: 'completed' })])).toBeNull()
  })

  it('counts agents only when more than one is busy', () => {
    expect(stripSummary([running()])).toBe('1 loop running')
    expect(stripSummary([running(), running({ loop_id: 'l2' })])).toBe('2 loops running')
    expect(stripSummary([running(), running({ loop_id: 'l2', agent_name: 'writer' })]))
      .toBe('2 loops running on 2 agents')
  })
})

describe('ent#458 — grouping', () => {
  it('keeps a participant with no loops out of the rendered list but in the map', () => {
    const grouped = byAgent([running()], ['scout', 'writer'])
    expect(grouped.get('scout')).toHaveLength(1)
    expect(grouped.get('writer')).toEqual([])
  })

  it('puts active loops above finished ones', () => {
    const grouped = byAgent(
      [running({ loop_id: 'done', status: 'completed' }), running({ loop_id: 'live' })], ['scout'])
    expect(grouped.get('scout')[0].loop_id).toBe('live')
  })
})

describe('ent#458 — start form pre-flight', () => {
  it('requires an instruction', () => {
    expect(validateStartForm({ message: '   ', max_runs: 5 }).errors.message).toBeTruthy()
  })

  it('bounds runs to the range the server accepts', () => {
    expect(validateStartForm({ message: 'go', max_runs: 0 }).valid).toBe(false)
    expect(validateStartForm({ message: 'go', max_runs: 101 }).valid).toBe(false)
    expect(validateStartForm({ message: 'go', max_runs: 100 }).valid).toBe(true)
  })

  it('rejects a doom-loop threshold of 1, which cannot mean anything', () => {
    // "stop after 1 identical reply" needs at least two to compare (#1157) —
    // the server rejects it with 422, so a clamp here would hide the reason.
    expect(validateStartForm({ message: 'go', max_runs: 5, no_progress_threshold: 1 }).valid).toBe(false)
    expect(validateStartForm({ message: 'go', max_runs: 5, no_progress_threshold: 0 }).valid).toBe(true)
  })

  it('surfaces the agent ceiling before the round trip (ent#338)', () => {
    const res = validateStartForm(
      { message: 'go', max_runs: 5, timeout_per_run: 1200 }, { agentTimeoutCap: 600 })
    expect(res.valid).toBe(false)
    expect(res.errors.timeout_per_run).toContain('600')
  })

  it('refuses a deadline shorter than a single run (#1156)', () => {
    const res = validateStartForm(
      { message: 'go', max_runs: 5, timeout_per_run: 300, max_duration_seconds: 100 })
    expect(res.errors.max_duration_seconds).toBeTruthy()
  })

  it('treats an empty budget as "no budget", not as zero', () => {
    expect(validateStartForm({ message: 'go', max_runs: 5, max_cost_usd: '' }).valid).toBe(true)
    expect(validateStartForm({ message: 'go', max_runs: 5, max_cost_usd: 0 }).valid).toBe(false)
  })
})

describe('ent#458 — request body', () => {
  it('omits empty optionals so the server default applies', () => {
    const body = startPayload({ message: ' go ', max_runs: 5, max_cost_usd: '', stop_signal: '  ' })
    expect(body).toEqual({ message: 'go', max_runs: 5 })
  })

  it('sends the optionals that were set', () => {
    const body = startPayload({ message: 'go', max_runs: 5, max_cost_usd: '1.5', max_duration_seconds: 900 })
    expect(body.max_cost_usd).toBe(1.5)
    expect(body.max_duration_seconds).toBe(900)
  })
})

describe('ent#458 — refusal messages', () => {
  it('reads the structured agent-cap refusal (ent#338)', () => {
    const msg = startErrorMessage({ response: { data: { detail: {
      error: 'loop_timeout_exceeds_agent_cap', agent_cap_seconds: 600 } } } })
    expect(msg).toContain('600')
  })

  it('passes through a server message rather than inventing one', () => {
    expect(startErrorMessage({ response: { data: { detail: { message: 'nope' } } } })).toBe('nope')
    expect(startErrorMessage({ response: { data: { detail: 'plain' } } })).toBe('plain')
  })

  it('has an answer when the server said nothing useful', () => {
    expect(startErrorMessage({})).toBeTruthy()
  })
})

describe('ent#458 — defaults mirror the server, and are honest about which do', () => {
  it('pins each mirrored default against models.StartLoopRequest', () => {
    // Cross-language, so it cannot be imported. This fails loudly instead of
    // the panel quietly promising a bound the server no longer applies.
    const models = BACKEND('models.py')
    const block = models.slice(models.indexOf('class StartLoopRequest'))
      .slice(0, models.slice(models.indexOf('class StartLoopRequest')).indexOf('\nclass '))
    expect(block).toMatch(/no_progress_threshold:\s*Optional\[int\]\s*=\s*Field\(default=3/)
    expect(block).toMatch(/max_consecutive_failures:\s*int\s*=\s*Field\(\s*default=3/)
    expect(block).toMatch(/on_failure:\s*Literal\["abort",\s*"continue"\]\s*=\s*"abort"/)
    expect(block).toMatch(/delay_seconds:\s*int\s*=\s*Field\(default=0/)
    expect(GUARDRAIL_DEFAULTS.no_progress_threshold).toBe(3)
    expect(GUARDRAIL_DEFAULTS.max_consecutive_failures).toBe(3)
    expect(GUARDRAIL_DEFAULTS.on_failure).toBe('abort')
    expect(GUARDRAIL_DEFAULTS.delay_seconds).toBe(0)
  })

  it('keeps max_runs OUT of the server-default block, because it has none', () => {
    // `max_runs: int = Field(..., ge=1, le=...)` — required. 10 is this form's
    // suggestion; pinning it as a "default" would enshrine a fiction.
    expect(GUARDRAIL_DEFAULTS.max_runs).toBeUndefined()
    expect(FORM_INITIAL.max_runs).toBe(10)
    const models = BACKEND('models.py')
    expect(models).toMatch(/max_runs:\s*int\s*=\s*Field\(\.\.\./)
  })
})

describe('ent#458 — the panel is the platform door only', () => {
  const sfc = SRC('components/portal/PortalLoops.vue')

  it('renders nothing for an external client', () => {
    // ent#78's auth-path invariant. Hidden, not disabled: a disabled control
    // advertises a capability a portal token can never satisfy.
    expect(sfc).toMatch(/isPlatformSession/)
    expect(sfc).toMatch(/v-if="visible"/)
  })

  it('offers Stop for every active loop (AC #1: "Stop always available")', () => {
    expect(sfc).toMatch(/v-if="store\.isActive\(loop\)"/)
    expect(sfc).toMatch(/@click="onStop\(loop\)"/)
  })

  it('teaches how to start from the empty state (AC #5: no dead empty state)', () => {
    expect(sfc).toMatch(/portal-loops-empty/)
    expect(sfc).toMatch(/Start a loop/)
  })

  it('shows the self-stopping guardrails before Start, not after', () => {
    expect(sfc).toMatch(/GUARDRAIL_DEFAULTS\.no_progress_threshold/)
    expect(sfc).toMatch(/GUARDRAIL_DEFAULTS\.max_consecutive_failures/)
  })

  it('uses semantic colour tokens only', () => {
    expect(sfc).not.toMatch(/\b(?:bg|text|border|ring)-(?:indigo|green|red|amber|yellow|blue|orange|purple|rose)-\d{2,3}\b/)
  })
})

describe('ent#458 — auth and lifecycle races (caught in review; re-homed by ent#475)', () => {
  // The strip used to own `stores/portalLoops.js` from inside TWO mount points
  // and guard three races (a late `isPlatformSession`, the incoming/outgoing
  // panels clearing each other, a re-fire resetting the chosen agent). ent#475
  // moved ownership to ONE place — `composables/usePortalRailFeeds.js` — so the
  // guards now pin that owner, and the body is pinned to NOT own anything.
  const owner = SRC('composables/usePortalRailFeeds.js')
  const sfc = SRC('components/portal/PortalLoops.vue')

  it('the owner watches the derived door set, so a late auth confirmation re-fires it', () => {
    // `isPlatformSession` settles asynchronously; `loops` then APPEARS in the
    // visible set. Keyed on the feed set + the joined participant key, never
    // on array identity.
    expect(owner).toMatch(/watch\(\[visible, participantsKey, wantsKey\]/)
    expect(owner).toContain("const participantsKey = computed(() => participants.value.join(' '))")
    expect(owner).toContain('if (w.loops) {')
    expect(owner).toContain('loops.setParticipants(names)')
    expect(owner).toContain('loops.fetchLoops()')
  })

  it('the body owns no participants and no fetch — there is nothing left to race', () => {
    expect(sfc).not.toContain('setParticipants')
    expect(sfc).not.toContain('ownedKey')
    // The only fetch left is the failed-state Retry in the template.
    expect(sfc.slice(sfc.indexOf('<script'))).not.toContain('fetchLoops')
  })

  it('a chat switch re-scopes the store through its participant set; leaving the rail clears it', () => {
    expect(owner).toContain('loops.setParticipants(names)')
    expect(owner).toContain('if (!isVisible) { reset(); return }')
    expect(owner).toMatch(/function reset\(\) \{\s*loops\.clear\(\)/)
  })
})

describe('ent#458 — live push degrades to poll (AC #4)', () => {
  const store = SRC('stores/portalLoops.js')
  const ws = SRC('utils/websocket.js')

  it('routes the fleet-wide loop broadcast to the workspace store too', () => {
    // Same event, two consumers — the reportsStore + fleetReportsStore shape.
    expect(ws).toMatch(/portalLoopsStore\.handleWebSocketEvent\(data\)/)
    expect(ws).toMatch(/loopsStore\.handleWebSocketEvent\(data\)/)
  })

  it('polls only while something is active, so an idle tab is silent', () => {
    expect(store).toMatch(/if \(hasActive\.value\)/)
    expect(store).toMatch(/stopPolling\(\)/)
  })

  it('keeps what loaded when one agent fails, instead of blanking the panel', () => {
    expect(store).toMatch(/allSettled/)
    expect(store).toMatch(/may be incomplete/)
  })
})

describe('ent#458 — review findings', () => {
  const SFC = SRC('components/portal/PortalLoops.vue')
  const STORE = SRC('stores/portalLoops.js')

  it('ignores an event that cannot say which agent it belongs to', () => {
    // The filter was `if (name && !includes(name)) return`, so a payload with
    // no `agent_name` fell through and refetched EVERY participant — and a
    // 100-run loop anywhere in the fleet emits 100 of these.
    expect(STORE).toContain('if (!name || !participants.value.includes(name)) return')
  })

  it('watches what changed, not a new array identity every render', () => {
    // The shell passes a fresh array per render; a non-deep watch compares with
    // `Object.is`, so the form's agent must follow the JOINED key — otherwise a
    // poll tick would silently discard the agent the user picked.
    expect(SFC).toContain("const participantsKey = computed(() => participants.value.join(' '))")
    expect(SFC).toMatch(/watch\(participantsKey,/)
  })

  it('does not reset the chosen agent unless it left the chat', () => {
    // The re-fire used to set `form.agent = names[0]` mid-selection, so a Start
    // clicked in that window ran the loop on the wrong agent.
    expect(SFC).toMatch(/if \(!names\.includes\(form\.agent\)\) form\.agent = names\[0\]/)
  })

  it('never stops the backstop poll from the body', () => {
    // The 12s backstop is armed from `fetchLoops`'s finally and cleared only by
    // the shell's reset; a body that touched it would leave the rail on WS
    // alone — the failure the backstop exists to cover.
    expect(SFC).not.toMatch(/store\.stopPolling\(\)/)
  })

  it('has no collapse of its own — the rail owns it (ent#475)', () => {
    expect(SFC).not.toContain('autoExpanded')
    expect(SFC).not.toContain('portal-loops-toggle')
  })

  it('sends every bound the form shows', () => {
    // `FORM_INITIAL` spreads `GUARDRAIL_DEFAULTS`; the payload dropped two of
    // them, and the strip shows `max_consecutive_failures` as a promise about
    // this loop.
    const body = startPayload({ ...FORM_INITIAL, message: 'go', max_runs: 3 })
    expect(body.max_consecutive_failures).toBe(FORM_INITIAL.max_consecutive_failures)
    expect(body.on_failure).toBe(FORM_INITIAL.on_failure)
  })
})
