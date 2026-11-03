/**
 * trinity-enterprise#660 — the one rule behind "Filter skills": what a query
 * matches, which source and category options a list offers, and how the three
 * narrow it (`utils/skillFilter.js`). The Skills tab, the Assign skills dialog
 * and the Library all read it, so the three cannot disagree. Their wiring is
 * mounted in skillsTab.mount, skillAssignSelectAll and librarySkillsFilter.mount.
 */
import { describe, it, expect } from 'vitest'
import {
  matchesQuery, sourceOptions, categoryOptions, categoryKey, categoryLabel, filterSkills,
} from '../../src/utils/skillFilter'

const SKILLS = [
  { name: 'backlog', description: 'Show what is next', source_id: 'src_a', source_name: 'Team', category: 'project-management' },
  { name: 'roadmap', description: 'Priorities', source_id: 'src_a', source_name: 'Team', category: 'Project Management' },
  { name: 'chart', description: 'Draw a diagram', source_id: 'src_b', source_name: 'Vendor', category: 'visual_communication' },
  { name: 'plain', description: null, source_id: 'src_b', source_name: 'Vendor', category: null },
]
const names = (rows) => rows.map((s) => s.name)

describe('what a query matches', () => {
  it('the name or the description, ignoring case and outer spaces', () => {
    expect(matchesQuery(SKILLS[0], 'BACK')).toBe(true)
    expect(matchesQuery(SKILLS[0], '  what is ')).toBe(true)
    expect(matchesQuery(SKILLS[0], 'nothing')).toBe(false)
  })

  it('an empty query matches everything, a missing description is no crash', () => {
    expect(matchesQuery(SKILLS[3], '')).toBe(true)
    expect(matchesQuery(SKILLS[3], '   ')).toBe(true)
    expect(matchesQuery(SKILLS[3], 'diagram')).toBe(false)
  })
})

describe('which sources a list offers', () => {
  it('each once, by id, labelled by name', () => {
    expect(sourceOptions(SKILLS)).toEqual([
      { value: 'src_a', label: 'Team' },
      { value: 'src_b', label: 'Vendor' },
    ])
  })

  it('a source with no id is keyed by its name, one with no name labelled by its id', () => {
    expect(sourceOptions([{ name: 'a', source_name: 'Team' }, { name: 'b', source_id: 'src_c' }])).toEqual([
      { value: 'src_c', label: 'src_c' },
      { value: 'Team', label: 'Team' },
    ])
  })

  it('a skill with no provenance offers nothing', () => {
    expect(sourceOptions([{ name: 'a' }])).toEqual([])
  })
})

describe('which categories a list offers', () => {
  it('only values present, spellings folded, each readable', () => {
    expect(categoryOptions(SKILLS)).toEqual([
      { value: 'project-management', label: 'Project management' },
      { value: 'visual-communication', label: 'Visual communication' },
    ])
  })

  it('none when no skill has one', () => {
    expect(categoryOptions([{ name: 'a' }, { name: 'b', category: null }, { name: 'c', category: '  ' }])).toEqual([])
  })

  it.each([
    ['Project Management', 'project-management'],
    ['  documents_and_data ', 'documents-and-data'],
    ['research--and  analysis', 'research-and-analysis'],
    ['-edges-', 'edges'],
    ['', ''],
    [null, ''],
    [7, ''],
  ])('%j folds to %j', (raw, key) => {
    expect(categoryKey(raw)).toBe(key)
  })

  it('a key reads as a sentence-case label', () => {
    expect(categoryLabel('documents-and-data')).toBe('Documents and data')
  })

  it('an acronym the author capitalised stays capitalised (PR review)', () => {
    expect(categoryLabel('AI & ML')).toBe('AI & ML')
    expect(categoryLabel('Research and UX')).toBe('Research and UX')
    expect(categoryLabel('Project Management')).toBe('Project management')
  })

  it('the label is the first spelling seen, and later spellings join it', () => {
    expect(categoryOptions([
      { name: 'a', category: 'AI & ML' }, { name: 'b', category: 'ai & ml' }, { name: 'c', category: 'ai-&-ml' },
    ])).toEqual([{ value: 'ai-&-ml', label: 'AI & ML' }])
  })
})

describe('how the three narrow a list', () => {
  it('no filter keeps every row, in order', () => {
    expect(names(filterSkills(SKILLS, {}))).toEqual(['backlog', 'roadmap', 'chart', 'plain'])
  })

  it('by source, by key', () => {
    expect(names(filterSkills(SKILLS, { source: 'src_b' }))).toEqual(['chart', 'plain'])
  })

  it('by category, whichever way the author spelled it', () => {
    expect(names(filterSkills(SKILLS, { category: 'project-management' }))).toEqual(['backlog', 'roadmap'])
  })

  it('a skill with no category appears only under all categories', () => {
    expect(names(filterSkills(SKILLS, { category: 'visual-communication' }))).toEqual(['chart'])
  })

  it('all three together', () => {
    expect(names(filterSkills(SKILLS, { query: 'priorities', source: 'src_a', category: 'project-management' })))
      .toEqual(['roadmap'])
    expect(filterSkills(SKILLS, { query: 'priorities', source: 'src_b' })).toEqual([])
  })
})
