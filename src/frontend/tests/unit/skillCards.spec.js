/**
 * trinity-enterprise#754 — the Skills tab's rules, as a pure module.
 *
 * Every decision the merged tab makes lives in `utils/skillCards.js` so it can
 * be executed here without a mount: which section a skill lands in (exactly
 * one, a #2914 conflict in both), whether Run is offered, what the gate line
 * says, who sees the approval controls, and the author's mode chip. The tab
 * component only renders what this returns (its wiring is mounted in
 * skillsTab.mount.spec.js).
 */
import { describe, it, expect } from 'vitest'
import {
  buildSkillCards, gateKeyFor, modeChip, kindLabel, kindOptionLabel, SKILL_NAME_RE,
} from '../../src/utils/skillCards'

const live = (name, over = {}) => ({
  name, description: `${name} does things`, path: `.claude/skills/${name}/SKILL.md`,
  user_invocable: true, automation: null, argument_hint: null,
  source: 'agent', dir: name, approval: null, ...over,
})
const row = (skill_name, over = {}) => ({ skill_name, individual: true, via_sets: [], delivery_status: null, ...over })
const lib = (name, over = {}) => ({ name, description: `${name} from the library`, automation: null,
  user_invocable: true, deprecated: false, superseded_by: null, approval: null, version: 'abcdef123456', ...over })
const gate = (skill_name, over = {}) => ({ skill_name, approver: 'primary', approver_reachable: true, origin: 'set', ...over })
const PRIMARY_ME = [{ kind: 'primary', reachable: true, viewer_fills: true }]
const PRIMARY_OTHER = [{ kind: 'primary', reachable: true, viewer_fills: false }]

function cards(over = {}) {
  return buildSkillCards({
    agentList: [], agentListState: 'live', running: true,
    assigned: [], library: [], conflictNames: new Set(), injectionResults: {},
    gates: [], approvers: PRIMARY_OTHER,
    canManage: true, isSystem: false, isEphemeral: false,
    ...over,
  })
}
const names = (list) => list.map((c) => c.name)

describe('sections — every skill in exactly one, a conflict in both', () => {
  it('an own skill is Own, a library skill the platform delivered is Shared only', () => {
    const r = cards({
      agentList: [live('mine'), live('from-lib', { source: 'platform' })],
      assigned: [row('from-lib')], library: [lib('from-lib')],
    })
    expect(names(r.own)).toEqual(['mine'])
    expect(names(r.shared)).toEqual(['from-lib'])
  })

  it('a #2914 name conflict shows in both: the agent\'s own copy is what runs', () => {
    const r = cards({
      agentList: [live('dup')], assigned: [row('dup', { delivery_status: 'conflict' })],
      library: [lib('dup')], conflictNames: new Set(['dup']),
    })
    expect(names(r.own)).toEqual(['dup'])
    expect(names(r.shared)).toEqual(['dup'])
    expect(r.shared[0].conflict).toBe(true)
  })

  it('an assigned name whose live copy carries no marker is the agent\'s own copy too', () => {
    const r = cards({ agentList: [live('dup', { source: 'agent' })], assigned: [row('dup')], library: [lib('dup')] })
    expect(names(r.own)).toEqual(['dup'])
    expect(r.shared[0].run.enabled).toBe(false)
  })

  it('a marked directory that is no longer assigned is Own, labelled as left from the library', () => {
    const r = cards({ agentList: [live('leftover', { source: 'platform' })] })
    expect(names(r.own)).toEqual(['leftover'])
    expect(r.own[0].badges.map((b) => b.label)).toContain('from library')
  })

  it('with the assignments not known yet, nothing is called left over', () => {
    const r = cards({ agentList: [live('maybe-assigned', { source: 'platform' })], assignmentsKnown: false })
    expect(r.own[0].leftover).toBe(false)
    expect(r.own[0].badges.map((b) => b.label)).not.toContain('from library')
  })

  it('an old agent image (no source field) falls back to the assigned names', () => {
    const old = (n) => { const s = live(n); delete s.source; delete s.dir; return s }
    const r = cards({ agentList: [old('mine'), old('from-lib')], assigned: [row('from-lib')], library: [lib('from-lib')] })
    expect(names(r.own)).toEqual(['mine'])
    expect(names(r.shared)).toEqual(['from-lib'])
  })

  it('an assigned skill missing from the library still renders, as no longer in the library', () => {
    const r = cards({ assigned: [row('orphan')] })
    expect(names(r.shared)).toEqual(['orphan'])
    expect(r.shared[0].note.text).toMatch(/no longer in the library/i)
  })

  it('matching is case-insensitive and also by directory', () => {
    const r = cards({ agentList: [live('Weekly Report', { dir: 'weekly-report', source: 'platform' })],
      assigned: [row('weekly-report')], library: [lib('weekly-report')] })
    expect(r.own).toEqual([])
    expect(r.shared[0].run.enabled).toBe(true)
  })
})

