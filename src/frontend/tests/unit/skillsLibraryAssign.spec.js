/**
 * ent#386 — the Library's assign/unassign write half, at the store boundary.
 *
 * Three things live here that a source-structure guard cannot see:
 *
 *   1. **Targets are not holders.** The dropdown is `assignable − holders`.
 *      Offering an agent that already holds the skill produces a write whose
 *      honest answer is "already assigned" — a control that looks like it does
 *      something and does not.
 *
 *   2. **The map is patched, not refetched.** AC 3 forbids a reload that resets
 *      scroll position or tab state, and a refetch would re-run a fleet-wide
 *      O(agents × skills) read for a one-row change. Patching is what makes the
 *      zero-holder → one-holder transition visible without one.
 *
 *   3. **A failed write returns the server's reason.** AC 4 asks for a named
 *      validation error rather than a generic 500 or a silent no-op, and the
 *      names ("Skill 'x' not found in library", "Agent not found") come from
 *      the write route — inventing our own wording here would drift from them.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('@/api', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))

import api from '@/api'
import { useSkillsLibraryStore } from '@/stores/skillsLibrary'

const payload = (assignments, assignable, scope = 'accessible') => ({
  data: { assignments, scope, assignable_agents: assignable },
})

describe('ent#386 — assign targets', () => {
  let store

  beforeEach(async () => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    store = useSkillsLibraryStore()
    api.get.mockResolvedValue(
      payload(
        { research: [{ name: 'scout', display_label: null }] },
        [
          { name: 'scout', display_label: null },
          { name: 'scribe', display_label: 'Scribe' },
        ],
      ),
    )
    await store.loadAssignments()
  })

  it('offers only agents that do not already hold the skill', () => {
    expect(store.assignableFor('research').map((a) => a.name)).toEqual(['scribe'])
  })

  it('offers every assignable agent for a skill nobody holds', () => {
    expect(store.assignableFor('writing').map((a) => a.name)).toEqual(['scout', 'scribe'])
  })

  it('sorts targets by the name actually rendered, not the slug', async () => {
    // `display_label` is what the reader sees; ordering by slug makes the
    // visible list look arbitrary (the same reasoning as `agentsFor`).
    // Labels chosen so slug order and label order genuinely disagree, and in
    // one case so the assertion does not depend on the runtime's collation
    // (mixed case orders differently under ICU and under code-unit fallback).
    api.get.mockResolvedValue(
      payload({}, [
        { name: 'aaa-slug-first', display_label: 'zulu' },
        { name: 'zzz-slug-last', display_label: 'alpha' },
      ]),
    )
    await store.loadAssignments()

    expect(store.assignableFor('writing').map((a) => a.display_label))
      .toEqual(['alpha', 'zulu'])
  })

  it('knows which holders the caller may modify', () => {
    expect(store.canModify('scout')).toBe(true)
    expect(store.canModify('sage')).toBe(false)
  })

  it('drops the targets when the read fails, so no write runs on a stale set', async () => {
    api.get.mockRejectedValueOnce({ response: { data: { detail: 'boom' } } })

    await store.loadAssignments()

    expect(store.assignableAgents).toEqual([])
    expect(store.assignmentsError).toBe('boom')
  })
})

describe('ent#386 — assigning', () => {
  let store

  beforeEach(async () => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    store = useSkillsLibraryStore()
    api.get.mockResolvedValue(
      payload({}, [{ name: 'scout', display_label: 'Scout' }]),
    )
    await store.loadAssignments()
  })

  it('calls the per-agent write route, not a skill-keyed one', async () => {
    api.post.mockResolvedValue({ data: { success: true } })

    await store.assignSkill('research', 'scout')

    expect(api.post).toHaveBeenCalledWith('/api/agents/scout/skills/research')
  })

  it('patches the holder map in place rather than refetching', async () => {
    api.post.mockResolvedValue({ data: { success: true } })

    const err = await store.assignSkill('research', 'scout')

    expect(err).toBeNull()
    expect(store.agentsFor('research').map((a) => a.name)).toEqual(['scout'])
    expect(api.get).toHaveBeenCalledTimes(1)   // the initial load only
  })

  it('carries the label through, so the new chip is not suddenly a slug', async () => {
    api.post.mockResolvedValue({ data: { success: true } })

    await store.assignSkill('research', 'scout')

    expect(store.agentsFor('research')[0].display_label).toBe('Scout')
  })

  it('removes an assigned agent from its own dropdown', async () => {
    api.post.mockResolvedValue({ data: { success: true } })

    await store.assignSkill('research', 'scout')

    expect(store.assignableFor('research')).toEqual([])
  })

  it('returns the server reason for an unknown skill', async () => {
    api.post.mockRejectedValue({
      response: { data: { detail: "Skill 'nope' not found in library" } },
    })

    expect(await store.assignSkill('nope', 'scout'))
      .toBe("Skill 'nope' not found in library")
  })

  it('returns the server reason for an inaccessible agent', async () => {
    api.post.mockRejectedValue({ response: { data: { detail: 'Agent not found' } } })

    expect(await store.assignSkill('research', 'sage')).toBe('Agent not found')
  })

  it('never leaves a failed write looking like a success', async () => {
    api.post.mockRejectedValue({ response: { data: { detail: 'Agent not found' } } })

    await store.assignSkill('research', 'sage')

    expect(store.agentsFor('research')).toEqual([])
  })

  it('falls back to a plain message when the server sends no detail', async () => {
    api.post.mockRejectedValue(new Error('network down'))

    expect(await store.assignSkill('research', 'scout')).toBe('Could not assign the skill')
  })

  it('encodes names, so a skill with a slash cannot forge a path', async () => {
    api.post.mockResolvedValue({ data: { success: true } })

    await store.assignSkill('a/b', 'scout')

    expect(api.post).toHaveBeenCalledWith('/api/agents/scout/skills/a%2Fb')
  })
})

describe('ent#386 — unassigning', () => {
  let store

  beforeEach(async () => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    store = useSkillsLibraryStore()
    api.get.mockResolvedValue(
      payload(
        {
          research: [
            { name: 'scout', display_label: null },
            { name: 'scribe', display_label: 'Scribe' },
          ],
        },
        [{ name: 'scout', display_label: null }],
      ),
    )
    await store.loadAssignments()
  })

  it('calls the per-agent delete route', async () => {
    api.delete.mockResolvedValue({ data: { success: true } })

    await store.unassignSkill('research', 'scout')

    expect(api.delete).toHaveBeenCalledWith('/api/agents/scout/skills/research')
  })

  it('removes only that holder', async () => {
    api.delete.mockResolvedValue({ data: { success: true } })

    await store.unassignSkill('research', 'scout')

    expect(store.agentsFor('research').map((a) => a.name)).toEqual(['scribe'])
  })

  it('drops the skill key entirely at zero holders', async () => {
    // `agentsFor` treats a missing key and an empty array alike, but the
    // orphaned-assignments view keys off PRESENCE — an empty array left behind
    // invents a phantom holder set.
    api.delete.mockResolvedValue({ data: { success: true } })

    await store.unassignSkill('research', 'scout')
    await store.unassignSkill('research', 'scribe')

    expect(store.assignments.research).toBeUndefined()
  })

  it('returns the holder to the dropdown', async () => {
    api.delete.mockResolvedValue({ data: { success: true } })

    await store.unassignSkill('research', 'scout')

    expect(store.assignableFor('research').map((a) => a.name)).toEqual(['scout'])
  })

  it('returns the server reason and changes nothing on failure', async () => {
    api.delete.mockRejectedValue({ response: { data: { detail: 'Agent not found' } } })

    expect(await store.unassignSkill('research', 'scout')).toBe('Agent not found')
    expect(store.agentsFor('research').map((a) => a.name)).toEqual(['scout', 'scribe'])
  })
})
