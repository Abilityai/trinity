/**
 * trinity-enterprise#584 — a capability is described once, in its SKILL.md,
 * and "No description yet" is the one empty state (requirements/skills.md §23).
 *
 * The rules, executed. What each component does with them is mounted in
 * capabilityDescriptions.mount.spec.js.
 */
import { describe, it, expect } from 'vitest'
import { describeSkill, NO_DESCRIPTION } from '../../src/utils/skillDescription'
import { capabilitySection, hintLine } from '../../src/components/portal/portalUtils'
import { descriptionLine } from '../../src/utils/skillCards'

describe('describeSkill: the one blank rule', () => {
  it.each([
    [null, true], [undefined, true], ['', true], ['   ', true], ['\n\t', true], [42, true], [['x'], true],
  ])('%j has no description', (raw, missing) => {
    expect(describeSkill(raw)).toEqual({ text: NO_DESCRIPTION, missing })
  })

  it('a description is shown trimmed', () => {
    expect(describeSkill('  Writes the weekly report. ')).toEqual({ text: 'Writes the weekly report.', missing: false })
  })

  it('the copy is the ruled one', () => {
    expect(NO_DESCRIPTION).toBe('No description yet')
  })
})

describe('capabilitySection: the Workspace rail', () => {
  const pb = (title, description, kind = 'playbook') => ({ title, description, starter_prompt: `/${title} `, kind })

  it('playbooks are "what it can do", each with its description or the empty state', () => {
    const s = capabilitySection([pb('weekly', 'Writes it.'), pb('blank', '   '), pb('none', null)])
    expect(s.tier).toBe('playbooks')
    expect(s.items.map((i) => [i.title, i.line, i.missing])).toEqual([
      ['weekly', 'Writes it.', false],
      ['blank', NO_DESCRIPTION, true],
      ['none', NO_DESCRIPTION, true],
    ])
    // the rail pre-fills exactly what it always did (the trailing space invites an argument)
    expect(s.items[0].starter).toBe('/weekly ')
  })

  it('use cases are "what you can ask", and never claim a missing description', () => {
    const s = capabilitySection([pb('Summarise this week', null, 'use_case'), pb('Draft a reply', null, 'use_case')])
    expect(s.tier).toBe('use_cases')
    expect(s.items.map((i) => [i.title, i.line])).toEqual([['Summarise this week', null], ['Draft a reply', null]])
    expect(s.items[0].starter).toBe('/Summarise this week ')
  })

  it('a payload from before `kind` existed reads as playbooks', () => {
    expect(capabilitySection([{ title: 'x', starter_prompt: '/x ' }]).tier).toBe('playbooks')
  })

  it('nothing at all is the playbook tier with no items', () => {
    expect(capabilitySection([])).toEqual({ tier: 'playbooks', items: [] })
    expect(capabilitySection(null)).toEqual({ tier: 'playbooks', items: [] })
  })
})

describe('hintLine: the new-chat hint cards', () => {
  it('a playbook shows its description, or the empty state', () => {
    expect(hintLine({ kind: 'playbook', description: 'Writes it.' })).toEqual({ text: 'Writes it.', missing: false })
    expect(hintLine({ kind: 'playbook', description: ' ' })).toEqual({ text: NO_DESCRIPTION, missing: true })
    expect(hintLine({ description: null })).toEqual({ text: NO_DESCRIPTION, missing: true })
  })

  it('a use case has no description line at all', () => {
    expect(hintLine({ kind: 'use_case', description: null })).toBeNull()
  })
})

describe('descriptionLine: the Skills tab card', () => {
  it('a described skill shows its text', () => {
    expect(descriptionLine({ section: 'own', name: 'a', dir: 'a', description: ' Does A. ' }))
      .toEqual({ muted: false, segments: [{ text: 'Does A.' }], title: 'Does A.' })
  })

  it('an own skill without one tells the author where to write it', () => {
    const line = descriptionLine({ section: 'own', name: 'weekly-report', dir: 'weekly', description: '  ' })
    expect(line.muted).toBe(true)
    expect(line.segments).toEqual([
      { text: 'No description yet. Add ' }, { code: 'description:' }, { text: ' to its SKILL.md.' },
    ])
    expect(line.title).toBe('Add a description: line to the frontmatter of .claude/skills/weekly/SKILL.md')
  })

  it("a shared skill's file is in the skills library", () => {
    const line = descriptionLine({ section: 'shared', name: 'lib-skill', dir: 'lib-skill', description: null })
    expect(line.segments[0]).toEqual({ text: 'No description yet. Add ' })
    expect(line.title).toBe("Add a description: line to this skill's SKILL.md in the skills library")
  })

  it('a gate that matches no skill keeps its own line', () => {
    expect(descriptionLine({ section: 'own', name: 'gone', missing: true }))
      .toEqual({ muted: true, segments: [{ text: 'This gate stays until someone clears it.' }], title: undefined })
  })
})