describe('Run', () => {
  it('needs the agent running, the skill in the LIVE list and user-invocable', () => {
    expect(cards({ agentList: [live('a')] }).own[0].run.enabled).toBe(true)
    expect(cards({ agentList: [live('a')], running: false }).own[0].run).toEqual(
      { enabled: false, title: 'Start the agent to run' })
    expect(cards({ agentList: [live('a', { user_invocable: false })] }).own[0].run.enabled).toBe(false)
  })

  it('a last-known list never drives Run', () => {
    const r = cards({ agentList: [live('a')], agentListState: 'last_known', running: true })
    expect(r.own[0].run.enabled).toBe(false)
  })

  it('a shared skill not yet in the agent says so instead of running', () => {
    const r = cards({ assigned: [row('later')], library: [lib('later')] })
    expect(r.shared[0].run.enabled).toBe(false)
    expect(r.shared[0].note.text).toMatch(/not in the agent yet/i)
  })

  it('a gated skill still runs (it raises the approval) and says it will ask', () => {
    const r = cards({ agentList: [live('pay')], gates: [gate('pay')] })
    expect(r.own[0].run.enabled).toBe(true)
    expect(r.own[0].run.title).toMatch(/approver/i)
  })
})

describe('gate line — everyone sees it, it names a kind, never a person', () => {
  it('a gated skill says who approves', () => {
    const r = cards({ agentList: [live('pay')], gates: [gate('pay')] })
    expect(r.own[0].gateLine).toEqual({ tone: 'locked', text: 'Needs approval from the primary contact' })
  })

  it('"you approve this" when the viewer fills the kind', () => {
    const r = cards({ agentList: [live('pay')], gates: [gate('pay')], approvers: PRIMARY_ME, canManage: false })
    expect(r.own[0].gateLine).toEqual({ tone: 'locked', text: 'Needs approval: you approve this' })
  })

  it('a kind nobody fills is a warning, not a lock', () => {
    const r = cards({ agentList: [live('pay')], gates: [gate('pay')],
      approvers: [{ kind: 'primary', reachable: false, viewer_fills: false }] })
    expect(r.own[0].gateLine.tone).toBe('warn')
    expect(r.own[0].gateLine.text).toMatch(/nobody fills it yet/)
  })

  it('without an approvers list the gate entry\'s own reach decides (never "nobody" by default)', () => {
    const reach = (r) => cards({ agentList: [live('pay')], gates: [gate('pay', { approver_reachable: r })], approvers: [] })
    expect(reach(true).own[0].gateLine).toEqual({ tone: 'locked', text: 'Needs approval from the primary contact' })
    expect(reach(false).own[0].gateLine.tone).toBe('warn')
  })

  it('a gate keyed on the directory finds the card whose frontmatter name differs', () => {
    const r = cards({ agentList: [live('Weekly Report', { dir: 'weekly-report' })], gates: [gate('weekly-report')] })
    expect(r.own[0].gate.skill_name).toBe('weekly-report')
    expect(r.unmatchedGates).toEqual([])
  })

  it('"approval: recommended" and not gated warns the owner only', () => {
    const own = live('pay', { approval: 'recommended' })
    expect(cards({ agentList: [own] }).own[0].gateLine).toEqual(
      { tone: 'warn', text: 'Author recommends approval: not gated' })
    expect(cards({ agentList: [own], canManage: false }).own[0].gateLine).toBeNull()
    const r = cards({ assigned: [row('lib')], library: [lib('lib', { approval: 'recommended' })] })
    expect(r.shared[0].gateLine.text).toBe('Author recommends approval: not gated')
  })

  it('a gate with no matching skill is kept on its own card — only when a list exists', () => {
    const r = cards({ agentList: [live('a')], gates: [gate('gone')] })
    expect(r.unmatchedGates).toHaveLength(1)
    expect(r.unmatchedGates[0].gateLine.text).toBe("Not in this agent's skills list: gate kept")
    expect(r.unmatchedGates[0].controls.clear).toBe(true)
    expect(cards({ agentListState: 'none', gates: [gate('gone')] }).unmatchedGates).toEqual([])
    expect(cards({ agentList: [live('a')], gates: [gate('gone')], canManage: false })
      .unmatchedGates[0].controls.clear).toBe(false)
  })

  it('a gate on an assigned skill not yet in the agent is matched, not "gone"', () => {
    const r = cards({ agentList: [live('a')], assigned: [row('later')], library: [lib('later')], gates: [gate('later')] })
    expect(r.unmatchedGates).toEqual([])
    expect(r.shared[0].gate.skill_name).toBe('later')
  })
})

