/**
 * Each person's own Atlas: the one they own on their roster, or a new one under their
 * own address. Someone else's guide, even one shared with them, is never used.
 */
import { describe, expect, it, vi } from 'vitest'
import { deployMyGuide, myGuideName } from '../../src/components/portal/builder/useMyGuide'
import { buildManifest, guideNameFor } from '../../src/components/portal/builder/portalBuild'

const ME = 'person@example.com'

describe('myGuideName', () => {
  it('is the guide this person owns', () => {
    expect(myGuideName({ roster: [{ name: 'atlas-22222222', owned: true }], username: ME })).toBe('atlas-22222222')
    expect(myGuideName({ roster: [{ name: 'atlas-22222222', owned: true }, { name: 'build-guide', owned: true }], username: ME })).toBe('build-guide')
  })

  it("is a new address of their own when the only guide around is someone else's", () => {
    expect(myGuideName({ roster: [{ name: 'build-guide', owned: false }], username: ME })).toBe(guideNameFor(ME))
    expect(myGuideName({ roster: [], username: ME })).toBe(guideNameFor(ME))
  })
})

describe('deployMyGuide', () => {
  it('deploys one guide under their address and names it Atlas', async () => {
    const store = { deployBuildSystem: vi.fn().mockResolvedValue({ status: 'deployed', failed: [] }) }
    const result = await deployMyGuide(store, ME)
    const name = guideNameFor(ME)
    expect(store.deployBuildSystem).toHaveBeenCalledWith(buildManifest(name), name)
    expect(result).toEqual({ name, failure: '' })
  })

  it('steps on to the next address when Trinity still holds the name', async () => {
    const name = guideNameFor(ME)
    const store = {
      deployBuildSystem: vi.fn()
        .mockResolvedValueOnce({ status: 'failed', failed: [{ name, reason: 'Agent already exists' }] })
        .mockResolvedValueOnce({ status: 'deployed', failed: [] }),
    }
    const result = await deployMyGuide(store, ME)
    expect(store.deployBuildSystem).toHaveBeenLastCalledWith(buildManifest(`${name}-2`), `${name}-2`)
    expect(result).toEqual({ name: `${name}-2`, failure: '' })
  })

  it('says why in words a person can act on when it is refused', async () => {
    const store = { deployBuildSystem: vi.fn().mockRejectedValue({ response: { status: 403, data: {} } }) }
    const result = await deployMyGuide(store, ME)
    expect(result.failure).toBe("Your account can't create agents on this instance. Ask an administrator for an account that can.")
    expect(store.deployBuildSystem).toHaveBeenCalledTimes(1)
  })

  it('steps on when Trinity refuses a held name with its error reply', async () => {
    const name = guideNameFor(ME)
    const store = {
      deployBuildSystem: vi.fn()
        .mockRejectedValueOnce({ response: { status: 500, data: { status: 'failed', agents_created: [], failed: [{ name, reason: 'Agent already exists' }] } } })
        .mockResolvedValueOnce({ status: 'deployed', agents_created: [`${name}-2`], failed: [] }),
    }
    expect(await deployMyGuide(store, ME)).toEqual({ name: `${name}-2`, failure: '' })
  })

  it('takes the name Trinity really gave it', async () => {
    const name = guideNameFor(ME)
    const store = { deployBuildSystem: vi.fn().mockResolvedValue({ status: 'deployed', agents_created: [`${name}_2`], failed: [] }) }
    expect(await deployMyGuide(store, ME)).toEqual({ name: `${name}_2`, failure: '' })
  })

  it('sets nothing up without knowing who is signed in', async () => {
    const store = { deployBuildSystem: vi.fn() }
    const result = await deployMyGuide(store, '')
    expect(result.failure).toBe("Trinity doesn't know who is signed in yet. Reload the page and try again.")
    expect(store.deployBuildSystem).not.toHaveBeenCalled()
  })
})