describe('controls — role, ephemeral and system', () => {
  it('the owner or an admin gets the approval row and unassign; a viewer gets neither', () => {
    const r = cards({ agentList: [live('a')], assigned: [row('s')], library: [lib('s')] })
    expect(r.own[0].controls.approval).toBe(true)
    expect(r.shared[0].controls.unassign.show).toBe(true)
    const v = cards({ agentList: [live('a')], assigned: [row('s')], library: [lib('s')], canManage: false })
    expect(v.own[0].controls.approval).toBe(false)
    expect(v.shared[0].controls.unassign.show).toBe(false)
  })

  it('no approval controls on an ephemeral agent or the system agent', () => {
    expect(cards({ agentList: [live('a')], isEphemeral: true }).own[0].controls.approval).toBe(false)
    expect(cards({ agentList: [live('a')], isSystem: true }).own[0].controls.approval).toBe(false)
  })

  it('a set-only member cannot be unassigned on its own', () => {
    const r = cards({ assigned: [row('m', { individual: false, via_sets: ['kit'] })], library: [lib('m')] })
    expect(r.shared[0].controls.unassign).toEqual(
      { show: true, disabled: true, title: 'Assigned via kit: unassign the set to remove it' })
  })

  it('a name that cannot carry a gate disables the toggle with the reason', () => {
    const r = cards({ agentList: [live('Has Space', { dir: 'also bad' })] })
    expect(r.own[0].gateKey).toBeNull()
    expect(r.own[0].controls.toggleDisabled).toMatch(/can't carry a gate/)
  })

  it('with the gate map unread, every approval toggle is held, with the reason', () => {
    const r = cards({ agentList: [live('daily')], assigned: [row('shared-one')], library: [lib('shared-one')],
      gatesKnown: false })
    expect(r.own[0].controls.toggleDisabled).toMatch(/couldn't be read/)
    expect(r.shared[0].controls.toggleDisabled).toMatch(/couldn't be read/)
    const viewer = cards({ agentList: [live('daily')], gatesKnown: false, canManage: false })
    expect(viewer.own[0].controls.toggleDisabled).toBeNull()      // no approval row to hold
  })
})

describe('shared badges and the note line', () => {
  it('via set, deprecated, conflict and the last sync verdict', () => {
    const r = cards({
      agentList: [live('a', { source: 'platform' })],
      assigned: [row('a', { individual: false, via_sets: ['kit'] })],
      library: [lib('a', { deprecated: true, superseded_by: 'b' })],
      injectionResults: { a: { status: 'injected', warnings: [] } },
    })
    const labels = r.shared[0].badges.map((b) => b.label)
    expect(labels).toEqual(['via kit', 'deprecated', 'synced'])
    expect(r.shared[0].note).toMatchObject({ text: 'Superseded by b', testid: 'skill-superseded-assigned-a' })
  })

  it('one delivery warning is shown in words, several as a count with details', () => {
    const one = cards({ assigned: [row('a')], library: [lib('a')],
      injectionResults: { a: { status: 'unchanged', warnings: ['missing_env:GH_TOKEN', 'deprecated:b'] } } })
    expect(one.shared[0].note.text).toBe("GH_TOKEN is not set in this agent's environment")
    const two = cards({ assigned: [row('a')], library: [lib('a')],
      injectionResults: { a: { status: 'unchanged', warnings: ['missing_env:X', 'missing_binary:jq'] } } })
    expect(two.shared[0].note).toMatchObject({ text: '2 delivery warnings', detail: true })
  })

  it('the conflict note wins and opens the details', () => {
    const r = cards({ assigned: [row('dup', { delivery_status: 'conflict' })], library: [lib('dup')],
      conflictNames: new Set(['dup']) })
    expect(r.shared[0].note).toMatchObject({ detail: true, testid: 'skill-conflict-note' })
    expect(r.shared[0].badges.map((b) => b.label)).toContain('name conflict')
  })
})

describe('mode chip, names and labels', () => {
  it('relabels the author\'s automation value by meaning (Q4)', () => {
    expect(modeChip('autonomous')).toMatchObject({ label: 'runs unattended', variant: 'autonomous' })
    expect(modeChip('gated')).toMatchObject({ label: 'asks mid-run', variant: 'info' })
    expect(modeChip('manual')).toMatchObject({ label: 'start by hand', variant: 'neutral' })
    expect(modeChip('gated').title).toBe('automation: gated')
    expect(modeChip('custom')).toMatchObject({ label: 'custom', variant: 'neutral' })
    expect(modeChip(null)).toBeNull()
    expect(modeChip('')).toBeNull()
  })

  it('a gate key is the name when it can carry one, else the directory, lower-cased', () => {
    expect(gateKeyFor({ name: 'Pay-Invoice', dir: 'x' })).toBe('pay-invoice')
    expect(gateKeyFor({ name: 'Pay Invoice', dir: 'pay-invoice' })).toBe('pay-invoice')
    expect(gateKeyFor({ name: 'a b', dir: 'c d' })).toBeNull()
    expect(SKILL_NAME_RE.test('-nope')).toBe(false)
  })

  it('kind labels never name a person', () => {
    expect(kindLabel('primary')).toBe('the primary contact')
    expect(kindLabel('approver')).toBe('an approver')
    expect(kindOptionLabel('primary')).toBe('Primary contact')
    expect(kindOptionLabel('approver')).toBe('Approver')
    expect(kindLabel('something')).toBe('something')
  })
})
